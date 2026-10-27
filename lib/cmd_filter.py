"""命令红线过滤器 — 集群命令唯一出口(SSHPool)上的闸门。

三级:READONLY 放行 / WRITE 默认拒(approved 令牌放行,放行须留痕)/ FORBIDDEN 无条件拒。
教训继承:匹配基于「分段命令词 + 定向正则」,不做全串子串匹配(v3 误杀根源)。
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import List, Optional, Set

import yaml

from lib.shell_lex import mask_quoted_substitutions

_WRAPPERS = {"sudo", "timeout", "nice", "env", "nohup"}


class Level(Enum):
    READONLY = "readonly"
    WRITE = "write"
    FORBIDDEN = "forbidden"


@dataclass
class Verdict:
    level: Level
    reason: str


class CommandDenied(Exception):
    def __init__(self, cmd: str, verdict: Verdict):
        self.cmd, self.verdict = cmd, verdict
        super().__init__(f"[{verdict.level.value}] {verdict.reason} :: {cmd}")


def default_rules_path() -> Path:
    return Path(__file__).parent / "data" / "filter_rules.yaml"


# 环境包红线补充:只许收紧,列表项追加在核心规则之后
REDLINE_SUPPLEMENT = Path("policy") / "redlines.yaml"
_TIGHTENING_KEYS = frozenset({
    "forbidden_commands", "forbidden_patterns", "forbidden_word_flags",
    "write_commands", "write_patterns", "write_word_flags",
})


def redline_sources(pack=None) -> List[Path]:
    """有效红线规则的来源文件:核心规则 + 环境包补充(存在才有);双向锁按它们算 sha。"""
    from lib.envpack import active_env_pack

    pack = pack or active_env_pack()
    supplement = pack.root / REDLINE_SUPPLEMENT
    return [default_rules_path()] + ([supplement] if supplement.is_file() else [])


def effective_rules(pack=None) -> dict:
    """核心规则合并环境包补充;补充里出现放宽类或未知键即显式失败。"""
    sources = redline_sources(pack)
    rules = yaml.safe_load(sources[0].read_text(encoding="utf-8")) or {}
    for path in sources[1:]:
        extra = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        unknown = sorted(set(extra) - _TIGHTENING_KEYS)
        if unknown:
            raise ValueError(f"{path}: 红线补充只许收紧规则,不认 {unknown}")
        for key, values in extra.items():
            rules[key] = list(rules.get(key) or []) + list(values or [])
    return rules


def _command_word(segment: str) -> Optional[str]:
    """取管道段的命令词:跳过 VAR=、sudo -u <user> 等 wrapper,basename 化。"""
    try:
        tokens = shlex.split(segment)
    except ValueError:
        tokens = segment.split()
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if re.match(r"^\w+=", t):
            i += 1
        elif t in _WRAPPERS:
            i += 1
            while i < len(tokens):
                if tokens[i] in ("-u", "--user") and i + 1 < len(tokens):
                    i += 2
                elif tokens[i].startswith("-") or re.match(r"^\d+[smhd]?$", tokens[i]):
                    i += 1
                else:
                    break
        elif t.startswith("<"):
            i += 1
        else:
            return Path(t).name
    return None


def _split_segments(cmd: str) -> List[str]:
    """按 shell 分隔符(| ; & 换行)拆段,跳过引号内与反斜杠转义的分隔符。

    引号内的 | 拆出假段曾致误杀(grep '(sh|bash) -c' 参数被当命令段)。
    """
    segs: List[str] = []
    buf: List[str] = []
    quote: Optional[str] = None
    escaped = False
    i = 0
    while i < len(cmd):
        ch = cmd[i]
        if escaped:
            buf.append(ch)
            escaped = False
        elif quote:
            if ch == "\\" and quote == '"':
                escaped = True
            elif ch == quote:
                quote = None
            buf.append(ch)
        elif ch == "\\":
            escaped = True
            buf.append(ch)
        elif ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif ch in "|;&\n\r":
            segs.append("".join(buf))
            buf = []
            if ch in "|&" and i + 1 < len(cmd) and cmd[i + 1] == ch:
                i += 1
        else:
            buf.append(ch)
        i += 1
    segs.append("".join(buf))
    return [s.strip() for s in segs if s.strip()]


# 这些工具把单引号参数当搜索词/文件名消费,因此可以在 write_patterns 匹配前中和。
# 刻意不含 mysql/hive/beeline/impala-shell(执行 SQL)、bash/sh/python/perl(执行代码)、
# sed/awk(执行程序)、find/xargs/eval(派生命令)——它们的引号只挡 shell,挡不住自己。
#
# 【加成员前必须满足的不变量】该命令不得把自己的参数回显到 stdout。
# 否则 `<成员> '<shell 代码>' | sh` 会把被中和的载荷送进下游解释器,而中和只看
# 本段段首、不看下游段。现有 19 个成员均以参数为文件名/模式/字符集,故安全;
# echo / printf / basename / dirname / seq / yes / rev 一旦加入即刻可利用。
# 由 test_allowlisted_leaders_never_echo_args_to_interpreter 把关。
#
# 注意:成员并非全然不写文件——`sort -o FILE` 会写,`sort --compress-program`
# 会执行任意二进制。那是 filter_rules.yaml 的既有缺口(本改动前后判定一致),
# 已记 ROADMAP Bug;别把本清单当作「这些命令绝对无副作用」的依据。
_LITERAL_ARG_CMDS = frozenset({
    "grep", "egrep", "fgrep",
    "tail", "head", "cat", "tac", "nl",
    "ls", "wc", "sort", "uniq", "cut", "tr", "od", "strings", "stat", "file",
})


# 中和的准入是「白名单文法」而非「黑名单构造」:黑名单在对手可控的 shell 文法上
# 天然 fail-open —— 枚举多少就只挡多少,漏掉的静默放行(实测两次:嵌套命令上下文、
# 以及 bash 在词法分析前拼回的行继续符 `$\<换行>(`)。
#
# 反过来只认这一种极规整的形态:若干由 | 连接的段,每段是「裸词 / 单引号字面量」
# 的空白分隔序列。诊断动作渲染出的命令全部落在其中(shlex.quote 产出的正是单引号)。
# 任何越出文法的东西——$ 展开、命令替换、重定向、反引号、注释、换行、heredoc、
# 双引号、反斜杠——一律不中和,原串交给 write_patterns。新 shell 语法或未知构造
# 因此默认落在安全侧。
_SAFE_TOKEN = r"(?:'[^']*'|[A-Za-z0-9_.,:=/+@%-]+)"
_NEUTRALIZE_SAFE_GRAMMAR = re.compile(
    rf"[ \t]*{_SAFE_TOKEN}(?:[ \t]+{_SAFE_TOKEN})*"
    rf"(?:[ \t]*\|[ \t]*{_SAFE_TOKEN}(?:[ \t]+{_SAFE_TOKEN})*)*[ \t]*"
)


def _neutralize_literal_args(cmd: str) -> str:
    """把只读工具的单引号参数换成占位符,供 write_patterns 整串匹配使用。

    write_patterns 用 search 扫整串,不理解 shell 引号;而诊断动作的自由参数
    经 shlex.quote 后正是单引号包裹的纯数据。搜索词里出现 `systemctl stop`
    一类字面量本会被误判成写操作,让整个排查任务失败(参数值问题不该是致命类)。
    只中和单引号:双引号内 $ 与反引号仍会展开,不能视为字面量。
    """
    if not _NEUTRALIZE_SAFE_GRAMMAR.fullmatch(cmd):
        return cmd
    out: List[str] = []
    seg: List[str] = []          # 当前段已扫描内容,用于取命令词
    quote: Optional[str] = None
    escaped = False
    i = 0
    while i < len(cmd):
        ch = cmd[i]
        if escaped:
            out.append(ch); seg.append(ch); escaped = False
        elif quote:
            if ch == "\\" and quote == '"':
                escaped = True
            elif ch == quote:
                quote = None
            out.append(ch); seg.append(ch)
        elif ch == "\\":
            escaped = True
            out.append(ch); seg.append(ch)
        elif ch == "'":
            end = cmd.find("'", i + 1)
            if end == -1:                     # 引号未闭合:保守起见原样保留
                out.append(cmd[i:])
                return "".join(out)
            literal = cmd[i:end + 1]
            word = _command_word("".join(seg).strip())
            out.append(
                "'" + "\x01" * (end - i - 1) + "'"
                if word in _LITERAL_ARG_CMDS else literal
            )
            seg.append(literal)
            i = end
        elif ch == '"':
            quote = ch
            out.append(ch); seg.append(ch)
        elif ch in "|;&\n\r":
            out.append(ch); seg = []
        else:
            out.append(ch); seg.append(ch)
        i += 1
    return "".join(out)


def _has_short_flag(segment: str, flag: str) -> bool:
    """段内是否出现含 flag 字母的单横线旗标簇(-c/-lc/-xc;不匹配 --长选项与引号内字符串)。"""
    try:
        tokens = shlex.split(segment)
    except ValueError:
        tokens = segment.split()
    pat = re.compile(rf"^-[a-zA-Z]*{re.escape(flag)}")
    return any(pat.match(t) for t in tokens)


class CmdFilter:
    def __init__(self, rules: dict):
        self._allow = [re.compile(p) for p in rules.get("allow_patterns", [])]
        self._forbid_cmds: Set[str] = set(rules.get("forbidden_commands", []))
        self._forbid_pats = [re.compile(p) for p in rules.get("forbidden_patterns", [])]
        self._write_cmds: Set[str] = set(rules.get("write_commands", []))
        self._write_pats = [re.compile(p) for p in rules.get("write_patterns", [])]
        self._write_word_flags = [
            (re.compile(item["words"]), str(item["flag"]))
            for item in rules.get("write_word_flags", [])
        ]
        self._forbid_word_flags = [
            (re.compile(item["words"]), str(item["flag"]))
            for item in rules.get("forbidden_word_flags", [])
        ]

    @classmethod
    def from_yaml(cls, path: Path) -> "CmdFilter":
        return cls(yaml.safe_load(Path(path).read_text(encoding="utf-8")))

    @classmethod
    def default(cls, pack=None) -> "CmdFilter":
        """生产用:核心规则 + 当前环境包的红线补充。"""
        return cls(effective_rules(pack))

    def classify(self, cmd: str) -> Verdict:
        for pat in self._forbid_pats:
            if pat.search(cmd):
                return Verdict(Level.FORBIDDEN, f"forbidden pattern: {pat.pattern}")
        segments = _split_segments(cmd)
        seg_words = [(s, _command_word(s)) for s in segments]
        for seg, w in seg_words:
            if w and w in self._forbid_cmds:
                return Verdict(Level.FORBIDDEN, f"forbidden command: {w}")
            for word_pat, flag in self._forbid_word_flags:
                if w and word_pat.match(w) and _has_short_flag(seg, flag):
                    return Verdict(
                        Level.FORBIDDEN, f"forbidden command flag: {w} -{flag}"
                    )
        for seg, w in seg_words:
            if any(pat.search(seg) for pat in self._allow):
                continue  # allow 段级豁免:该段跳过 write 命令词判定,不短路其余段
            if w and w in self._write_cmds:
                return Verdict(Level.WRITE, f"write command: {w}")
            for word_pat, flag in self._write_word_flags:
                if w and word_pat.match(w) and _has_short_flag(seg, flag):
                    return Verdict(Level.WRITE, f"write command flag: {w} -{flag}")
        # 只读工具的单引号参数是数据不是命令;forbidden_pats 仍扫原串保持严格。
        # 任何命令单引号里的 $( 与反引号都不会被展开(awk 的 $(NF-1)),屏蔽后再
        # 匹配命令替换规则;扫描有歧义(注释/行继续/heredoc 等)时不屏蔽。
        # 行继续符在词法分析前就被 shell 拼回(`$\<换行>(` 即 `$(`),匹配前同样拼回
        neutralized = mask_quoted_substitutions(_neutralize_literal_args(cmd)).replace("\\\n", "")
        for pat in self._write_pats:
            if pat.search(neutralized):
                return Verdict(Level.WRITE, f"write pattern: {pat.pattern}")
        return Verdict(Level.READONLY, "default readonly")

    def check(self, cmd: str, approved: bool = False) -> Verdict:
        v = self.classify(cmd)
        if v.level is Level.FORBIDDEN:
            raise CommandDenied(cmd, v)
        if v.level is Level.WRITE and not approved:
            raise CommandDenied(cmd, v)
        return v
