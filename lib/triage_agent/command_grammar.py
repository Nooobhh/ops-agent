"""内置工具正表:每个工具声明它允许的选项与形态,**未登记的一律拒绝**。

这是与 `filter_rules.yaml` 相反的方向。红线是黑名单——枚举危险的、其余放行,
面对的是无限的命令空间,补不完;本模块是白名单——枚举安全的、其余拒绝,
面对的是有限的工具集,可以收敛。两者叠加使用:

    红线(黑名单)   拦掉写/破坏类命令,不可绕过的底闸
    本模块(白名单) 在只读命令里再划出「结构上确定安全」的子集
    用户名单       在两者之上做例外(人工批准后放行正表外的命令)

**未知选项必须拒绝**是本模块唯一不可退让的性质。默认放行未知选项会让白名单
退化成黑名单——`sort -o/tmp/pwn`、`curl -dfoo`、`tar -xvf` 这类附值/簇写形态
正是这样漏过去的。

有三类工具的危险藏在**参数内容**里而非选项上(awk 的程序、sed 的脚本、
mysql 的 SQL),它们各有一段内容校验;校验不了的形态一律拒。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Sequence

from .path_policy import check_paths


class GrammarError(RuntimeError):
    pass


@dataclass(frozen=True)
class GrammarVerdict:
    ok: bool
    reason: str = ""
    # 「本档位不收,换档可能收」——未登记工具、宽档专属工具、非凭据的路径
    # 越界都属此类,宽档要转给审核模型或直接放行。判它危险的(选项越界、内容
    # 含执行构造、凭据路径)不带此标,宽档一样拒:放开工具集不等于放开约束
    tier_gated: bool = False
    # 命中凭据/密钥路径:三档一律挂人工审核,bypass 也不例外
    credential: bool = False
    paths: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ToolGrammar:
    """一个工具的准入文法。"""

    # 允许的选项(精确名)。短选项簇会先拆成单字母再逐个查
    options: FrozenSet[str] = frozenset()
    # 这些选项要吃掉一个值(下一个 token,或 -oVALUE / --opt=VALUE 的内联形态)
    options_with_value: FrozenSet[str] = frozenset()
    # 是否允许位置参数(文件路径、PID 等)
    positional: bool = True
    # 子命令白名单:第一个位置参数必须在其中
    subcommands: FrozenSet[str] = frozenset()
    # 子命令下的动作白名单,如 ("dfs",) -> {"-ls", "-cat", ...}
    sub_actions: Dict[str, FrozenSet[str]] = field(default_factory=dict)
    # 参数内容校验器名(awk / sed / mysql)
    content_check: str = ""
    # `head -15` / `tail -200`:数字是行数而非选项名,GNU 的老式写法。
    # 不认它等于把最常见的截断写法整个拒掉
    numeric_option: bool = False


def _o(*names: str) -> FrozenSet[str]:
    return frozenset(names)


_NUMERIC = re.compile(r"^-\d+$")


# 通用只读文本/文件工具的常见选项。逐工具列全太长,这里取排查实际会用到的。
_GREP = ToolGrammar(
    options=_o("-c", "-n", "-i", "-r", "-R", "-l", "-L", "-v", "-w", "-x",
               "-F", "-E", "-o", "-q", "-s", "-h", "-H", "-a", "-z",
               "--color", "--no-messages", "--text", "--binary-files"),
    options_with_value=_o("-A", "-B", "-C", "-m", "-e", "-f",
                          "--include", "--exclude", "--exclude-dir"),
)

TOOLS: Dict[str, ToolGrammar] = {
    # ---- 文本 ----
    "grep": _GREP,
    "egrep": _GREP,
    "fgrep": _GREP,
    "zgrep": _GREP,
    "cat": ToolGrammar(options=_o("-n", "-A", "-e", "-t", "-v", "-s")),
    "zcat": ToolGrammar(),
    "head": ToolGrammar(options_with_value=_o("-n", "-c", "--lines", "--bytes"),
                        numeric_option=True),
    "tail": ToolGrammar(
        # 不含 -f/-F/--follow:跟随输出永不退出,会占满预算并拖住通道
        options_with_value=_o("-n", "-c", "--lines", "--bytes"),
        numeric_option=True,
    ),
    "wc": ToolGrammar(options=_o("-l", "-w", "-c", "-m", "-L")),
    "cut": ToolGrammar(options_with_value=_o("-d", "-f", "-c", "-b",
                                             "--delimiter", "--fields")),
    "tr": ToolGrammar(options=_o("-d", "-s", "-c", "-t")),
    "uniq": ToolGrammar(options=_o("-c", "-d", "-u", "-i"),
                        options_with_value=_o("-f", "-s", "-w")),
    "nl": ToolGrammar(options=_o("-b", "-n")),
    # 只读 stdin/文件写 stdout;排查里当 GROUP_CONCAT 的替代用
    "paste": ToolGrammar(options=_o("-s", "-z", "--serial"),
                         options_with_value=_o("-d", "--delimiters")),
    "sort": ToolGrammar(
        # 不含 -o/--output(写文件)与 --compress-program(执行任意二进制)
        options=_o("-n", "-r", "-u", "-f", "-b", "-h", "-V", "-g", "-M", "-s"),
        options_with_value=_o("-k", "-t", "--key", "--field-separator"),
    ),
    "sed": ToolGrammar(
        # 不含 -i(原地改写),也不含 -f/--file:脚本在远端文件里,内容对本地
        # 校验完全不可见,等于把 e/w/r 命令藏进一个我们看不到的地方
        options=_o("-n", "-E", "-r", "-s", "-z", "--quiet", "--silent"),
        options_with_value=_o("-e", "--expression"),
        content_check="sed",
    ),
    "awk": ToolGrammar(
        options_with_value=_o("-F", "-v", "--field-separator"),
        content_check="awk",
    ),
    # jq 没有 system()、写不了文件,危险面只有「读到不该读的」:--slurpfile /
    # --rawfile / --argfile 能读任意文件绕开路径策略,-f 把程序藏进远端文件,
    # 一律不登记(未登记选项本就拒)。$ENV / env 由内容校验拦
    "jq": ToolGrammar(
        options=_o("-r", "--raw-output", "-c", "--compact-output",
                   "-n", "--null-input", "-e", "--exit-status",
                   "-s", "--slurp", "-S", "--sort-keys",
                   "-j", "--join-output", "-a", "--ascii-output",
                   "-R", "--raw-input", "--tab",
                   "-M", "--monochrome-output"),
        options_with_value=_o("--indent"),
        content_check="jq",
    ),
    # ---- 文件 ----
    "ls": ToolGrammar(options=_o("-l", "-a", "-A", "-h", "-t", "-r", "-S",
                                 "-d", "-1", "-i", "-R", "--full-time",
                                 "--color")),
    "stat": ToolGrammar(options=_o("-L", "-f"), options_with_value=_o("-c", "--format")),
    "file": ToolGrammar(options=_o("-b", "-i", "-L")),
    "readlink": ToolGrammar(options=_o("-f", "-e", "-m")),
    "basename": ToolGrammar(),
    "dirname": ToolGrammar(),
    "du": ToolGrammar(options=_o("-h", "-s", "-a", "-c", "-x", "-b", "-k", "-m"),
                      options_with_value=_o("-d", "--max-depth")),
    "df": ToolGrammar(options=_o("-h", "-i", "-k", "-m", "-T", "-P", "-a")),
    "find": ToolGrammar(
        # 只留 predicate 与 -print;-delete/-exec/-fprintf 等一律不在表内
        options=_o("-type", "-name", "-iname", "-path", "-ipath", "-print",
                   "-print0", "-empty", "-readable", "-nouser", "-nogroup",
                   "-follow", "-xdev", "-ls"),
        # -printf 只格式化到 stdout;写文件的是 -fprintf,不在表内
        options_with_value=_o("-maxdepth", "-mindepth", "-mtime", "-mmin",
                              "-size", "-user", "-group", "-perm", "-newer",
                              "-regex", "-iregex", "-printf"),
    ),
    # ---- 进程与资源 ----
    "ps": ToolGrammar(options=_o("-e", "-f", "-A", "-l", "-u", "-x", "-a",
                                 "aux", "auxf"),
                      options_with_value=_o("-o", "-p", "-C", "--sort")),
    "pgrep": ToolGrammar(options=_o("-l", "-a", "-f"), options_with_value=_o("-u")),
    "free": ToolGrammar(options=_o("-m", "-g", "-h", "-k", "-b", "-t", "-w")),
    "uptime": ToolGrammar(options=_o("-p", "-s")),
    "vmstat": ToolGrammar(options=_o("-s", "-d", "-a", "-w")),
    "mpstat": ToolGrammar(options=_o("-P")),
    "lsof": ToolGrammar(options=_o("-n", "-P"), options_with_value=_o("-i", "-p", "-u")),
    "lscpu": ToolGrammar(),
    "lsblk": ToolGrammar(options=_o("-f", "-a", "-l")),
    # ---- 网络 ----
    "netstat": ToolGrammar(options=_o("-a", "-n", "-p", "-t", "-u", "-l", "-i",
                                      "-r", "-s")),
    "ss": ToolGrammar(
        # 不含 -K(强杀连接)
        options=_o("-a", "-n", "-p", "-t", "-u", "-l", "-s", "-i", "-m"),
    ),
    # ---- 身份与系统 ----
    "hostname": ToolGrammar(options=_o("-f", "-s", "-i"), positional=False),
    "id": ToolGrammar(options=_o("-u", "-g", "-n")),
    "whoami": ToolGrammar(positional=False),
    "uname": ToolGrammar(options=_o("-a", "-r", "-s", "-m", "-n")),
    "date": ToolGrammar(
        # 不含 -s/--set:改系统时间
        options=_o("-u", "-R"), options_with_value=_o("-d", "--date"),
    ),
    "getent": ToolGrammar(),
    "last": ToolGrammar(options_with_value=_o("-n")),
    "dmesg": ToolGrammar(
        # 不含 -C/--clear/-c:清空内核环形缓冲
        options=_o("-T", "-x", "-H", "-k", "-u"),
    ),
    "journalctl": ToolGrammar(
        # 不含 -f/--follow 与 --vacuum-*/--rotate/--flush(写/删日志)
        options=_o("-x", "-e", "-r", "-k", "--no-pager", "--no-hostname"),
        options_with_value=_o("-u", "-n", "-p", "--since", "--until",
                              "--unit", "--lines", "--priority"),
    ),
    "systemctl": ToolGrammar(
        # 不含 -H/--host(指向别的机器)
        options=_o("--no-pager", "-l", "--full", "--all"),
        subcommands=_o("status", "show", "cat", "is-active", "is-enabled",
                       "is-failed", "list-units", "list-unit-files",
                       "list-timers", "list-sockets"),
    ),
    # ---- JVM ----
    "jps": ToolGrammar(options=_o("-l", "-v", "-m")),
    "jstat": ToolGrammar(options=_o("-gcutil", "-gc", "-gccapacity", "-gcnew",
                                    "-gcold", "-class", "-compiler")),
    "jstack": ToolGrammar(
        # 不含 -F(强制 attach,可能挂起或影响目标 JVM)
        options=_o("-l"),
    ),
    # ---- 大数据组件 ----
    "hdfs": ToolGrammar(
        subcommands=_o("dfs", "dfsadmin", "fsck", "version", "getconf",
                       "classpath"),
        sub_actions={
            # 不含 -get/-copyToLocal(写本地)与 -rm/-put 等写操作
            "dfs": _o("-ls", "-cat", "-tail", "-du", "-df", "-count", "-stat",
                      "-test", "-checksum", "-text", "-find", "-help"),
            # 不含 -safemode enter/leave、-refreshNodes 等状态变更
            "dfsadmin": _o("-report", "-printTopology", "-getDatanodeInfo"),
            # 不含 -delete/-move
            "fsck": _o("-files", "-blocks", "-locations", "-racks"),
        },
    ),
    "hadoop": ToolGrammar(
        subcommands=_o("fs", "version", "classpath", "checknative"),
        sub_actions={
            "fs": _o("-ls", "-cat", "-tail", "-du", "-df", "-count", "-stat",
                     "-test", "-checksum", "-text", "-find", "-help"),
        },
    ),
    "yarn": ToolGrammar(
        subcommands=_o("application", "node", "queue", "version", "classpath"),
        sub_actions={
            # 不含 -kill;logs 子命令整体不在表内(-out 会写本地目录)
            "application": _o("-list", "-status", "-appStates", "-appTypes"),
            "node": _o("-list", "-status", "-all", "-states"),
            "queue": _o("-status"),
        },
    ),
    "mysql": ToolGrammar(
        options=_o("-N", "-B", "-s", "-r", "--skip-column-names", "--batch",
                   "--table", "-t"),
        options_with_value=_o("-e", "-h", "-P", "-u", "--execute", "--host",
                              "--port", "--user", "--database", "-D"),
        content_check="mysql",
    ),
}

# 显式不入表的高能力工具,拒绝时给出具体理由而不是笼统的「未登记」
_REFUSED = {
    "xargs": "把后续 argv 当命令执行,等于绕过本表",
    "env": "可执行任意命令并泄露全部环境变量",
    "hbase": "hbase shell 可执行任意 HBase 操作",
    "bash": "shell 解释器",
    "sh": "shell 解释器",
    "zsh": "shell 解释器",
    "eval": "间接执行",
    "exec": "间接执行",
    "command": "wrapper,会掩盖真实命令词",
    "sudo": "提权 wrapper",
    "nohup": "脱离会话执行",
    "timeout": "wrapper;执行时限由 runner 统一施加,不接受调用方表达",
    "watch": "反复执行且永不退出",
    "tee": "写文件",
}

# 宽档(auto / bypass)才准入的工具。它们做不到「静态验证只读」——解释器能做任何
# 事,curl 能外传数据,tar 能写盘——所以窄档(default)一律不收。放开它们靠的不是
# 静态判定而是另外两种担保:auto 用审核模型看意图,bypass 用人的信任。
#
# 与 _REFUSED 的区别:那里面的东西任何档位都不给,因为它们会瓦解判定机制本身
# (bash -c 让 shell 重新解析、xargs 把 argv 当命令、sudo 掩盖命令词),批了也
# 看不清真实执行的是什么。这里的东西只是危险,不是不可判读。
_WIDE_ONLY = {
    "python": "内联解释器",
    "python3": "内联解释器",
    "perl": "内联解释器",
    "ruby": "内联解释器",
    "node": "内联解释器",
    "nodejs": "内联解释器",
    "php": "内联解释器",
    "curl": "可上传/提交数据并把响应写盘",
    "wget": "默认把响应写入当前目录",
    "tar": "可解包写文件,并支持 --to-command 执行",
    "gzip": "写文件",
    "gunzip": "写文件",
    "jmap": "-dump 会写出巨型 heap 文件并可能长时间暂停 JVM",
}

# jq 执行不了命令也写不了文件,能越界的只有读:$ENV / env 吐出整个环境(里面
# 有凭据),input / inputs 吃掉本该由管道上游控制的额外输入
_JQ_FORBIDDEN = re.compile(r"\$ENV\b|\benv\b|\binputs?\b")

# 参数即代码:这些构造能让工具跳出「只读」的语义
_AWK_FORBIDDEN = re.compile(
    # print/printf 到语句末尾之间出现 > 即视为重定向写文件。`print ($1 > 5)`
    # 这类比较会被一并拦下,属有意的保守取舍:误拒只是多问一次,漏判是 root 写盘
    r"\bsystem\s*\(|\bENVIRON\b|\bgetline\s*<|"
    # 管道:`print "x" | "id"` 与 `"id" | getline v` 都能执行外部程序。只拦
    # 管到字符串/变量/getline 的形态,`/error|warn/` 这类正则交替不受影响。
    # `||` 是逻辑或不是管道——`if(!(k in mn)||$4<mn[k])` 这种寻常写法被拦下
    # 的话,聚合类 awk 程序基本都过不去,故两侧都要排除掉 ||
    r"(?<!\|)\|(?!\|)\s*&|(?<!\|)\|(?!\|)\s*(?:[\"'$]|getline\b)|"
    r"\b(?:print|printf)\b[^;}\n]*>|"
    r"\bclose\s*\(|\bfflush\s*\(|"
    # gawk 扩展:载入外部脚本或共享库
    r"@(?:include|load)\b",
    re.IGNORECASE,
)

# sed 的危险命令:e(执行)、w/W(写文件)、r/R(读任意文件)
_SED_DANGEROUS_CMDS = frozenset("ewWrR")
# 地址前缀:行号 / $ / /regex/ / 范围 / step / 取反
_SED_ADDRESS = re.compile(
    r"^\s*(?:"
    r"\d+|\$|/(?:\\.|[^/\\])*/[IM]*|\\(.)(?:\\.|(?!\1).)*\1[IM]*"
    r")"
    r"(?:\s*[,~]\s*(?:\d+|\$|\+\d+|/(?:\\.|[^/\\])*/[IM]*))?"
    r"\s*!?\s*"
)
_SQL_READONLY = re.compile(r"^\s*(select|show|desc|describe|explain|with)\b", re.I)
_SQL_FORBIDDEN = re.compile(
    # 每个分支自带边界:统一在组末尾加 \b 会让 `get_lock('x')` 这类以 ( 收尾的
    # 形态匹配不上——( 与 ' 都是非单词字符,中间没有单词边界
    r"\binto\s+outfile\b|\binto\s+dumpfile\b|\bload\s+data\b|\binto\s+@|"
    r"\bload_file\s*\(|\bsleep\s*\(|\bbenchmark\s*\(|\bget_lock\s*\(|"
    # 以下几种虽以 select 开头,却会取锁或改会话状态,在生产库上能阻塞业务
    r"\bfor\s+update\b|\bfor\s+share\b|\block\s+in\s+share\s+mode\b",
    re.I,
)


def check_pipeline(
    pipeline: Sequence[Sequence[str]],
    path_policy=None,
    *,
    tier: str = "narrow",
) -> GrammarVerdict:
    """逐段检查工具与选项,再统一检查路径。任一项不过整条拒绝。

    tier="narrow"(default 档)只收结构上可静态验证只读的工具;tier="wide"
    (auto / bypass 档)额外收 _WIDE_ONLY——那些工具的安全性由审核模型或人的
    信任担保,不由本函数担保。两档共用同一份选项文法与路径策略。

    path_policy 缺省时退回内置兜底根(粗粒度)。调用方应传入由
    `load_path_policy(catalog=…)` 构造的策略,那样日志目录才精确到采集器
    登记的那些目录——不传等于 catalog 白采。
    """
    if not pipeline:
        return GrammarVerdict(False, "空 pipeline")
    path_verdict = check_paths(pipeline, path_policy)
    # 凭据/密钥路径先判,优先于工具是否登记:宽档的未登记工具会在下面的逐段
    # 检查里提前返回,`curl file:///etc/shadow` 就此绕开路径策略。这一类要求
    # 三档都挂人工,不能因为工具没登记而漏掉
    if not path_verdict.ok and path_verdict.credential:
        return GrammarVerdict(
            False, path_verdict.reason, credential=True, paths=path_verdict.paths,
        )
    for index, segment in enumerate(pipeline, start=1):
        verdict = _check_segment(segment, first=index == 1, tier=tier)
        if not verdict.ok:
            return GrammarVerdict(
                False,
                f"第 {index} 段{verdict.reason}",
                tier_gated=verdict.tier_gated,
                paths=path_verdict.paths,
            )
    # 其余路径问题放在最后:先确认工具与选项合法,再看它能碰哪里。反过来的话
    # 一条 `wget /var/log/x` 会报路径问题,把真正的原因(工具不在正表)盖住。
    # 非凭据的越界是档位相关的——读 /etc/hive/conf/hive-site.xml 在 default
    # 档拒,在 auto / bypass 档该按那两档的规矩走,不是三档一律拒
    if not path_verdict.ok:
        return GrammarVerdict(
            False, path_verdict.reason, tier_gated=True, paths=path_verdict.paths,
        )
    return GrammarVerdict(True, paths=path_verdict.paths)


def _check_segment(
    segment: Sequence[str], *, first: bool = True, tier: str = "narrow",
) -> GrammarVerdict:
    if not segment:
        return GrammarVerdict(False, "为空")
    raw_tool = str(segment[0])
    if "/" in raw_tool:
        # 绝对/相对路径直指二进制:`/tmp/cat` 与 PATH 上的 cat 不是一回事
        return GrammarVerdict(False, f"工具须用裸命令名,不接受路径: {raw_tool}")
    tool = raw_tool
    if tool in _REFUSED:
        return GrammarVerdict(False, f"工具 {tool} 不在正表({_REFUSED[tool]})")
    if tool in _WIDE_ONLY and tier != "wide":
        # 说清「换档能做」而不只是「不行」:模型据此改用等价只读写法,或者
        # 让发起人把任务切到 auto/bypass,而不是当成功能不可用直接放弃
        return GrammarVerdict(
            False,
            f"工具 {tool}({_WIDE_ONLY[tool]})只在 auto / bypass 档可用,"
            f"当前档位只收可静态验证只读的工具",
        )
    grammar = TOOLS.get(tool)
    if grammar is None:
        if tier == "wide":
            # 宽档的未登记工具不由本表判定:auto 交审核模型,bypass 直接放行。
            # 在这里拒掉的话宽档就退化成窄档,分档也就没有意义了
            return GrammarVerdict(False, f"工具 {tool} 未登记", tier_gated=True)
        return GrammarVerdict(False, f"工具 {tool} 未登记")

    args = [str(token) for token in segment[1:]]
    for token in args:
        # 不依赖调用方先过 Pydantic:策略层自己也要挡住空参数与控制字符
        if not token:
            return GrammarVerdict(False, f"{tool} 含空参数")
        if any(ch in token for ch in "\x00\n\r"):
            return GrammarVerdict(False, f"{tool} 的参数含控制字符")
        if token == "-" and first:
            # 首段的 stdin 无人喂,`-` 会让命令一直挂着;下游段的 stdin 正是
            # 上一段的输出,`paste -sd, -` 这类写法必须放行
            return GrammarVerdict(False, f"{tool} 不接受 stdin 参数 -")
    positionals: list[str] = []
    # hdfs/hadoop/yarn 的 `-ls`、`-report` 是子命令的**动作**而非工具选项,
    # 交给下面的 sub_actions 正表判定,这里不按选项处理
    action_style = bool(grammar.sub_actions)
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            positionals.extend(args[index + 1:])
            break
        if token.startswith("-") and token != "-":
            if action_style:
                index += 1
                continue
            consumed = _check_option(tool, grammar, token, args, index)
            if isinstance(consumed, GrammarVerdict):
                return consumed
            index += consumed
            continue
        positionals.append(token)
        index += 1

    if positionals and not grammar.positional:
        return GrammarVerdict(False, f"{tool} 不接受位置参数")
    if grammar.subcommands:
        if not positionals:
            return GrammarVerdict(False, f"{tool} 缺子命令")
        sub = positionals[0]
        if sub not in grammar.subcommands:
            return GrammarVerdict(False, f"{tool} 子命令 {sub} 不在正表")
        actions = grammar.sub_actions.get(sub)
        if actions is not None:
            for token in args:
                if token.startswith("-") and token not in actions:
                    return GrammarVerdict(
                        False, f"{tool} {sub} 的动作 {token} 不在正表"
                    )
    if tool == "jstat" and len(positionals) == 2:
        # `jstat -gcutil PID 1000` 带采样间隔但没有次数,会一直采到超时为止;
        # 给了次数才是有界的一次性取样
        return GrammarVerdict(
            False, "jstat 给了采样间隔就必须给次数,否则会持续采样"
        )
    if grammar.content_check:
        return _check_content(tool, grammar, args, positionals)
    return GrammarVerdict(True)


def _check_option(tool, grammar, token, args, index):
    """返回消耗的 token 数,或 GrammarVerdict(拒绝)。

    判定顺序要紧:必须先整体匹配,再试短选项簇,最后才试内联附值。反过来的话
    `-lh`、`-anp`、`-eo` 这些寻常簇写会被误当成 `-l`+值,而 `-maxdepth` 会被
    拆成 `-m`——正表看着严,实则把日常排查命令全拒了。
    """
    # 1. 整体命中带值选项:值在下一个 token
    if token in grammar.options_with_value:
        if index + 1 >= len(args):
            return GrammarVerdict(False, f"{tool} 的 {token} 缺少取值")
        return 2
    # 2. 整体命中开关选项
    if token in grammar.options:
        return 1
    # 2.5 `head -15`:数字是行数,只有登记过 numeric_option 的工具认
    if grammar.numeric_option and _NUMERIC.match(token):
        return 1
    # 3. 长选项内联 --opt=value
    if token.startswith("--") and "=" in token:
        name = token.split("=", 1)[0]
        if name in grammar.options_with_value:
            return 1
        if name in grammar.options:
            return GrammarVerdict(False, f"{tool} 的 {name} 不接受取值")
        return GrammarVerdict(False, f"{tool} 的选项 {name} 不在正表")
    # 4. 逐字母消费短选项串:开关继续往下,遇到带值选项就把剩余部分当它的值。
    #    一条规则同时覆盖纯簇写(-lh / -anp)、簇尾带值(-eo VALUE)、内联附值
    #    (-n50 / -t$'\t')与两者的混合(paste -sd, = -s 加 -d 内联逗号)。
    #    分成「先试纯簇再试内联」两步的话,-sd, 会因为逗号不是字母而漏掉簇那
    #    一步,再按 -s 内联附值判定,最终误拒。
    #    `sort -o/tmp/pwn` 仍在此被认出:-o 不在 sort 正表,扫到即拒。
    if len(token) > 2 and not token.startswith("--"):
        body = token[1:]
        for offset, char in enumerate(body):
            letter = f"-{char}"
            if letter in grammar.options:
                continue
            if letter in grammar.options_with_value:
                if body[offset + 1:]:
                    return 1
                if index + 1 >= len(args):
                    return GrammarVerdict(False, f"{tool} 的 {letter} 缺少取值")
                return 2
            return GrammarVerdict(False, f"{tool} 的选项 {letter} 不在正表")
        return 1
    return GrammarVerdict(False, f"{tool} 的选项 {token} 不在正表")


def _option_values(grammar, args, names) -> list[str]:
    """按 _check_option 的同一消费规则取出指定带值选项的全部取值。

    内容校验必须看到每一个取值:只认独立的 `-e` 会让簇写 `-NBe SQL`、内联
    `-eSQL` 里的语句逃过只读检查(`mysql -e 'select 1' -Be 'drop table t'`)。
    调用前选项已逐个过 _check_option,这里只做同口径的取值,不再判拒。
    """
    values: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            break
        if not token.startswith("-") or token == "-":
            index += 1
            continue
        if token in grammar.options_with_value:
            if token in names and index + 1 < len(args):
                values.append(args[index + 1])
            index += 2
            continue
        if token in grammar.options or token.startswith("--"):
            if token.startswith("--") and "=" in token:
                name, value = token.split("=", 1)
                if name in names:
                    values.append(value)
            index += 1
            continue
        consumed = 1
        body = token[1:]
        for offset, char in enumerate(body):
            letter = f"-{char}"
            if letter not in grammar.options_with_value:
                continue
            value = body[offset + 1:]
            if not value:
                value = args[index + 1] if index + 1 < len(args) else ""
                consumed = 2
            if letter in names:
                values.append(value)
            break
        index += consumed
    return values


def _check_sed_script(script: str) -> GrammarVerdict:
    """按 sed 脚本文法逐条剥地址、看命令字符,而不是拿正则在整串里碰运气。

    正则判「命令位置」不可靠:`1,3w /tmp/out` 的 w 带逗号地址、`s/a/b/w f` 的 w
    是替换标志,两者都能写文件却长得完全不同。这里逐段解析:剥掉地址前缀后的
    第一个字符就是命令,s/y 再单独看它的标志段。解析不动的形态一律拒(fail-closed)。
    """
    for raw in re.split(r"[;\n]", script):
        segment = raw.strip()
        while segment[:1] in {"{", "}", "!"}:
            segment = segment[1:].strip()
        if not segment:
            continue
        stripped = _SED_ADDRESS.sub("", segment, count=1).strip()
        if not stripped:
            continue
        command = stripped[0]
        if command in _SED_DANGEROUS_CMDS:
            return GrammarVerdict(
                False, f"sed 脚本含 {command} 命令(执行或读写文件)"
            )
        if command in {"s", "y"} and len(stripped) > 1:
            flags = _sed_flags(stripped[1:], stripped[1])
            if flags is None:
                return GrammarVerdict(False, "sed 替换式无法解析,按不安全处理")
            if any(ch in flags for ch in "ewW"):
                return GrammarVerdict(
                    False, "sed 替换式带 e/w 标志(执行或写文件)"
                )
    return GrammarVerdict(True)


def _sed_flags(body: str, delimiter: str):
    """取 `s/a/b/FLAGS` 的 FLAGS 段:第三个未转义分隔符之后的内容。

    body 以分隔符起头(`/a/b/g`)。分隔符不足三个说明式子不完整,返回 None 让
    调用方按不安全处理——解析不动的形态不该放行。
    """
    count, escaped = 0, False
    for position, ch in enumerate(body):
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == delimiter:
            count += 1
            if count == 3:
                return body[position + 1:]
    return None


def _check_content(tool, grammar, args, positionals) -> GrammarVerdict:
    if tool == "jq":
        program = positionals[0] if positionals else ""
        if _JQ_FORBIDDEN.search(program):
            return GrammarVerdict(
                False, "jq 程序含环境变量或额外输入构造($ENV、env、input)"
            )
        return GrammarVerdict(True)
    if tool == "awk":
        program = positionals[0] if positionals else ""
        if _AWK_FORBIDDEN.search(program):
            return GrammarVerdict(
                False, "awk 程序含执行/重定向构造(system、getline <、print > 等)"
            )
        return GrammarVerdict(True)
    if tool == "sed":
        scripts = list(positionals[:1])
        for i, token in enumerate(args):
            if token in {"-e", "--expression"} and i + 1 < len(args):
                scripts.append(args[i + 1])
            elif token.startswith("--expression="):
                scripts.append(token.split("=", 1)[1])
        for script in scripts:
            verdict = _check_sed_script(script)
            if not verdict.ok:
                return verdict
        return GrammarVerdict(True)
    if tool == "mysql":
        statements = _option_values(grammar, args, {"-e", "--execute"})
        if not statements:
            return GrammarVerdict(False, "mysql 必须用 -e 给出只读语句")
        for statement in statements:
            verdict = check_readonly_sql(statement)
            if not verdict.ok:
                return verdict
        return GrammarVerdict(True)
    return GrammarVerdict(True)


# 反斜杠转义随 sql_mode(NO_BACKSLASH_ESCAPES)变,注释里的引号不成对:出现时引号边界不可靠
_SQL_QUOTE_AMBIGUOUS = re.compile(r"\\|--|#|/\*")


def sql_statements(statement: str) -> List[str]:
    """按 ; 切分语句;引号内的 ; 不切。

    仅当语句里没有反斜杠与注释时才按引号切——两者会让引号边界随服务端设置变化,此时退回
    逐个 ; 切分(宁可把字符串里的 ; 误判成分隔符,也不能把真分隔符藏进「引号」)。
    """
    if _SQL_QUOTE_AMBIGUOUS.search(statement):
        return statement.split(";")
    parts: List[str] = []
    current: List[str] = []
    quote: Optional[str] = None
    for char in statement:
        if quote is not None:
            if char == quote:
                quote = None
        elif char in "'\"`":
            quote = char
        elif char == ";":
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return parts


def check_readonly_sql(statement: str) -> GrammarVerdict:
    """逐条(按 ; 切分,引号内不切)校验 SQL 只读;节点侧 mysql -e 与中台 MySQL 直连模板共用。"""
    for part in sql_statements(statement):
        if not part.strip():
            continue
        if not _SQL_READONLY.match(part):
            return GrammarVerdict(False, f"mysql 语句非只读: {part.strip()[:40]}")
        if _SQL_FORBIDDEN.search(part):
            return GrammarVerdict(False, "mysql 语句含文件读写或延时函数")
    return GrammarVerdict(True)
