"""命令闸:裸 shell 的统一入口(A2 §3.1),所有档位一致、不可关。

合并 cmd_filter 红线与 A1 原型的裸 shell 加严。判定顺序:

    1 红线       FORBIDDEN 拒;WRITE 转人工审批
    2 结构       写文件重定向、heredoc、进程替换、后台、命令替换、算术求值等拒
    3 逐段       禁用词 / 交互式 / 选项级 / 子命令白名单 / SQL 只读 / 重查询
    4 凭据路径   转人工审批(三档都审,bypass 也不例外)
    5 未登记     不在只读命令目录也不在禁用词里 → 交给三档命令策略

多条结论取最严:deny > approval > unregistered > readonly。命令闸只判命令,
不碰执行;SSHPool.run() 上的红线照旧是最后一道闸。
"""
from __future__ import annotations

import posixpath
import re
import shlex
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Tuple

from lib.cmd_filter import CmdFilter, Level
from lib.shell_lex import scan
from lib.triage_agent.command_policy import PolicyVerdict
from lib.triage_agent.command_grammar import (
    TOOLS,
    _AWK_FORBIDDEN,
    _JQ_FORBIDDEN,
    _check_sed_script,
    check_readonly_sql,
    sql_statements,
)
from lib.triage_agent.path_policy import DEFAULT_DENY_PATTERNS

READONLY = "readonly"
UNREGISTERED = "unregistered"
APPROVAL = "approval"
DENY = "deny"
_RANK = {READONLY: 0, UNREGISTERED: 1, APPROVAL: 2, DENY: 3}

OUTPUT_CAP_BYTES = 1024 * 1024
COMMAND_TIMEOUT_SECONDS = 120
# 管道首段整文件读取超过此大小即拒(执行前由 shell 工具查大小,见 full_scan_reads)
HEAVY_FILE_BYTES = 1 << 30
HEAVY_FILE_HINT = "先 ls -lh 看大小,再用 tail -c / tail -n 或按日期行定位截取后过滤"

# 凭据/密钥:自由命令禁读清单 + 数据库客户端凭据文件(绝对路径锚定)
CREDENTIAL_PATTERNS = tuple(re.compile(p) for p in (
    *DEFAULT_DENY_PATTERNS,
    r"\.cnf$", r"(^|/)\.pgpass$", r"(^|/)\.netrc$",
))
# 按文件名认的凭据:路径前缀是变量、相对路径或备份副本时绝对锚定的规则认不出
CREDENTIAL_NAME_PATTERNS = tuple(re.compile(p) for p in (
    r"(^|/)g?shadow-?$", r"(^|/)sudoers(\.d)?(/|$)", r"(^|/)\.ssh(/|$)",
    r"(^|/)environ$", r"^/proc/[^/]+/mem$",
))
# 通配符路径与这些代表路径比对,能匹配上任何一个即按凭据处理
CREDENTIAL_EXEMPLARS = (
    "/etc/shadow", "/etc/shadow-", "/etc/gshadow", "/etc/sudoers", "/etc/sudoers.d/x",
    "/etc/ssh/ssh_host_rsa_key", "/etc/my.cnf", "/etc/security/keytabs/x.keytab",
    "/root/.ssh/id_rsa", "/root/.pgpass", "/root/.netrc", "/home/x/.ssh/id_rsa",
    "/proc/1/environ", "/proc/self/environ", "/proc/1/mem", "/proc/kcore",
)
# 禁用词:提权、横移、解释器与 shell、间接执行、文件改动、下载、编辑器与分页器、
# 常驻/后台、可写的数据客户端。任何档位都不经 shell 执行
DENY_WORDS = {
    "sudo": "提权", "su": "提权", "pkexec": "提权", "doas": "提权",
    **{w: "横向连接" for w in ("ssh", "scp", "sftp", "rsync", "nc", "ncat", "netcat",
                             "socat", "telnet", "ftp", "nsenter", "chroot")},
    **{w: "解释器/shell" for w in ("python", "python2", "python3", "perl", "ruby", "node",
                                  "nodejs", "php", "lua", "java", "bash", "sh", "zsh",
                                  "ksh", "dash", "csh", "tcsh", "expect", "script")},
    **{w: "间接执行" for w in ("eval", "exec", "source", ".", "xargs", "env", "builtin",
                             "enable", "trap", "coproc", "fc", "alias", "unalias",
                             "mapfile", "readarray", "let", "declare", "typeset")},
    **{w: "改动文件" for w in ("cp", "install", "ln", "mkdir", "rmdir", "chmod", "chown",
                            "chgrp", "chattr", "dd", "truncate", "shred", "patch",
                            "split", "csplit", "tar", "unzip", "zip", "cpio", "gzip",
                            "gunzip", "bzip2", "xz", "unxz")},
    "wget": "下载写文件", "nohup": "脱离会话", "setsid": "脱离会话", "disown": "脱离会话",
    "screen": "常驻会话", "tmux": "常驻会话", "at": "定时执行", "batch": "定时执行",
    "watch": "反复执行不退出",
    **{w: "编辑器/分页器" for w in ("vi", "vim", "nano", "emacs", "less", "more", "man")},
    **{w: "可写的数据客户端" for w in (
        "mysqldump", "beeline", "hive", "impala-shell", "spark-sql", "spark-shell",
        "spark-submit", "pyspark", "presto", "trino", "hbase", "sqlplus", "mongo",
        "redis-cli", "zkCli.sh", "zookeeper-client", "kafka-console-producer.sh",
        "kafka-topics.sh", "kafka-configs.sh", "kafka-consumer-groups.sh",
        "kafka-reassign-partitions.sh", "kafka-acls.sh", "sqoop")},
    **{w: "附着/改动进程" for w in ("gdb", "gcore", "strace", "ltrace", "jhsdb")},
    # MySQL 的凭证、连接与查询一律经 MySQL 插件(插件页按连接配置),节点上的客户端不再使用
    **{w: "MySQL 一律经 MySQL 插件工具查询" for w in ("mysql", "mariadb", "mysqladmin")},
}

# 只读命令目录:结构化正表 + 排查常用的只读命令(写/交互式选项由逐段检查兜住)
CATALOG = frozenset(TOOLS) | frozenset({
    "tac", "bzcat", "xzcat", "column", "join", "comm", "diff", "cmp", "fold", "expand",
    "strings", "od", "xxd", "hexdump", "base64", "md5sum", "sha1sum", "sha256sum",
    "cksum", "rev", "seq", "printf", "echo", "gawk", "mawk", "realpath", "tree",
    "findmnt", "blkid", "top", "iostat", "pidstat", "sar", "nproc", "lsmem", "pidof",
    "getconf", "sysctl", "w", "who", "groups", "users", "timedatectl", "ip",
    "ifconfig", "route", "arp", "nslookup", "dig", "host", "ping", "tracepath",
    "traceroute", "curl", "jcmd", "jinfo", "jmap", "klist", "psql", "crontab", "service", "chkconfig", "ulimit", "cd", "pwd", "true",
    "false", "test", "[", ":", "sleep", "read", "break", "continue", "exit",
    "return", "type", "which", "whereis", "wait", "unset", "mount", "dmidecode", "ipcs",
    "numactl", "nginx",
})

_WRAPPERS = {"timeout", "nice", "ionice", "stdbuf", "command", "time"}
_KEYWORDS = {"!", "{", "}", "(", ")", "do", "then", "else", "elif", "if", "while", "until"}
_CLOSERS = {"done", "fi", "esac", "}", ")"}
_ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(\[[^\]]*\])?\+?=")
# 进程会读的环境变量能改变它执行什么(PATH、LD_*、JAVA_TOOL_OPTIONS…);只放行
# 小写临时变量与少数无害的区域设置
_SAFE_ASSIGN = re.compile(r"^(?:[a-z_][a-z0-9_]*|LANG|LANGUAGE|LC_[A-Z]+|TZ|COLUMNS|LINES|TERM)$")

_HDFS_READ = {"-ls", "-lsr", "-cat", "-du", "-dus", "-count", "-stat", "-tail", "-text",
              "-test", "-df", "-checksum", "-find", "-head", "-getfacl", "-getfattr"}
_YARN_READ = {"application", "app", "applicationattempt", "container", "node", "queue",
              "logs", "version", "classpath"}
_YARN_WRITE = {"-kill", "-movetoqueue", "-changeQueue", "-updatePriority",
               "-updateLifetime", "-signal", "-fail", "-out"}
_JCMD_READ = {"VM.flags", "VM.version", "VM.uptime", "VM.system_properties",
              "VM.command_line", "GC.heap_info", "Thread.print", "VM.native_memory",
              "GC.class_histogram", "help", "-l"}
_IP_WRITE = {"add", "del", "delete", "set", "flush", "change", "replace", "append",
             "prepend", "restore", "exec", "save", "monitor"}
_CURL_WRITE = {"-o", "-O", "--output", "--remote-name", "--remote-name-all", "-T",
               "--upload-file", "-F", "--form", "-K", "--config", "-c", "--cookie-jar",
               "--create-dirs", "--trace", "--trace-ascii", "--libcurl", "--stderr",
               "--output-dir", "-D", "--dump-header"}
_SQL_CLIENT_DENY = ("--pager", "--tee", "--init-command", "--plugin-dir", "--default-auth",
                    "--local-infile", "--load-data-local-dir", "--histfile")
_SQL_META = re.compile(r"\\(?!G\s*$)[A-Za-z!|.]")
_USE_STATEMENT = re.compile(r"^\s*use\s+`?\w+`?\s*$", re.I)

_AWK_STRING = re.compile(r'"(?:[^"\\]|\\.)*"')
_SUBSTITUTION_HINT = "命令替换请分两步执行:先查出值,再把值写进下一条命令"


@dataclass(frozen=True)
class GuardVerdict:
    level: str
    category: str = "ok"
    reason: str = ""
    hint: str = ""
    # 未登记的段签名:三档策略与永久名单按段匹配
    unregistered: Tuple[str, ...] = ()
    # 全部段签名(永久 deny 名单对任何段生效)
    signatures: Tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.level == READONLY


@dataclass
class _Acc:
    worst: GuardVerdict = field(default_factory=lambda: GuardVerdict(READONLY))
    unregistered: List[str] = field(default_factory=list)
    signatures: List[str] = field(default_factory=list)

    def add(self, verdict: Optional[GuardVerdict]) -> None:
        if verdict is not None and _RANK[verdict.level] > _RANK[self.worst.level]:
            self.worst = verdict


def _deny(category: str, reason: str, hint: str = "") -> GuardVerdict:
    return GuardVerdict(DENY, category, reason, hint)


class CommandGuard:
    def __init__(self, cmd_filter: Optional[CmdFilter] = None):
        self._filter = cmd_filter or CmdFilter.default()

    def check(self, command: str) -> GuardVerdict:
        command = command.strip()
        if not command:
            return _deny("unsupported", "空命令")
        acc = _Acc()
        redline = self._filter.classify(command)
        if redline.level is Level.FORBIDDEN:
            return _deny("forbidden", f"红线绝对禁止({redline.reason})")
        if redline.level is Level.WRITE:
            acc.add(GuardVerdict(APPROVAL, "write", f"写操作须人工审批({redline.reason})"))
        lexed = scan(command)
        if lexed is None:
            return _deny("unsupported", "命令无法解析(引号不配对?)")
        if lexed.ambiguous:
            return _deny("unsupported", f"不支持的 shell 构造:{lexed.ambiguous}")
        if lexed.live_substitution:
            return _deny("substitution", "命令替换 $(…) / 反引号不允许", _SUBSTITUTION_HINT)
        found = _structure_checks(lexed.structure)
        if found:
            return found
        cwd: Optional[str] = None
        for segment in lexed.segments:
            verdict, cwd = _segment_check(segment, cwd, acc)
            if verdict is not None and verdict.level == DENY:
                return verdict
            acc.add(verdict)
        worst = acc.worst
        if worst.level == READONLY and acc.unregistered:
            words = ", ".join(sorted({s.split()[0] for s in acc.unregistered}))
            worst = GuardVerdict(UNREGISTERED, "unregistered", f"未登记的命令: {words}")
        return GuardVerdict(
            worst.level, worst.category, worst.reason, worst.hint,
            tuple(dict.fromkeys(acc.unregistered)), tuple(dict.fromkeys(acc.signatures)),
        )


_REDIRECT = re.compile(r"(\d*)(&>>|&>|>>|>\||>&|>)\s*(\S*)")


def _structure_checks(structure: str) -> Optional[GuardVerdict]:
    if re.search(r"[<>]\(", structure):
        return _deny("structure", "进程替换不允许")
    if "<>" in structure:
        return _deny("structure", "读写方式打开文件不允许")
    for match in _REDIRECT.finditer(structure):
        operator, target = match.group(2), match.group(3)
        if target in ("/dev/null", "/dev/stdout", "/dev/stderr"):
            continue
        if operator == ">&" and re.fullmatch(r"\d+|-", target):
            continue
        return _deny("structure", f"输出重定向写文件({match.group(0).strip()})",
                     "结果直接输出即可,Harness 会保存完整输出")
    # 单个 & 是后台;&& 与 >& / &> / |& / <& 不是
    if re.search(r"(?<![&>|<])&(?![&>])", structure):
        return _deny("structure", "后台进程不允许")
    if re.search(r"(?<!\$)\(\(|\[\[", structure) or re.search(r"\$\{[!#]?[^}]*@[A-Za-z]", structure) \
            or "${!" in structure:
        return _deny("structure", "算术求值 (( )) / [[ ]] / 间接展开不允许",
                     "条件判断改用 [ ] / test,计数改用 awk")
    if re.search(r"\w\s*\(\s*\)", structure):
        return _deny("structure", "函数定义不允许")
    return None


def _segment_check(segment: str, cwd: Optional[str], acc: _Acc):
    try:
        tokens = shlex.split(segment, posix=True)
    except ValueError:
        return _deny("unsupported", "命令无法解析"), cwd
    while tokens and tokens[0] in _KEYWORDS:
        tokens = tokens[1:]
    while tokens and tokens[0].startswith("(") and tokens[0] != "(":
        tokens[0] = tokens[0].lstrip("(")
    if not tokens or tokens[0] in _CLOSERS:
        return None, cwd
    if tokens[0] in ("for", "select", "case"):
        if tokens[0] != "for":
            return _deny("structure", f"{tokens[0]} 语句不支持"), cwd
        # for 头只有数据;循环体按段单独检查
        return _credential_check("for", tokens[1:], cwd), cwd
    rest = list(tokens)
    while rest and _ASSIGN.match(rest[0]):
        name = _ASSIGN.match(rest[0]).group(1)
        if not _SAFE_ASSIGN.match(name):
            return _deny("structure", f"不允许设置环境变量 {name}",
                         "临时变量请用小写名"), cwd
        found = _credential_check("=", [rest[0].split("=", 1)[1]], cwd)
        if found:
            return found, cwd
        rest = rest[1:]
    if not rest:
        return None, cwd
    rest, lookup = _strip_wrappers(rest)
    if not rest:
        return None, cwd
    word = rest[0].rsplit("/", 1)[-1]
    # 重定向已在结构检查里判过;输入重定向的文件留给凭据检查
    args, inputs = _split_redirects(rest[1:])
    acc.signatures.append(_signature(word, args))
    if lookup:
        return None, cwd
    if word in DENY_WORDS:
        return _deny("denied_word", f"{word} 不经 shell 执行({DENY_WORDS[word]})"), cwd
    found = _option_checks(word, args, segment)
    if found:
        return found, cwd
    found = _heavy_checks(word, args)
    if found:
        return found, cwd
    found = _credential_check(word, args + inputs, cwd)
    if word == "cd":
        target = next((a for a in args if not a.startswith("-")), "")
        cwd = posixpath.normpath(posixpath.join(cwd or "/", target)) if target.startswith("/") or cwd else None
    if found:
        return found, cwd
    if word not in CATALOG:
        acc.unregistered.append(_signature(word, args))
    return None, cwd


_REDIRECT_TOKEN = re.compile(r"^(\d*|&)(<<<|>>?|>&|>\||<&?)(.*)$")


def _split_redirects(tokens: List[str]) -> Tuple[List[str], List[str]]:
    args: List[str] = []
    inputs: List[str] = []
    index = 0
    while index < len(tokens):
        match = _REDIRECT_TOKEN.match(tokens[index])
        if not match:
            args.append(tokens[index])
            index += 1
            continue
        target = match.group(3)
        if not target and index + 1 < len(tokens):
            index += 1
            target = tokens[index]
        if match.group(2) == "<":
            inputs.append(target)
        index += 1
    return args, inputs


def _strip_wrappers(tokens: List[str]) -> Tuple[List[str], bool]:
    """剥掉 timeout/nice/command 等前缀包装;`command -v x` 只是查找,返回 lookup=True。"""
    rest = list(tokens)
    while rest and rest[0] in _WRAPPERS:
        wrapper = rest[0]
        rest = rest[1:]
        while rest and (rest[0].startswith("-") or re.fullmatch(r"[\d.]+[smhd]?", rest[0])):
            if wrapper == "command" and rest[0] in ("-v", "-V"):
                return rest, True
            if wrapper == "ionice" and rest[0] == "-p":
                return ["ionice", "-p"], False
            if wrapper in ("nice", "ionice") and rest[0] in ("-n", "-c") and len(rest) > 1:
                rest = rest[1:]
            rest = rest[1:]
    return rest, False


def _signature(word: str, args: Sequence[str]) -> str:
    options = sorted({re.sub(r"^-\d+$", "-N", a.split("=", 1)[0]) for a in args if a.startswith("-")})
    return " ".join([word, *options])


def _option_value(args: Sequence[str], names: Iterable[str]) -> List[str]:
    """取选项值;短选项允许簇写在末尾(-Ne SQL / -tAc SQL)。"""
    names = tuple(names)
    values: List[str] = []
    for index, token in enumerate(args):
        for name in names:
            if name.startswith("--"):
                if token == name and index + 1 < len(args):
                    values.append(args[index + 1])
                elif token.startswith(name + "="):
                    values.append(token.split("=", 1)[1])
            elif re.fullmatch(rf"-[A-Za-z]*{name[1:]}", token) and index + 1 < len(args):
                values.append(args[index + 1])
            elif re.fullmatch(rf"-[A-Za-z]*{name[1:]}.+", token) and not token.startswith("--"):
                values.append(token[token.index(name[1:]) + 1:])
    return values


def _curl_readonly_forms(args: List[str]) -> Tuple[List[str], Optional[GuardVerdict]]:
    """剥掉 curl 两种只读等价写法,其余参数照常按写操作检查:
    -o/--output /dev/null(只丢弃响应体);-G/--get 下的 -d/--data*(拼进 GET 查询串)。
    -G 下数据不得读本地文件(@文件 / 名字@文件),否则等于把节点文件发往 URL。"""
    get = any(a in ("-G", "--get") or re.fullmatch(r"-[A-Za-z]*G[A-Za-z]*", a) for a in args)
    kept: List[str] = []
    index = 0
    while index < len(args):
        arg, value = args[index], args[index + 1] if index + 1 < len(args) else None
        short = re.fullmatch(r"-([A-Za-z]*)o", arg)
        if value == "/dev/null" and (arg == "--output" or (short and not re.search(r"[oOTFKcDd]", short.group(1)))):
            if short and short.group(1):
                kept.append("-" + short.group(1))
            index += 2
            continue
        if arg == "--output=/dev/null":
            index += 1
            continue
        if get:
            data = None
            if arg in ("-d", "--data", "--data-raw", "--data-ascii", "--data-binary", "--data-urlencode"):
                data, index = value, index + 2
            elif arg.startswith(("--data=", "--data-raw=", "--data-ascii=", "--data-binary=", "--data-urlencode=")):
                data, index = arg.split("=", 1)[1], index + 1
            elif re.fullmatch(r"-d.+", arg):
                data, index = arg[2:], index + 1
            if data is not None:
                if "@" in data.split("=", 1)[0]:
                    return kept, _deny("option", "curl -G 的数据不允许 @文件(会把节点文件发往 URL)")
                continue
        kept.append(arg)
        index += 1
    return kept, None


def _option_checks(word: str, args: List[str], segment: str) -> Optional[GuardVerdict]:
    if word == "crontab":
        rest = list(args)
        if "-u" in rest[:-1]:  # -u <用户> 可在 -l 前后
            index = rest.index("-u")
            del rest[index:index + 2]
        if rest != ["-l"]:
            return _deny("option", "crontab 只允许 -l(可带 -u <用户>)")
    if word == "tail" and any(a in ("-f", "-F") or a.startswith("--follow") or
                              (re.fullmatch(r"-[A-Za-z0-9]*[fF][A-Za-z0-9]*", a) and not a[1:].isdigit())
                              for a in args):
        return _deny("interactive", "tail -f 会挂住", "改用 tail -n <行数>")
    if word == "top" and not any(re.fullmatch(r"-[A-Za-z0-9]*b[A-Za-z0-9]*", a) for a in args):
        return _deny("interactive", "top 须用批处理", "改用 top -b -n 1")
    if word == "journalctl" and any(a in ("-f", "--follow", "--rotate", "--flush", "--sync")
                                    or a.startswith(("--vacuum", "--setup-keys", "--update-catalog",
                                                     "--relinquish-var")) for a in args):
        return _deny("option", "journalctl 只允许查询")
    if word == "dmesg" and any(a in ("-c", "-C", "--clear", "--read-clear", "-D", "-E", "-n",
                                     "--console-off", "--console-on") or a.startswith("--console-level")
                               for a in args):
        return _deny("option", "dmesg 只允许查询,不允许清空或改控制台级别")
    if word == "find" and any(a in ("-exec", "-execdir", "-ok", "-okdir", "-delete", "-fls")
                              or a.startswith("-fprint") for a in args):
        return _deny("option", "find 不允许 -exec/-delete/-fprint", "先 find 列出,再对结果单独查看")
    if word in ("awk", "gawk", "mawk"):
        if any(a == "-f" or a.startswith(("-f", "--file")) and a != "-F" and not a.startswith("-F")
               for a in args):
            return _deny("option", "awk 不允许 -f 读程序文件")
        program = _first_positional(args, ("-F", "-v", "--field-separator", "--assign"))
        # 字符串字面量清空内容、保留引号:`print $NF" | "$0` 不误判,`print | "cmd"` 照拦
        if program is not None and _AWK_FORBIDDEN.search(_AWK_STRING.sub('""', program)):
            return _deny("option", "awk 程序不允许 system/getline/写文件/管道/ENVIRON")
    if word == "sed":
        if any(a.startswith(("-i", "--in-place", "-f", "--file")) for a in args):
            return _deny("option", "sed 不允许 -i 原地改写或 -f 读脚本文件")
        scripts = _option_value(args, ("-e", "--expression"))
        if not scripts:
            program = _first_positional(args, ())
            scripts = [program] if program is not None else []
        for script in scripts:
            if not _check_sed_script(script).ok:
                return _deny("option", "sed 脚本不允许 e/w/r 命令")
    if word == "jq":
        if any(a.startswith(("--slurpfile", "--rawfile", "--argfile", "-f", "--from-file")) for a in args):
            return _deny("option", "jq 不允许读额外文件")
        program = _first_positional(args, ("--arg", "--argjson", "--indent"))
        if program is not None and _JQ_FORBIDDEN.search(program):
            return _deny("option", "jq 程序不允许读环境变量或额外输入")
    if word == "curl":
        args, found = _curl_readonly_forms(args)
        if found is not None:
            return found
        if any(a in _CURL_WRITE or a.startswith(("--data", "--output=", "--form=", "--json",
                                                 "--upload", "--config=")) or
               re.fullmatch(r"-[A-Za-z]*[oOTFKcD][A-Za-z]*", a) or re.fullmatch(r"-d.*", a)
               for a in args):
            return _deny("option", "curl 只允许读取,不允许写文件/上传/提交数据")
        for method in _option_value(args, ("-X", "--request")):
            if method.upper() not in ("GET", "HEAD"):
                return _deny("option", f"curl 方法 {method} 不是只读")
        for arg in args:
            if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", arg) and not re.match(r"^https?://", arg, re.I):
                return _deny("option", f"curl 只允许 http/https({arg.split(':', 1)[0]}:// 不允许)")
    if word == "psql":
        if any(a.startswith(_SQL_CLIENT_DENY) for a in args) or any(
                a in ("-f", "-o", "-L") or a.startswith(("--file", "--output", "--log-file")) for a in args):
            return _deny("option", f"{word} 不允许写文件、外部命令或插件类选项")
        names = ("-c", "--command")
        statements = _option_value(args, names)
        if not statements:
            return _deny("option", f"{word} 须用 {names[0]} 给出只读 SQL")
        for sql in statements:
            if _SQL_META.search(sql):
                return _deny("sql", "SQL 里不允许客户端元命令(\\! / \\g 等)")
            parts = [p for p in sql_statements(sql) if p.strip() and not _USE_STATEMENT.match(p)]
            verdict = check_readonly_sql(";".join(parts))
            if not verdict.ok:
                return _deny("sql", verdict.reason)
    if word in ("hdfs", "hadoop"):
        sub = args[0] if args else ""
        if sub in ("dfs", "fs"):
            action = args[1] if len(args) > 1 else ""
            if action not in _HDFS_READ:
                return _deny("option", f"{word} {sub} {action} 不在只读子命令内")
        elif sub == "dfsadmin":
            if args[1:2] != ["-report"] and args[1:3] != ["-safemode", "get"]:
                return _deny("option", "dfsadmin 只允许 -report / -safemode get")
        elif sub == "haadmin":
            if not any(a in ("-getServiceState", "-getAllServiceState") for a in args):
                return _deny("option", "haadmin 只允许查询状态")
        elif sub not in ("fsck", "version", "classpath", "getconf", "jmxget"):
            return _deny("option", f"{word} {sub} 不在只读子命令内")
        if sub == "fsck" and any(a in ("-delete", "-move") for a in args):
            return _deny("option", "fsck 不允许 -delete/-move")
    if word == "yarn":
        sub = args[0] if args else ""
        if sub not in _YARN_READ:
            return _deny("option", f"yarn {sub} 不在只读子命令内")
        if any(a in _YARN_WRITE for a in args):
            return _deny("option", "yarn 不允许改变应用状态或写本地文件")
    if word == "jmap" and (not args or args[0] not in ("-heap", "-histo") or any(":live" in a for a in args)):
        return _deny("option", "jmap 只允许 -heap / -histo(不带 :live,避免触发 Full GC 或落 dump)")
    if word == "jcmd" and len(args) >= 2 and args[1] not in _JCMD_READ:
        return _deny("option", f"jcmd {args[1]} 不在只读命令内")
    if word == "jinfo" and any(a == "-flag" and len(args) > args.index(a) + 1 and
                               re.match(r"[+-]|.*=", args[args.index(a) + 1]) or a.startswith(("-flag+", "-flag-"))
                               for a in args):
        return _deny("option", "jinfo 不允许修改 flag")
    if word == "jstack" and "-F" in args:
        return _deny("option", "jstack 不允许 -F 强制附着")
    if word == "sysctl" and any(a in ("-w", "-p", "--load", "--system", "--write") or "=" in a for a in args):
        return _deny("option", "sysctl 只允许查询")
    if word == "date" and _date_sets_clock(args):
        return _deny("option", "date 不允许改系统时间")
    if word == "hostname" and any(not a.startswith("-") for a in args):
        return _deny("option", "hostname 不允许改主机名")
    if word == "timedatectl" and args and args[0] not in ("status", "show", "list-timezones"):
        return _deny("option", "timedatectl 只允许查询")
    if word == "ip" and any(a in _IP_WRITE for a in args):
        return _deny("option", "ip 只允许查询(show/list)")
    if word in ("ifconfig", "route", "arp") and any(
            a in ("up", "down", "add", "del", "delete", "-d", "-s", "flush") for a in args):
        return _deny("option", f"{word} 只允许查询")
    if word == "mount" and args:
        return _deny("option", "mount 只允许无参数列出", "改用 findmnt")
    if word in ("chkconfig",) and args != ["--list"] and not (len(args) == 2 and args[0] == "--list"):
        return _deny("option", "chkconfig 只允许 --list")
    if word == "service" and (len(args) != 2 or args[1] != "status") and args != ["--status-all"]:
        return _deny("option", "service 只允许 <服务> status")
    if word == "numactl" and not args or word == "numactl" and any(
            a not in ("-H", "--hardware", "-s", "--show") for a in args):
        return _deny("option", "numactl 只允许 -H / -s 查询(带命令会执行该命令)")
    if word == "nginx" and (not any(a in ("-t", "-T", "-v", "-V") for a in args) or
                            any(a.startswith("-s") for a in args)):
        return _deny("option", "nginx 只允许 -t / -T / -v / -V 查询(不带参数会启动 nginx)")
    if word == "dmidecode" and any(a.startswith("--dump-bin") for a in args):
        return _deny("option", "dmidecode 不允许 --dump-bin 写文件")
    if word == "ping" and not any(re.fullmatch(r"-[A-Za-z]*c\d*", a) for a in args):
        return _deny("option", "ping 须用 -c 限定次数", "隧道不转发 ICMP,探活优先用端口")
    return None


def _date_sets_clock(args: Sequence[str]) -> bool:
    """-s/--set 或裸的 MMDDhhmm 位置参数会改系统时间;-d/-r/-f 的取值只是被换算的时间。"""
    index = 0
    while index < len(args):
        token = args[index]
        if token in ("-s", "--set") or token.startswith(("--set=", "-s")):
            return True
        if token in ("-d", "--date", "-r", "--reference", "-f", "--file"):
            index += 2
            continue
        if not token.startswith(("-", "+")) and re.match(r"\d", token):
            return True
        index += 1
    return False


def _first_positional(args: Sequence[str], with_value: Iterable[str]) -> Optional[str]:
    with_value = tuple(with_value)
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            return args[index + 1] if index + 1 < len(args) else None
        if token in with_value:
            index += 2
            continue
        if token.startswith("-") and token != "-":
            index += 1
            continue
        return token
    return None


def _heavy_checks(word: str, args: List[str]) -> Optional[GuardVerdict]:
    roots = [a for a in args if a in ("/", "/*")]
    if not roots:
        return None
    if word in ("hdfs", "hadoop") and ("-lsr" in args or ("-ls" in args and "-R" in args)):
        return _deny("heavy", "HDFS 根目录递归列举是全树扫描", "按具体目录逐层 -ls")
    if word == "du" or (word == "find" and "-maxdepth" not in args) or \
            (word in ("grep", "egrep", "zgrep") and any(re.fullmatch(r"-[A-Za-z]*[rR][A-Za-z]*", a) for a in args)) or \
            (word == "ls" and any(re.fullmatch(r"-[A-Za-z]*R[A-Za-z]*", a) for a in args)):
        return _deny("heavy", f"{word} 从根目录全树扫描", "限定到具体目录,或 find 加 -maxdepth")
    return None


def _glob_regex(token: str) -> re.Pattern:
    out, index = [], 0
    while index < len(token):
        char = token[index]
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[":
            end = token.find("]", index + 2)
            if end == -1:
                out.append(re.escape(char))
            else:
                body = token[index + 1:end].replace("\\", "\\\\")
                out.append("[^" + body[1:] + "]" if body[:1] in "!^" and body else "[" + body + "]")
                index = end
        else:
            out.append(re.escape(char))
        index += 1
    return re.compile("".join(out) + r"\Z")


def _credential_check(word: str, args: Sequence[str], cwd: Optional[str]) -> Optional[GuardVerdict]:
    for arg in args:
        if re.match(r"^https?://", arg, re.I):
            continue  # URL 路径不是本机文件
        candidates = [arg]
        if "=" in arg:
            candidates.append(arg.split("=", 1)[1])
        for value in candidates:
            if _is_credential(value, cwd):
                return GuardVerdict(APPROVAL, "credential", f"{arg} 属凭据/密钥路径,读取须人工审批")
    return None


def _is_credential(value: str, cwd: Optional[str]) -> bool:
    paths = [value]
    if value.startswith("/") or value.startswith("~"):
        paths.append(posixpath.normpath(value))
    elif cwd and value and not value.startswith("-"):
        paths.append(posixpath.normpath(posixpath.join(cwd, value)))
    for path in paths:
        if any(p.search(path) for p in CREDENTIAL_PATTERNS):
            return True
        if "/" in path and any(p.search(path) for p in CREDENTIAL_NAME_PATTERNS):
            return True
        if any(ch in path for ch in "*?[") and path.startswith("/"):
            pattern = _glob_regex(path)
            if any(pattern.match(exemplar) for exemplar in CREDENTIAL_EXEMPLARS):
                return True
    return False


def decide(
    verdict: GuardVerdict,
    mode: str,
    *,
    allow: Iterable[str] = (),
    deny: Iterable[str] = (),
    task_defaults: Iterable[str] = (),
) -> PolicyVerdict:
    """命令闸结论 → 三档动作(A2 §3.6):「命令闸判不了的命令由谁担保」。

    名单按段签名匹配:永久 deny 对任何段生效;永久 allow 与本任务默认只能覆盖
    未登记的段,覆盖不了红线写操作与凭据路径(那两类每次都要人看)。
    """
    if verdict.level == DENY:
        return PolicyVerdict("deny", verdict.reason)
    denied = next((s for s in verdict.signatures if s in set(deny)), None)
    if denied is not None:
        return PolicyVerdict("deny", "命中永久黑名单", denied)
    if verdict.level == APPROVAL:
        return PolicyVerdict("ask", verdict.reason)
    if verdict.level == READONLY:
        return PolicyVerdict("allow", "命令闸判只读")
    approved = set(allow) | set(task_defaults)
    pending = [s for s in verdict.unregistered if s not in approved]
    if not pending:
        return PolicyVerdict("allow", "未登记的命令已在名单内", " ; ".join(verdict.unregistered))
    if mode == "bypass":
        return PolicyVerdict("allow", "bypass 模式:未登记命令直接执行")
    if mode == "auto":
        return PolicyVerdict("review", f"auto 档:{verdict.reason},交审核模型")
    return PolicyVerdict("ask", f"default 档:{verdict.reason},转人工审批")


# 整文件读取类命令;程序/模式参数不是文件
_FULL_SCAN_WORDS = frozenset({
    "cat", "tac", "nl", "grep", "egrep", "fgrep", "zcat", "zgrep", "bzcat", "xzcat", "awk",
    "gawk", "mawk", "sed", "sort", "uniq", "wc", "cut", "strings", "md5sum", "sha1sum",
    "sha256sum", "base64", "od", "xxd", "paste", "column", "jq",
})
_PROGRAM_FIRST = {"grep": ("-e", "-f", "--regexp", "--file"), "egrep": ("-e", "-f"),
                  "fgrep": ("-e", "-f"), "zgrep": ("-e", "-f"), "awk": (), "gawk": (),
                  "mawk": (), "sed": ("-e", "--expression"), "jq": ()}
_GREP_VALUES = ("-A", "-B", "-C", "-m", "-e", "-f", "--include", "--exclude", "--regexp", "--file")
# 各工具带值的选项(值不是文件);未列出的工具按无带值选项处理
_WITH_VALUE = {
    **{w: _GREP_VALUES for w in ("grep", "egrep", "fgrep", "zgrep")},
    **{w: ("-F", "-v", "-f") for w in ("awk", "gawk", "mawk")},
    "sed": ("-e", "-f", "--expression"), "sort": ("-k", "-t", "-S", "-T"),
    "cut": ("-d", "-f", "-c", "-b"), "uniq": ("-f", "-s", "-w"), "paste": ("-d",),
    "jq": ("--indent", "--arg", "--argjson"), "od": ("-A", "-t", "-N", "-j"), "xxd": ("-l", "-s", "-c"),
    "column": ("-s",), "base64": ("-w",),
}


def full_scan_reads(command: str) -> List[str]:
    """管道首段(不读上游管道)里会被整份读取的文件路径,供执行前查大小(§3.3)。

    head/tail 只读头尾不在此列;取不到路径(相对路径且未 cd、变量)的不返回。
    """
    lexed = scan(command.strip())
    if lexed is None:
        return []
    paths: List[str] = []
    cwd: Optional[str] = None
    for segment, piped in zip(lexed.segments, lexed.pipe_fed):
        try:
            tokens = shlex.split(segment, posix=True)
        except ValueError:
            continue
        while tokens and (tokens[0] in _KEYWORDS or _ASSIGN.match(tokens[0])):
            tokens = tokens[1:]
        tokens, _ = _strip_wrappers(tokens)
        if not tokens:
            continue
        word = tokens[0].rsplit("/", 1)[-1]
        args, inputs = _split_redirects(tokens[1:])
        if word == "cd":
            target = next((a for a in args if not a.startswith("-")), "")
            cwd = posixpath.normpath(posixpath.join(cwd or "/", target)) if target.startswith("/") or cwd else None
            continue
        if word not in _FULL_SCAN_WORDS:
            continue
        positionals = _positionals(args, _WITH_VALUE.get(word, ()))
        program_flags = _PROGRAM_FIRST.get(word)
        if program_flags is not None and not any(a.startswith(program_flags) for a in args if a.startswith("-")):
            positionals = positionals[1:]
        candidates = inputs + ([] if piped and not positionals else positionals)
        for value in candidates:
            if "$" in value or value == "-":
                continue
            if value.startswith("/"):
                paths.append(posixpath.normpath(value))
            elif cwd:
                paths.append(posixpath.normpath(posixpath.join(cwd, value)))
    return list(dict.fromkeys(paths))


def _positionals(args: Sequence[str], with_value: Sequence[str]) -> List[str]:
    found: List[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            found.extend(args[index + 1:])
            break
        if token in with_value:
            index += 3 if token in ("--arg", "--argjson") else 2
            continue
        if token.startswith("-") and token != "-":
            index += 1
            continue
        found.append(token)
        index += 1
    return found


_DEFAULT_GUARD: Optional[CommandGuard] = None


def check_command(command: str) -> GuardVerdict:
    global _DEFAULT_GUARD
    if _DEFAULT_GUARD is None:
        _DEFAULT_GUARD = CommandGuard()
    return _DEFAULT_GUARD.check(command)
