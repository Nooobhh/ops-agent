from pathlib import Path

import pytest

from lib.cmd_filter import CmdFilter, Level, default_rules_path, effective_rules, redline_sources
from lib.envpack import load_env_pack
from lib.kb.redlines import check_redlines_hash, rules_sha256
from tests.envpack_helpers import write_redlines

REPO = Path(__file__).resolve().parents[1]
RULES = default_rules_path()


def test_hash_matches_effective_rules(tmp_path):
    """redlines.md 的 filter_rules_sha256 等于有效规则(核心 + 环境包红线补充)的 sha 即通过。"""
    redlines = write_redlines(tmp_path / "redlines.md")
    assert check_redlines_hash(redlines, redline_sources(load_env_pack(REPO))) is None


def test_hash_mismatch_reported(tmp_path):
    """人为改一个字节让 rules 变化,应报错。"""
    fake_rules = tmp_path / "filter_rules.yaml"
    fake_rules.write_text(RULES.read_text(encoding="utf-8") + "\n# mutated\n",
                          encoding="utf-8")
    err = check_redlines_hash(write_redlines(tmp_path / "redlines.md"), [fake_rules])
    assert err is not None and "sha256" in err.lower()


def test_missing_frontmatter_field_reported(tmp_path):
    """redlines.md 缺 filter_rules_sha256 字段应报错。"""
    fake = tmp_path / "redlines.md"
    fake.write_text(
        "---\ntitle: x\ntype: procedure\nplatform: shared\nservice: system\n"
        "status: draft\n---\n# 正文\n",
        encoding="utf-8",
    )
    err = check_redlines_hash(fake, [RULES])
    assert err is not None and "sha256" in err.lower()


def test_rules_hash_of_core_only_equals_file_sha_and_supplement_changes_it(tmp_path):
    import hashlib

    supplement = tmp_path / "redlines.yaml"
    supplement.write_text("write_patterns:\n  - '\\bdemo-ctl\\s+stop\\b'\n", encoding="utf-8")
    assert rules_sha256([RULES]) == hashlib.sha256(RULES.read_bytes()).hexdigest()
    assert rules_sha256([RULES, supplement]) != rules_sha256([RULES])


def _pack_with_supplement(tmp_path, body):
    from tests.envpack_helpers import as_env_pack

    root = as_env_pack(tmp_path)
    (root / "policy").mkdir()
    (root / "policy" / "redlines.yaml").write_text(body, encoding="utf-8")
    return load_env_pack(root)


def test_env_pack_supplement_tightens_rules(tmp_path):
    pack = _pack_with_supplement(tmp_path, "write_patterns:\n  - '\\bdemo-ctl\\b[^|;&\\n]*\\bstop\\b'\n")
    assert redline_sources(pack)[-1] == pack.root / "policy" / "redlines.yaml"
    merged = CmdFilter.default(pack)
    assert merged.classify("demo-ctl stop").level is Level.WRITE
    assert merged.classify("demo-ctl status").level is Level.READONLY
    assert CmdFilter.from_yaml(RULES).classify("demo-ctl stop").level is Level.READONLY


def test_env_pack_supplement_cannot_relax_rules(tmp_path):
    pack = _pack_with_supplement(tmp_path, "allow_patterns:\n  - '.*'\n")
    with pytest.raises(ValueError, match="只许收紧"):
        effective_rules(pack)


def test_pack_without_supplement_uses_core_rules_only(tmp_path):
    from tests.envpack_helpers import as_env_pack

    pack = load_env_pack(as_env_pack(tmp_path))
    assert redline_sources(pack) == [RULES]
