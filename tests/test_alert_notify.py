import json
import base64
import hashlib
import hmac
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from lib.alerts.model import AlertEvent
from lib.alerts.notify import (
    MAX_FLUSH_PER_TICK,
    MAX_MARKDOWN_BYTES,
    BaseNotifier,
    FeishuNotifier,
    MultiNotifier,
    WecomNotifier,
    render_event_block,
)


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def event():
    return AlertEvent(
        source="prometheus",
        platform="alpha",
        alertname="cpu_hot",
        severity="critical",
        host="node1",
        host_raw="198.51.100.101:9100",
        service="system",
        state="firing",
        detail="hot",
    )


def test_render_event_block_locks_first_line_and_handoff_line():
    entry = {
        "first_seen": "2026-07-16T08:00:00+00:00",
        "class": "watch",
        "class_reason": "default",
    }
    outcome = SimpleNamespace(
        run_id="al-20260716-0800-abcd",
        matched_doc="knowledge/alpha/system/cpu.md",
        verdict="needs_llm",
        severity="P1",
        conclusion_hint="cpu pressure",
        failed_steps=[{"name": "load", "actual": "9", "expect": "<4"}],
        status="done",
        error=None,
    )

    block = render_event_block(event(), entry, outcome)
    first_line = block.splitlines()[0]

    # 首行契约:含 severity·platform 段 + alertname + host(markdown 装饰不影响子串)
    assert first_line.startswith("【")
    assert "CRITICAL·alpha" in first_line
    assert "cpu_hot" in first_line
    assert "node1" in first_line
    assert "接手: records/troubleshoot/al-20260716-0800-abcd" in block
    assert "✗ load" in block and "9" in block and "<4" in block


def test_dispatch_paginates_blocks_by_utf8_byte_limit(tmp_path, monkeypatch):
    sent = []
    notifier = WecomNotifier("key", tmp_path / "queue.jsonl")
    monkeypatch.setattr(notifier, "send_markdown", lambda content: sent.append(content) or True)

    notifier.dispatch(["a" * 1500, "b" * 1500, "c" * 1500])

    assert len(sent) == 2
    assert sent[0].startswith("(第 1/2 页)\n")
    assert sent[1].startswith("(第 2/2 页)\n")
    assert all(len(content.encode("utf-8")) <= MAX_MARKDOWN_BYTES for content in sent)


def test_dispatch_truncates_single_oversized_block_preserving_first_and_handoff(tmp_path, monkeypatch):
    sent = []
    notifier = WecomNotifier("key", tmp_path / "queue.jsonl")
    monkeypatch.setattr(notifier, "send_markdown", lambda content: sent.append(content) or True)
    block = "\n".join([
        "【CRITICAL·alpha】big_alert @ node1",
        "detail " + ("x" * 8000),
        "接手: records/troubleshoot/al-20260716-0800-abcd",
    ])

    notifier.dispatch([block])

    assert len(sent) == 1
    assert sent[0].splitlines()[0] == "【CRITICAL·alpha】big_alert @ node1"
    assert "接手: records/troubleshoot/al-20260716-0800-abcd" in sent[0]
    assert "..." in sent[0]
    assert len(sent[0].encode("utf-8")) <= MAX_MARKDOWN_BYTES


def test_dispatch_counts_page_header_in_payload_budget(tmp_path, monkeypatch):
    sent = []
    notifier = WecomNotifier("key", tmp_path / "queue.jsonl")
    monkeypatch.setattr(notifier, "send_markdown", lambda content: sent.append(content) or True)
    large = "\n".join([
        "【WARNING·alpha】large @ node1",
        "x" * 4080,
        "接手: records/troubleshoot/al-20260716-0800-abcd",
    ])

    notifier.dispatch(["small", large])

    assert len(sent) == 2
    assert all(len(content.encode("utf-8")) <= MAX_MARKDOWN_BYTES for content in sent)


def test_failed_send_is_queued_then_flushed_and_expired_items_are_dropped(tmp_path, monkeypatch):
    queue = tmp_path / "queue.jsonl"
    notifier = WecomNotifier("key", queue)
    sends = iter([False, True, True])
    monkeypatch.setattr(notifier, "send_markdown", lambda content: next(sends))

    notifier.dispatch(["first"])
    assert queue.exists()
    assert "first" in queue.read_text(encoding="utf-8")

    old_ts = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    with queue.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": old_ts, "content": "expired"}, ensure_ascii=False) + "\n")

    notifier.dispatch(["second"])
    notifier.flush_queue()

    assert queue.read_text(encoding="utf-8") == ""
    assert notifier.dropped_expired == 1


def test_send_markdown_treats_nonzero_errcode_as_failure(monkeypatch, tmp_path):
    requests = []

    def fake_urlopen(req, timeout=0):
        requests.append((req.full_url, json.loads(req.data.decode("utf-8"))))
        return FakeResponse({"errcode": 40001}, status=200)

    monkeypatch.setattr("lib.alerts.notify.request.urlopen", fake_urlopen)

    ok = WecomNotifier("abc", tmp_path / "queue.jsonl").send_markdown("hello")

    assert ok is False
    assert requests[0][0].endswith("key=abc")
    assert requests[0][1] == {
        "msgtype": "markdown",
        "markdown": {"content": "hello"},
    }


def test_feishu_payload_unsigned_and_signed(monkeypatch, tmp_path):
    requests = []

    def fake_urlopen(req, timeout=0):
        requests.append((req.full_url, json.loads(req.data.decode("utf-8")), timeout))
        return FakeResponse({"code": 0, "msg": "ok"})

    monkeypatch.setattr("lib.alerts.notify.request.urlopen", fake_urlopen)
    monkeypatch.setattr("lib.alerts.notify.time.time", lambda: 1599360473)

    assert FeishuNotifier("https://open.feishu.cn/hook/a", "", tmp_path / "q1").send("hello")
    assert requests[-1][1] == {"msg_type": "text", "content": {"text": "hello"}}

    assert FeishuNotifier("https://open.feishu.cn/hook/a", "test", tmp_path / "q2").send("signed")
    payload = requests[-1][1]
    expected = base64.b64encode(
        hmac.new(b"1599360473\ntest", b"", digestmod=hashlib.sha256).digest()
    ).decode("ascii")
    assert payload == {
        "msg_type": "text",
        "content": {"text": "signed"},
        "timestamp": "1599360473",
        "sign": expected,
    }
    assert requests[-1][2] == 10


def test_feishu_dispatch_converts_rendered_markdown_to_plain_text(tmp_path, monkeypatch):
    entry = {"first_seen": "2026-07-16", "class": "watch", "class_reason": "default"}
    block = render_event_block(event(), entry, None)
    sent = []
    notifier = FeishuNotifier("https://open.feishu.cn/hook/a", "", tmp_path / "queue")
    monkeypatch.setattr(notifier, "send", lambda content: sent.append(content) or True)

    notifier.dispatch([block])

    assert len(sent) == 1
    assert "<font" not in sent[0]
    assert "**" not in sent[0]
    assert "`" not in sent[0]
    assert "CRITICAL·alpha" in sent[0]
    assert "cpu_hot" in sent[0]
    assert "node1" in sent[0]
    assert "人工接手建议" in sent[0]


def test_probe_transport_error_never_leaks_endpoint_or_credentials(monkeypatch, tmp_path):
    url = "https://open.feishu.cn/open-apis/bot/v2/hook/token-secret"

    def fail(*args, **kwargs):
        raise TimeoutError(f"failed to reach {url}")

    monkeypatch.setattr("lib.alerts.notify.request.urlopen", fail)
    result = FeishuNotifier(url, "signing-secret", tmp_path / "queue").probe("test")

    assert result == {"ok": False, "code": None, "msg": "网络请求失败: TimeoutError"}
    assert "token-secret" not in result["msg"]
    assert "signing-secret" not in result["msg"]
    assert url not in result["msg"]


class RecordingNotifier(BaseNotifier):
    def __init__(self, queue_path, send_impl):
        super().__init__(queue_path)
        self.send_impl = send_impl

    def send(self, content):
        return self.send_impl(content)

    def probe(self, content):
        return {"ok": self.send(content), "code": 0, "msg": ""}


def test_multi_notifier_isolates_slow_channel_and_uses_two_phases(tmp_path):
    now = datetime.now(timezone.utc).isoformat()
    queue_a = tmp_path / "a.jsonl"
    queue_a.write_text(
        "".join(json.dumps({"ts": now, "content": f"old-{i}"}) + "\n" for i in range(30)),
        encoding="utf-8",
    )
    slow_started = threading.Event()
    release_slow = threading.Event()
    b_sent = threading.Event()
    calls_a = []
    calls_b = []

    def send_a(content):
        calls_a.append(content)
        if content == "new":
            slow_started.set()
            assert release_slow.wait(timeout=5)
        return True

    def send_b(content):
        calls_b.append(content)
        b_sent.set()
        return True

    multi = MultiNotifier([
        RecordingNotifier(queue_a, send_a),
        RecordingNotifier(tmp_path / "b.jsonl", send_b),
    ])
    worker = threading.Thread(target=multi.dispatch, args=(["new"],))
    worker.start()

    assert slow_started.wait(timeout=2)
    assert b_sent.wait(timeout=2)
    assert calls_a == ["new"]
    release_slow.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert calls_b == ["new"]
    assert len(calls_a) - 1 == MAX_FLUSH_PER_TICK
    assert len(queue_a.read_text(encoding="utf-8").splitlines()) == 20


def test_multi_notifier_warns_on_channel_exception_and_continues(tmp_path, monkeypatch, capsys):
    failing = WecomNotifier("credential-must-not-leak", tmp_path / "wecom.jsonl")

    def explode(_blocks):
        raise RuntimeError("https://example.invalid/hook/credential-must-not-leak")

    monkeypatch.setattr(failing, "dispatch", explode)
    sent = []
    healthy = RecordingNotifier(
        tmp_path / "healthy.jsonl",
        lambda content: sent.append(content) or True,
    )

    MultiNotifier([failing, healthy]).dispatch(["new alert"])

    warning = capsys.readouterr().err
    assert "warning:" in warning
    assert "wecom" in warning
    assert "RuntimeError" in warning
    assert "credential-must-not-leak" not in warning
    assert "example.invalid" not in warning
    assert sent == ["new alert"]


def test_multi_notifier_does_not_retry_new_failed_pages_in_same_tick(tmp_path):
    queue = tmp_path / "failed.jsonl"
    sent = []
    notifier = RecordingNotifier(queue, lambda content: sent.append(content) or False)
    blocks = ["a" * 3000, "b" * 3000]

    MultiNotifier([notifier]).dispatch(blocks)

    assert len(sent) == 2
    assert len(set(sent)) == 2
    queued = [json.loads(line)["content"] for line in queue.read_text().splitlines()]
    assert queued == sent


def test_flush_queue_limit_preserves_remainder_and_drops_expired(tmp_path):
    queue = tmp_path / "queue.jsonl"
    now = datetime.now(timezone.utc)
    rows = [
        {"ts": (now - timedelta(hours=25)).isoformat(), "content": "expired"},
        *({"ts": now.isoformat(), "content": f"current-{i}"} for i in range(4)),
    ]
    queue.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    sent = []
    notifier = RecordingNotifier(queue, lambda content: sent.append(content) or True)

    notifier.flush_queue(2)

    assert sent == ["current-0", "current-1"]
    assert notifier.dropped_expired == 1
    assert [json.loads(line)["content"] for line in queue.read_text().splitlines()] == [
        "current-2", "current-3"
    ]

    MultiNotifier([]).dispatch(["ignored"])
    MultiNotifier([]).flush_queue()
