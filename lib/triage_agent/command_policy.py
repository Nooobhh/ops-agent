"""自由命令的放行策略:三档模式 + 三级名单 + 分档正表。

四层分工(顺序不可颠倒):

    1 结构化 argv    render_pipeline 逐 token 转义,消除远端 shell 的二次展开,
                     使「校验的串」与「执行的串」是同一个
    2 cmd_filter     SSHPool.run() 唯一出口上的红线,不可绕过的底闸
    3 command_grammar 工具正表,分窄(default)/宽(auto、bypass)两档
    4 本模块          把上面三层的结论与名单、模式合成最终动作

三档的差别**不在检查的严格程度,而在工具集大小与谁来担保**:

    default  窄正表。只收结构上可静态验证只读的工具,表外一律拒
    auto     宽正表(加解释器、curl、tar)。表外交审核模型看意图,判安全即执行
    bypass   宽正表。表外直接执行,靠人的信任担保

三档共有、谁也绕不过的是两条:

    红线绝对禁止   会瓦解判定机制本身的构造(bash -c 让 shell 重新解析、
                   xargs 把 argv 当命令、shutdown 类)。三档硬拒,名单也覆盖不了
    ask 断路器     红线写操作(rm / 服务启停)与凭据路径(shadow / 私钥 / keytab)。
                   三档都挂人工审核,**bypass 也不例外**

放开解释器的代价要说明白:红线此后只能拦明文可见的危险构造,
`python -c "__import__('os').system('r'+'m -rf /')"` 这类拼接混淆拦不住。
auto 档靠审核模型看意图兜底,bypass 档靠信任——这是 bypass 的固有属性。
"""
from __future__ import annotations

import hashlib
import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Literal, Optional, Sequence

from lib.cmd_filter import CmdFilter, Level, Verdict
from lib.file_lock import FileLock

from .command_grammar import check_pipeline
from .path_policy import offending_paths

Mode = Literal["whitelist", "auto", "bypass"]
# review = 挂人工审核;ask = 挂人工审核且**任何档位、任何名单都能到达**
# (红线 WRITE 与凭据路径两类)。两者对运行时是同一个动作,分开命名是
# 为了让「bypass 也要审」这条契约在判定里显式可查
Action = Literal["allow", "review", "ask", "deny"]

MODES: tuple[Mode, ...] = ("whitelist", "auto", "bypass")

# 默认收窄到 whitelist:分档正表排在名单之后,基础只读工具(如 ls/cat)仍照常
# 放行;表外命令由五档人工裁决收口,需要临时放宽时可按任务升到 auto/bypass。
DEFAULT_MODE: Mode = "whitelist"

# `head -20` 的 -20 是行数不是选项名
_NUMERIC_OPTION = re.compile(r"^-\d+$")

# 参数即代码的工具:签名必须绑定参数内容,否则一次批准放行一整类
CODE_BEARING_TOOLS = frozenset({"awk", "sed", "mysql", "gawk"})


class CommandPolicyError(RuntimeError):
    pass


def _tier(mode: Mode) -> str:
    """default 收窄表;auto / bypass 收宽表(解释器、curl、tar 等)。"""
    return "narrow" if mode == "whitelist" else "wide"


@dataclass(frozen=True)
class PolicyVerdict:
    action: Action
    reason: str
    rule: Optional[str] = None


def render_pipeline(pipeline: Sequence[Sequence[str]]) -> str:
    """结构化 argv → 可执行命令串。

    每个 token 过 shlex.quote,于是远端 shell 不会对参数做任何二次展开:
    `${VAR:-x}`、`$(...)`、`&`、`;` 都以字面量交给工具。这正是红线过滤能够
    成立的前提——过滤器看到的串与 shell 实际执行的串必须是同一个,否则
    「过滤时是 ${OPS:--o}、执行时变成 -o」这类偏差会直接绕过判定。
    管道只出现在段与段之间,由这里拼接,模型无从自行插入。

    审批预览与实际执行共用本函数:两份实现会漂移,而人批准的必须正是要跑的。
    """
    return " | ".join(
        " ".join(shlex.quote(str(token)) for token in segment)
        for segment in pipeline
    )


def command_signature(pipeline: Sequence[Sequence[str]]) -> str:
    """提取用于名单匹配的签名。

    `grep -c ERROR /var/log/a.log`  →  `grep -c`
    `ps -eo args= | grep java | head -20`  →  `ps -eo | grep | head -N`

    只留工具名与选项名,参数值一律丢弃;选项排序去重,使同一组选项的不同书写
    顺序落到同一签名。带值的长选项按 `--opt=value` 的 `--opt` 记。

    `head -20` / `tail -500` 里的数字是行数而非选项,一律归一成 `-N`——否则
    每换一个行数就得重新批准一次,名单会被同一条命令的变体撑爆。

    两处**不做**归一,因为归一会让一次批准放行一整类命令:

    - 工具名保留原样,不做 basename。否则批准 `cat` 会连 `/tmp/cat` 一起放行,
      正表对路径的拒绝就被名单绕过了。
    - awk / sed / mysql 的危险藏在参数内容里(程序、脚本、SQL),签名附带整段
      argv 的摘要。否则批准一个 `awk '{print $1}'` 等于批准
      `awk 'BEGIN{system("id")}'`。
    """
    parts: List[str] = []
    for segment in pipeline:
        if not segment:
            raise CommandPolicyError("pipeline 段为空,无法提取签名")
        tool = str(segment[0])
        options = sorted({
            _normalize_option(str(token))
            for token in segment[1:]
            if str(token).startswith("-")
        })
        piece = " ".join([tool, *options])
        if tool.rsplit("/", 1)[-1] in CODE_BEARING_TOOLS:
            piece = f"{piece} #{_argv_digest(segment)}"
        parts.append(piece)
    signature = " | ".join(parts)
    # 越界路径必须进签名。否则「批准读 /etc/hive/conf/hive-site.xml」会因为
    # 签名只是 `cat` 而变成「批准 cat 读任何文件」——人看到的是一个文件,
    # 批出去的是整个工具。在允许范围内的路径不进签名,泛化能力照旧。
    outside = offending_paths(pipeline)
    if outside:
        signature = f"{signature} @{','.join(sorted(set(outside)))}"
    return signature


def _argv_digest(segment: Sequence[str]) -> str:
    payload = json.dumps([str(token) for token in segment], ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _normalize_option(token: str) -> str:
    name = token.split("=", 1)[0]
    if _NUMERIC_OPTION.match(name):
        return "-N"
    return name


@dataclass
class PolicyConfig:
    mode: Mode = DEFAULT_MODE
    allow: List[str] = None  # type: ignore[assignment]
    deny: List[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.allow is None:
            self.allow = []
        if self.deny is None:
            self.deny = []


class CommandPolicy:
    """`state/command-policy.json` 的读写。运行时状态,不入库。"""

    def __init__(self, path: Path, path_policy=None, cmd_filter=None):
        self.path = Path(path)
        # 由 load_path_policy(catalog=…) 构造;缺省退回粗粒度兜底根
        self.path_policy = path_policy
        # 与 SSHPool.run() 那道闸同一份规则:策略层要提前知道红线会怎么判,
        # 才能把 WRITE 变成「挂人工审核」而不是等执行时被硬拒
        self._filter = cmd_filter or CmdFilter.default()

    def _redline(
        self, pipeline: Optional[Sequence[Sequence[str]]]
    ) -> Optional[Verdict]:
        if pipeline is None:
            return None
        verdict = self._filter.classify(render_pipeline(pipeline))
        return None if verdict.level is Level.READONLY else verdict

    # ---- 读 ----

    def load(self) -> PolicyConfig:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return PolicyConfig()
        except (OSError, json.JSONDecodeError) as exc:
            # 策略文件损坏时不能静默退回「全放行」,也不该让整个排查崩掉:
            # 退回默认配置(空名单 + auto),等价于「全部走人工裁决」
            raise CommandPolicyError(f"命令策略文件无法解析: {exc}") from exc
        mode = raw.get("mode", DEFAULT_MODE)
        if mode not in MODES:
            raise CommandPolicyError(
                f"命令策略 mode 无效: {mode!r},须取 {', '.join(MODES)}"
            )
        return PolicyConfig(
            mode=mode,
            allow=[str(x) for x in (raw.get("allow") or [])],
            deny=[str(x) for x in (raw.get("deny") or [])],
        )

    # ---- 判定 ----

    def evaluate(
        self,
        signature: str,
        *,
        task_defaults: Iterable[str] = (),
        config: Optional[PolicyConfig] = None,
        pipeline: Optional[Sequence[Sequence[str]]] = None,
    ) -> PolicyVerdict:
        """判定顺序:红线 → 名单 → 凭据路径 → 分档正表 → 模式兜底。

        1. 红线 FORBIDDEN      三档硬拒,名单也覆盖不了(会瓦解判定机制本身的构造)
        2. 永久 deny           拒绝优先于放行
        3. 红线 WRITE          rm / 服务启停 → ask,**bypass 也要人工审核**
        4. 凭据路径            /etc/shadow、id_rsa、keytab → ask,同样三档都审
        5. 名单                永久 allow / 本任务默认 → 放行
        6. 分档正表            default 收窄表,auto / bypass 收宽表
        7. 模式兜底            default 拒 / auto 交审核模型 / bypass 放行

        名单排在正表**之前**、红线与凭据之**后**:人工明确批准过的命令应当能
        覆盖正表的拒绝(否则「永久加入白名单」这一档形同虚设),但覆盖不了红线
        与凭据——那两类是每次都要人看一眼的东西,批过一次不等于永久授权。
        """
        cfg = config if config is not None else self.load()
        redline = self._redline(pipeline)
        if redline is not None and redline.level is Level.FORBIDDEN:
            return PolicyVerdict("deny", f"红线绝对禁止({redline.reason})")
        if signature in cfg.deny:
            return PolicyVerdict("deny", "命中永久黑名单", signature)
        if redline is not None and redline.level is Level.WRITE:
            return PolicyVerdict("ask", f"写操作须人工审核({redline.reason})")

        grammar = None
        if pipeline is not None:
            grammar = check_pipeline(pipeline, self.path_policy, tier=_tier(cfg.mode))
            if not grammar.ok and grammar.credential:
                return PolicyVerdict("ask", grammar.reason)

        if signature in cfg.allow:
            return PolicyVerdict("allow", "命中永久白名单", signature)
        if signature in set(task_defaults):
            return PolicyVerdict("allow", "命中本次排查已批准的同类命令", signature)

        if grammar is not None and grammar.ok:
            return PolicyVerdict("allow", "命中内置工具正表")
        if grammar is not None and not grammar.tier_gated:
            # 判它危险(选项越界 / 内容含执行构造 / 路径越界)而不是「管不了」,
            # 宽档一样拒:放开工具集不等于放开对这些工具的约束
            return PolicyVerdict("deny", grammar.reason)
        reason = grammar.reason if grammar is not None else ""
        if cfg.mode == "bypass":
            return PolicyVerdict("allow", "bypass 模式:仅红线与凭据路径把关")
        if cfg.mode == "whitelist":
            return PolicyVerdict("deny", reason or "default 档:签名不在白名单内")
        return PolicyVerdict("review", reason or "auto 档:未登记的命令,交审核模型")

    # ---- 写 ----

    def remember(self, signature: str, *, allow: bool) -> PolicyConfig:
        """把签名写入永久名单。FileLock 保护:中台是常驻进程,可能并发写。"""
        lock = FileLock(str(self.path) + ".lock")
        with lock:
            try:
                config = self.load()
            except CommandPolicyError:
                config = PolicyConfig()
            target = config.allow if allow else config.deny
            other = config.deny if allow else config.allow
            if signature in other:
                other.remove(signature)
            if signature not in target:
                target.append(signature)
            self._write(config)
            return config

    def set_mode(self, mode: str) -> PolicyConfig:
        if mode not in MODES:
            raise CommandPolicyError(
                f"命令策略 mode 无效: {mode!r},须取 {', '.join(MODES)}"
            )
        lock = FileLock(str(self.path) + ".lock")
        with lock:
            try:
                config = self.load()
            except CommandPolicyError:
                config = PolicyConfig()
            config.mode = mode  # type: ignore[assignment]
            self._write(config)
            return config

    def _write(self, config: PolicyConfig) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "mode": config.mode,
            "allow": config.allow,
            "deny": config.deny,
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(self.path)
