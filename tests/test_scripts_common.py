import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import _common


def test_emit_ok_prints_json_and_returns_zero(capsys):
    rc = _common.emit_ok(foo=1, bar="中文")
    raw = capsys.readouterr().out
    assert "中文" in raw          # ensure_ascii=False:中文字面输出,不是 \u 转义
    out = json.loads(raw)
    assert rc == 0
    assert out == {"ok": True, "foo": 1, "bar": "中文"}


def test_emit_err_prints_json_and_returns_one(capsys):
    rc = _common.emit_err("bad_args", "缺参数", cmd="x")
    raw = capsys.readouterr().out
    assert "缺参数" in raw        # ensure_ascii=False:中文字面输出,不是 \u 转义
    out = json.loads(raw)
    assert rc == 1
    assert out == {"ok": False, "error": "bad_args", "message": "缺参数", "cmd": "x"}
