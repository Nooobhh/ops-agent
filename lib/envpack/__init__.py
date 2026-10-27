"""环境包:一个环境特有的全部内容(环境定义、集群与角色、清单来源、词表等)。

环境包是应用数据,不随程序发布。定位:``<数据根>/config/paths.local.yaml`` 的 ``env_pack``
(绝对路径,或相对数据根)→ ``<数据根>/env/``(有 ``env.yaml`` 即用)→ 程序自带的示例包
``envs/example``(只读演示)。不设环境变量入口;程序只读环境目录,不往里写。

路径一律相对环境包目录:环境数据在 ``config/``,节点侧只读脚本在 ``playbooks/``(知识库
登记的 ``playbooks/…`` 引用解析到这里),Python 扩展在 ``python`` 声明的目录(见 extensions)。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from lib.data_root import data_config_path, resolve_data_root

ENV_FILE = "env.yaml"
CONFIG_DIR = "config"
PLAYBOOKS_DIR = "playbooks"
CODE_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_PACK_DIR = CODE_ROOT / "envs" / "example"
DATA_ENV_DIR = "env"        # <数据根>/env/:一台机器一个环境
# 核心认识的扩展点;env.yaml 的 extensions 只能从这里选(各扩展点的形态见 lib/envpack/extensions.py)
EXTENSION_POINTS = ("node_credentials", "credential_profiles", "alert_sources", "graphs")

# 核心认识的集群角色;env.yaml 只能从这里选
ROLES = frozenset({"managed", "knowledge", "mysql_direct"})
# 知识库里代表「环境级、不绑定集群」的平台键(核心词表,知识库目录与检索都按它组织)
KNOWLEDGE_ENV_SCOPE = "shared"

_ENV_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_CLUSTER_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
# 插件 id 即模型可见工具名的命名空间(mcp__<id>__<tool>,总长受 64 字符限制)
PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,23}$")
PLUGIN_AUTH_SCHEMES = ("none", "basic", "bearer", "header", "connections")
# 需要请求头名的认证:header 放单个值;connections 放中台按连接列表序列化的值(插件页逐条编辑)
PLUGIN_HEADER_SCHEMES = ("header", "connections")
PLUGIN_HEADER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,63}$")
PLUGIN_TOOL_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
PLUGIN_LABEL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
PLUGIN_LABEL_VALUE_RE = re.compile(r"^[A-Za-z0-9_.:/@+-]{1,64}$")
# 中台后台采集向插件取数的角色;核心只认角色,工具名由环境包声明
PLUGIN_FEEDS = {
    "alerts": ("current", "history", "rules"),
    "resources": ("snapshot", "targets", "query"),
    "schedule": ("definitions", "hosts"),  # 流程调度拓扑:流程定义分页、流程 → 执行机
}


class EnvPackError(ValueError):
    pass


@dataclass(frozen=True)
class ClusterDecl:
    id: str
    legacy_key: str
    display_name: str
    roles: Tuple[str, ...]
    sources: Mapping[str, str] = field(default_factory=dict)
    short_name: str = ""
    # 知识库目录与 frontmatter 用的平台键;缺省同旧短键,旧短键与环境级范围键重名时须另设
    knowledge_key: str = ""


@dataclass(frozen=True)
class PluginWriteDecl:
    """两阶段写(计划 → 人工审批 → Harness 执行)的工具配对;核心只认角色,不认工具名。

    plan 对模型可见(调用即提出写操作);execute 只由 Harness 在人工批准后调用;
    track 跟踪执行、reconcile 在中断后按计划标记对账;impact 可选,给审批卡片列下游影响。
    """
    plan: str
    execute: str
    track: str
    reconcile: str
    impact: str = ""


@dataclass(frozen=True)
class PluginScopeDecl:
    """全局插件(不绑集群)按标签区分集群。

    tools 里的工具查的是分集群的实时数据:任务已定集群时 Harness 强制给 ``argument`` 参数
    带上 ``label=<本集群标签值>``;其余工具是环境级参考,不加过滤。
    """
    label: str
    clusters: Mapping[str, str]  # 集群旧短键 → 标签值
    tools: Tuple[str, ...]
    argument: str = "filters"

    def cluster_of(self, value: Any) -> Optional[str]:
        """标签值 → 集群旧短键;不在映射里返回 None。"""
        return next((key for key, item in self.clusters.items() if item == str(value or "")), None)


@dataclass(frozen=True)
class PluginDecl:
    """官方插件(MCP 服务)声明;本机地址覆盖、开关与凭据在插件页配置。"""
    id: str
    url: str
    title: str = ""
    cluster: Optional[str] = None
    auth: str = "none"
    header_name: str = ""
    default_disabled: Tuple[str, ...] = ()
    write: Optional[PluginWriteDecl] = None
    scope: Optional[PluginScopeDecl] = None
    # 后台采集:{alerts|resources|schedule: {角色: 工具名}},见 PLUGIN_FEEDS
    feeds: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    # connections 认证:插件页「测试」逐条试连用的工具(调用时带 probe=true)
    connection_probe: str = ""


@dataclass(frozen=True)
class AssemblyDecl:
    """分组库的确定性组装:按图谱里的流程/任务号识别类型,组装「通用 → 类型通用 → 特化」必读。

    语境(点名 service 或命中图谱里的流程/任务对)内检索按库加权:本库与 home_services 原权重,
    runtime_services 乘 runtime_weight,其余乘 other_weight。
    """
    library: str
    graph: str
    label: str
    service: str
    refresh_hint: str = ""
    home_services: Tuple[str, ...] = ()
    runtime_services: Tuple[str, ...] = ()
    runtime_weight: float = 1.0
    other_weight: float = 1.0

    @property
    def graph_key(self) -> str:
        """检索快照里该图谱的 graph_id。"""
        return f"{self.graph}_topology"


@dataclass(frozen=True)
class EnvPack:
    root: Path
    id: str
    display_name: str
    timezone: str
    clusters: Tuple[ClusterDecl, ...]
    inventory: Mapping[str, str] = field(default_factory=dict)
    plugins: Tuple[PluginDecl, ...] = ()
    # 知识库封闭词表:frontmatter 的 service / task_type 只能取这里
    services: Tuple[str, ...] = ()
    task_types: Tuple[str, ...] = ()
    # 服务显示名、查询别名与分组库组装(knowledge 段;均可缺省)
    service_labels: Mapping[str, str] = field(default_factory=dict)
    service_aliases: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)
    assembly: Optional[AssemblyDecl] = None
    # Python 扩展:加进 sys.path 的目录与按扩展点登记的原始声明
    python_dir: Optional[Path] = None
    extensions: Mapping[str, Any] = field(default_factory=dict)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def cluster(self, legacy_key: str) -> Optional[ClusterDecl]:
        return next((item for item in self.clusters if item.legacy_key == legacy_key), None)

    def cluster_keys(self, role: Optional[str] = None) -> Tuple[str, ...]:
        """按声明顺序的旧短键;无需构建清单即可回答「哪些集群有某角色」。"""
        return tuple(
            item.legacy_key for item in self.clusters if role is None or role in item.roles
        )

    def knowledge_platforms(self) -> Tuple[str, ...]:
        """知识库 platform 合法值:knowledge 集群的知识库键 + 环境级范围键。"""
        return tuple(item.knowledge_key for item in self.clusters if "knowledge" in item.roles) \
            + (KNOWLEDGE_ENV_SCOPE,)

    def knowledge_cluster(self, key: str) -> Optional[ClusterDecl]:
        """按知识库键找 knowledge 集群;环境级范围键与未知键返回 None。"""
        return next((item for item in self.clusters
                     if "knowledge" in item.roles and item.knowledge_key == key), None)

    def knowledge_key(self, legacy_key: str) -> str:
        """任务/告警的旧短键 → 知识库平台键(检索、晋升等按知识库口径比较时用);无对应时原样返回。"""
        decl = self.cluster(legacy_key)
        return decl.knowledge_key if decl is not None and "knowledge" in decl.roles else legacy_key

    def short_name(self, legacy_key: str) -> str:
        decl = self.cluster(legacy_key)
        return decl.short_name if decl is not None else legacy_key

    def knowledge_source_path(self, key: str, name: str) -> Optional[Path]:
        """按知识库键取 knowledge 集群声明的来源文件;未声明返回 None。"""
        decl = self.knowledge_cluster(key)
        spec = decl.sources.get(name) if decl is not None else None
        return self.resolve(spec) if spec else None

    def source_path(self, legacy_key: str, name: str) -> Optional[Path]:
        """集群声明的某个来源文件;未声明返回 None。"""
        decl = self.cluster(legacy_key)
        spec = decl.sources.get(name) if decl is not None else None
        return self.resolve(spec) if spec else None

    def resolve(self, spec: str) -> Path:
        return self.root / str(spec)

    @property
    def config_dir(self) -> Path:
        return self.root / CONFIG_DIR

    def config_file(self, name: str) -> Path:
        """环境数据文件 ``config/<name>``(可带子目录);存在与否由调用方按各自语义处理。"""
        return self.config_dir / name

    def playbook_path(self, ref: str) -> Path:
        """知识库登记的 playbook 引用(``playbooks/…``)→ 环境包内文件;前缀不符或越界即拒。"""
        text = str(ref or "").strip()
        if not text.startswith(f"{PLAYBOOKS_DIR}/"):
            raise EnvPackError(f"playbook 引用须以 {PLAYBOOKS_DIR}/ 开头: {ref!r}")
        base = (self.root / PLAYBOOKS_DIR).resolve()
        path = (self.root / text).resolve()
        if not path.is_relative_to(base):
            raise EnvPackError(f"playbook 引用越界: {ref!r}")
        return path


_PATHS_CACHE: Dict[Path, Tuple[Tuple[int, int], str]] = {}


def _configured_pack(config: Path) -> str:
    """paths.local.yaml 的 env_pack 值,按文件 mtime/size 缓存(热路径每次都会问)。"""
    try:
        stat = config.stat()
    except OSError:
        return ""
    signature = (stat.st_mtime_ns, stat.st_size)
    cached = _PATHS_CACHE.get(config)
    if cached is not None and cached[0] == signature:
        return cached[1]
    data = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise EnvPackError(f"{config}: 顶层必须是映射")
    value = str(data.get("env_pack") or "").strip()
    _PATHS_CACHE[config] = (signature, value)
    return value


def resolve_env_pack_dir(repo_root: Path) -> Path:
    """env_pack → <数据根>/env/ → 自带示例包;配置了却不是环境包时显式失败,不往下退。"""
    data_root = resolve_data_root(repo_root).resolve()
    config = data_config_path(data_root, "paths.local.yaml")
    value = _configured_pack(config) if config.is_file() else ""
    if value:
        path = Path(value).expanduser()
        path = path if path.is_absolute() else data_root / path
        if not (path / ENV_FILE).is_file():
            raise EnvPackError(f"环境包缺少 {ENV_FILE}: {path}")
        return path.resolve()
    if (data_root / DATA_ENV_DIR / ENV_FILE).is_file():
        return (data_root / DATA_ENV_DIR).resolve()
    if not (EXAMPLE_PACK_DIR / ENV_FILE).is_file():
        raise EnvPackError(
            f"未找到环境包:请把环境目录放到 {data_root / DATA_ENV_DIR},或在 {config} 配置 env_pack"
        )
    return EXAMPLE_PACK_DIR.resolve()


def env_pack_origin(repo_root: Path, root: Path) -> str:
    """已定位的环境包从哪来:example(自带示例)/ data_root(<数据根>/env)/ configured(env_pack)。"""
    root = Path(root).resolve()
    if root == EXAMPLE_PACK_DIR.resolve():
        return "example"
    if root == (resolve_data_root(repo_root) / DATA_ENV_DIR).resolve():
        return "data_root"
    return "configured"


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def env_pack_commit(root: Path) -> Optional[str]:
    """环境目录自成 git 仓时的 HEAD 短提交号;只读 .git 下的文件(安装机可能没有 git),取不到为 None。"""
    git = Path(root) / ".git"
    try:
        if git.is_file():                       # 工作树/子模块:.git 是指向真实目录的文件
            text = git.read_text(encoding="utf-8").strip()
            if not text.startswith("gitdir:"):
                return None
            git = (Path(root) / text[len("gitdir:"):].strip()).resolve()
        if not git.is_dir():
            return None
        common = git
        if (git / "commondir").is_file():
            common = (git / (git / "commondir").read_text(encoding="utf-8").strip()).resolve()
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            ref = head[len("ref:"):].strip()
            loose = common / ref
            head = ""
            if loose.is_file():
                head = loose.read_text(encoding="utf-8").strip()
            elif (common / "packed-refs").is_file():
                for line in (common / "packed-refs").read_text(encoding="utf-8").splitlines():
                    sha, _, name = line.partition(" ")
                    if name.strip() == ref:
                        head = sha
                        break
    except (OSError, UnicodeError):
        return None
    return head[:7] if _SHA_RE.match(head) else None


_CACHE: Dict[Path, Tuple[Tuple[int, int], EnvPack]] = {}


def load_env_pack(repo_root: Path) -> EnvPack:
    root = resolve_env_pack_dir(repo_root)
    path = root / ENV_FILE
    stat = path.stat()
    signature = (stat.st_mtime_ns, stat.st_size)
    cached = _CACHE.get(path)
    if cached is not None and cached[0] == signature:
        return cached[1]
    pack = parse_env_pack(root)
    _CACHE[path] = (signature, pack)
    return pack


def active_env_pack() -> EnvPack:
    """拿不到仓库根的库函数用:按代码仓(及其数据根下的 paths.local.yaml)定位。"""
    return load_env_pack(CODE_ROOT)


def env_timezone() -> ZoneInfo:
    """环境时区(env.yaml 的 timezone);业务时间一律用它,不读宿主时区。"""
    return active_env_pack().tz


def parse_env_pack(root: Path) -> EnvPack:
    path = Path(root) / ENV_FILE
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise EnvPackError(f"{path}: 不可读: {exc}") from exc
    if not isinstance(data, dict):
        raise EnvPackError(f"{path}: 顶层必须是映射")
    if data.get("version") != 1:
        raise EnvPackError(f"{path}: 仅支持 version: 1")
    env_id = _text(data, "id", path)
    if not _ENV_ID_RE.match(env_id):
        raise EnvPackError(f"{path}: 非法环境 id {env_id!r}")
    timezone = _text(data, "timezone", path)
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise EnvPackError(f"{path}: 无法识别的时区 {timezone!r}") from exc

    clusters = []
    for index, raw in enumerate(data.get("clusters") or []):
        where = f"{path}: clusters[{index}]"
        if not isinstance(raw, dict):
            raise EnvPackError(f"{where} 必须是映射")
        cluster_id = _text(raw, "id", where)
        legacy_key = str(raw.get("legacy_key") or cluster_id).strip()
        if not _CLUSTER_ID_RE.match(cluster_id) or not _KEY_RE.match(legacy_key):
            raise EnvPackError(f"{where}: 非法集群 id 或 legacy_key")
        roles = tuple(str(item) for item in raw.get("roles") or [])
        unknown = sorted(set(roles) - ROLES)
        if unknown:
            raise EnvPackError(f"{where}: 未知角色 {unknown},可选 {sorted(ROLES)}")
        sources = raw.get("sources") or {}
        if not isinstance(sources, dict) or not all(isinstance(v, str) for v in sources.values()):
            raise EnvPackError(f"{where}: sources 必须是 名称→路径 映射")
        _check_pack_paths(sources.values(), where)
        display_name = str(raw.get("display_name") or cluster_id)
        knowledge_key = str(raw.get("knowledge_key") or legacy_key).strip()
        if not _KEY_RE.match(knowledge_key):
            raise EnvPackError(f"{where}: 非法 knowledge_key")
        if "knowledge" in roles and knowledge_key == KNOWLEDGE_ENV_SCOPE:
            raise EnvPackError(f"{where}: 知识库键与环境级范围键 {KNOWLEDGE_ENV_SCOPE!r} 重名,请设 knowledge_key")
        clusters.append(ClusterDecl(
            id=cluster_id,
            legacy_key=legacy_key,
            display_name=display_name,
            roles=roles,
            sources=dict(sources),
            short_name=str(raw.get("short_name") or display_name),
            knowledge_key=knowledge_key,
        ))
    for attr in ("id", "legacy_key"):
        values = [getattr(item, attr) for item in clusters]
        if len(values) != len(set(values)):
            raise EnvPackError(f"{path}: 集群 {attr} 重复")
    knowledge_keys = [item.knowledge_key for item in clusters if "knowledge" in item.roles]
    if len(knowledge_keys) != len(set(knowledge_keys)):
        raise EnvPackError(f"{path}: 集群 knowledge_key 重复")

    inventory = data.get("inventory") or {}
    if not isinstance(inventory, dict):
        raise EnvPackError(f"{path}: inventory 必须是映射")
    _check_pack_paths((str(value) for value in inventory.values()), f"{path}: inventory")
    _check_requires_core(str(data.get("requires_core") or "").strip(), path)
    python = str(data.get("python") or "").strip()
    if python:
        _check_pack_paths([python], f"{path}: python")
    extensions = data.get("extensions") or {}
    if not isinstance(extensions, dict):
        raise EnvPackError(f"{path}: extensions 必须是映射")
    unknown = sorted(set(extensions) - set(EXTENSION_POINTS))
    if unknown:
        raise EnvPackError(f"{path}: 未知扩展点 {unknown},可选 {list(EXTENSION_POINTS)}")
    if extensions and not python:
        raise EnvPackError(f"{path}: 声明了 extensions 须同时给出 python 目录")
    knowledge = data.get("knowledge") or {}
    if not isinstance(knowledge, dict):
        raise EnvPackError(f"{path}: knowledge 必须是映射")
    vocabulary = {}
    for name in ("services", "task_types"):
        values = knowledge.get(name) or []
        if not isinstance(values, list) or not all(isinstance(item, str) and item.strip() for item in values):
            raise EnvPackError(f"{path}: knowledge.{name} 必须是非空字符串列表")
        if len(set(values)) != len(values):
            raise EnvPackError(f"{path}: knowledge.{name} 有重复项")
        vocabulary[name] = tuple(values)
    labels = knowledge.get("service_labels") or {}
    aliases = knowledge.get("service_aliases") or {}
    if not isinstance(labels, dict) or not all(isinstance(v, str) for v in labels.values()):
        raise EnvPackError(f"{path}: knowledge.service_labels 须为 服务 → 显示名")
    if not isinstance(aliases, dict) or not all(
            isinstance(v, list) and all(isinstance(item, str) and item.strip() for item in v) for v in aliases.values()):
        raise EnvPackError(f"{path}: knowledge.service_aliases 须为 服务 → 别名列表")
    return EnvPack(
        root=Path(root).resolve(),
        id=env_id,
        display_name=str(data.get("display_name") or env_id),
        timezone=timezone,
        clusters=tuple(clusters),
        inventory={str(k): str(v) for k, v in inventory.items()},
        plugins=_parse_plugins(data.get("plugins") or [], path, {item.legacy_key for item in clusters}),
        services=vocabulary["services"],
        task_types=vocabulary["task_types"],
        service_labels={str(k).lower(): str(v) for k, v in labels.items()},
        service_aliases={str(k).lower(): tuple(str(item) for item in v) for k, v in aliases.items()},
        assembly=_parse_assembly(knowledge.get("assembly"), path),
        python_dir=(Path(root).resolve() / python) if python else None,
        extensions=dict(extensions),
    )


def _parse_assembly(raw: Any, path: Path) -> Optional[AssemblyDecl]:
    if raw is None:
        return None
    where = f"{path}: knowledge.assembly"
    if not isinstance(raw, dict):
        raise EnvPackError(f"{where} 必须是映射")
    unknown = set(raw) - set(AssemblyDecl.__dataclass_fields__)
    if unknown:
        raise EnvPackError(f"{where} 有未知字段 {', '.join(sorted(unknown))}")
    try:
        return AssemblyDecl(
            library=_text(raw, "library", where), graph=_text(raw, "graph", where),
            label=_text(raw, "label", where), service=_text(raw, "service", where),
            refresh_hint=str(raw.get("refresh_hint") or ""),
            home_services=tuple(str(item) for item in raw.get("home_services") or []),
            runtime_services=tuple(str(item) for item in raw.get("runtime_services") or []),
            runtime_weight=float(raw.get("runtime_weight", 1.0)),
            other_weight=float(raw.get("other_weight", 1.0)),
        )
    except (TypeError, ValueError) as exc:
        raise EnvPackError(f"{where}: {exc}") from exc


def _check_pack_paths(values, where: str) -> None:
    """声明的路径只能落在环境包内:绝对路径、``..`` 越界与已停用的 ``repo:`` 前缀一律拒。"""
    for value in values:
        text = str(value)
        if text.startswith("repo:") or Path(text).is_absolute() or ".." in Path(text).parts:
            raise EnvPackError(f"{where}: 路径须相对环境包目录且不越界: {text!r}")


def core_version() -> str:
    match = re.search(r'^version\s*=\s*"([^"]+)"',
                      (CODE_ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.MULTILINE)
    if not match:
        raise EnvPackError("pyproject.toml 缺少 version")
    return match.group(1)


def _release(text: str) -> Tuple[int, ...]:
    parts = re.match(r"^\d+(?:\.\d+)*", text.strip())
    if not parts:
        raise EnvPackError(f"无法识别的版本号 {text!r}")
    return tuple(int(item) for item in parts.group(0).split("."))


def _check_requires_core(spec: str, path: Path) -> None:
    """``requires_core: ">=1.0,<2"``:逗号分隔的比较条件,按发布号逐段比较(忽略预发布后缀)。"""
    if not spec:
        return
    current = _release(core_version())
    for clause in spec.split(","):
        match = re.match(r"^\s*(>=|<=|==|>|<)\s*(\S+)\s*$", clause)
        if not match:
            raise EnvPackError(f"{path}: requires_core 条件无法识别 {clause!r}")
        op, bound = match.group(1), _release(match.group(2))
        width = max(len(current), len(bound))
        left, right = current + (0,) * (width - len(current)), bound + (0,) * (width - len(bound))
        ok = {">=": left >= right, "<=": left <= right, "==": left == right,
              ">": left > right, "<": left < right}[op]
        if not ok:
            raise EnvPackError(f"{path}: 环境包要求核心 {spec},当前核心 {core_version()}")


def check_plugin_endpoint(url: str, auth: str, header_name: str) -> None:
    """官方声明与插件页自定义共用的地址/认证校验。"""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise EnvPackError(f"插件地址须是 http(s) URL: {url!r}")
    if auth not in PLUGIN_AUTH_SCHEMES:
        raise EnvPackError(f"插件认证方式须取 {', '.join(PLUGIN_AUTH_SCHEMES)}")
    if (auth in PLUGIN_HEADER_SCHEMES) != bool(header_name) \
            or (header_name and not PLUGIN_HEADER_RE.match(header_name)):
        raise EnvPackError("请求头 / 连接列表认证须给出合法的请求头名,其他方式不填")


def _parse_plugins(raw: Any, path: Path, cluster_keys: set) -> Tuple[PluginDecl, ...]:
    if not isinstance(raw, list):
        raise EnvPackError(f"{path}: plugins 必须是列表")
    plugins = []
    for index, item in enumerate(raw):
        where = f"{path}: plugins[{index}]"
        if not isinstance(item, dict):
            raise EnvPackError(f"{where} 必须是映射")
        plugin_id = _text(item, "id", where)
        if not PLUGIN_ID_RE.match(plugin_id):
            raise EnvPackError(f"{where}: 非法插件 id {plugin_id!r}")
        cluster = str(item.get("cluster") or "").strip() or None
        if cluster is not None and cluster not in cluster_keys:
            raise EnvPackError(f"{where}: 未声明的集群 {cluster!r}")
        scope = _parse_scope(item.get("scope"), where, cluster_keys)
        if scope is not None and cluster is not None:
            raise EnvPackError(f"{where}: cluster 与 scope 只能二选一")
        decl = PluginDecl(
            id=plugin_id,
            url=_text(item, "url", where),
            title=str(item.get("title") or ""),
            cluster=cluster,
            auth=str(item.get("auth") or "none"),
            header_name=str(item.get("header_name") or ""),
            default_disabled=tuple(str(name) for name in item.get("default_disabled") or []),
            write=_parse_write(item.get("write"), where),
            scope=scope,
            feeds=_parse_feeds(item.get("feeds"), where),
            connection_probe=str(item.get("connection_probe") or ""),
        )
        try:
            check_plugin_endpoint(decl.url, decl.auth, decl.header_name)
        except EnvPackError as exc:
            raise EnvPackError(f"{where}: {exc}") from None
        if decl.connection_probe and (decl.auth != "connections" or not PLUGIN_TOOL_RE.match(decl.connection_probe)):
            raise EnvPackError(f"{where}: connection_probe 只用于 connections 认证,且须是合法工具名")
        plugins.append(decl)
    if len({item.id for item in plugins}) != len(plugins):
        raise EnvPackError(f"{path}: 插件 id 重复")
    return tuple(plugins)


def _parse_write(raw: Any, where: str) -> Optional[PluginWriteDecl]:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise EnvPackError(f"{where}: write 必须是映射")
    unknown = set(raw) - {"plan", "execute", "track", "reconcile", "impact"}
    if unknown:
        raise EnvPackError(f"{where}: write 有未知字段 {', '.join(sorted(unknown))}")
    names = {}
    for role in ("plan", "execute", "track", "reconcile", "impact"):
        value = str(raw.get(role) or "").strip()
        if not value and role != "impact":
            raise EnvPackError(f"{where}: write 缺少 {role}")
        if value and not PLUGIN_TOOL_RE.match(value):
            raise EnvPackError(f"{where}: write.{role} 不是合法工具名 {value!r}")
        names[role] = value
    used = [value for value in names.values() if value]
    if len(set(used)) != len(used):
        raise EnvPackError(f"{where}: write 各角色的工具不能重名")
    return PluginWriteDecl(**names)


def _parse_scope(raw: Any, where: str, cluster_keys: set) -> Optional[PluginScopeDecl]:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise EnvPackError(f"{where}: scope 必须是映射")
    unknown = set(raw) - {"label", "clusters", "tools", "argument"}
    if unknown:
        raise EnvPackError(f"{where}: scope 有未知字段 {', '.join(sorted(unknown))}")
    label = str(raw.get("label") or "").strip()
    argument = str(raw.get("argument") or "filters").strip()
    if not PLUGIN_LABEL_RE.match(label) or not PLUGIN_LABEL_RE.match(argument):
        raise EnvPackError(f"{where}: scope.label / scope.argument 不是合法标识符")
    clusters = raw.get("clusters")
    if not isinstance(clusters, dict) or not clusters:
        raise EnvPackError(f"{where}: scope.clusters 必须是非空映射(集群 → 标签值)")
    mapping = {}
    for key, value in clusters.items():
        if str(key) not in cluster_keys:
            raise EnvPackError(f"{where}: scope.clusters 有未声明的集群 {key!r}")
        if not PLUGIN_LABEL_VALUE_RE.match(str(value or "")):
            raise EnvPackError(f"{where}: scope.clusters.{key} 标签值不合法")
        mapping[str(key)] = str(value)
    if len(set(mapping.values())) != len(mapping):
        raise EnvPackError(f"{where}: scope.clusters 标签值不能重复")
    tools = raw.get("tools")
    if not isinstance(tools, list) or not tools \
            or not all(isinstance(name, str) and PLUGIN_TOOL_RE.match(name) for name in tools):
        raise EnvPackError(f"{where}: scope.tools 必须是非空工具名列表")
    return PluginScopeDecl(label=label, clusters=mapping, tools=tuple(tools), argument=argument)


def _parse_feeds(raw: Any, where: str) -> Mapping[str, Mapping[str, str]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise EnvPackError(f"{where}: feeds 必须是映射")
    feeds = {}
    for kind, roles in raw.items():
        expected = PLUGIN_FEEDS.get(str(kind))
        if expected is None:
            raise EnvPackError(f"{where}: feeds 只能是 {', '.join(PLUGIN_FEEDS)}")
        if not isinstance(roles, dict) or set(roles) != set(expected):
            raise EnvPackError(f"{where}: feeds.{kind} 须且只须声明 {', '.join(expected)}")
        for role, name in roles.items():
            if not PLUGIN_TOOL_RE.match(str(name or "")):
                raise EnvPackError(f"{where}: feeds.{kind}.{role} 不是合法工具名")
        feeds[str(kind)] = {str(role): str(name) for role, name in roles.items()}
    return feeds


def _text(data: Mapping[str, Any], key: str, where: Any) -> str:
    value = str(data.get(key) or "").strip()
    if not value:
        raise EnvPackError(f"{where}: 缺少 {key}")
    return value


__all__ = [
    "AssemblyDecl", "ClusterDecl", "EXTENSION_POINTS", "EnvPack", "EnvPackError", "KNOWLEDGE_ENV_SCOPE", "PLUGIN_AUTH_SCHEMES", "PLUGIN_FEEDS",
    "PLUGIN_ID_RE", "PluginDecl", "PluginScopeDecl", "PluginWriteDecl", "ROLES", "active_env_pack", "check_plugin_endpoint", "env_timezone", "load_env_pack",
    "env_pack_commit", "env_pack_origin", "parse_env_pack", "resolve_env_pack_dir",
]
