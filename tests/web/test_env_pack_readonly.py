"""守卫:设置页的写接口只写数据根(本机配置与 state),不写环境包目录——环境包是只读的应用数据。"""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from tests.envpack_helpers import EXAMPLE_PACK, REPO
from web.server.app import create_app


def _snapshot(root: Path) -> dict:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*")) if path.is_file() and not path.is_symlink()
    }


def _set_readonly(root: Path, readonly: bool) -> None:
    for path in (root, *root.rglob("*")):
        if not path.is_symlink():
            path.chmod((0o555 if readonly else 0o755) if path.is_dir() else (0o444 if readonly else 0o644))


@pytest.fixture
def readonly_pack(tmp_path):
    """示例包副本(扩展目录软链源包)设成只读,数据根另起一处并经 env_pack 指向它。"""
    pack = tmp_path / "pack"
    shutil.copytree(EXAMPLE_PACK, pack, ignore=shutil.ignore_patterns("__pycache__", "python"))
    env = yaml.safe_load((pack / "env.yaml").read_text(encoding="utf-8"))
    (pack / env["python"]).symlink_to(EXAMPLE_PACK / env["python"], target_is_directory=True)
    data = tmp_path / "data"
    (data / "config").mkdir(parents=True)
    (data / "config" / "paths.local.yaml").write_text(
        yaml.safe_dump({"env_pack": str(pack), "records_root": str(data / "records")}), encoding="utf-8",
    )
    (data / "state").mkdir()        # 采集全关(同验收实例),免得调度器起真实 tick
    shutil.copy(REPO / "packaging" / "acquisition-disabled.json", data / "state" / "acquisition-settings.json")
    before = _snapshot(pack)
    _set_readonly(pack, True)
    try:
        yield pack, data, before
    finally:
        _set_readonly(pack, False)


def test_settings_writes_land_in_the_data_root_not_the_env_pack(readonly_pack):
    pack, data, before = readonly_pack
    acquisition = {
        "alerts": {"enabled": False, "interval_seconds": 300, "history_root": "alerts", "retention_days": 30},
        "resources": {"enabled": False, "interval_seconds": 300, "snapshot_root": "telemetry/resources",
                      "retention_days": 30},
        "maintenance": {"enabled": False, "hour": 2, "minute": 30},
    }
    writes = [
        ("put", "/api/weekly-report-settings", {"start_weekday": 0, "end_weekday": 4}),
        ("put", "/api/alert-sources/prometheus/notify", {"notify": True}),
        ("post", "/api/alert-config/prometheus/HostDown", {"severity": "critical", "notify": False}),
        ("put", "/api/resource-alert-settings", {"schema_version": 1, "overrides": {"cpu": {"warning_pct": 70}}}),
        ("put", "/api/acquisition/settings", acquisition),
        ("patch", "/api/plugins/prometheus", {"enabled": True, "timeout_seconds": 30}),
        ("put", "/api/command-policy", {"mode": "auto"}),
    ]
    with TestClient(create_app(state_dir=data / "state", repo_root=data)) as client:
        assert client.get("/api/env").json()["origin"] == "configured"
        for method, url, body in writes:
            response = getattr(client, method)(url, json=body)
            assert response.status_code == 200, (url, response.text)
        assert client.get("/api/weekly-report-settings").json()["start_label"] == "周一"
    _set_readonly(pack, False)
    assert _snapshot(pack) == before
    assert (data / "config" / "weekly-report.local.yaml").is_file()
    assert (data / "config" / "plugins.local.yaml").is_file()
