"""auto 模式下的命令预筛:用低价快模型把「明显安全」的挑出来直接放行,
其余交人工裁决。

**它只做减法。** 调用点在红线之后——送到这里的命令,cmd_filter 已经判过
readonly。审核器唯一能做的是把其中仍然可疑的**升级**为人工裁决;它说 safe
也只是维持红线原本的放行,不会让任何被红线拒绝的命令通过。

因此它判错的方向是有偏的:漏判(把可疑的说成 safe)退回红线的保护水平,
误判(把安全的说成可疑)只是多问人一次。模型不可用、响应无法解析、超时——
一律当作可疑,走人工。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from lib.triage_agent.model_client import ModelClient, ModelProtocolError

REVIEW_SYSTEM_PROMPT = """
You screen shell commands for an ops diagnosis agent working on production big
data clusters. A separate, authoritative filter has already rejected every
write, destructive, service-restart and configuration-change command, so what
reaches you is read-only by that filter's judgement. Your job is only to catch
what that filter cannot see.

Answer with a single JSON object and nothing else:
{"safe": true|false, "reason": "<one short sentence>"}

Mark safe=false when the command, despite being read-only in form, could:
- execute a command or create a side effect through a tool's scripting facility
  (awk system(), sed e/w, xargs forwarding, a script path passed to an
  interpreter, or Python/Perl/Ruby/Node code that writes, deletes, changes
  configuration, starts a process, or opens a network connection)
- run for an unbounded time or produce unbounded output (tail -f, a follow or
  watch flag, a sampling tool with no count, a scan rooted at /)
- read credentials or secrets (private keys, shadow, a process environ,
  connection configs) rather than operational evidence
- send data off the host (any network client with a remote destination)
- put real load on a production service (a heavy scan, an unbounded query, a
  JVM operation that pauses the process)

Mark safe=true for ordinary read-only investigation: inspecting logs, listing
files, querying process and resource state, bounded database SELECTs, cluster
status reports, and interpreter code that only computes, parses, formats, or
prints data without side effects. Do not reject an interpreter merely because
it uses -c or -e: for example, `python3 -c 'print(1)'` and
`python3 -c 'import json; print(json.dumps({"ok": True}))'` are safe, while
`python3 -c 'open("/tmp/x", "w").write("x")'` and
`python3 -c 'import subprocess; subprocess.run(["rm", "-f", "/tmp/x"])'`
are unsafe.

When you are unsure, answer safe=false. A needless question to an operator is
cheap; a wrong "safe" runs as root on production.
""".strip()


@dataclass(frozen=True)
class ReviewVerdict:
    safe: bool
    reason: str
    # 审核器是否真的作出了判断。未配置模型/调用失败时为 False,
    # 调用方据此把「没被审过」与「审过且判 safe」区分开
    reviewed: bool = True


UNAVAILABLE = ReviewVerdict(
    safe=False,
    reason="未配置审核模型,按未知命令走人工裁决",
    reviewed=False,
)


class CommandReviewer:
    def __init__(self, client: Optional[ModelClient]):
        self._client = client

    @property
    def available(self) -> bool:
        return self._client is not None

    def review(
        self,
        *,
        command: str,
        signature: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> ReviewVerdict:
        if self._client is None:
            return UNAVAILABLE
        payload = {
            "command": command,
            "signature": signature,
            **(context or {}),
        }
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False),
            },
        ]
        try:
            reply = self._client.complete(messages, [])
        except (ModelProtocolError, Exception) as exc:  # noqa: BLE001
            # 审核不可用不该拖垮排查:退回人工裁决即可
            return ReviewVerdict(
                safe=False,
                reason=f"审核模型调用失败({type(exc).__name__}),转人工裁决",
                reviewed=False,
            )
        return _parse_verdict(reply.content)


def _parse_verdict(content: Optional[str]) -> ReviewVerdict:
    """解析不出结构就当可疑——绝不把解析失败当作放行。"""
    if not content:
        return ReviewVerdict(False, "审核模型未返回内容,转人工裁决", False)
    text = content.strip()
    if text.startswith("```"):
        # 容忍模型裹了 markdown 代码块
        text = text.strip("`")
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return ReviewVerdict(False, "审核模型响应不是 JSON,转人工裁决", False)
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return ReviewVerdict(False, "审核模型响应无法解析,转人工裁决", False)
    if not isinstance(data, dict) or not isinstance(data.get("safe"), bool):
        return ReviewVerdict(False, "审核模型响应缺少 safe 字段,转人工裁决", False)
    reason = data.get("reason")
    return ReviewVerdict(
        safe=bool(data["safe"]),
        reason=str(reason) if reason else "(审核模型未给理由)",
    )
