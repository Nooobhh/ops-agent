"""命令能碰哪些路径。

为什么必须按路径管而不是按工具管:正表里能把文件内容打到 stdout 的工具有十几个
(cat/head/tail/grep -v/awk/sed/nl/cut/zcat…),每加一个只读工具就多一条读取途径。
路径是它们唯一的共同收口。

两件事一起管:

  读取范围  越界读会把 SSH 私钥、/etc/shadow 这类内容带进模型上下文并落进
            records/(打码器认不出裸私钥内容),而 records 是个会 push 的 git 仓
  扫描范围  `grep -rn x /` 会递归扫整个根文件系统,在生产节点上打满 IO

**拒绝必须是可操作的,不能是死胡同。** 越界不等于「查不了」——配置项改走
插件的配置查询接口(还自带密码脱敏),确需读渲染后的文件则转人工裁决放行那一个文件。
理由里要把这条出路写清楚,否则模型只会像没有自由命令时那样直接放弃。
"""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

# 与日志无关的基础根,由本模块固定持有(日志目录清单不管这些)
BASE_ALLOW_ROOTS: tuple[str, ...] = (
    "/proc",                 # 系统与进程状态
    "/sys",                  # 硬件/内核/cgroup 状态
    "/tmp",                  # 临时文件与 core dump;敏感文件仍由 deny 模式兜住
    "/opt",                  # 部署根(沿用 fs-roots.yaml)
    "/var/lib/mysql",        # 只用于 du/df 看占用,内容读取由 deny 兜住
    # /etc 下按**具体文件**开放,不开整个目录:组件配置(/etc/*/conf/)走
    # 插件的配置查询工具拿(值更准且密码已脱敏),而 shadow/sudoers 必须挡住
    "/etc/hosts", "/etc/resolv.conf", "/etc/nsswitch.conf",
    "/etc/fstab", "/etc/mtab",
    "/etc/os-release", "/etc/redhat-release", "/etc/system-release",
    "/etc/passwd", "/etc/group",      # 世界可读,只有账号名没有口令
    "/etc/sysctl.conf", "/etc/security/limits.conf",
    "/etc/localtime", "/etc/timezone",
)

# 日志允许根 = 本组 ∪ 日志目录清单(环境包 log-catalog)登记的目录。
#
# /var/log 固定放行:系统日志(messages / secure / cron / dmesg 等)不在组件
# 日志清单里,只用 catalog 会把它们整个挡在外面;而它下面本就没有凭据类文件,deny 模式
# 也仍然生效。
LOG_BASE_ROOTS: tuple[str, ...] = ("/var/log",)

# 日志目录清单覆盖 /var/log 之外的组件日志(如 /usr/lib/*/logs、/data*/hadoop/yarn/log);
# 这几条只在清单读不到时兜底,粒度比采集结果粗。环境专有的部署根(如组件安装根)
# 登记在环境包 fs-roots.yaml,随策略配置并入允许根。
FALLBACK_LOG_ROOTS: tuple[str, ...] = (
    "/usr/lib",
    "/data", "/data1", "/data2", "/data3", "/data4",
    "/data5", "/data6", "/data7",
)

# 不传策略时的默认值:与 load_path_policy 在 catalog 缺失时给出的一致
DEFAULT_ALLOW_ROOTS: tuple[str, ...] = (
    BASE_ALLOW_ROOTS + LOG_BASE_ROOTS + FALLBACK_LOG_ROOTS
)

# 这些工具的路径参数属于**另一个命名空间**,不能拿本地文件系统的允许根去卡。
# `hdfs dfs -ls /user/hive/warehouse` 里的路径在 HDFS 上,与本地 / 无关。
# HDFS 侧的读取范围由 hdfs 自身的子动作正表约束(只放行 -ls/-cat/-du 等)。
FOREIGN_NAMESPACE_TOOLS: Dict[str, frozenset] = {
    "hdfs": frozenset({"dfs", "dfsadmin", "fsck"}),
    "hadoop": frozenset({"fs"}),
    "yarn": frozenset({"application", "node", "queue", "logs"}),
}

# 优先于 allow。命中即拒,哪怕它落在某个允许根下
DEFAULT_DENY_PATTERNS: tuple[str, ...] = (
    # 凭据与密钥
    r"^/root(/|$)",
    r"^/home/[^/]+/\.ssh(/|$)",
    r"/\.ssh(/|$)",
    r"^/etc/shadow$", r"^/etc/gshadow$", r"^/etc/sudoers(\.d)?(/|$)",
    r"^/etc/ssh/ssh_host_.*_key$",
    r"\.pem$", r"\.key$", r"(^|/)id_(rsa|dsa|ecdsa|ed25519)(\.|$)",
    r"(^|/)\.aws(/|$)", r"(^|/)\.kube(/|$)", r"(^|/)credentials(\.json)?$",
    r"(^|/)keytab(s)?(/|$)", r"\.jceks$", r"\.jks$",
    # /proc 里会泄密或过大的
    r"^/proc/\d+/environ$",
    r"^/proc/\d+/mem$",
    r"^/proc/kcore$",
    # 数据文件本体:允许 du/df 看占用,不允许读内容
    r"^/var/lib/mysql/.+\.(ibd|MYD|MYI|frm)$",
)


class PathPolicyError(RuntimeError):
    pass


@dataclass(frozen=True)
class PathVerdict:
    ok: bool
    reason: str = ""
    # 越界时给出的具体路径:签名要按它精确化,避免「批准一次 = 放行整个工具」
    offending: Optional[str] = None
    # 命中明令禁止(凭据/密钥/进程环境)而非单纯越界。前者三档一律挂人工审核,
    # 连 bypass 也不例外——生产数据泄露不可逆,不该由档位开关决定;后者只是不
    # 在允许根内,宽档可以放行
    credential: bool = False
    # 本次遍历识别到的全部路径 token,包括允许范围内的路径。
    paths: List[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.paths is None:
            object.__setattr__(self, "paths", [])


@dataclass(frozen=True)
class PathPolicy:
    allow_roots: tuple[str, ...] = DEFAULT_ALLOW_ROOTS
    deny_patterns: tuple[str, ...] = DEFAULT_DENY_PATTERNS

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "_deny", tuple(re.compile(p) for p in self.deny_patterns)
        )

    # ---- 单个路径 ----

    def check_path(self, raw: str) -> PathVerdict:
        # 凭据模式先对原始串查一遍,再谈是不是合法的绝对路径。`file:///root/
        # .ssh/id_rsa` 不以 / 开头,只按「须用绝对路径」驳回的话它就落进了
        # 「档位相关」那一类,宽档照样放行——凭据要求三档都审,不能这样漏掉
        for pattern in self._deny:  # type: ignore[attr-defined]
            if pattern.search(raw):
                return PathVerdict(
                    False,
                    f"{raw} 属明令禁止读取的路径(凭据/密钥/进程环境等)",
                    raw,
                    credential=True,
                )
        if not raw.startswith("/"):
            # 相对路径的落点取决于远端 cwd,我们无从判断;要求绝对路径同时
            # 也堵掉了一部分 ../ 逃逸
            return PathVerdict(
                False, f"须用绝对路径,不接受 {raw!r}", raw
            )
        normalized = posixpath.normpath(raw)
        if normalized != "/" and normalized.endswith("/"):
            normalized = normalized.rstrip("/")
        for pattern in self._deny:  # type: ignore[attr-defined]
            if pattern.search(normalized):
                # normpath 之后才现形的:`/var/log/../../root/.ssh/id_rsa`
                return PathVerdict(
                    False,
                    f"{normalized} 属明令禁止读取的路径(凭据/密钥/进程环境等)",
                    normalized,
                    credential=True,
                )
        if not self._within_allowed(normalized):
            return PathVerdict(
                False,
                f"{normalized} 不在允许范围内",
                normalized,
            )
        return PathVerdict(True)

    def _within_allowed(self, normalized: str) -> bool:
        for root in self.allow_roots:
            if normalized == root or normalized.startswith(root.rstrip("/") + "/"):
                return True
        return False

    def is_root_itself(self, raw: str) -> bool:
        """路径是否恰好是某个允许根(而非其下的子路径)。

        递归扫描以允许根为起点仍然太宽:`grep -r x /var/log` 会扫掉 catalog 里
        103 个目录。要求下钻一层再递归。
        """
        normalized = posixpath.normpath(raw)
        return any(normalized == root for root in self.allow_roots)


def log_roots_from_catalog(catalog: Optional[Dict]) -> tuple[str, ...]:
    """从 log-catalog.yaml 取日志目录。

    catalog 由环境包的日志目录采集器生成:组件日志目录以清单登记的为准,不在这里另立一份。
    """
    if not catalog:
        return ()
    found: set = set()

    def walk(node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in {"path", "log_dir", "dir"} and isinstance(value, str):
                    if value.startswith("/"):
                        found.add(posixpath.normpath(value))
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(catalog)
    return tuple(sorted(found))


def load_path_policy(
    config: Optional[Dict] = None,
    platform: str = "",
    catalog: Optional[Dict] = None,
) -> PathPolicy:
    """构造路径策略。

    允许根 = 基础根 + **/var/log ∪ 日志目录清单登记的目录** + 显式配置的额外根。

    取并集而不是只用 catalog:/var/log 下的系统日志不在组件清单里,只认 catalog
    会把 messages / secure / cron 挡在外面;而 catalog 覆盖 /var/log 之外的组件
    日志(/usr/lib/*/logs、/data*/hadoop/yarn/log 等)。
    catalog 读不到时退回 FALLBACK_LOG_ROOTS——路径策略不能因为少个文件就整个
    失效,但兜底项本身仍是有界的几个根,不是放开 /。
    """
    log_roots = LOG_BASE_ROOTS + (
        log_roots_from_catalog(catalog) or FALLBACK_LOG_ROOTS
    )
    deny = list(DEFAULT_DENY_PATTERNS)
    extra_allow: tuple[str, ...] = ()
    if config:
        platforms = config.get("platforms") or {}
        # 不指定平台时取所有平台的并集:CommandPolicy 是进程级的,构造时还不
        # 知道任务属于哪个集群。各集群的根差异只是部署根这类,放宽
        # 一点不构成边界——真正的边界是 deny 模式与红线
        entries = (
            [platforms.get(platform) or {}] if platform
            else list(platforms.values())
        )
        collected: List[str] = []
        for entry in entries:
            if isinstance(entry, list):
                # config/fs-roots.yaml 的形态:平台下直接是 [{path, note}]。
                # 它是人工维护的「可读根白名单」,与 fs.list_dir 共用同一份
                # 信源——自由命令读文件和诊断列目录不该各认各的根
                collected.extend(
                    str(item["path"]) for item in entry
                    if isinstance(item, dict) and item.get("path")
                )
            elif isinstance(entry, dict):
                collected.extend(
                    str(item) for item in (entry.get("allow") or [])
                )
                deny.extend(str(item) for item in (entry.get("deny") or []))
        collected.extend(str(item) for item in (config.get("allow") or []))
        deny.extend(str(item) for item in (config.get("deny") or []))
        extra_allow = tuple(dict.fromkeys(collected))
    return PathPolicy(
        allow_roots=BASE_ALLOW_ROOTS + log_roots + extra_allow,
        deny_patterns=tuple(deny),
    )


# 拒绝时附给模型的出路。没有这段,模型只会当成「查不了」然后放弃,
# 就退回到没有自由命令时那种「读两篇文档就 verdict=insufficient」的老样子
GUIDANCE = (
    "组件配置项请改用插件的配置查询工具(插件页启用后可用):它给的是当前生效值,"
    "且密码已脱敏。若这是个日志目录,可能是组件升级后日志目录清单(log-catalog)"
    "尚未重新采集(它按登记的目录精确放行,新版本目录未采集就不在册),"
    "可先用已登记的同组件目录取证。以上都不适用、确需读取本文件时,"
    "重发本命令会转人工裁决,由发起人决定是否放行这一个文件。"
)


# 第一个位置参数不是文件路径而是「程序/模式」的工具。awk 的
# `/error|warn/{print}`、sed 的 `/error/p`、grep 的 `"/api/v1"` 都含 `/`,
# 按路径去查会把最常用的排查写法拒掉
_PATTERN_ARG_TOOLS = {
    "awk", "gawk", "sed", "grep", "egrep", "fgrep", "zgrep",
}
# 给出了模式/脚本的选项:此时位置参数就全是文件了
_PATTERN_OPTIONS = {"-e", "--expression", "-f", "--file", "--regexp"}


def _value_options(tool: str) -> frozenset:
    """某工具「要吃掉下一个 token 作为值」的选项。

    直接取正表里的定义,不另维护一份:选项语义是**工具相关**的——`-v` 在 awk
    里带值(变量赋值),在 grep 里不带值(反选)。用一份工具无关的表会把
    `grep -v zzz /etc/shadow` 的 zzz 当成 -v 的取值吃掉,于是 /etc/shadow 变成
    第一个位置参数被当模式跳过,敏感文件就这么漏过去了。
    """
    from .command_grammar import TOOLS  # 延迟导入:command_grammar 依赖本模块

    grammar = TOOLS.get(tool)
    return grammar.options_with_value if grammar else frozenset()


def _non_path_indexes(tool: str, segment: Sequence[str]) -> set:
    """返回该段里**不该按路径判定**的参数下标。

    两类:模式/程序文本(第一个位置参数),以及带值选项的取值(`-name '*.log'`
    的 `*.log`、`grep -e '/api'` 的 `/api`)。
    """
    args = [str(t) for t in segment[1:]]
    skip: set = set()
    value_options = _value_options(tool)
    has_pattern_option = any(a in _PATTERN_OPTIONS for a in args)
    seen_positional = False
    index = 1
    skip_next = False
    for token in args:
        if skip_next:
            skip.add(index)
            skip_next = False
            index += 1
            continue
        if token == "--":
            index += 1
            continue
        if token.startswith("-") and token != "-":
            base = token.split("=", 1)[0]
            if base in value_options and "=" not in token:
                skip_next = True
            index += 1
            continue
        if (
            not seen_positional
            and tool in _PATTERN_ARG_TOOLS
            and not has_pattern_option
        ):
            skip.add(index)   # 第一个位置参数是模式/程序,不是路径
        seen_positional = True
        index += 1
    return skip


def check_paths(
    pipeline: Sequence[Sequence[str]],
    policy: Optional[PathPolicy] = None,
) -> PathVerdict:
    """检查整条 pipeline 里出现的所有路径。

    识别规则:凡**含 `/`** 的 token 都当路径查,再由 `_non_path_indexes` 排除
    模式/程序文本与带值选项的取值。用「含 /」而不是「以 / 开头」,是因为后者
    漏掉相对路径——`cat var/log/x` 与 `cat ../../root/.ssh/id_rsa` 都不以 /
    开头,却照样能读到东西。
    """
    active = policy or PathPolicy()
    recognised = _recognised_paths(pipeline)
    for segment in pipeline:
        if not segment:
            continue
        tool = str(segment[0]).rsplit("/", 1)[-1]
        # 跨命名空间的工具(hdfs/hadoop/yarn)整段跳过本地路径判定
        foreign = FOREIGN_NAMESPACE_TOOLS.get(tool)
        if foreign is not None:
            positional = [
                str(t) for t in segment[1:] if not str(t).startswith("-")
            ]
            if positional and positional[0] in foreign:
                continue
        recursive = any(
            str(token) in {"-r", "-R", "--recursive"} or (
                str(token).startswith("-")
                and not str(token).startswith("--")
                and "r" in str(token)[1:]
                and tool in {"grep", "egrep", "fgrep", "zgrep"}
            )
            for token in segment[1:]
        )
        skip = _non_path_indexes(tool, segment)
        for index, token in enumerate(segment[1:], start=1):
            text = str(token)
            if "/" not in text or index in skip:
                continue
            verdict = active.check_path(text)
            if not verdict.ok:
                return PathVerdict(
                    False,
                    f"{verdict.reason};{GUIDANCE}",
                    verdict.offending,
                    credential=verdict.credential,
                    paths=recognised,
                )
            if recursive and active.is_root_itself(text):
                return PathVerdict(
                    False,
                    f"{text} 是允许根本身,递归扫描它范围过大,"
                    f"请下钻到具体子目录再用递归;{GUIDANCE}",
                    posixpath.normpath(text),
                    paths=recognised,
                )
        # find 以允许根为起点会遍历整棵子树(如 /var/log 下 catalog 记了 103 个
        # 目录)。给了 -maxdepth 就是有界的,放行;没给才拒
        if tool == "find":
            bounded = any(
                str(token) in {"-maxdepth", "--maxdepth"}
                for token in segment[1:]
            )
            for token in segment[1:]:
                text = str(token)
                if not text.startswith("/"):
                    continue
                if active.is_root_itself(text) and not bounded:
                    return PathVerdict(
                        False,
                        f"find 以允许根 {text} 为起点且未限定 -maxdepth,"
                        f"会遍历整棵子树;请下钻到子目录或加 -maxdepth;{GUIDANCE}",
                        posixpath.normpath(text),
                        paths=recognised,
                    )
                break  # 起点只看第一个路径参数
    return PathVerdict(True, paths=recognised)


def _recognised_paths(pipeline: Sequence[Sequence[str]]) -> List[str]:
    """Return path tokens using the same exclusions as ``check_paths``."""
    found: List[str] = []
    for segment in pipeline:
        if not segment:
            continue
        tool = str(segment[0]).rsplit("/", 1)[-1]
        foreign = FOREIGN_NAMESPACE_TOOLS.get(tool)
        if foreign is not None:
            positional = [
                str(token) for token in segment[1:]
                if not str(token).startswith("-")
            ]
            if positional and positional[0] in foreign:
                continue
        skip = _non_path_indexes(tool, segment)
        for index, raw in enumerate(segment[1:], start=1):
            token = str(raw)
            if "/" in token and index not in skip:
                found.append(token)
    return found


def offending_paths(
    pipeline: Sequence[Sequence[str]],
    policy: Optional[PathPolicy] = None,
) -> List[str]:
    """列出越界路径,供签名精确化使用。"""
    active = policy or PathPolicy()
    found: List[str] = []
    for segment in pipeline:
        if not segment:
            continue
        tool = str(segment[0]).rsplit("/", 1)[-1]
        foreign = FOREIGN_NAMESPACE_TOOLS.get(tool)
        if foreign is not None:
            positional = [str(t) for t in segment[1:] if not str(t).startswith("-")]
            if positional and positional[0] in foreign:
                continue
        skip = _non_path_indexes(tool, segment)
        for index, raw in enumerate(segment[1:], start=1):
            token = str(raw)
            if "/" not in token or index in skip:
                continue
            verdict = active.check_path(token)
            if not verdict.ok and verdict.offending:
                found.append(verdict.offending)
    return found
