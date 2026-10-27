"""环境包 alert_sources 扩展源:按集群各一个,源 id 为「类型:集群」,上下文带集群配置、窗口与账号页凭据。"""
import sys

import pytest
import yaml

from lib.alerts.model import AlertEvent
from lib.alerts.state import AlertsState
from lib.credentials import CredentialStore
from scripts import alert_poll
from tests.envpack_helpers import as_env_pack

EXT = '''
from lib.envpack.ext import AlertEvent, SourceError

SEEN = []


class DemoSource:
    def __init__(self, context):
        self.context = context

    def fetch(self):
        SEEN.append(self.context)
        if self.context.config.get("broken"):
            raise SourceError(self.context.source_id, "upstream down")
        return [AlertEvent(source="demo", platform=self.context.platform, alertname="demo_rule",
                           severity="warning", host="node1", host_raw="node1", service="DEMO",
                           state="firing", maintenance=False, detail="demo",
                           source_active_at="2026-10-26T00:00:00+00:00")]


def create(context):
    return DemoSource(context)
'''


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))
    root = as_env_pack(tmp_path)
    env = yaml.safe_load((root / "env.yaml").read_text(encoding="utf-8"))
    env.update(python="python", extensions={"alert_sources": {"demo": "fake_alert_ext:create"}})
    (root / "env.yaml").write_text(yaml.safe_dump(env, allow_unicode=True, sort_keys=False), encoding="utf-8")
    (root / "python").mkdir()
    (root / "python" / "fake_alert_ext.py").write_text(EXT, encoding="utf-8")
    (root / "config" / "platform-alpha.yaml").write_text(
        "platform: alpha\nnodes:\n  node1: {host: 192.0.2.1}\nendpoints:\n  demo: {url: 'http://192.0.2.9'}\n",
        encoding="utf-8",
    )
    store = CredentialStore(root / "config" / "auth-profiles.json", merge_home=False, specs=())
    monkeypatch.setattr(alert_poll, "_credential_store", lambda: store)
    monkeypatch.chdir(root)
    yield root
    sys.modules.pop("fake_alert_ext", None)


def _collect(root, sources):
    config = {"sources": sources}
    return alert_poll.collect_events(config, AlertsState(root / "state" / "alerts.json"))


def test_extension_source_is_built_per_cluster_and_keyed_by_type_and_cluster(repo):
    events, errors, successes = _collect(repo, {"demo-hd": {"type": "demo", "platform": "alpha"}})
    assert errors == [] and successes == {"demo:alpha"}
    assert [(event.source, event.platform, event.alertname) for event in events] == [("demo", "alpha", "demo_rule")]
    context = sys.modules["fake_alert_ext"].SEEN[-1]
    assert (context.source_id, context.platform) == ("demo:alpha", "alpha")
    assert context.platform_config.endpoints["demo"]["url"] == "http://192.0.2.9"
    assert context.query_end_ms > context.query_start_ms
    assert isinstance(context.credentials, CredentialStore)


@pytest.mark.parametrize("source, message", [
    ({"type": "demo"}, "platform"),                                   # 扩展源必须按集群
    ({"type": "demo", "platform": "nope"}, "platform"),
    ({"type": "undeclared", "platform": "alpha"}, "未在环境包"),
    ({"type": "demo", "platform": "alpha", "broken": True}, "upstream down"),
])
def test_extension_source_errors_are_explicit_source_errors(repo, source, message):
    events, errors, successes = _collect(repo, {"demo-hd": source})
    assert events == [] and successes == set()
    assert len(errors) == 1 and message in str(errors[0])


def test_event_source_ids_keep_global_plugin_source_unscoped():
    from lib.alerts.source_notify import event_source_id

    assert event_source_id("prometheus", "alpha") == "prometheus"
    assert event_source_id("demo", "alpha") == "demo:alpha"
    assert event_source_id("demo", None) == "demo:unknown"
    assert AlertEvent  # 事件模型经核心导出,扩展用 lib.envpack.ext 取同一个类
