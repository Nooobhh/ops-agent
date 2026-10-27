import gzip
import json

import httpx
import pytest

from lib.agent_replay.transport import (
    RecordingTransport,
    ReplayScriptExhausted,
    ScriptedTransport,
    usage_totals,
)

BODY = {
    "model": "m1",
    "choices": [],
    "usage": {
        "prompt_tokens": 10, "completion_tokens": 2,
        "prompt_tokens_details": {"cached_tokens": 4},
        "completion_tokens_details": {"reasoning_tokens": 1},
    },
}


def test_recording_transport_keeps_body_usage_and_decodes_once():
    def handler(request):
        return httpx.Response(
            200,
            headers={"content-encoding": "gzip", "content-type": "application/json"},
            content=gzip.compress(json.dumps(BODY).encode()),
        )

    transport = RecordingTransport(httpx.MockTransport(handler))
    with httpx.Client(transport=transport) as client:
        response = client.post("http://model.test/v1/chat/completions", json={})

    assert response.json()["model"] == "m1"
    assert transport.responses[0]["usage"] == {
        "prompt_tokens": 10, "completion_tokens": 2, "cached_tokens": 4, "reasoning_tokens": 1,
    }
    totals = usage_totals(transport.responses + [{"status_code": 429, "body": None, "usage": {}}])
    assert totals["requests"] == 2 and totals["http_errors"] == 1 and totals["models"] == ["m1"]


def test_scripted_transport_serves_in_order_then_refuses():
    transport = ScriptedTransport([{"status_code": 200, "body": BODY}])
    with httpx.Client(transport=transport) as client:
        assert client.post("http://model.test/v1/chat/completions").json()["model"] == "m1"
        with pytest.raises(ReplayScriptExhausted):
            client.post("http://model.test/v1/chat/completions")
    assert transport.served == 1 and transport.exhausted is True
