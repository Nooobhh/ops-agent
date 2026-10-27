import inspect
from datetime import datetime, timedelta, timezone

from lib.alerts.model import AlertEvent
from lib.alerts.notify import (
    MAX_MARKDOWN_BYTES,
    _markdown_to_text,
    render_chronic_transition_block,
    render_cluster_wide_block,
    render_host_added_block,
)


def event(**changes):
    values = {
        "source": "prometheus", "platform": "alpha", "alertname": "XX_ALERT",
        "severity": "warning", "host": "nodeA", "host_raw": "nodeA",
        "service": "HDFS", "state": "firing", "detail": "detail",
    }
    values.update(changes)
    return AlertEvent(**values)


def entry(**changes):
    values = {
        "first_seen": "2026-07-23T08:00:00+00:00",
        "class": "chronic",
        "class_reason": "auto_chronic:duration",
    }
    values.update(changes)
    return values


def test_render_chronic_transition_block_first_line():
    block = render_chronic_transition_block(event(), entry(), {})
    assert block.splitlines()[0] == (
        '【<font color="comment">CHRONIC·alpha</font>】'
        "**XX_ALERT** @ `nodeA` 转为常驻"
    )
    assert "2026-07-23T08:00:00+00:00" in block
    assert "不再自动排查" in block
    assert "接手: records/" not in block


def test_render_host_added_block_first_line():
    block = render_host_added_block(event(host="nodeC"), entry(), ["nodeC"], 3)
    assert "XX_ALERT" in block.splitlines()[0]
    assert "新增受影响主机 `nodeC`" in block.splitlines()[0]
    assert "当前受影响 3 台" in block


def test_render_cluster_wide_block_first_line():
    block = render_cluster_wide_block(
        event(), entry(), {"active_count": 12, "denominator": 17, "threshold": 12},
    )
    first = block.splitlines()[0]
    assert '<font color="warning">CLUSTER-WIDE·alpha</font>' in first
    assert "覆盖 12/17 台" in first
    assert "超 70%" in block
    assert "分母 17" in block
    assert "threshold 12" in block


def test_render_chronic_transition_uses_group_first_upgraded_at():
    upgraded = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    block = render_chronic_transition_block(
        event(), entry(first_seen="2026-01-01T00:00:00+00:00"),
        {"first_upgraded_at": upgraded},
    )
    assert upgraded in block
    assert "已存续 2h" in block


def test_render_host_added_multiple_new_hosts():
    block = render_host_added_block(
        event(host="nodeC"), entry(), ["nodeC", "nodeD"], 4,
    )
    assert "`nodeC`、`nodeD`" in block
    assert "新增 2 台" in block


def test_all_three_within_max_bytes():
    long = "警" * 5000
    huge_event = event(alertname=long, detail=long)
    blocks = [
        render_chronic_transition_block(
            huge_event, entry(class_reason=long), {"first_upgraded_at": long},
        ),
        render_host_added_block(huge_event, entry(), [long, long], 2),
        render_cluster_wide_block(
            huge_event, entry(), {"active_count": 7, "denominator": 10, "threshold": 7},
        ),
    ]
    assert all(len(block.encode("utf-8")) <= MAX_MARKDOWN_BYTES for block in blocks)


def test_feishu_text_downgrade_and_signatures():
    blocks = [
        render_chronic_transition_block(event(), entry(), {}),
        render_host_added_block(event(), entry(), ["nodeC"], 3),
        render_cluster_wide_block(
            event(), entry(), {"active_count": 7, "denominator": 10, "threshold": 7},
        ),
    ]
    for block in blocks:
        text = _markdown_to_text(block)
        assert "<font" not in text and "**" not in text and "`" not in text
        assert text.splitlines()[0].startswith("【")
    assert list(inspect.signature(render_chronic_transition_block).parameters) == [
        "event", "entry", "group",
    ]
    assert list(inspect.signature(render_host_added_block).parameters) == [
        "event", "entry", "new_hosts", "active_count",
    ]
    assert list(inspect.signature(render_cluster_wide_block).parameters) == [
        "event", "entry", "cluster_info",
    ]
