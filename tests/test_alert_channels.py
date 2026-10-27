import json
import multiprocessing
from datetime import datetime, timezone

from lib.alerts.channels import load_channels, migrate_wecom, save_channel


NOW = datetime(2026, 7, 21, 8, 30, tzinfo=timezone.utc)


def test_save_load_roundtrip_and_disk_contract(tmp_path):
    entry = save_channel(
        tmp_path,
        "feishu",
        enabled=False,
        credentials_update={"webhook": "https://open.feishu.cn/hook/abcd", "secret": "s"},
        now=NOW,
    )

    assert entry == {
        "enabled": False,
        "webhook": "https://open.feishu.cn/hook/abcd",
        "secret": "s",
        "updated_at": NOW.isoformat(),
        "updated_by": "web",
    }
    assert load_channels(tmp_path) == {"feishu": entry}
    assert json.loads((tmp_path / "notify-channels.json").read_text()) == {
        "version": 1,
        "channels": {"feishu": entry},
    }


def test_load_tolerates_missing_broken_and_wrong_shapes(tmp_path):
    assert load_channels(tmp_path) == {}
    path = tmp_path / "notify-channels.json"
    for content in ("{", "[]", '{"channels": []}'):
        path.write_text(content, encoding="utf-8")
        assert load_channels(tmp_path) == {}


def test_partial_credentials_updates_use_three_state_semantics(tmp_path):
    save_channel(
        tmp_path,
        "wecom",
        enabled=True,
        credentials_update={"webhook_key": "old-key"},
    )
    entry = save_channel(tmp_path, "wecom", enabled=False, credentials_update=None)
    assert entry["webhook_key"] == "old-key"
    entry = save_channel(
        tmp_path,
        "wecom",
        enabled=True,
        credentials_update={"webhook_key": ""},
    )
    assert entry["webhook_key"] == "old-key"

    save_channel(
        tmp_path,
        "feishu",
        enabled=True,
        credentials_update={"webhook": "https://open.feishu.cn/hook/a", "secret": "old"},
    )
    entry = save_channel(
        tmp_path,
        "feishu",
        enabled=True,
        credentials_update={"webhook": "", "secret": ""},
    )
    assert entry["webhook"] == "https://open.feishu.cn/hook/a"
    assert entry["secret"] == ""


def test_migration_preserves_feishu_and_existing_wecom(tmp_path):
    feishu = save_channel(
        tmp_path,
        "feishu",
        enabled=False,
        credentials_update={"webhook": "https://open.feishu.cn/hook/a", "secret": ""},
        now=NOW,
    )
    migrate_wecom(tmp_path, "legacy", now=NOW)
    channels = load_channels(tmp_path)
    assert channels["feishu"] == feishu
    assert channels["wecom"] == {
        "enabled": True,
        "webhook_key": "legacy",
        "updated_at": NOW.isoformat(),
        "updated_by": "migration",
    }

    save_channel(tmp_path, "wecom", enabled=False, credentials_update=None, now=NOW)
    before = load_channels(tmp_path)
    migrate_wecom(tmp_path, "replacement", now=NOW)
    assert load_channels(tmp_path) == before


def _save_feishu(state_dir):
    save_channel(
        state_dir,
        "feishu",
        enabled=True,
        credentials_update={"webhook": "https://open.feishu.cn/hook/a", "secret": ""},
    )


def _migrate_wecom(state_dir):
    migrate_wecom(state_dir, "legacy")


def test_cross_process_transactions_do_not_lose_channels(tmp_path):
    processes = [
        multiprocessing.Process(target=_save_feishu, args=(tmp_path,)),
        multiprocessing.Process(target=_migrate_wecom, args=(tmp_path,)),
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    assert set(load_channels(tmp_path)) == {"wecom", "feishu"}
