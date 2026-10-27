"""命令输出脱敏:record 落盘与 ts_exec 回显前打码疑似凭证。

红线过滤(cmd_filter)只管命令输入;节点配置文件(application.properties 等)
cat 出来的 secret 会随 session.json 入 records 仓并被 push,此层是输出侧唯一闸门。
只打码值、保留键名与结构,不影响 host/port 等普通配置的可读性。
"""
from __future__ import annotations

import re

REDACTED = "***REDACTED***"

# 敏感键名:通用长词(password/secret/token/…含于键名任意位置,其后可续 .xxx,
# 如 mqtt.secret.key)、整段的 pass(super_pass / appmanager.pass / KEYSTORE_PASS;
# 前后须是串首尾或 ._- 分隔,避免误伤 bypass/passive),或末段短键 ak/sk
# (mqtt.ak= 形态;要求其前是串首或 ._- 分隔,避免误伤 task= 等)
_KEY = (
    r"(?:"
    r"[\w.\-]*(?:password|passwd|pwd|secret|token|credential"
    r"|access[_-]?key|api[_-]?key|private[_-]?key)[\w.\-]*"
    r"|(?:[\w.\-]+[._\-])?pass(?:[._\-][\w.\-]*)?"
    r"|(?:[\w.\-]+[._\-])?(?:ak|sk)"
    r")"
)

# properties/yaml/json 通吃:key=value / key: value / "key": "value"
_KV = re.compile(
    rf"(?i)(?<![\w.\-])(?P<prefix>[\"']?{_KEY}[\"']?\s*[=:]\s*)"
    r"(?P<q>[\"']?)(?P<val>[^\"'\s,;&]+)(?P=q)"
)
# 命令行长选项以空格隔开的值:--password 'x' / --db-password x(--k=v 已由 _KV 覆盖)。
# 2026-10 实测漏网:sqoop/DataX 任务命令的 --password '…' 原样进了 records
_FLAG = re.compile(
    rf"(?i)(?<![\w\-])(?P<prefix>--{_KEY}\s+)"
    r"(?:(?P<q>[\"'])(?P<qval>[^\"'\n]*)(?P=q)|(?P<val>[^\s\"'`;&|]+))"
)
_SECRET_KEY = re.compile(rf"(?i){_KEY}")
# URL 内嵌凭证 scheme://user:pass@host
_URL_CRED = re.compile(r"(://[^/\s:@]+:)([^@\s/]+)(@)")
# Authorization 头
_AUTH = re.compile(r"(?i)(authorization\s*[=:]\s*(?:bearer|basic)\s+)\S+")


def redact_secrets(text: str) -> str:
    if not text:
        return text
    text = _KV.sub(lambda m: f"{m.group('prefix')}{m.group('q')}{REDACTED}{m.group('q')}", text)
    text = _FLAG.sub(lambda m: f"{m.group('prefix')}{m.group('q') or ''}{REDACTED}{m.group('q') or ''}", text)
    text = _URL_CRED.sub(rf"\g<1>{REDACTED}\g<3>", text)
    text = _AUTH.sub(rf"\g<1>{REDACTED}", text)
    return text


def redact_config_value(key: str, value):
    """结构化配置的单值打码:键名敏感则整值替换,否则按行内规则打码内嵌口令。

    须在 json.dumps 之前做:转义后的 `\\n` 不是空白,行内规则会把后续行一并吞掉。
    上游系统自带的 `SECRET:` 掩码原样保留。
    """
    if not isinstance(value, str) or not value or value.startswith("SECRET:"):
        return value
    if _SECRET_KEY.fullmatch(key or ""):
        return REDACTED
    return redact_secrets(value)
