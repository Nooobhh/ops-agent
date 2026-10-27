#!/usr/bin/env python
"""命令闸离线回放:把 Agent 转录里的 shell 命令逐条过命令闸,统计三档判定分布。

只读本地转录文件,不连集群、不调模型;stdout 为纯 JSON。
用法:scripts/command_guard_replay.py <transcript.jsonl>... [--details]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MODES = ("whitelist", "auto", "bypass")


def shell_commands(path: Path):
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record.get("type") != "assistant":
            continue
        for call in record.get("tool_calls") or []:
            if call.get("name") == "shell":
                yield call.get("arguments", {}).get("command", "")


def main(argv=None) -> int:
    from lib.command_guard import CommandGuard, decide

    parser = argparse.ArgumentParser(description="replay shell commands through the command guard")
    parser.add_argument("transcripts", nargs="+")
    parser.add_argument("--details", action="store_true", help="列出每条非只读命令")
    args = parser.parse_args(argv)

    guard = CommandGuard()
    levels, categories, words = Counter(), Counter(), Counter()
    actions = {mode: Counter() for mode in MODES}
    details = []
    total = 0
    for name in args.transcripts:
        path = Path(name)
        for command in shell_commands(path):
            total += 1
            verdict = guard.check(command)
            levels[verdict.level] += 1
            categories[verdict.category] += 1
            for signature in verdict.unregistered:
                words[signature.split()[0]] += 1
            for mode in MODES:
                actions[mode][decide(verdict, mode).action] += 1
            if args.details and verdict.level != "readonly":
                details.append({
                    "transcript": str(path), "level": verdict.level,
                    "category": verdict.category, "reason": verdict.reason,
                    "command": command,
                })
    print(json.dumps({
        "ok": True,
        "total": total,
        "levels": dict(levels),
        "categories": dict(categories),
        "actions": {mode: dict(counter) for mode, counter in actions.items()},
        "unregistered_words": dict(words.most_common()),
        **({"details": details} if args.details else {}),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
