"""triage 路由匹配:告警名精确命中,--query 症状召回,或 (platform[, service]) 候选集。stdout 纯 JSON。"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _common import emit_err, emit_ok
from lib.data_root import ambient_data_root
from lib.envpack import active_env_pack
from lib.triage import filter_candidates, load_routing, match_alert


def run(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--alert", help="精确告警名")
    ap.add_argument("--platform", choices=active_env_pack().knowledge_platforms())
    ap.add_argument("--service", help="服务名,配合 --platform 收窄候选")
    ap.add_argument("--query", help="症状描述,走 kb-recall 排序召回(无精确告警名时用)")
    ap.add_argument("--routing", default=str(ambient_data_root() / "build" / "routing.json"))
    ap.add_argument("--recall-index", default=str(ambient_data_root() / "build" / "kb-recall.sqlite"))
    a = ap.parse_args(argv)

    if not a.alert and not a.platform:
        return emit_err("bad_args", "--alert 与 --platform 至少给一个")
    if a.alert and not a.platform:
        return emit_err("bad_args", "--alert 必须同时给 --platform")
    if a.query and not a.platform:
        return emit_err("bad_args", "--query 必须同时给 --platform")

    try:
        routing = load_routing(Path(a.routing))
    except (OSError, json.JSONDecodeError) as e:
        return emit_err("config_error", f"routing 索引不可读(先跑 scripts/build_routing.py?): {e}")
    hit = match_alert(routing, a.alert, a.platform) if a.alert else None
    if hit:
        return emit_ok(hit=asdict(hit), candidates=[])
    if not a.platform:
        return emit_ok(hit=None, candidates=[])
    if a.query:
        from lib.kb.recall import RecallIndexError, search_recall_index
        try:
            hits = search_recall_index(Path(a.recall_index), a.query,
                                       a.platform, a.service or None)
        except RecallIndexError as e:
            return emit_err("recall_index_error", str(e))
        return emit_ok(hit=None, candidates=[asdict(h) for h in hits], recall=True)
    cs = filter_candidates(routing, platform=a.platform, service=a.service)
    return emit_ok(hit=None, candidates=[asdict(c) for c in cs])


if __name__ == "__main__":
    sys.exit(run())
