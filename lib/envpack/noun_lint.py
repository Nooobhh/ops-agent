"""专有名词 lint:核心仓不应出现的环境专有内容与通用敏感信息。

两段词条:环境包词表(``lint/nouns.yaml``,本环境的公司名、内网地址、集群名、产品名…)与内置通用
敏感模式(私网地址、邮箱、凭据形态,公开核心自己也跑)。扫描面为全部入库文件(拿不到 git 时遍历目录),
环境包目录 ``envs/`` 不扫;docx/xlsx/pptx/zip 解包扫其中文本。行内写 ``noun-lint: allow`` 的行不计。
"""
from __future__ import annotations

import re
import subprocess
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import yaml

from . import EnvPack, EnvPackError

NOUNS_FILE = Path("lint") / "nouns.yaml"
ALLOW_MARKER = "noun-lint: allow"
SKIP_PREFIXES = ("envs/",)
EXCLUDED_PARTS = frozenset({"node_modules", "dist", "__pycache__", ".git", ".venv", "build", "state"})
ARCHIVE_SUFFIXES = frozenset({".docx", ".xlsx", ".pptx", ".zip"})
ARCHIVE_TEXT = re.compile(r"\.(xml|txt|json|md|csv|rels)$")
MAX_TEXT_BYTES = 8_000_000


@dataclass(frozen=True)
class NounTerm:
    id: str
    label: str
    pattern: re.Pattern
    note: str = ""
    skip_line: Optional[re.Pattern] = None   # 命中行同时匹配它则不计(如第三方版权行里的邮箱)


def load_terms(pack: EnvPack) -> List[NounTerm]:
    path = pack.root / NOUNS_FILE
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise EnvPackError(f"{path}: 不可读: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != 1:
        raise EnvPackError(f"{path}: 仅支持 version: 1")
    terms = []
    for index, raw in enumerate(data.get("terms") or []):
        if not isinstance(raw, dict) or not raw.get("id") or not raw.get("pattern"):
            raise EnvPackError(f"{path}: terms[{index}] 需要 id 与 pattern")
        try:
            pattern = re.compile(str(raw["pattern"]))
        except re.error as exc:
            raise EnvPackError(f"{path}: terms[{index}] 正则非法: {exc}") from exc
        terms.append(NounTerm(
            id=str(raw["id"]),
            label=str(raw.get("label") or raw["id"]),
            pattern=pattern,
            note=str(raw.get("note") or ""),
        ))
    return terms


def builtin_terms() -> List[NounTerm]:
    """内置通用敏感模式:放行文档地址段与回环、noreply/example 邮箱;测试占位值用行内 allow 标记。"""
    octet = r"\d{1,3}"
    return [
        NounTerm(
            id="private-ip", label="私网/保留地址",
            pattern=re.compile(
                rf"(?<![\d.])(?:10\.{octet}|172\.(?:1[6-9]|2\d|3[01])|192\.168|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7]))"
                rf"\.{octet}\.{octet}(?![\d])"
            ),
            note="10/8、172.16/12、192.168/16、100.64/10;示例请用 192.0.2.0/24 等文档地址段",
        ),
        NounTerm(
            id="email", label="邮箱",
            pattern=re.compile(
                r"[A-Za-z0-9._%+-]+@(?!users\.noreply\.github\.com\b)(?!example\.)(?!anthropic\.com\b)"
                r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"
            ),
            skip_line=re.compile(r"(?i)copyright|co-authored-by"),
        ),
        NounTerm(
            id="credential", label="凭据形态",
            pattern=re.compile(
                r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
                r"|\b(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16})\b"
                r"""|(?<![\w-])(?i:password|passwd|secret|token|api_?key)["']?\s*[:=]\s*["']"""
                r"""(?![^"'\n]*(?i:secret|passw|token|test|temporary|probe|leak|sample|dummy|"""
                r"""replace|example|changeme|placeholder|your[-_]))[A-Za-z0-9+/=_.\-]{8,}["']"""
            ),
            note="私钥头、GitHub/OpenAI/AWS 令牌、带引号的口令字面量(值里带 secret/test/example 等占位词的除外)",
        ),
    ]


def tracked_files(repo_root: Path) -> Optional[List[str]]:
    """git 入库文件(相对路径);不是 git 仓或没有 git 时返回 None。"""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "-z"], capture_output=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return [item for item in result.stdout.decode("utf-8").split("\0") if item]


def staged_files(repo_root: Path) -> List[str]:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR"],
        capture_output=True, check=True,
    )
    return [item for item in result.stdout.decode("utf-8").split("\0") if item]


def _walk(repo_root: Path) -> List[str]:
    return [
        path.relative_to(repo_root).as_posix()
        for path in sorted(Path(repo_root).rglob("*"))
        if path.is_file() and not EXCLUDED_PARTS & set(path.relative_to(repo_root).parts)
    ]


def candidate_files(repo_root: Path, *, dirs: Optional[Iterable[str]] = None,
                    exclude: Sequence[str] = (), files: Optional[Iterable[str]] = None) -> List[str]:
    """扫描面:给定文件,或全部入库文件(可限定目录);跳过环境包、构建产物与 exclude 前缀。"""
    if files is None:
        files = tracked_files(repo_root)
        if files is None:
            files = _walk(repo_root)
    prefixes = tuple(item.rstrip("/") + "/" for item in dirs) if dirs is not None else None
    selected = []
    for relative in files:
        parts = set(Path(relative).parts)
        if relative.startswith(SKIP_PREFIXES) or EXCLUDED_PARTS & parts:
            continue
        if prefixes is not None and not relative.startswith(prefixes):
            continue
        if any(relative == item or relative.startswith(item.rstrip("/") + "/") for item in exclude):
            continue
        selected.append(relative)
    return sorted(selected)


def _texts(name: str, data: bytes) -> List[Tuple[str, str]]:
    """(位置, 文本):普通文本文件一段;压缩文档按成员各一段;二进制返回空。"""
    if Path(name).suffix.lower() in ARCHIVE_SUFFIXES:
        try:
            with zipfile.ZipFile(BytesIO(data)) as archive:
                return [
                    (f"{name}!{member}", archive.read(member).decode("utf-8", errors="ignore"))
                    for member in archive.namelist() if ARCHIVE_TEXT.search(member)
                ]
        except zipfile.BadZipFile:
            return []
    if len(data) > MAX_TEXT_BYTES or b"\0" in data[:8192]:
        return []
    try:
        return [(name, data.decode("utf-8"))]
    except UnicodeDecodeError:
        return []


def _matches(text: str, terms: Sequence[NounTerm]) -> List[Tuple[int, str, str]]:
    found = []
    for number, line in enumerate(text.splitlines(), start=1):
        if ALLOW_MARKER in line:
            continue
        for term in terms:
            if term.skip_line is not None and term.skip_line.search(line):
                continue
            for match in term.pattern.finditer(line):
                found.append((number, term.id, match.group(0)))
    return found


def scan(repo_root: Path, terms: List[NounTerm], dirs: Optional[Iterable[str]] = None, *,
         exclude: Sequence[str] = (), files: Optional[Iterable[str]] = None,
         reader: Optional[Callable[[str], bytes]] = None) -> Dict[str, Any]:
    """按词条统计命中;reader 缺省读工作区文件(pre-commit 传读暂存区的函数)。"""
    repo_root = Path(repo_root)
    reader = reader or (lambda relative: (repo_root / relative).read_bytes())
    report_files: List[Dict[str, Any]] = []
    totals = {term.id: {"files": 0, "hits": 0} for term in terms}
    scanned = 0
    for relative in candidate_files(repo_root, dirs=dirs, exclude=exclude, files=files):
        try:
            data = reader(relative)
        except (OSError, subprocess.CalledProcessError):
            continue
        texts = _texts(relative, data)
        if not texts:
            continue
        scanned += 1
        matches = [
            {"where": where, "line": line, "term": term_id, "match": text[:80]}
            for where, content in texts for line, term_id, text in _matches(content, terms)
        ]
        if not matches:
            continue
        counts: Dict[str, int] = {}
        for item in matches:
            counts[item["term"]] = counts.get(item["term"], 0) + 1
        for key, value in counts.items():
            totals[key]["files"] += 1
            totals[key]["hits"] += value
        report_files.append({"path": relative, "hits": len(matches), "terms": counts, "matches": matches})
    report_files.sort(key=lambda item: (-item["hits"], item["path"]))
    return {
        "schema_version": 2,
        "scan_dirs": list(dirs) if dirs is not None else ["(全部入库文件)"],
        "excluded": list(exclude),
        "scanned_files": scanned,
        "terms": [
            {"id": term.id, "label": term.label, **totals[term.id]} for term in terms
        ],
        "total_hits": sum(item["hits"] for item in totals.values()),
        "files": report_files,
    }


def compare(report: Mapping[str, Any], baseline: Mapping[str, Any]) -> Dict[str, Any]:
    """与基线报告比较:只列出增长的词条与文件(计数下降是进展,不必提示)。"""
    before_terms = {item["id"]: item for item in baseline.get("terms") or []}
    before_files = {item["path"]: item for item in baseline.get("files") or []}
    grown_terms = [
        {"id": item["id"], "hits": item["hits"],
         "baseline": (before_terms.get(item["id"]) or {}).get("hits", 0)}
        for item in report.get("terms") or []
        if item["hits"] > (before_terms.get(item["id"]) or {}).get("hits", 0)
    ]
    grown_files = [
        {"path": item["path"], "hits": item["hits"],
         "baseline": (before_files.get(item["path"]) or {}).get("hits", 0)}
        for item in report.get("files") or []
        if item["hits"] > (before_files.get(item["path"]) or {}).get("hits", 0)
    ]
    return {
        "total_hits": report.get("total_hits", 0),
        "baseline_total_hits": baseline.get("total_hits", 0),
        "grown_terms": grown_terms,
        "grown_files": grown_files,
    }


def render_text(report: Mapping[str, Any], *, top: int = 20, details: bool = False,
                comparison: Optional[Mapping[str, Any]] = None) -> str:
    lines = [f"扫描 {', '.join(report['scan_dirs'])}:{report['scanned_files']} 个文件,"
             f"共 {report['total_hits']} 处", "", "文件  处数  词条"]
    for item in report["terms"]:
        lines.append(f"{item['files']:>4}  {item['hits']:>4}  {item['label']}")
    if top and report["files"]:
        lines += ["", f"命中最多的 {min(top, len(report['files']))} 个文件:"]
        for item in report["files"][:top]:
            detail = ", ".join(f"{key} {value}" for key, value in item["terms"].items())
            lines.append(f"  {item['hits']:>4}  {item['path']}  ({detail})")
    if details and report["files"]:
        lines += ["", "逐处(文件 · 行 · 词条 · 命中):"]
        for item in report["files"]:
            for match in item.get("matches") or []:
                lines.append(f"  {match['where']}:{match['line']}  {match['term']}  {match['match']}")
    if comparison is not None:
        lines += ["", f"对比基线:{comparison['baseline_total_hits']} → {comparison['total_hits']}"]
        for item in comparison["grown_terms"]:
            lines.append(f"  词条增长 {item['id']}: {item['baseline']} → {item['hits']}")
        for item in comparison["grown_files"]:
            lines.append(f"  文件增长 {item['path']}: {item['baseline']} → {item['hits']}")
        if not comparison["grown_terms"] and not comparison["grown_files"]:
            lines.append("  无增长")
    return "\n".join(lines)
