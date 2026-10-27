import json
import logging

import httpx
import pytest

from scripts import model_probe
from lib.triage_agent.model_client import (
    ModelConfig,
    ModelProtocolError,
    ModelTransientError,
    OpenAICompatibleClient,
    load_model_config,
    probe_native_tools,
)


def _tool_response(arguments='{"diagnostic_id":"system.memory_summary","params":{}}'):
    return {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "run_diagnostic",
                        "arguments": arguments,
                    },
                }],
            },
        }],
    }


def test_load_model_config_requires_openai_compatible_and_hides_key(tmp_path):
    path = tmp_path / "model.yaml"
    path.write_text(
        "provider: openai_compatible\n"
        "base_url: https://relay.test/v1\n"
        "api_key: secret-value\n"
        "model: test-model\n",
        encoding="utf-8",
    )

    config = load_model_config(path)

    assert config.model == "test-model"
    assert config.api_key.get_secret_value() == "secret-value"
    assert "secret-value" not in repr(config)


def test_load_model_config_rejects_unknown_provider(tmp_path):
    path = tmp_path / "model.yaml"
    path.write_text(
        "provider: other\nbase_url: https://relay.test/v1\n"
        "api_key: secret\nmodel: test-model\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="openai_compatible"):
        load_model_config(path)


def test_load_model_config_uses_manual_default_from_registry(tmp_path):
    path = tmp_path / "model.yaml"
    path.write_text(
        "schema_version: 1\n"
        "models:\n"
        "  - id: model-1\n"
        "    title: One\n"
        "    provider: openai_compatible\n"
        "    base_url: https://one.test/v1\n"
        "    api_key: secret-one\n"
        "    model: model-one\n"
        "    default_reasoning_effort: low\n"
        "  - id: model-2\n"
        "    title: Two\n"
        "    provider: openai_compatible\n"
        "    base_url: https://two.test/v1\n"
        "    api_key: secret-two\n"
        "    model: model-two\n"
        "    default_reasoning_effort: xhigh\n"
        "defaults:\n"
        "  auto: {model_id: model-1, reasoning_effort: low}\n"
        "  manual: {model_id: model-2, reasoning_effort: high}\n"
        "timeout_seconds: 45\n"
        "max_tool_turns: 9\n",
        encoding="utf-8",
    )

    config = load_model_config(path)

    assert config.base_url == "https://two.test/v1"
    assert config.api_key.get_secret_value() == "secret-two"
    assert config.model == "model-two"
    assert config.reasoning_effort == "high"
    assert config.timeout_seconds == 45
    assert config.max_tool_turns == 9


def test_complete_parses_native_tool_call_and_preserves_assistant_message():
    def handler(request):
        assert request.headers["authorization"] == "Bearer secret"
        body = json.loads(request.content)
        assert body["model"] == "test-model"
        return httpx.Response(200, json=_tool_response())

    client = OpenAICompatibleClient(
        ModelConfig(
            base_url="https://relay.test/v1",
            api_key="secret",
            model="test-model",
        ),
        transport=httpx.MockTransport(handler),
    )

    reply = client.complete(
        [{"role": "user", "content": "inspect"}],
        [{"type": "function", "function": {
            "name": "run_diagnostic",
            "parameters": {"type": "object"},
        }}],
    )

    assert reply.tool_calls[0].name == "run_diagnostic"
    assert reply.tool_calls[0].arguments == {
        "diagnostic_id": "system.memory_summary",
        "params": {},
    }
    assert reply.assistant_message["tool_calls"][0]["id"] == "call-1"


def test_complete_logs_request_lifecycle_without_prompt_or_key(caplog):
    client = OpenAICompatibleClient(
        ModelConfig(
            base_url="https://relay.test/v1",
            api_key="top-secret",
            model="test-model",
        ),
        transport=httpx.MockTransport(lambda _request: httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
        )),
        context={"task_id": "tr-test", "client_role": "agent"},
    )

    with caplog.at_level(logging.INFO, logger="ops_agent.triage.model"):
        client.complete([{"role": "user", "content": "private prompt"}], [])

    logs = "\n".join(record.message for record in caplog.records)
    assert "model_request_start" in logs
    assert "model_request_response" in logs
    assert "tr-test" in logs
    assert "test-model" in logs
    assert "private prompt" not in logs
    assert "top-secret" not in logs


def test_complete_logs_connect_timeout_phase(caplog):
    client = OpenAICompatibleClient(
        ModelConfig(base_url="https://relay.test/v1", api_key="secret", model="m"),
        transport=httpx.MockTransport(
            lambda _request: (_ for _ in ()).throw(httpx.ConnectTimeout("unreachable")),
        ),
    )

    with caplog.at_level(logging.INFO, logger="ops_agent.triage.model"):
        with pytest.raises(ModelProtocolError, match="ConnectTimeout"):
            client.complete([], [])

    assert '"event": "model_request_error"' in caplog.text
    assert '"phase": "connect"' in caplog.text


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh"])
def test_complete_sends_configured_reasoning_effort(effort):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{
                    "message": {"role": "assistant", "content": "ok"},
                }],
            },
        )

    client = OpenAICompatibleClient(
        ModelConfig(
            base_url="https://relay.test/v1",
            api_key="secret",
            model="m",
            reasoning_effort=effort,
        ),
        transport=httpx.MockTransport(handler),
    )

    client.complete([], [])

    assert bodies[0]["reasoning_effort"] == effort


def test_complete_omits_reasoning_effort_when_unspecified():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{
                    "message": {"role": "assistant", "content": "ok"},
                }],
            },
        )

    client = OpenAICompatibleClient(
        ModelConfig(
            base_url="https://relay.test/v1",
            api_key="secret",
            model="m",
            reasoning_effort="none",
        ),
        transport=httpx.MockTransport(handler),
    )

    client.complete([], [])

    assert "reasoning_effort" not in bodies[0]


@pytest.mark.parametrize("arguments", ["free -m", "[]", '"text"'])
def test_complete_rejects_non_object_tool_arguments(arguments):
    client = OpenAICompatibleClient(
        ModelConfig(base_url="https://relay.test/v1", api_key="secret", model="m"),
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=_tool_response(arguments)),
        ),
    )

    with pytest.raises(ModelProtocolError, match="arguments"):
        client.complete([], [])


def test_complete_rejects_empty_assistant_response():
    client = OpenAICompatibleClient(
        ModelConfig(base_url="https://relay.test/v1", api_key="secret", model="m"),
        transport=httpx.MockTransport(lambda _request: httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": None}}]},
        )),
    )

    with pytest.raises(ModelProtocolError, match="content"):
        client.complete([], [])


def test_http_error_does_not_expose_api_key():
    client = OpenAICompatibleClient(
        ModelConfig(
            base_url="https://relay.test/v1",
            api_key="top-secret",
            model="m",
        ),
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(401, json={"error": "bad key"}),
        ),
    )

    with pytest.raises(ModelProtocolError) as caught:
        client.complete([], [])

    assert "top-secret" not in str(caught.value)


@pytest.mark.parametrize("status,body,expected", [
    (403, {"code": "INSUFFICIENT_BALANCE",
           "message": "Insufficient account balance"},
     "Insufficient account balance"),
    (429, {"error": {"message": "rate limit exceeded"}}, "rate limit exceeded"),
    (502, "upstream is down", "upstream is down"),
])
def test_http_error_carries_the_status_and_reason(status, body, expected):
    """只报 HTTPStatusError 的话,余额不足/key 失效/限流在任务详情里长得
    一模一样,排障得另开一路探测才知道原因。"""
    kwargs = {"json": body} if isinstance(body, dict) else {"text": body}
    client = OpenAICompatibleClient(
        ModelConfig(base_url="https://relay.test/v1", api_key="k", model="m"),
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(status, **kwargs),
        ),
    )

    with pytest.raises(ModelProtocolError) as caught:
        client.complete([], [])

    assert f"HTTP {status}" in str(caught.value)
    assert expected in str(caught.value)


def test_probe_requires_tool_call_and_followup_text():
    class FakeClient:
        def __init__(self):
            self.calls = []

        def complete(self, messages, tools):
            self.calls.append((messages, tools))
            if len(self.calls) == 1:
                return type("Reply", (), {
                    "content": None,
                    "tool_calls": [type("Call", (), {
                        "call_id": "probe-1",
                        "name": "return_probe",
                        "arguments": {"token": "ops-agent-probe"},
                    })()],
                    "assistant_message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": "probe-1",
                            "type": "function",
                            "function": {
                                "name": "return_probe",
                                "arguments": '{"token":"ops-agent-probe"}',
                            },
                        }],
                    },
                })()
            return type("Reply", (), {
                "content": "probe complete",
                "tool_calls": [],
                "assistant_message": {
                    "role": "assistant",
                    "content": "probe complete",
                },
            })()

    client = FakeClient()

    result = probe_native_tools(client)

    assert result == {"native_tools": True, "tool_result_roundtrip": True}
    assert client.calls[1][0][-1]["role"] == "tool"
    assert client.calls[1][0][-1]["tool_call_id"] == "probe-1"


def test_probe_cli_emits_single_json_object(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "model.yaml"
    config_path.write_text(
        "provider: openai_compatible\nbase_url: https://relay.test/v1\n"
        "api_key: secret\nmodel: m\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        model_probe,
        "probe_native_tools",
        lambda _client: {"native_tools": True, "tool_result_roundtrip": True},
    )

    assert model_probe.run(["--config", str(config_path)]) == 0

    assert json.loads(capsys.readouterr().out) == {
        "ok": True,
        "native_tools": True,
        "tool_result_roundtrip": True,
    }


def test_timeouts_raise_retryable_timeout_error_and_other_failures_do_not():
    def timeout(request):
        raise httpx.ReadTimeout("timed out", request=request)

    config = ModelConfig(
        base_url="https://relay.test/v1", api_key="secret", model="m",
        reasoning_effort="none",
    )
    with pytest.raises(ModelTransientError, match="ReadTimeout"):
        OpenAICompatibleClient(
            config, transport=httpx.MockTransport(timeout),
        ).complete([], [])

    with pytest.raises(ModelProtocolError) as excinfo:
        OpenAICompatibleClient(
            config,
            transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
        ).complete([], [])
    assert not isinstance(excinfo.value, ModelTransientError)


@pytest.mark.parametrize("respond", [
    lambda request: (_ for _ in ()).throw(httpx.RemoteProtocolError("Server disconnected", request=request)),
    lambda request: (_ for _ in ()).throw(httpx.ConnectError("refused", request=request)),
    lambda request: httpx.Response(504, text="<html><head><title>504 Gateway Time-out</title></head><body>nginx</body></html>"),
    lambda request: httpx.Response(503, json={"error": {"message": "upstream busy"}}),
    lambda request: httpx.Response(429, json={"error": {"message": "rate limited"}}),
])
def test_dropped_connections_and_gateway_errors_are_transient(respond):
    config = ModelConfig(
        base_url="https://relay.test/v1", api_key="secret", model="m",
        reasoning_effort="none",
    )
    with pytest.raises(ModelTransientError) as excinfo:
        OpenAICompatibleClient(config, transport=httpx.MockTransport(respond)).complete([], [])
    assert "<html" not in str(excinfo.value)


def test_gateway_html_error_page_is_reduced_to_its_title():
    config = ModelConfig(
        base_url="https://relay.test/v1", api_key="secret", model="m",
        reasoning_effort="none",
    )
    page = "<html><head><title>504 Gateway Time-out</title></head><body><h1>504</h1></body></html>"
    with pytest.raises(ModelTransientError) as excinfo:
        OpenAICompatibleClient(
            config, transport=httpx.MockTransport(lambda _r: httpx.Response(504, text=page)),
        ).complete([], [])
    assert str(excinfo.value) == "模型请求失败: HTTP 504 504 Gateway Time-out"


def _chat(content, tool_calls=None):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return lambda _request: httpx.Response(200, json={"choices": [{"message": message}]})
