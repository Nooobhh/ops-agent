"""回放与评估实跑代人审批的只读边界(源自 A1 精简 Harness):只拦危险,不管排查流程。

裸 shell 串比结构化 argv 多出一批写/外连途径(重定向、heredoc、后台进程、解释器、
横向 ssh、多语句 SQL 等),cmd_filter 按 argv 语义设计、对这些判只读。这里在
cmd_filter 之上只做加严:任何一条命中即拒,永不放宽 cmd_filter 的结论。
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Iterable, List, Optional

from lib.cmd_filter import CmdFilter, Level
from lib.triage_agent.command_grammar import check_readonly_sql
from lib.triage_agent.path_policy import DEFAULT_DENY_PATTERNS

OUTPUT_CAP_BYTES = 1024 * 1024
COMMAND_TIMEOUT_SECONDS = 120

# 凭据/密钥:沿用自由命令的禁读清单,另加数据库客户端凭据文件
CREDENTIAL_PATTERNS = tuple(re.compile(p) for p in (
    *DEFAULT_DENY_PATTERNS,
    r"\.cnf$", r"(^|/)\.pgpass$", r"(^|/)\.netrc$",
))
# 只引用不读取:客户端自己读凭据,内容不进输出
CREDENTIAL_REFERENCE_OPTIONS = ("--defaults-extra-file=", "--defaults-file=")
CREDENTIAL_REFERENCE_COMMANDS = {"mysql", "mariadb", "mysqladmin"}

# 写文件、外连、提权、解释器、交互式/常驻;精简档同样一律不放
DENY_COMMANDS = {
    "sudo", "su", "ssh", "scp", "sftp", "rsync", "nc", "ncat", "netcat", "socat", "telnet", "ftp",
    "cp", "install", "ln", "mkdir", "rmdir", "chmod", "chown", "chgrp", "chattr", "dd",
    "truncate", "shred", "wget", "python", "python2", "python3", "perl", "ruby", "node", "php",
    "lua", "java", "bash", "sh", "zsh", "ksh", "dash", "eval", "exec", "source", ".", "xargs",
    "env", "nohup", "setsid", "disown", "screen", "tmux", "at", "batch", "watch", "vi", "vim",
    "nano", "less", "more", "mysqldump", "beeline", "hive", "impala-shell", "spark-sql",
    "spark-shell", "pyspark", "presto", "trino", "hbase", "sqlplus", "mongo", "redis-cli",
    "zkCli.sh", "kafka-console-producer.sh", "kafka-topics.sh", "kafka-configs.sh",
}
# 前缀包装:剥掉后检查真正执行的命令
_WRAPPERS = {"timeout", "nice", "ionice", "stdbuf", "command", "time"}
_HDFS_READ = {"-ls", "-lsr", "-cat", "-du", "-dus", "-count", "-stat", "-tail", "-text",
              "-test", "-df", "-checksum", "-find", "-head", "-getfacl", "-getfattr"}
_YARN_READ = {"application", "app", "applicationattempt", "container", "node", "queue", "logs",
              "version", "classpath"}
_JCMD_READ = {"VM.flags", "VM.version", "VM.uptime", "VM.system_properties", "VM.command_line",
              "GC.heap_info", "Thread.print", "VM.native_memory", "GC.class_histogram"}
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


@dataclass(frozen=True)
class ShellVerdict:
    allowed: bool
    category: str  # ok / forbidden / write / credential / unsupported
    reason: str = ""


def _deny(category: str, reason: str) -> ShellVerdict:
    return ShellVerdict(False, category, reason)


def _scan(command: str):
    """按引号与转义扫描:返回(去掉引号内容的结构文本, 按未引号操作符切开的段)。"""
    structure: List[str] = []
    segments: List[str] = []
    current: List[str] = []
    quote: Optional[str] = None
    index = 0
    while index < len(command):
        char = command[index]
        if quote:
            current.append(char)
            if char == "\\" and quote == '"' and index + 1 < len(command):
                current.append(command[index + 1])
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char in "'\"":
            quote = char
            current.append(char)
            structure.append("Q")
            index += 1
            continue
        if char == "\\" and index + 1 < len(command):
            current.append(command[index:index + 2])
            structure.append("E")
            index += 2
            continue
        two = command[index:index + 2]
        if two in ("&&", "||"):
            segments.append("".join(current))
            current = []
            structure.append(" ; ")
            index += 2
            continue
        if char in "|;\n":
            segments.append("".join(current))
            current = []
            structure.append(" ; ")
            index += 1
            continue
        current.append(char)
        structure.append(char)
        index += 1
    if quote:
        return None, None
    segments.append("".join(current))
    return "".join(structure), [segment.strip() for segment in segments if segment.strip()]


_REDIRECT = re.compile(r"(\d*|&)(>>?|>\|)\s*(&?\S*)")


def _structure_checks(structure: str) -> Optional[ShellVerdict]:
    for match in _REDIRECT.finditer(structure):
        target = match.group(3)
        if target == "/dev/null" or re.fullmatch(r"&\d", target):
            continue
        return _deny("write", f"输出重定向写文件({match.group(0).strip()})")
    if "<<" in structure.replace("<<<", ""):
        return _deny("write", "heredoc 不允许")
    if re.search(r"[<>]\(", structure):
        return _deny("unsupported", "进程替换不允许")
    # 单个 & 是后台进程;&& 已在切段时替换,>& / &> 属重定向
    if re.search(r"(?<![>&])&(?![>&\d])", structure):
        return _deny("write", "后台进程不允许")
    return None


def _effective_tokens(tokens: List[str]) -> List[str]:
    rest = list(tokens)
    while rest:
        word = rest[0]
        if _ENV_ASSIGN.match(word):
            rest = rest[1:]
            continue
        if word in _WRAPPERS:
            rest = rest[1:]
            # 包装器自己的选项与数字参数(timeout 30 / nice -n 10 / ionice -c3)
            while rest and (rest[0].startswith("-") or re.fullmatch(r"[\d.]+[smhd]?", rest[0])):
                rest = rest[1:]
            continue
        break
    return rest


def _option_value(tokens: List[str], names: Iterable[str]) -> Optional[str]:
    names = tuple(names)
    for index, token in enumerate(tokens):
        for name in names:
            if token == name and index + 1 < len(tokens):
                return tokens[index + 1]
            if name.startswith("--") and token.startswith(name + "="):
                return token.split("=", 1)[1]
            if not name.startswith("--") and token.startswith(name) and len(token) > len(name):
                return token[len(name):]
    return None


def _segment_checks(segment: str) -> Optional[ShellVerdict]:
    try:
        tokens = _effective_tokens(shlex.split(segment, posix=True))
    except ValueError:
        return _deny("unsupported", "命令无法解析(引号不配对?)")
    if not tokens:
        return None
    word = tokens[0].rsplit("/", 1)[-1]
    args = tokens[1:]
    if word in DENY_COMMANDS:
        return _deny("write", f"{word} 不在只读范围(写文件/外连/解释器/交互式)")
    if word == "crontab" and args != ["-l"]:
        return _deny("write", "crontab 只允许 -l")
    if word == "tail" and any(arg in ("-f", "-F", "--follow") or arg.startswith("--follow") for arg in args):
        return _deny("unsupported", "tail -f 会挂住,请用 tail -n")
    if word == "top" and "-b" not in args:
        return _deny("unsupported", "top 须用批处理 -b -n 1")
    if word == "find" and any(arg in ("-exec", "-execdir", "-ok", "-okdir", "-delete") or arg.startswith("-fprint") or arg == "-fls" for arg in args):
        return _deny("write", "find 不允许 -exec/-delete/-fprint")
    if word in ("awk", "gawk", "mawk") and re.search(r"system\s*\(|getline|>\s*\"|\|\s*\"|\|&", segment):
        return _deny("write", "awk 程序不允许 system/getline/写文件/管道")
    if word == "sed" and re.search(r"(^|[;{/'\"\s])[wWe](\s|$)", " ".join(args)):
        return _deny("write", "sed 脚本不允许 w/e 命令")
    if word == "curl":
        bad = {"-o", "-O", "--output", "--remote-name", "--remote-name-all", "-T", "--upload-file",
               "-F", "--form", "-K", "--config"}
        if any(arg in bad or arg.startswith(("--data", "-d", "--output=", "--form=")) for arg in args):
            return _deny("write", "curl 只允许读取,不允许写文件/上传/提交数据")
        method = _option_value(args, ("-X", "--request"))
        if method and method.upper() not in ("GET", "HEAD"):
            return _deny("write", f"curl 方法 {method} 不是只读")
    if word in ("mysql", "mariadb", "psql"):
        sql = _option_value(args, ("-e", "--execute") if word != "psql" else ("-c", "--command"))
        if sql is None:
            return _deny("unsupported", f"{word} 须用 {'-c' if word == 'psql' else '-e'} 给出只读 SQL")
        verdict = check_readonly_sql(sql)
        if not verdict.ok:
            return _deny("write", verdict.reason)
    if word in ("hdfs", "hadoop"):
        sub = args[0] if args else ""
        if sub in ("dfs", "fs"):
            action = args[1] if len(args) > 1 else ""
            if action not in _HDFS_READ:
                return _deny("write", f"hdfs dfs {action} 不在只读子命令内")
        elif sub == "dfsadmin":
            if args[1:2] != ["-report"] and args[1:3] != ["-safemode", "get"]:
                return _deny("write", "dfsadmin 只允许 -report / -safemode get")
        elif sub == "haadmin":
            if not any(arg in ("-getServiceState", "-getAllServiceState") for arg in args):
                return _deny("write", "haadmin 只允许查询状态")
        elif sub not in ("fsck", "version", "classpath", "getconf", "jmxget"):
            return _deny("write", f"{word} {sub} 不在只读子命令内")
    if word == "yarn":
        sub = args[0] if args else ""
        if sub not in _YARN_READ:
            return _deny("write", f"yarn {sub} 不在只读子命令内")
        if any(arg in ("-kill", "-movetoqueue", "-changeQueue", "-updatePriority",
                       "-updateLifetime", "-signal", "-fail") for arg in args):
            return _deny("write", "yarn 不允许改变应用状态")
    if word == "jmap":
        if not args or args[0] not in ("-heap", "-histo") or any(":live" in arg for arg in args):
            return _deny("write", "jmap 只允许 -heap / -histo(不带 :live,避免触发 Full GC 或落 dump)")
    if word == "jcmd":
        if len(args) >= 2 and args[1] not in _JCMD_READ:
            return _deny("write", f"jcmd {args[1]} 不在只读命令内")
    if word == "jinfo" and any(arg == "-flag" and "=" in " ".join(args) or arg.startswith(("-flag+", "-flag-")) for arg in args):
        return _deny("write", "jinfo 不允许修改 flag")
    return _credential_check(word, args)


def _credential_check(word: str, args: List[str]) -> Optional[ShellVerdict]:
    for arg in args:
        value = arg
        if word in CREDENTIAL_REFERENCE_COMMANDS and arg.startswith(CREDENTIAL_REFERENCE_OPTIONS):
            continue  # 交给客户端读,内容不进输出
        for pattern in CREDENTIAL_PATTERNS:
            if pattern.search(value):
                return _deny("credential", f"{value} 属凭据/密钥路径,禁止读取内容")
    return None


def check_shell(command: str, cmd_filter: CmdFilter) -> ShellVerdict:
    command = command.strip()
    if not command:
        return _deny("unsupported", "空命令")
    verdict = cmd_filter.classify(command)
    if verdict.level is Level.FORBIDDEN:
        return _deny("forbidden", verdict.reason)
    if verdict.level is Level.WRITE:
        return _deny("write", verdict.reason)
    structure, segments = _scan(command)
    if structure is None:
        return _deny("unsupported", "命令无法解析(引号不配对?)")
    found = _structure_checks(structure)
    if found:
        return found
    for segment in segments:
        found = _segment_checks(segment)
        if found:
            return found
    return ShellVerdict(True, "ok")
