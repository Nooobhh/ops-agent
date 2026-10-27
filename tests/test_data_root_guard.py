"""数据根守卫:运行时写入只经 lib/data_root.py 定位,设 OPS_AGENT_DATA_ROOT 后代码目录不被写。

静态:扫描 lib/ scripts/ web/server/,禁止把 state/records/build/knowledge 或本机私有配置
直接拼到仓库根上;动态:设数据根后经 TestClient 建任务、改采集设置、写 worklog,再跑
build_wiki,断言产物全在数据根、代码目录零写入。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from lib.data_root import DATA_ROOT_ENV

REPO = Path(__file__).resolve().parents[1]
SCAN_DIRS = ("lib", "scripts", "web/server")
_NAMES = (
    r"(model\.local\.yaml|credentials\.json|auth-profiles\.json|vector\.local\.yaml"
    r"|paths\.local\.yaml|local-dev\.yaml|user\.local\.yaml)"
)
PATTERNS = {
    "仓库根拼运行时目录": re.compile(
        r"\b(repo_root|REPO_ROOT|ROOT|root|repo|repo_dir)\)?\s*/\s*[\"'](state|records|build|knowledge)[\"']"
    ),
    "仓库根拼私有配置": re.compile(r"[\"']config[\"']\s*/\s*[\"']" + _NAMES + r"[\"']"),
    "私有配置相对路径": re.compile(r"[\"']config/" + _NAMES + r"[\"']"),
    "cwd 相对运行时路径": re.compile(r"Path\(\s*[\"'](state|records|build|knowledge)(/[^\"']*)?[\"']\s*\)"),
    # 工作记录/排查记录只经 lib.datastore 读写(数据层守卫)
    "绕过数据层拼记录目录": re.compile(
        r"[\"']records[\"']\s*/\s*[\"'](worklog|troubleshoot)[\"']|[\"']work-items[\"']"),
    "绕过数据层写记录": re.compile(r"(?<![.\w])(?<!def )(start_session|append_entry)\("),
    # 记录根可在数据根之外(paths.local.yaml 的 records_root),只经 resolve_records_root/records_root_for
    "数据根拼记录目录": re.compile(r"/\s*[\"']records[\"']"),
    # 模块级解析会在导入时固定记录根,测试 chdir 后仍写到 paths.local.yaml 指的真 records
    "导入时解析数据层": re.compile(r"^[A-Z_]+\s*=\s*.*\b(open_datastore|resolve_records_root|records_root_for)\("),
}
# 经 resolve_data_root(...) 包裹的写法是合法入口,扫描前先抹掉(支持一层嵌套括号)
_DATA_ROOT_CALL = re.compile(r"resolve_data_root\((?:[^()]|\([^()]*\))*\)")
# (文件, 行内片段, 理由):相对片段在使用处已拼到数据根上,或不属于运行时写入
ALLOWED = (
    ("lib/data_root.py", "", "唯一解析入口"),
    ("lib/datastore/local.py", "", "数据层本地实现"),
    ("lib/apphost/prepare.py", 'INDEX_STAMP = Path("build")', "相对片段,使用处拼数据根"),
    ("lib/weekly_report.py", 'relative = Path("records")', "相对片段,使用处拼数据根"),
    ("scripts/build_package.py", '"config/', "打包排除清单:私有配置不得进包"),
    ("scripts/build_package.py", 'REPO_ROOT / "build" / "release"', "开发机打包产物,非运行时数据"),
    ("scripts/release.py", 'REPO_ROOT / "build" / "release"', "开发机发版产物,非运行时数据"),
    ("lib/agent_replay/driver.py", 'self.out_dir / "records"', "评估产物目录自带 records,非运行时记录"),
    ("lib/agent_replay/lean_replay.py", 'work / "records"', "评估产物目录自带 records,非运行时记录"),
)


def _allowed(rel: str, line: str) -> bool:
    return any(rel == path and fragment in line for path, fragment, _reason in ALLOWED)


def scan(root: Path) -> list[str]:
    hits = []
    for base in SCAN_DIRS:
        for path in sorted((root / base).rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                code = _DATA_ROOT_CALL.sub("DATA_ROOT", line.split("#", 1)[0])
                for label, pattern in PATTERNS.items():
                    if pattern.search(code) and not _allowed(rel, line):
                        hits.append(f"{rel}:{number} [{label}] {line.strip()}")
    return hits


def test_patterns_catch_known_bypasses():
    samples = [
        'state = repo_root / "state"',
        'Path(repo_root) / "records" / "troubleshoot"',
        'self.paths.repo_root / "build" / "kb-recall.sqlite"',
        'ROOT / "config" / "credentials.json"',
        'ap.add_argument("--local-dev", default="config/local-dev.yaml")',
        'records_root=Path("records/troubleshoot")',
        'ROOT / "config" / "user.local.yaml"',
        'root = data_root / "records" / "worklog"',
        'session = start_session(records_root, run_id=run_id)',
        'append_entry(entry, records_root=root)',
        'records_root=ambient_data_root() / "records" / "patrol"',
        'root = records_root / "work-items"',
        'TROUBLESHOOT = open_datastore().troubleshoot',
    ]
    for sample in samples:
        code = _DATA_ROOT_CALL.sub("DATA_ROOT", sample)
        assert any(pattern.search(code) for pattern in PATTERNS.values()), sample
    assert not any(
        pattern.search(_DATA_ROOT_CALL.sub("DATA_ROOT", 'resolve_data_root(Path(repo_root)) / "build"'))
        for pattern in PATTERNS.values()
    )


def test_no_runtime_path_joined_to_repo_root():
    hits = scan(REPO)
    assert not hits, "运行时路径须经 lib/data_root.py(resolve_data_root/local_config_path):\n" + "\n".join(hits)


def _files_below(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


def _fake_repo(root: Path) -> Path:
    """空的代码目录:环境数据随环境包(代码仓 envs/),knowledge/state/records/build 一概不放。"""
    (root / "config").mkdir(parents=True)
    return root


def _seed_knowledge(data: Path) -> None:
    doc = data / "knowledge" / "alpha" / "system" / "alpha-system-demo-case.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(
        "---\ntitle: alpha-system-demo-case\ntype: troubleshoot\nplatform: alpha\n"
        "service: system\nproblem: demo-case\nsymptoms: []\nalerts: [demo_alert]\n"
        "playbook: \nstatus: draft\n---\n\n## 现象\n\nx\n", encoding="utf-8")


class _OfflineClient:
    """分类模型替身:测试进程不外呼,分类器按失败回退规则分类。"""

    def __init__(self, *args, **kwargs):
        pass

    def complete(self, *args, **kwargs):
        raise RuntimeError("测试不外呼模型")


def test_runtime_writes_land_in_data_root(tmp_path, monkeypatch):
    from lib.data_root import local_config_path
    from lib.kb.bases import KNOWLEDGE_ROOT_ENV
    from lib.triage_agent.model_registry import ModelRegistry, ModelUpsert
    from web.server import app as app_module

    monkeypatch.delenv(KNOWLEDGE_ROOT_ENV, raising=False)
    repo = _fake_repo(tmp_path / "repo")
    data = (tmp_path / "data").resolve()
    _seed_knowledge(data)
    monkeypatch.setenv(DATA_ROOT_ENV, str(data))
    before = _files_below(repo)

    ModelRegistry(local_config_path(repo, "model.local.yaml")).create_model(ModelUpsert(
        title="M", base_url="https://m.test/v1", api_key="k", model="m",
        default_reasoning_effort="medium",
    ))
    monkeypatch.setattr(app_module, "OpenAICompatibleClient", _OfflineClient)
    # 不进 lifespan:不起 scheduler/节点凭据初始化,只走路由写路径
    client = TestClient(app_module.create_app(state_dir=data / "state", repo_root=repo))
    created = client.post("/api/triage-tasks", json={
        "message": "node1 内存持续升高", "platform": "alpha", "node": "node1",
    })
    assert created.status_code == 201, created.text
    task_id = created.json()["task"]["task_id"]
    settings = client.get("/api/acquisition").json()["settings"]
    settings["alerts"]["retention_days"] = 30
    assert client.put("/api/acquisition/settings", json=settings).status_code == 200, settings
    assert client.put("/api/local-user", json={"id": "alice", "display_name": "Haze"}).status_code == 200
    assert client.post("/api/worklogs", json={
        "date": "2026-10-20", "status": "DONE", "category": "OPS", "title": "数据根守卫",
    }).status_code == 201
    vector = client.get("/api/vector-settings")
    assert vector.status_code == 200, vector.text
    assert vector.json()["settings"]["embedded_path"] == "build/kb-vectors.sqlite"

    from scripts import build_wiki
    monkeypatch.setattr(sys, "argv", ["build_wiki.py", "--repo-root", str(repo)])
    assert build_wiki.main() == 0

    # 读接口在数据根布局下不得因按仓库根求相对路径而 5xx
    for url in (
        "/api/health", "/api/overview", "/api/credentials", "/api/acquisition",
        "/api/knowledge/tree", "/api/knowledge/health", "/api/knowledge/topology",
        "/api/knowledge/search?q=demo", "/api/knowledge/doc?path=alpha/system/alpha-system-demo-case.md",
        "/api/worklogs", "/api/weekly-reports", "/api/triage-tasks", f"/api/triage-tasks/{task_id}",
        "/api/model-settings", "/api/triage-health", "/api/resources/latest", "/api/alerts",
    ):
        response = client.get(url)
        assert response.status_code < 500, (url, response.text[:300])

    assert _files_below(repo) == before, "代码目录被写入"
    for relative in (
        f"state/triage-tasks/{task_id}/task.json",
        "state/acquisition-settings.json",
        "records/worklog/2026-10-20.md",
        "config/model.local.yaml",
        "config/user.local.yaml",
        "build/routing.json",
        "build/wiki.html",
        "build/kb-recall.sqlite",
        "build/kb-retrieval.sqlite",
        "build/kb-topology.json",
    ):
        assert (data / relative).is_file(), relative
    assert "- **作者**: alice" in (data / "records/worklog/2026-10-20.md").read_text(encoding="utf-8")


def test_records_root_outside_data_root(tmp_path, monkeypatch):
    """paths.local.yaml 把记录根放到数据根之外时,记录写入只落记录根。"""
    from lib.kb.bases import KNOWLEDGE_ROOT_ENV
    from web.server import app as app_module

    monkeypatch.delenv(KNOWLEDGE_ROOT_ENV, raising=False)
    repo = _fake_repo(tmp_path / "repo")
    data = (tmp_path / "data").resolve()
    records = (tmp_path / "records").resolve()
    _seed_knowledge(data)
    (data / "config").mkdir(parents=True)
    (data / "config" / "paths.local.yaml").write_text(f"records_root: {records}\n", encoding="utf-8")
    monkeypatch.setenv(DATA_ROOT_ENV, str(data))
    before = _files_below(repo)

    client = TestClient(app_module.create_app(state_dir=data / "state", repo_root=repo))
    created = client.post("/api/worklogs", json={
        "date": "2026-10-20", "status": "DONE", "category": "OPS", "title": "记录根",
    })
    assert created.status_code == 201, created.text
    assert created.json()["path"] == "records/worklog/2026-10-20.md"
    acquisition = client.get("/api/acquisition").json()
    assert acquisition["paths"]["alerts"]["resolved"] == str(records / "alerts")
    assert acquisition["paths"]["resources"]["resolved"] == str(records / "telemetry" / "resources")
    for url in ("/api/worklogs", "/api/weekly-reports", "/api/knowledge/tree"):
        response = client.get(url)
        assert response.status_code < 500, (url, response.text[:300])

    assert (records / "worklog" / "2026-10-20.md").is_file()
    assert not (data / "records").exists()
    assert _files_below(repo) == before, "代码目录被写入"
