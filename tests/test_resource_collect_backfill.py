from datetime import datetime, timezone

from scripts import resource_collect


def test_short_gap_backfills_five_minute_buckets(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    state = repo / "state"
    config = repo / "config"
    config.mkdir(parents=True)
    config.joinpath("resource-metrics.yaml").write_text(
        "version: 1\nrisk_defaults: {}\n", encoding="utf-8",
    )
    appended = []
    calls = []

    monkeypatch.setattr(resource_collect, "latest_snapshots", lambda _root: {
        "alpha": {"scheduled_at": "2026-08-28T00:00:00+00:00"},
        "beta": {"scheduled_at": "2026-08-28T00:00:00+00:00"},
    })

    def collect(_repo, *, now, mode="live"):
        calls.append((now, mode))
        stamp = now.astimezone(timezone.utc).isoformat()
        return ([{
            "schema_version": 1, "snapshot_id": f"{mode}-{stamp}",
            "scheduled_at": stamp, "collected_at": stamp,
            "platform": "alpha", "nodes": [], "services": [],
            "status": "ok", "errors": [],
            "coverage": {"expected_nodes": 0, "observed_nodes": 0,
                         "expected_services": 0, "observed_services": 0},
        }], {"services": [], "unmonitored_components": []})

    monkeypatch.setattr(resource_collect, "collect_resource_snapshots", collect)
    monkeypatch.setattr(
        resource_collect, "append_snapshot",
        lambda _root, row: appended.append(row) or (_root / "day.jsonl"),
    )
    monkeypatch.setattr(resource_collect, "save_catalog", lambda *_args: None)
    monkeypatch.setattr(
        resource_collect, "analyze_resource_history",
        lambda *_args, **_kwargs: {"findings": []},
    )
    monkeypatch.setattr(resource_collect, "save_risks", lambda *_args: None)

    result = resource_collect.run(
        ["--repo-root", str(repo), "--state-dir", str(state)],
        now=datetime(2026, 8, 28, 0, 20, tzinfo=timezone.utc),
    )

    assert result == 0
    assert [mode for _now, mode in calls] == ["backfill", "backfill", "backfill", "live"]
    assert len(appended) == 4
