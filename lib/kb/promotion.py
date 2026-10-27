"""Shared promotion policy for console auto-promotion and health reporting."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path

from lib.envpack import active_env_pack

from .bases import doc_id_aliases, resolve_knowledge_root
from .browse import strip_frontmatter
from .frontmatter import FrontmatterError, load_doc


_RUN_ID_RE = re.compile(r"\bts-\d{8}-\d{4,6}-[0-9a-z]+\b")
_TERM_RE = re.compile(r"[a-zA-Z0-9_.%-]{4,}|[\u3400-\u9fff]{3,}")
_STOPWORDS = {
    "进行", "问题", "排查", "告警", "节点", "服务", "情况", "已经", "本次",
    "结果", "确认", "建议", "执行", "report", "records", "troubleshoot",
}


@dataclass(frozen=True)
class PromotionDecision:
    eligible: bool
    doc: str
    run_id: str
    reasons: tuple[str, ...]
    explicit_doc_reference: bool
    matched_terms: tuple[str, ...]

    def to_dict(self) -> dict:
        return asdict(self)


def _terms(text: str) -> set[str]:
    terms: set[str] = set()
    for token in _TERM_RE.findall(text.lower()):
        if token in _STOPWORDS:
            continue
        if token.isascii() or len(token) == 3:
            terms.add(token)
        else:
            terms.update(token[index:index + 3] for index in range(len(token) - 2))
    return terms


def _history_runs(markdown: str) -> list[str]:
    runs: list[str] = []
    in_history = False
    for line in markdown.splitlines():
        if line.startswith("## "):
            in_history = line.strip() == "## 历史案例"
            continue
        if not in_history or not line.strip().startswith("-"):
            continue
        match = _RUN_ID_RE.search(line)
        if match:
            runs.append(match.group(0))
    return runs


def evaluate_auto_promotion(repo_root: Path, doc_rel: str, run_id: str) -> PromotionDecision:
    repo_root = Path(repo_root).resolve()
    kb_root = resolve_knowledge_root(repo_root)
    target = (kb_root / doc_rel).resolve()
    reasons: list[str] = []
    try:
        target.relative_to(kb_root)
    except ValueError:
        return PromotionDecision(False, doc_rel, run_id, ("文档路径越界",), False, ())
    try:
        doc = load_doc(target)
        _, body = strip_frontmatter(target.read_text(encoding="utf-8"))
    except (OSError, FrontmatterError) as exc:
        return PromotionDecision(
            False, doc_rel, run_id, (f"文档不可读: {exc}",), False, (),
        )
    if doc.type != "troubleshoot":
        reasons.append("自动晋升只适用于 troubleshoot")
    if doc.status != "draft":
        reasons.append("文档当前不是 draft")

    history_runs = _history_runs(body)
    if run_id not in history_runs:
        reasons.append("历史案例未记录本次 run")
    origin_run = None
    if doc.raw.get("source") == "triage":
        origin_run = str(doc.raw.get("source_ref") or "") or None
    elif doc.raw.get("source") != "manual_import" and history_runs:
        origin_run = min(history_runs)
    if origin_run == run_id:
        reasons.append("创建草稿的原始 run 不能验证自己")

    from lib.datastore import DataStoreError, open_datastore  # 延迟导入:lib.record 经 lib.kb 回引

    store = open_datastore(repo_root).troubleshoot
    try:
        session = store.session(run_id)
        report = store.report(run_id) or ""
    except DataStoreError:
        session, report = None, ""
    if session is None or not report:
        reasons.append("完整 report/session 不可读")
        return PromotionDecision(False, doc_rel, run_id, tuple(reasons), False, ())
    if session.get("verdict") != "conclusive":
        reasons.append("后续报告结论不是 conclusive")
    task_platform = active_env_pack().knowledge_key(str(session.get("platform") or ""))
    task_service = str(session.get("service") or "")
    if doc.platform != "shared" and task_platform and task_platform != doc.platform:
        reasons.append("报告平台与文档不一致")
    if task_service and doc.service and task_service != doc.service:
        reasons.append("报告服务与文档不一致")

    report_text = "\n".join([
        report, str(session.get("trigger") or ""),
        str(session.get("root_cause") or ""),
        "\n".join(str(item) for item in session.get("evidence") or []),
        "\n".join(str(item) for item in session.get("remediation") or []),
    ])
    doc_text = "\n".join([
        str(doc.title or ""), str(doc.problem or ""),
        "\n".join(doc.symptoms), "\n".join(doc.alerts), body,
    ])
    # 迁移前的报告引用的是旧 ID,别名一并认
    explicit = any(
        alias in report_text or f"knowledge/{alias}" in report_text
        for alias in doc_id_aliases(kb_root, doc_rel)
    )
    matched = sorted(_terms(doc_text) & _terms(report_text))
    if not ((explicit and len(matched) >= 2) or len(matched) >= 4):
        reasons.append("报告与文档关键内容呼应不足")
    return PromotionDecision(
        not reasons, doc_rel, run_id, tuple(reasons), explicit, tuple(matched[:8]),
    )
