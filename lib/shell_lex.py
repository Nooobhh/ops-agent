"""shell 串的引号上下文扫描:红线(cmd_filter)与命令闸(command_guard)共用一份。

只回答两件事:哪里是单引号字面量、哪里有会被 shell 展开的命令替换,以及按未引号
的控制操作符切段。扫描器与真实 shell 理解不一致的构造(注释、行继续符、ANSI-C /
locale 引号、heredoc、alias)不做猜测,标为 ambiguous,由调用方按不安全处理。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

_ALIAS_WORD = re.compile(r"(?<![\w-])alias(?![\w-])")


@dataclass(frozen=True)
class ShellScan:
    # 引号内容替换成 Q 后的文本:结构检查(重定向、后台、heredoc)只看这一份
    structure: str
    # 按未引号的 ; & | 换行 切开的段(保留原引号)
    segments: List[str]
    # 与 segments 对齐:该段的输入来自上一段的管道(| 或 |&)
    pipe_fed: List[bool] = field(default_factory=list)
    # 单引号字面量的 [start, end) 区间(含两侧引号)
    single_quoted: List[Tuple[int, int]] = field(default_factory=list)
    # 单引号外被反斜杠转义的 $ 与反引号的位置(`\``、`\$(` 是字面量)
    escaped: List[int] = field(default_factory=list)
    # 单引号之外出现了 $( 或反引号(双引号内同样会展开)
    live_substitution: bool = False
    # 扫描器无法与 shell 保持一致的构造;非空时调用方不得依赖本扫描结论放宽判定
    ambiguous: str = ""


def scan(command: str) -> Optional[ShellScan]:
    """引号不配对返回 None。"""
    structure: List[str] = []
    segments: List[Tuple[str, bool]] = []
    current: List[str] = []
    fed = False
    spans: List[Tuple[int, int]] = []
    escaped: List[int] = []
    live = False
    ambiguous = ""
    quote: Optional[str] = None  # "'" / '"' / "$'"
    braces = 0  # ${…} 嵌套深度:里面的 | ; & 是模式字符,不是操作符
    start = 0
    word_start = True
    index = 0
    length = len(command)
    while index < length:
        char = command[index]
        if quote == "'":
            current.append(char)
            if char == "'":
                spans.append((start, index + 1))
                quote = None
            index += 1
            continue
        if quote in ('"', "$'"):
            current.append(char)
            if char == "\\" and index + 1 < length:
                if quote == '"' and command[index + 1] in "$`":
                    escaped.append(index + 1)
                current.append(command[index + 1])
                index += 2
                continue
            if quote == '"' and (char == "`" or command.startswith("$(", index)):
                live = True
            if char == quote[-1]:
                quote = None
            index += 1
            continue
        if char == "\\":
            if command.startswith("\\\n", index):
                ambiguous = ambiguous or "行继续符"
            elif command[index + 1:index + 2] in ("$", "`"):
                escaped.append(index + 1)
            current.append(command[index:index + 2])
            structure.append("E")
            index += 2
            word_start = False
            continue
        if char == "$" and command[index + 1:index + 2] in ("'", '"'):
            ambiguous = ambiguous or "ANSI-C/locale 引号 $'…'/$\"…\""
            quote = "$'" if command[index + 1] == "'" else '"'
            current.append(command[index:index + 2])
            structure.append("Q")
            index += 2
            word_start = False
            continue
        if char in "'\"":
            quote = char
            start = index
            current.append(char)
            structure.append("Q")
            index += 1
            word_start = False
            continue
        if char == "#" and word_start:
            ambiguous = ambiguous or "注释"
        if char == "`" or command.startswith("$(", index):
            live = True
        if command.startswith("${", index):
            braces += 1
        elif char == "}" and braces:
            braces -= 1
        two = command[index:index + 2]
        if two in ("&&", "||", ";;", "|&") and not braces:
            segments.append(("".join(current), fed))
            fed = two == "|&"
            current = []
            structure.append(f" {two} ")
            index += 2
            word_start = True
            continue
        if char in "|;\n" and not braces:
            segments.append(("".join(current), fed))
            fed = char == "|"
            current = []
            structure.append(f" {char} ")
            index += 1
            word_start = True
            continue
        current.append(char)
        structure.append(char)
        # `${#x}` 的 # 是取长度,不是注释
        word_start = char in " \t&()<>}" or (char == "{" and command[index - 1:index] != "$")
        index += 1
    if quote or braces:
        return None
    segments.append(("".join(current), fed))
    segments = [(text.strip(), piped) for text, piped in segments if text.strip()]
    text = "".join(structure)
    if "<<" in text.replace("<<<", ""):
        ambiguous = ambiguous or "heredoc"
    if _ALIAS_WORD.search(text):
        ambiguous = ambiguous or "alias"
    return ShellScan(
        structure=text,
        segments=[segment for segment, _ in segments],
        pipe_fed=[piped for _, piped in segments],
        single_quoted=spans,
        escaped=escaped,
        live_substitution=live,
        ambiguous=ambiguous,
    )


def mask_quoted_substitutions(command: str, placeholder: str = "\x02") -> str:
    """把单引号字面量里、以及被反斜杠转义的 `$` 与反引号换成占位符(长度不变)。

    单引号内 shell 不做任何展开,awk 的 `$(NF-1)` 只是字面量;反斜杠转义的反引号
    同样是字面量(SQL 里转义的反引号标识符)。扫描失败或含 ambiguous 构造时原样
    返回:宁可误判成命令替换,也不能把真实替换藏起来。
    """
    result = scan(command)
    if result is None or result.ambiguous or not (result.single_quoted or result.escaped):
        return command
    chars = list(command)
    for begin, end in result.single_quoted:
        for position in range(begin + 1, end - 1):
            if chars[position] in "$`":
                chars[position] = placeholder
    for position in result.escaped:
        chars[position] = placeholder
    return "".join(chars)
