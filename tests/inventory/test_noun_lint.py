import zipfile
from pathlib import Path

import pytest

from lib.envpack import EnvPackError, load_env_pack, parse_env_pack
from lib.envpack.noun_lint import builtin_terms, compare, load_terms, render_text, scan

REPO = Path(__file__).resolve().parents[2]


def _pack(tmp_path: Path, terms: str) -> Path:
    pack = tmp_path / "pack"
    (pack / "lint").mkdir(parents=True)
    (pack / "env.yaml").write_text("version: 1\nid: lab\ntimezone: UTC\nclusters: []\n", encoding="utf-8")
    (pack / "lint" / "nouns.yaml").write_text(terms, encoding="utf-8")
    return pack


TERMS = """\
version: 1
terms:
  - {id: keys, label: 平台键, pattern: '(?<![A-Za-z0-9_])["'']alpha["'']'}
  - {id: vendor, label: Vendor, pattern: '(?i)acme'}
"""


def test_core_carries_no_environment_nouns_or_secrets():
    """闸门:核心仓入库文件(envs/ 除外)零命中环境词表与通用敏感模式。"""
    terms = [*load_terms(load_env_pack(REPO)), *builtin_terms()]
    report = scan(REPO, terms)
    offenders = [f"{item['where']}:{item['line']} {item['term']}"
                 for entry in report["files"] for item in entry["matches"]]
    assert report["scanned_files"] > 300 and offenders == []


def test_scan_counts_terms_skips_packs_and_vendored(tmp_path):
    terms = load_terms(parse_env_pack(_pack(tmp_path, TERMS)))
    repo = tmp_path / "repo"
    (repo / "lib").mkdir(parents=True)
    (repo / "lib" / "a.py").write_text('KEY = "alpha"\n# ACME and acme\nalpha_var = 1\n', encoding="utf-8")
    (repo / "web" / "src" / "node_modules").mkdir(parents=True)
    (repo / "web" / "src" / "node_modules" / "x.js").write_text("acme", encoding="utf-8")
    (repo / "envs" / "lab").mkdir(parents=True)
    (repo / "envs" / "lab" / "env.yaml").write_text("acme", encoding="utf-8")
    (repo / "web" / "src" / "b.ts").write_text("const v = 'alpha' // acme\nacme // noun-lint: allow\n",
                                               encoding="utf-8")

    report = scan(repo, terms)
    assert report["scanned_files"] == 2
    assert {item["id"]: (item["files"], item["hits"]) for item in report["terms"]} == {
        "keys": (2, 2), "vendor": (2, 3),
    }
    first = report["files"][0]
    assert (first["path"], first["hits"], first["terms"]) == ("lib/a.py", 3, {"keys": 1, "vendor": 2})
    assert [(item["line"], item["term"]) for item in first["matches"]] == [(1, "keys"), (2, "vendor"), (2, "vendor")]
    assert "平台键" in render_text(report) and "lib/a.py:2" in render_text(report, details=True)
    assert scan(repo, terms, exclude=("web",))["scanned_files"] == 1

    (repo / "lib" / "a.py").write_text('KEY = "alpha"\n', encoding="utf-8")
    (repo / "lib" / "c.py").write_text('acme acme', encoding="utf-8")
    comparison = compare(scan(repo, terms), report)
    assert comparison["grown_files"] == [{"path": "lib/c.py", "hits": 2, "baseline": 0}]
    assert comparison["grown_terms"] == []


def test_office_documents_are_unpacked_and_scanned(tmp_path):
    terms = load_terms(parse_env_pack(_pack(tmp_path, TERMS)))
    repo = tmp_path / "repo"
    repo.mkdir()
    with zipfile.ZipFile(repo / "t.docx", "w") as archive:
        archive.writestr("word/document.xml", "<w:t>ACME 集团周报</w:t>")
        archive.writestr("word/media/a.png", b"\x89PNG acme")
    report = scan(repo, terms)
    assert [(item["where"], item["term"]) for item in report["files"][0]["matches"]] == [
        ("t.docx!word/document.xml", "vendor"),
    ]


@pytest.mark.parametrize("text, expected", [
    ("host 10.1.2.3 and 172.20.0.9 and 192.168.1.1 and 100.64.0.1", ["private-ip"] * 4),  # noun-lint: allow 正例
    ("doc ranges 192.0.2.10 198.51.100.7 203.0.113.9 127.0.0.1 100.0.0.1", []),  # noun-lint: allow 正例
    ("mail ops@corp.io", ["email"]),  # noun-lint: allow 正例
    ("noreply 1+bot@users.noreply.github.com and admin@example.org", []),
    ("Copyright 2017 Someone <someone@gmail.com>", []),
    ('password: "Zq8vT2mLw9xR"', ["credential"]),  # noun-lint: allow 正例
    ('api_key: "replace-in-local.yaml"  autoComplete={x ? "new-password" : "username"}', []),
    ('token: "unit-test-token"  password: "a-password"', []),
    ("token ghp_" + "a" * 36, ["credential"]),
    ("-----BEGIN OPENSSH PRIVATE KEY-----", ["credential"]),  # noun-lint: allow 正例
])
def test_builtin_sensitive_patterns(tmp_path, text, expected):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.md").write_text(text + "\n", encoding="utf-8")
    report = scan(repo, builtin_terms())
    assert [item["term"] for entry in report["files"] for item in entry["matches"]] == expected


def test_invalid_term_rejected(tmp_path):
    pack = parse_env_pack(_pack(tmp_path, "version: 1\nterms:\n  - {id: bad, pattern: '('}\n"))
    with pytest.raises(EnvPackError, match="正则"):
        load_terms(pack)
