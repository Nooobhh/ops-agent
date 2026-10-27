import yaml
from fastapi.testclient import TestClient

from tests.envpack_helpers import as_env_pack
from web.server.app import create_app


def test_env_reports_the_pack_identity_and_timezone(tmp_path):
    root = as_env_pack(tmp_path)
    env = yaml.safe_load((root / "env.yaml").read_text(encoding="utf-8"))
    env.update(id="lab", display_name="实验环境", timezone="Europe/Berlin")
    (root / "env.yaml").write_text(yaml.safe_dump(env, allow_unicode=True, sort_keys=False), encoding="utf-8")
    with TestClient(create_app(state_dir=root / "state", repo_root=root)) as client:
        body = client.get("/api/env").json()
    assert {key: body[key] for key in ("schema_version", "id", "display_name", "timezone")} == {
        "schema_version": 1, "id": "lab", "display_name": "实验环境", "timezone": "Europe/Berlin",
    }
    assert body["clusters"][0] == {"key": "alpha", "display_name": "Alpha 集群", "short_name": "Alpha",
                                   "roles": ["managed", "knowledge"]}
    assert [item["key"] for item in body["clusters"]] == ["alpha", "beta", "db", "shared", "cache", "edge"]


def test_env_reports_where_the_pack_comes_from(tmp_path):
    root = as_env_pack(tmp_path)
    with TestClient(create_app(state_dir=root / "state", repo_root=root)) as client:
        body = client.get("/api/env").json()
    assert (body["origin"], body["path"], body["commit"]) == ("configured", str(root), None)


def test_env_marks_the_bundled_example_pack(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    with TestClient(create_app(state_dir=root / "state", repo_root=root)) as client:
        body = client.get("/api/env").json()       # conftest 强制挂示例包 = 未挂载环境数据时的回退
    assert (body["id"], body["origin"]) == ("example", "example")
