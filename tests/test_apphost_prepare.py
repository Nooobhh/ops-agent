import json
from types import SimpleNamespace

from lib.apphost.controller import ServerController, ServerState
from lib.apphost.prepare import INDEX_STAMP, prepare_package


def _package(root):
    (root / "package-info.json").write_text(
        json.dumps({"version": "0.20.0", "commit": "abc1234"}), encoding="utf-8")


def _recorder(returncode=0):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=returncode)

    return calls, run


def test_source_checkout_is_left_alone(tmp_path):
    calls, run = _recorder()
    assert prepare_package(tmp_path, "py", tmp_path / "log", run=run) is None
    assert calls == []
    assert not (tmp_path / "knowledge").exists()


def test_first_run_builds_index_without_bundled_knowledge(tmp_path):
    """包内不带知识库:首启没有知识库也照常重建(空)索引,不解任何种子。"""
    _package(tmp_path)
    calls, run = _recorder()

    assert prepare_package(tmp_path, "py", tmp_path / "log", run=run) is None

    assert not (tmp_path / "knowledge").exists()
    argv, kwargs = calls[0]
    assert argv == ["py", str(tmp_path / "scripts" / "build_wiki.py")]
    assert kwargs["cwd"] == str(tmp_path)
    assert (tmp_path / INDEX_STAMP).read_text(encoding="utf-8") == "0.20.0+abc1234"


def test_existing_knowledge_is_never_overwritten_and_same_version_skips_rebuild(tmp_path):
    _package(tmp_path)
    (tmp_path / "knowledge" / "alpha").mkdir(parents=True)
    (tmp_path / "knowledge" / "alpha" / "a.md").write_text("本机草稿", encoding="utf-8")
    (tmp_path / "build").mkdir()
    (tmp_path / INDEX_STAMP).write_text("0.20.0+abc1234", encoding="utf-8")
    calls, run = _recorder()

    assert prepare_package(tmp_path, "py", tmp_path / "log", run=run) is None

    assert (tmp_path / "knowledge" / "alpha" / "a.md").read_text(encoding="utf-8") == "本机草稿"
    assert calls == []


def test_failed_rebuild_reports_and_keeps_stamp_unset(tmp_path):
    _package(tmp_path)
    _calls, run = _recorder(returncode=1)

    assert "索引" in prepare_package(tmp_path, "py", tmp_path / "log", run=run)
    assert not (tmp_path / INDEX_STAMP).exists()


def test_controller_reads_starting_while_preparing_and_logs_warning(tmp_path, caplog):
    seen = []

    class FakeProcess:
        pid = 1
        returncode = None

        def poll(self):
            return None

    def prepare():
        seen.append(controller.refresh())
        return "知识索引重建失败,详见日志"

    controller = ServerController(
        tmp_path, tmp_path / "state", probe=lambda: False,
        popen=lambda *_args, **_kwargs: FakeProcess(), prepare=prepare,
    )

    with caplog.at_level("WARNING"):
        assert controller.start() is True

    assert seen == [ServerState.STARTING]
    assert "知识索引重建失败" in caplog.text
    assert controller.refresh() is ServerState.STARTING
