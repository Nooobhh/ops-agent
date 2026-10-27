"""知识库 lint 唯一入库闸门。

规则:frontmatter 完整性(委托 frontmatter.load_doc)+ 命名约定(<platform>-<service>-<problem>.md
或 _index.md)+ 目录约定(knowledge/<platform>/<service>/)+ procedure 禁 verified 硬约束 +
alertname 全库唯一 + wikilink 断链 + playbook 指针存在(空值过渡态放行)。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Tuple

from lib.datastore.models import AUTHOR_ID_RE
from lib.envpack import EnvPackError, load_env_pack

from .contract import (
    DOCUMENT_PROFILES,
    DOCUMENT_SOURCES,
    ENTRYPOINTS,
    EXECUTION_SCOPES,
    KNOWLEDGE_LAYERS,
    OPTIONAL_SECTIONS,
    SCHEMA_VERSION,
    SECTION_ORDER,
    SUPPORTED_SCHEMA_VERSIONS,
    V2_OPTIONAL_SECTIONS,
    V2_SECTION_ORDER,
)
from .bases import discover_bases, iter_documents, split_doc_path
from .frontmatter import (
    CONFIDENCE_LEVELS, TASK_TYPES, Doc, FrontmatterError, is_platform_ref, load_doc, platform_ref,
)
from .index_sync import START as _INDEX_GENERATED_START
from .topology import knowledge_clusters

_SLUG_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_WIKILINK_RE = re.compile(
    r"\[\[((?:\\(?!\|)|[^\[\]|#\\])+?)(?:#[^\[\]|]*)?"
    r"(?:\\?\|[^\[\]]*)?\]\]"
)
_H1_RE = re.compile(r"^#\s+(.+?)\s*$")
_H2_RE = re.compile(r"(?m)^##\s+(.+?)\s*$")
_GENERATED_ONLY_SECTIONS = {"依赖", "依赖关系", "服务依赖"}


def _markdown_structure(text: str) -> tuple[list[str], list[str]]:
    h1: list[str] = []
    h2: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if re.match(r"^\s{0,3}(?:```|~~~)", line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _H1_RE.match(line)
        if match:
            h1.append(match.group(1).strip())
            continue
        match = _H2_RE.match(line)
        if match:
            h2.append(match.group(1).strip())
    return h1, h2


def _lint_canonical(path: Path, doc: Doc, text: str) -> List[str]:
    errors: List[str] = []
    raw = doc.raw
    version = raw.get("schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        return [f"{path}: schema_version 必须是 {SCHEMA_VERSION},得到 {version!r}"]
    if raw.get("source") not in DOCUMENT_SOURCES:
        errors.append(f"{path}: source 必须是 {sorted(DOCUMENT_SOURCES)}")
    if not isinstance(raw.get("source_ref"), str) or not raw["source_ref"].strip():
        errors.append(f"{path}: source_ref 不能为空")
    if raw.get("source") == "manual_import" and not re.fullmatch(
        r"[0-9a-f]{64}", str(raw.get("source_sha256") or ""),
    ):
        errors.append(f"{path}: manual_import 必须记录 source_sha256")
    if not isinstance(raw.get("summary"), str) or not raw["summary"].strip():
        errors.append(f"{path}: summary 不能为空")
    if not isinstance(raw.get("related"), list):
        errors.append(f"{path}: related 必须是 YAML 列表")
    if "author" in raw and not (
        isinstance(raw["author"], str) and AUTHOR_ID_RE.fullmatch(raw["author"])
    ):
        errors.append(f"{path}: author 必须是合法用户 ID")
    if version >= 3 and not is_platform_ref(raw.get("platform")):
        errors.append(f"{path}: schema v3 的 platform 须写资源引用,应为 "
                      f"{platform_ref(doc.platform)!r},得到 {raw.get('platform')!r}")
    if doc.type == "knowledge":
        if doc.status is not None:
            errors.append(f"{path}: knowledge 类型不设置 status（不进入 draft/verified 生命周期）")
    elif doc.status is None:
        errors.append(f"{path}: status 不能为空")
    if version >= 2:
        layer = raw.get("knowledge_layer")
        if layer not in KNOWLEDGE_LAYERS:
            errors.append(f"{path}: knowledge_layer 必须是 {sorted(KNOWLEDGE_LAYERS)}")
        entrypoints = raw.get("entrypoints")
        if not isinstance(entrypoints, list) or not entrypoints:
            errors.append(f"{path}: entrypoints 必须是非空 YAML 列表")
        elif any(item not in ENTRYPOINTS for item in entrypoints):
            errors.append(f"{path}: entrypoints 含未知值")
        profile = raw.get("profile")
        if profile not in DOCUMENT_PROFILES:
            errors.append(f"{path}: profile 必须是 {sorted(DOCUMENT_PROFILES)}")
        elif profile not in V2_SECTION_ORDER:
            errors.append(f"{path}: profile 没有对应的章节契约: {profile}")
        scope = raw.get("execution_scope")
        if scope not in EXECUTION_SCOPES:
            errors.append(f"{path}: execution_scope 必须是 {sorted(EXECUTION_SCOPES)}")
        confidence = raw.get("confidence")
        if confidence not in CONFIDENCE_LEVELS:
            errors.append(f"{path}: confidence 必须是 {sorted(CONFIDENCE_LEVELS)}")
        if "playbook" in raw:
            errors.append(f"{path}: schema v2 起不再使用单值 playbook,请使用 playbooks")
        if not isinstance(raw.get("playbooks", []), list):
            errors.append(f"{path}: playbooks 必须是 YAML 列表")
        if not isinstance(raw.get("diagnostic_paths", []), list):
            errors.append(f"{path}: diagnostic_paths 必须是 YAML 列表")
        if not isinstance(raw.get("relations", []), list):
            errors.append(f"{path}: relations 必须是 YAML 列表")
        else:
            for index, relation in enumerate(raw.get("relations") or []):
                if not isinstance(relation, dict):
                    errors.append(f"{path}: relations[{index}] 必须是映射")
                    continue
                if not str(relation.get("target") or relation.get("target_service") or "").strip():
                    errors.append(f"{path}: relations[{index}] 缺少 target")
                if not str(relation.get("relation") or "").strip():
                    errors.append(f"{path}: relations[{index}] 缺少 relation")
        if isinstance(raw.get("playbooks", []), list):
            for index, item in enumerate(raw.get("playbooks") or []):
                if not isinstance(item, dict) or not str(item.get("path") or "").strip():
                    errors.append(f"{path}: playbooks[{index}] 必须包含 path")
        if isinstance(raw.get("diagnostic_paths", []), list):
            for index, item in enumerate(raw.get("diagnostic_paths") or []):
                if not isinstance(item, dict) or not str(item.get("id") or "").strip():
                    errors.append(f"{path}: diagnostic_paths[{index}] 必须包含 id")
                elif not isinstance(item.get("playbooks", []), list):
                    errors.append(f"{path}: diagnostic_paths[{index}].playbooks 必须是列表")
                elif not isinstance(item.get("task_types", []), list) or any(
                    value not in TASK_TYPES for value in item.get("task_types") or []
                ):
                    errors.append(f"{path}: diagnostic_paths[{index}].task_types 须为任务类型列表 {sorted(TASK_TYPES)}")
        if profile == "baseline" and doc.type != "knowledge":
            errors.append(f"{path}: profile=baseline 必须使用 type=knowledge")
        if profile == "topology" and doc.type != "knowledge":
            errors.append(f"{path}: profile=topology 必须使用 type=knowledge")
        if profile in {"diagnosis", "sop"} and doc.type not in {"troubleshoot", "procedure"}:
            errors.append(f"{path}: profile={profile} 只能用于 troubleshoot/procedure")
        if layer == "alert" and (doc.type != "troubleshoot" or not doc.alerts):
            errors.append(f"{path}: knowledge_layer=alert 必须是带 alerts 的 troubleshoot")
        if layer == "basic_info" and doc.type != "knowledge":
            errors.append(f"{path}: knowledge_layer=basic_info 必须使用 type=knowledge")
        if doc.type == "troubleshoot" and not doc.symptoms:
            errors.append(f"{path}: troubleshoot.symptoms 至少一项")
        order = V2_SECTION_ORDER.get(str(profile), ())
        optional = V2_OPTIONAL_SECTIONS.get(str(profile), set())
    else:
        order = SECTION_ORDER[doc.type]
        optional = OPTIONAL_SECTIONS.get(doc.type, set())

    if doc.type == "troubleshoot" and version == 1:
        if not isinstance(doc.problem, str) or not _SLUG_RE.fullmatch(doc.problem):
            errors.append(f"{path}: troubleshoot.problem 必须是 kebab-case")
        if not doc.symptoms:
            errors.append(f"{path}: troubleshoot.symptoms 至少一项")
        if "alerts" not in raw or not isinstance(raw.get("alerts"), list):
            errors.append(f"{path}: troubleshoot.alerts 必须是 YAML 列表")
        if "playbook" not in raw or raw.get("playbook") is not None \
                and not isinstance(raw.get("playbook"), str):
            errors.append(f"{path}: troubleshoot.playbook 必须是字符串或 null")

    h1, h2 = _markdown_structure(text)
    if h1 != [str(doc.title).strip()]:
        errors.append(f"{path}: 正文必须且只能有一个 H1,并与 title 完全一致")
    expected = list(order)
    required = [name for name in expected if name not in optional]
    positions = {name: h2.index(name) for name in h2 if name in expected}
    missing = [name for name in required if name not in positions]
    if missing:
        errors.append(f"{path}: 缺少标准章节 {missing}")
    actual_known = [name for name in h2 if name in expected]
    if actual_known != sorted(actual_known, key=expected.index):
        errors.append(f"{path}: 标准章节顺序不正确")
    unknown = [name for name in h2 if name not in expected]
    if unknown:
        errors.append(f"{path}: schema v{version} 不允许未知二级章节 {unknown}")
    return errors


def _lint_grouped(path: Path, doc: Doc, base, parts: tuple[str, ...]) -> List[str]:
    """分组 flat 库(如按 task_type 分组):通用目录 + 每组一个子目录,特化文档带 pid/tid。"""
    errors: List[str] = []
    value = getattr(doc, base.group_field, None) or doc.raw.get(base.group_field)
    if len(parts) < 2:
        return [f"{path}: {base.id} 库文档须放在 {base.common_dir}/ 或 {base.group_field} 分组目录下"]
    folder = parts[0]
    if folder == base.common_dir:
        if value or doc.process_id or doc.task_id:
            errors.append(f"{path}: {base.common_dir}/ 下是不分组的通用文档,不得声明 "
                          f"{base.group_field}/process_id/task_id")
    elif not value:
        errors.append(f"{path}: 分组目录 {folder}/ 下的文档须声明 {base.group_field}")
    elif folder != str(value).lower():
        errors.append(f"{path}: 目录 {folder}/ 与 {base.group_field}={value!r} 不一致")
    if doc.task_id and not doc.process_id:
        errors.append(f"{path}: 声明 task_id 时必须同时声明 process_id")
    if doc.process_id:
        expected = f"p{doc.process_id}" + (f"-t{doc.task_id}" if doc.task_id else "")
        if not str(doc.problem or "").startswith(expected + "-"):
            errors.append(f"{path}: 特化文档 problem 须以 {expected}- 开头(文件名随之带上流程/任务号)")
    return errors


def _troubleshoot_store(repo_root: Path):
    from lib.datastore import open_datastore  # 延迟导入:lib.record 经 lib.kb 回引

    return open_datastore(Path(repo_root)).troubleshoot


def lint_doc(path: Path, kb_root: Path, repo_root: Path | None = None) -> List[str]:
    """lint 单篇文档,返回错误消息列表(空 = clean)。

    repo_root:playbook 指针相对该根查找,默认 = kb_root.parent。
    """
    path = Path(path)
    kb_root = Path(kb_root).resolve()
    errors: List[str] = []
    try:
        doc = load_doc(path)
    except FrontmatterError as e:
        return [str(e)]

    rel = path.resolve().relative_to(kb_root)
    base, inner = split_doc_path(rel, discover_bases(kb_root))
    if base is None:
        return [f"{path}: 文档必须位于某个知识库目录(含 kb.yaml)下"]
    parts = inner.parts

    if doc.is_index:
        # _index.md:目录合规 + 人工区不维护节点清单(由生成区「组件分布」承担)
        if not path.name.endswith("_index.md"):
            errors.append(f"{path}: index 文件名必须以 _index.md 结尾")
        manual = path.read_text(encoding="utf-8").split(_INDEX_GENERATED_START, 1)[0]
        for column, source in (("节点", "config/platform-<p>.yaml"),
                               ("端口", "config/topology/ 拓扑快照")):
            if any(
                line.strip().startswith("|")
                and column in [cell.strip() for cell in line.strip().strip("|").split("|")]
                for line in manual.splitlines()
            ):
                errors.append(f"{path}: 人工区表格不得含「{column}」列,由生成区从 {source} 渲染")
        for match in _H2_RE.finditer(manual):
            if match.group(1).strip() in _GENERATED_ONLY_SECTIONS:
                errors.append(
                    f"{path}: 人工区不得写「{match.group(1).strip()}」,服务依赖由生成区从 "
                    f"config/topology/ 渲染,采集不到的依赖登记到 manual-dependencies.yaml"
                )
        return errors

    text = path.read_text(encoding="utf-8")
    if "schema_version" in doc.raw:
        errors.extend(_lint_canonical(path, doc, text))

    # 非 index 文档:命名 + 目录 + title + procedure 状态
    if not doc.title or not str(doc.title).strip():
        errors.append(f"{path}: title 不能为空")

    stem = path.stem
    if not _SLUG_RE.fullmatch(stem):
        errors.append(f"{path}: 文件名 {stem!r} 不符合 slug 约定 "
                      f"(小写字母开头,-分段,仅 a-z0-9-)")

    # 平铺库(如分组库):库根或一级子目录,平台/服务须与 kb.yaml 一致
    if base.layout == "flat":
        if len(parts) > 2:
            errors.append(f"{path}: {base.id} 库只允许放在库根或一级子目录")
        if doc.type == "oplog":
            errors.append(f"{path}: oplog 只能放在集群库 oplog/ 下")
        for field, expected in (("platform", base.platform), ("service", base.service)):
            if expected and getattr(doc, field) != expected:
                errors.append(f"{path}: {base.id} 库要求 {field}={expected!r},得到 "
                              f"{getattr(doc, field)!r}")
        if base.group_field:
            errors.extend(_lint_grouped(path, doc, base, parts))
    # 三种目录布局分派
    elif len(parts) < 2:
        errors.append(f"{path}: 目录层级不足")
    elif doc.type == "oplog":
        # knowledge/oplog/<platform>/<file>.md
        if parts[0] != "oplog":
            errors.append(f"{path}: oplog 类必须放在 knowledge/oplog/ 下,"
                          f"当前 {parts[0]!r}")
        elif len(parts) < 3:
            errors.append(f"{path}: oplog 目录需 oplog/<platform>/<file>.md")
        elif parts[1] != doc.platform:
            errors.append(f"{path}: oplog/{parts[1]}/ 与 frontmatter platform "
                          f"{doc.platform!r} 不一致")
    elif doc.type == "procedure":
        # v1 procedure 仅支持 shared/procedures;v2 起允许平台专属人工 SOP
        if doc.raw.get("schema_version") in {2, 3} and doc.platform in knowledge_clusters():
            if len(parts) < 3 or parts[0] != doc.platform or parts[1] != doc.service:
                errors.append(
                    f"{path}: v2 起平台 procedure 应放在 knowledge/<platform>/<service>/ 下"
                )
        else:
            if parts[0] != "shared" or (len(parts) >= 2 and parts[1] != "procedures"):
                errors.append(f"{path}: procedure 类必须放在 knowledge/shared/procedures/ 下")
            if doc.platform != "shared":
                errors.append(f"{path}: shared/procedures/ 下的 procedure 类 platform "
                              f"必须是 'shared',得到 {doc.platform!r}")
    else:
        # 主流:knowledge/<platform>/<service>/<file>.md
        if len(parts) < 3:
            errors.append(f"{path}: 目录层级不足,应为 <platform>/<service>/<file>.md")
        else:
            dir_platform, dir_service = parts[0], parts[1]
            if dir_platform != doc.platform:
                errors.append(f"{path}: 目录 platform {dir_platform!r} 与 frontmatter "
                              f"{doc.platform!r} 不一致")
            if doc.service and dir_service != doc.service:
                errors.append(f"{path}: 目录 service {dir_service!r} 与 frontmatter "
                              f"{doc.service!r} 不一致")

    if doc.sop_kind and doc.type != "procedure":
        errors.append(f"{path}: sop_kind 只用于操作手册(type=procedure)")
    if doc.type == "procedure" and doc.status == "verified":
        errors.append(f"{path}: procedure 类型禁止 status=verified(spec §7.1 硬约束)")
    if doc.type == "oplog" and doc.status == "verified":
        errors.append(f"{path}: oplog 类型禁止 status=verified")

    # playbook 指针存在校验(空值为过渡态,放行);引用解析到环境包 playbooks/
    pack = load_env_pack(Path(repo_root) if repo_root else kb_root.parent)
    playbook_paths = [str(item.get("path")) for item in doc.playbooks]
    if not playbook_paths and doc.playbook:
        playbook_paths = [doc.playbook]
    for playbook in playbook_paths:
        try:
            pb_path = pack.playbook_path(playbook)
        except EnvPackError as exc:
            errors.append(f"{path}: {exc}")
            continue
        if not pb_path.exists():
            errors.append(f"{path}: playbook 指针 {playbook!r} 指向的文件不存在")

    return errors


def lint_kb(kb_root: Path, repo_root: Path | None = None) -> List[Tuple[Path, List[str]]]:
    """扫描全库,返回 [(path, errors)] 列表(errors 为空则 clean)。

    收集跨文档规则:(platform, alertname) 唯一;wikilink 目标必须存在;
    playbook 指针存在。
    """
    kb_root = Path(kb_root).resolve()
    repo_root = Path(repo_root).resolve() if repo_root is not None else kb_root.parent
    per_doc: List[Tuple[Path, List[str], "Doc | None", str]] = []
    all_slugs: Dict[str, Path] = {}
    all_rel: Dict[str, Path] = {}
    alert_owner: Dict[Tuple[str, str], Path] = {}

    for item in iter_documents(kb_root):     # 模板/评估集/根级仓库说明不参与 lint
        p = item.path
        errs = lint_doc(p, kb_root=kb_root, repo_root=repo_root)
        try:
            doc = load_doc(p)
        except FrontmatterError:
            doc = None
        text = p.read_text(encoding="utf-8")
        per_doc.append((p, errs, doc, text))

        if doc is None:
            continue
        stem = p.stem
        rel = str(p.relative_to(kb_root)).replace("\\", "/")[:-3]     # 去 .md
        all_slugs[stem] = p
        all_rel[rel] = p

        # (platform, alertname) 唯一(仅 troubleshoot 类)
        if doc.type == "troubleshoot":
            for a in doc.alerts:
                alertname = str(a).strip()
                if not alertname:
                    continue
                key = (doc.platform, alertname)
                if key in alert_owner:
                    errs.append(f"alertname {alertname!r} 在平台 {doc.platform!r} "
                                f"重复注册,已被 "
                                f"{alert_owner[key].relative_to(kb_root)} 占用")
                else:
                    alert_owner[key] = p

    # wikilink 断链检查(Windows 风格 `\` 归一为 `/` 再匹配)
    for p, errs, doc, text in per_doc:
        for m in _WIKILINK_RE.finditer(text):
            target = m.group(1).strip().replace("\\", "/")
            if not target:
                continue
            hit = (target in all_slugs) or (target in all_rel)
            if target.startswith("report:"):
                run_id = target.removeprefix("report:")
                hit = bool(re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", run_id,
                )) and (_troubleshoot_store(repo_root).run_dir(run_id) / "report.md").is_file()
            if not hit:
                errs.append(f"wikilink 断链: [[{target}]]")

    results = [(p, errs) for p, errs, _, _ in per_doc]
    for base in discover_bases(kb_root):        # 确定性组装引用的通用文档必须存在
        missing = [doc_id for doc_id in base.common_doc_ids() if not (kb_root / doc_id).is_file()]
        if missing:
            results.append((kb_root / base.id / "kb.yaml", [f"resolve.common 指向不存在的文档: {missing}"]))
    return results
