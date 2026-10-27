import http.client
import json
from dataclasses import fields
from pathlib import Path

import pytest

from lib.sanitize import REDACTED
from lib.triage_agent.actions import ActionGateway
from lib.triage_agent.diagnostics import (
    DiagnosticConfigError,
    DiagnosticDenied,
    DiagnosticRegistry,
    RenderedDiagnostic,
)
from lib.triage_agent.paths import AppPaths
from tests.envpack_helpers import as_env_pack


FAKE_PASSWORD = "unit-test-db-password"
# 真实目录已无 http 诊断(带认证的外部系统改由插件接入);通用 http 通道用自建定义测
HTTP_DIAGNOSTICS = """
version: 1
diagnostics:
  - id: example.status
    platforms: [alpha]
    transport: http
    note: 测试用 HTTP 诊断
    params:
      service: {type: string, enum: [hdfs, yarn]}
    value_maps:
      service: {hdfs: HDFS, yarn: YARN}
    url_template: "http://example.test/api/status?service={service}"
    timeout_seconds: 10
  - id: example.config
    platforms: [alpha]
    transport: http
    params:
      service: {type: string, enum: [hdfs, yarn]}
      key: {type: token, max_length: 128, pattern: "[a-zA-Z0-9._-]+"}
    url_template: "http://example.test/api/config?service={service}"
    extract: "/items/*/properties/{key}"
    timeout_seconds: 10
approved_actions: []
"""


@pytest.fixture
def registry(tmp_path):
    path = tmp_path / "diagnostics.yaml"
    path.write_text(HTTP_DIAGNOSTICS, encoding="utf-8")
    return DiagnosticRegistry.load(path)


@pytest.fixture
def http_gateway(tmp_path, registry):
    gateway = ActionGateway(
        AppPaths.from_repo_root(tmp_path),
        registry,
        pool_factory=lambda _platform: pytest.fail("HTTP diagnostic used SSH pool"),
    )
    session = gateway.start_session(
        platform="alpha", node="node1", trigger="test:http", service="hdfs"
    )
    return gateway, session


def run_status(gateway, session):
    return gateway.run_diagnostic(
        session.run_id,
        platform="alpha",
        node="node1",
        diagnostic_id="example.status",
        params={"service": "hdfs"},
    )


def test_http_definition_renders_url(registry):
    rendered = registry.render("example.status", {"service": "yarn"}, platform="alpha")

    assert rendered.transport == "http"
    assert rendered.url == "http://example.test/api/status?service=YARN"
    assert rendered.command == f"GET {rendered.url}"


def test_http_enum_is_closed_and_token_charset_constrained(registry):
    with pytest.raises(DiagnosticDenied):
        registry.render("example.status", {"service": "not-a-service"}, platform="alpha")
    for bad in ["../../etc/passwd", "a&x=1", "a b", "a/b"]:
        with pytest.raises(DiagnosticDenied):
            registry.render("example.config", {"service": "hdfs", "key": bad}, platform="alpha")


def test_diagnostic_auth_is_rejected(tmp_path):
    """诊断不再持有上游账号:带认证的外部系统一律经插件接入。"""
    path = tmp_path / "diagnostics.yaml"
    path.write_text(HTTP_DIAGNOSTICS.replace(
        "    timeout_seconds: 10\n  - id: example.config",
        "    auth: manager\n    timeout_seconds: 10\n  - id: example.config",
    ), encoding="utf-8")

    with pytest.raises(DiagnosticConfigError, match="auth"):
        DiagnosticRegistry.load(path)

def test_http_url_is_mutually_exclusive_with_ssh_templates(tmp_path):
    path = tmp_path / "diagnostics.yaml"
    path.write_text(
        """
version: 1
diagnostics:
  - id: invalid.http
    platforms: [alpha]
    transport: http
    params: {}
    command: uptime
    url_template: http://example.test/
    timeout_seconds: 10
approved_actions: []
""",
        encoding="utf-8",
    )

    with pytest.raises(DiagnosticConfigError, match="互斥|只能"):
        DiagnosticRegistry.load(path)


def test_missing_demo_endpoint_is_denied_at_render_time(tmp_path):
    as_env_pack(tmp_path)
    config = tmp_path / "config"
    (config / "platform-alpha.yaml").write_text(
        "platform: alpha\ncluster_name: c\nendpoints: {}\nnodes: {}\n",
        encoding="utf-8",
    )
    (config / "diagnostics.yaml").write_text(
        """
version: 1
diagnostics:
  - id: missing.endpoint
    platforms: [alpha]
    transport: http
    params: {}
    url_template: "{endpoint:manager}/api/v1/clusters/{cluster}"
    timeout_seconds: 10
approved_actions: []
""",
        encoding="utf-8",
    )
    loaded = DiagnosticRegistry.load(config / "diagnostics.yaml")

    with pytest.raises(DiagnosticDenied, match="manager"):
        loaded.render("missing.endpoint", {}, platform="alpha")


@pytest.mark.parametrize("status", [401, 500])
def test_http_error_is_readable(
    http_gateway, monkeypatch, status
):
    gateway, session = http_gateway
    monkeypatch.setattr(
        gateway,
        "_http_get",
        lambda _url, _timeout: (status, b"request rejected"),
    )

    result = run_status(gateway, session)

    assert f"HTTP {status}" in result["stderr"]


def test_large_http_error_keeps_status_while_marking_truncation(
    http_gateway, monkeypatch
):
    gateway, session = http_gateway
    monkeypatch.setattr(
        gateway,
        "_http_get",
        lambda _url, _timeout: (503, b"x" * (1024 * 1024 + 1)),
    )

    result = run_status(gateway, session)

    assert "HTTP 503" in result["stderr"]
    assert result["truncated"] is True


def test_http_response_value_is_redacted_before_return(
    http_gateway, monkeypatch
):
    gateway, session = http_gateway
    payload = {
        "items": [
            {
                "configurations": [
                    {
                        "type": "yarn-site",
                        "properties": {"db.password": FAKE_PASSWORD},
                    }
                ]
            }
        ]
    }
    monkeypatch.setattr(
        gateway,
        "_http_get",
        lambda _url, _timeout: (200, json.dumps(payload).encode()),
    )

    result = run_status(gateway, session)

    assert FAKE_PASSWORD not in result["stdout"]
    assert REDACTED in result["stdout"]


def test_incomplete_http_response_becomes_readable_result(
    http_gateway, monkeypatch
):
    gateway, session = http_gateway
    monkeypatch.setattr(
        gateway,
        "_http_get",
        lambda _url, _timeout: (_ for _ in ()).throw(
            http.client.IncompleteRead(b"partial")
        ),
    )

    result = run_status(gateway, session)

    assert "HTTP response invalid" in result["stderr"]


def test_http_get_uses_an_opener_that_does_not_follow_redirects(monkeypatch, tmp_path):
    seen = {}

    class Response:
        status = 302

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size):
            return b"redirect"

    class Opener:
        def open(self, _request, timeout):
            seen["timeout"] = timeout
            return Response()

    def build_opener(*handlers):
        seen["handlers"] = handlers
        return Opener()

    monkeypatch.setattr(
        "lib.triage_agent.actions.urlrequest.build_opener", build_opener
    )
    monkeypatch.setattr(
        "lib.triage_agent.actions.urlrequest.urlopen",
        lambda *_args, **_kwargs: pytest.fail("urlopen may follow redirects"),
    )
    gateway = ActionGateway(
        AppPaths.from_repo_root(tmp_path), DiagnosticRegistry({}, [])
    )

    status, body = gateway._http_get("http://example.test", 3)

    assert (status, body) == (302, b"redirect")
    assert seen["handlers"]


def test_http_recording_redacts_response_secrets(http_gateway, monkeypatch):
    gateway, session = http_gateway
    payload = {
        "items": [
            {
                "configurations": [
                    {
                        "type": "yarn-site",
                        "properties": {"db.password": FAKE_PASSWORD},
                    }
                ]
            }
        ]
    }
    monkeypatch.setattr(
        gateway,
        "_http_get",
        lambda _url, _timeout: (200, json.dumps(payload).encode()),
    )

    run_status(gateway, session)

    saved = (session.dir / "session.json").read_text(encoding="utf-8")
    assert FAKE_PASSWORD not in saved
    assert REDACTED in saved


def test_rendered_diagnostic_http_fields_have_backward_compatible_defaults():
    rendered = RenderedDiagnostic("id", "uptime", 10)

    names = [field.name for field in fields(RenderedDiagnostic)]
    assert names[-3:] == ["transport", "url", "extract"]
    assert rendered.transport == "ssh"
    assert rendered.url == ""
    assert rendered.extract is None


def test_command_display_distinguishes_different_config_keys(registry):
    """同一 service 下查不同参数,URL 完全相同,展示串必须靠 extract 区分。

    records 里的 cmd 是人复核证据的唯一线索,两次不同查询记成一样等于无法复核。
    """
    memory = registry.render(
        "example.config",
        {"service": "yarn", "key": "yarn.nodemanager.resource.memory-mb"},
        platform="alpha",
    )
    vcores = registry.render(
        "example.config",
        {"service": "yarn", "key": "yarn.nodemanager.resource.cpu-vcores"},
        platform="alpha",
    )
    assert memory.url == vcores.url, "前提:同 service 下 URL 本就相同"
    assert memory.command != vcores.command
    assert "yarn.nodemanager.resource.memory-mb" in memory.command
    assert "yarn.nodemanager.resource.cpu-vcores" in vcores.command


def test_http_get_disables_proxy_without_touching_environment(monkeypatch, tmp_path):
    """禁代理必须走 opener,不得改 NO_PROXY 环境变量。

    常驻中台里每次请求都并入 NO_PROXY 会把启动时设的 "*" 退化成具名列表。
    """
    import os
    from urllib import request as urlrequest

    handlers = []
    original_build_opener = urlrequest.build_opener

    def spy_build_opener(*args):
        handlers.extend(args)
        return original_build_opener(*args)

    monkeypatch.setattr(urlrequest, "build_opener", spy_build_opener)
    monkeypatch.setitem(os.environ, "NO_PROXY", "*")
    monkeypatch.setitem(os.environ, "no_proxy", "*")

    gateway = ActionGateway(AppPaths.from_repo_root(tmp_path), DiagnosticRegistry({}, []))
    try:
        gateway._http_get("http://127.0.0.1:9/api/v1/x", 0.05)
    except Exception:
        pass   # 连不通无所谓,只验证 opener 构造与 env 不被修改

    assert any(
        isinstance(handler, urlrequest.ProxyHandler) for handler in handlers
    ), "必须显式传 ProxyHandler({}) 禁代理"
    assert os.environ["NO_PROXY"] == "*", "NO_PROXY 不得被请求路径修改"


def test_extract_wraps_only_properties_paths():
    """properties 特例按字面量段名匹配,钉住边界防未来诊断被静默改形状。"""
    from lib.triage_agent.actions import _extract_json

    payload = {"items": [{"configurations": [
        {"type": "yarn-site", "properties": {"a.b": "42"}},
    ]}]}
    wrapped = _extract_json(
        payload, "/items/*/configurations/type=yarn-site/properties/a.b"
    )
    assert wrapped == {"a.b": "42"}, "properties 下的末段回包成 {key: value}"

    plain = {"items": [{"settings": {"a.b": "42"}}]}
    assert _extract_json(plain, "/items/*/settings/a.b") == "42", (
        "非 properties 路径保持裸值,不被特例波及"
    )
