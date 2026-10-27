"""工作项只读查询:列出未关闭事项,供 log-work skill 写 worklog 时选挂载对象。stdout 纯 JSON。

写操作(建项、改状态、挂载)只在中台工作项页做;本脚本不写。
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from lib.datastore import open_datastore
from lib.envpack import env_timezone

try:
    from scripts._common import emit_err, emit_ok
except ImportError:  # direct script execution
    from _common import emit_err, emit_ok

FIELDS = ("id", "title", "status", "priority", "kind", "plan_id", "plan_node", "start_date", "due_date")


def run(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("list", help="未关闭事项;--date 只留计划窗口覆盖该日或没填日期的")
    listing.add_argument("--date", help="YYYY-MM-DD")
    listing.add_argument("--all", action="store_true", help="含已完成/取消")
    a = ap.parse_args(argv)

    try:
        day = date.fromisoformat(a.date) if a.date else None
    except ValueError:
        return emit_err("bad_args", "--date 须是 YYYY-MM-DD")
    store = open_datastore(REPO_ROOT).work_items
    plans = {plan["id"]: plan["title"] for plan in store.list_plans()}
    items = []
    for item in store.list(include_closed=a.all):
        start, due = item.get("start_date"), item.get("due_date")
        if day is not None and (start or due):
            first, last = start or due, due or start
            if not first <= day.isoformat() <= last:
                continue
        items.append({**{key: item.get(key) for key in FIELDS}, "plan_title": plans.get(item.get("plan_id"))})
    return emit_ok(today=datetime.now(env_timezone()).date().isoformat(), items=items)


if __name__ == "__main__":
    sys.exit(run())
