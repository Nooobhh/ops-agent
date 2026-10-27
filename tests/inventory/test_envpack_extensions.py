import sys
from pathlib import Path

import pytest

from lib.envpack import EnvPackError, parse_env_pack
from lib.envpack.extensions import extension_decl, load_object

PACK = """\
version: 1
id: lab
display_name: Lab
timezone: UTC
python: py
extensions:
  node_credentials: lab_ext_probe.creds:PROVIDER.name
clusters: []
"""


@pytest.fixture
def pack(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))  # 导入路径改动只留在本用例
    root = tmp_path / "pack"
    (root / "py" / "lab_ext_probe").mkdir(parents=True)
    (root / "py" / "lab_ext_probe" / "__init__.py").write_text("", encoding="utf-8")
    (root / "py" / "lab_ext_probe" / "creds.py").write_text(
        "class PROVIDER:\n    name = 'lab-creds'\n", encoding="utf-8",
    )
    (root / "env.yaml").write_text(PACK, encoding="utf-8")
    yield parse_env_pack(root)
    for name in [key for key in sys.modules if key.startswith("lab_ext_probe")]:
        del sys.modules[name]


def test_declared_extension_resolves_inside_pack(pack):
    assert extension_decl(pack, "node_credentials") == "lab_ext_probe.creds:PROVIDER.name"
    assert extension_decl(pack, "graphs") is None
    assert load_object(pack, extension_decl(pack, "node_credentials")) == "lab-creds"
    assert str(pack.python_dir) in sys.path and sys.path[-1] == str(pack.python_dir)


@pytest.mark.parametrize("ref, message", [
    ("lab_ext_probe.creds", "模块:对象"),
    ("lab_ext_probe.missing:x", "导入失败"),
    ("lab_ext_probe.creds:NOPE", "不存在属性"),
    ("json:dumps", "环境包之外"),            # 与核心/标准库重名的模块不得冒充扩展
])
def test_bad_references_fail_explicitly(pack, ref, message):
    with pytest.raises(EnvPackError, match=message):
        load_object(pack, ref)


def test_unknown_point_and_missing_python_dir(pack, tmp_path):
    with pytest.raises(EnvPackError, match="未知扩展点"):
        extension_decl(pack, "bastion")
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "env.yaml").write_text("version: 1\nid: bare\ntimezone: UTC\nclusters: []\n", encoding="utf-8")
    with pytest.raises(EnvPackError, match="未声明 python"):
        load_object(parse_env_pack(bare), "x:y")
