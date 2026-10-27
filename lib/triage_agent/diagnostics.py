from __future__ import annotations

import difflib
import re
import shlex
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlsplit

import yaml

from lib.envpack import active_env_pack, load_env_pack
from lib.log_catalog import LogCatalog
from lib.triage_agent.platform_context import PlatformContext, PlatformContextError


_RESERVED_PLACEHOLDERS = {"endpoint", "cluster"}
_SAFE_ABSOLUTE_PATH = re.compile(r"/[A-Za-z0-9._/-]*")


# 模型编造/跨平台误用诊断 ID:可自纠,不计入连续驳回终止(由工具轮次预算兜底)
DIAGNOSTIC_NOT_REGISTERED = "DIAGNOSTIC_NOT_REGISTERED"
_MAX_CANDIDATES = 5


class DiagnosticDenied(ValueError):
    pass


class DiagnosticConfigError(ValueError):
    pass


@dataclass(frozen=True)
class RenderedDiagnostic:
    diagnostic_id: str
    command: str
    timeout_seconds: float
    transport: str = "ssh"
    url: str = ""
    extract: Optional[str] = None


@dataclass(frozen=True)
class _Definition:
    diagnostic_id: str
    platforms: tuple
    params: Dict[str, Dict[str, Any]]
    command_template: str
    value_maps: Dict[str, Dict[Any, str]]
    timeout_seconds: float
    note: str = ""
    # enum_from 参数的按平台合法值(枚举值=登记绝对路径本身)
    platform_enums: Dict[str, Dict[str, List[str]]] = field(default_factory=dict)
    endpoint_names: tuple = ()
    needs_cluster: bool = False
    repo_root: Optional[Path] = None
    transport: str = "ssh"
    extract_template: Optional[str] = None

    def render(self, raw_params: Dict[str, Any], platform: str) -> RenderedDiagnostic:
        if platform not in self.platforms:
            raise DiagnosticDenied(
                f"诊断动作 {self.diagnostic_id} 不适用于平台 {platform}"
            )
        expected = set(self.params)
        actual = set(raw_params)
        if actual != expected:
            raise DiagnosticDenied(
                f"诊断动作参数不匹配: expected={sorted(expected)} actual={sorted(actual)}"
            )

        values = {}
        structured_values = {}
        raw_values = {}
        for name, schema in self.params.items():
            value = raw_params[name]
            kind = schema.get("type")
            allowed = schema.get("enum")
            if kind == "integer":
                if type(value) is not int:
                    raise DiagnosticDenied(f"参数 {name} 必须是 integer")
                if isinstance(allowed, list):
                    if value not in allowed:
                        raise DiagnosticDenied(f"参数 {name} 不在允许枚举中")
                else:
                    minimum = schema.get("min")
                    maximum = schema.get("max")
                    if type(minimum) is not int or type(maximum) is not int:
                        raise DiagnosticConfigError(
                            f"integer 参数 {name} 必须声明 enum 或 min/max"
                        )
                    if not minimum <= value <= maximum:
                        raise DiagnosticDenied(
                            f"参数 {name} 必须在闭区间 [{minimum}, {maximum}] 内"
                        )
                values[name] = value
                structured_values[name] = value
                raw_values[name] = value
            elif kind == "string":
                if type(value) is not str:
                    raise DiagnosticDenied(f"参数 {name} 必须是 string")
                platform_allowed = self.platform_enums.get(name, {}).get(platform)
                if platform_allowed is not None:
                    # enum_from 参数:枚举值就是登记绝对路径,平台各一套
                    if value not in platform_allowed:
                        raise DiagnosticDenied(
                            f"参数 {name} 不是平台 {platform} 登记的日志目录: "
                            f"{value}"
                        )
                    values[name] = value
                    structured_values[name] = value
                    raw_values[name] = value
                    continue
                if not isinstance(allowed, list) or not allowed:
                    raise DiagnosticConfigError(f"string 参数 {name} 必须声明非空 enum")
                if value not in allowed:
                    raise DiagnosticDenied(f"参数 {name} 不在允许枚举中")
                mapped = self.value_maps.get(name, {}).get(value, value)
                values[name] = mapped
                structured_values[name] = mapped
                raw_values[name] = value
            elif kind in {"token", "text"}:
                if type(value) is not str:
                    raise DiagnosticDenied(f"参数 {name} 必须是 {kind}")
                max_length = schema.get("max_length")
                if type(max_length) is not int or max_length <= 0:
                    raise DiagnosticConfigError(
                        f"{kind} 参数 {name} 必须声明正整数 max_length"
                    )
                if len(value) > max_length:
                    raise DiagnosticDenied(
                        f"参数 {name} 长度不能超过 {max_length}"
                    )
                if kind == "token":
                    if ".." in value:
                        raise DiagnosticDenied(f"参数 {name} 不能包含 ..")
                    if re.fullmatch(r"[A-Za-z0-9._-]+", value) is None:
                        raise DiagnosticDenied(
                            f"参数 {name} 不符合 token 字符集约束"
                        )
                elif not all(character.isprintable() for character in value):
                    raise DiagnosticDenied(f"参数 {name} 只能包含可打印字符")
                pattern = schema.get("pattern")
                if pattern is not None and re.fullmatch(pattern, value) is None:
                    raise DiagnosticDenied(f"参数 {name} 不匹配声明的 pattern")
                values[name] = shlex.quote(value)
                structured_values[name] = value
                raw_values[name] = value
            else:
                raise DiagnosticConfigError(f"参数 {name} type 不受支持: {kind}")

        endpoints: Dict[str, Any] = {}
        cluster = None
        if self.endpoint_names or self.needs_cluster:
            if self.repo_root is None:
                raise DiagnosticConfigError(
                    f"诊断动作缺 platform context 根目录: {self.diagnostic_id}"
                )
            # 不变量:所有 endpoint 必须在此**预解析**完毕。缺失走 DiagnosticDenied
            # (模型可自纠),而不是留到 format_map 阶段让 _EndpointValues.__format__
            # 抛 KeyError——那会被下方 `except (KeyError, ValueError)` 吞成
            # DiagnosticConfigError(配置错,不可自纠),错误分级就反了。
            # 改动此处时务必保持「先解析、后渲染」的顺序。
            try:
                context = PlatformContext.load(self.repo_root, platform)
                endpoints = {
                    name: context.endpoint(name) for name in self.endpoint_names
                }
                cluster = context.cluster_name() if self.needs_cluster else None
            except PlatformContextError as exc:
                raise DiagnosticDenied(str(exc)) from exc
            if endpoints:
                endpoint_values = _EndpointValues(endpoints)
                values["endpoint"] = endpoint_values
                structured_values["endpoint"] = endpoint_values
            if cluster is not None:
                values["cluster"] = cluster
                structured_values["cluster"] = cluster

        try:
            if self.transport == "http":
                url_values = {
                    name: (
                        value
                        if name == "endpoint"
                        else quote(str(value), safe="")
                    )
                    for name, value in structured_values.items()
                }
                url = self.command_template.format_map(url_values)
                parsed_url = urlsplit(url)
                if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
                    raise DiagnosticConfigError(
                        f"HTTP 诊断 URL 无效: {self.diagnostic_id}"
                    )
                extract = None
                if self.extract_template is not None:
                    extract_values = {
                        name: quote(str(value), safe="")
                        for name, value in raw_values.items()
                        if name not in _RESERVED_PLACEHOLDERS
                    }
                    extract = self.extract_template.format_map(extract_values)
                # 展示串带上 extract:同一 service 下查不同参数时 URL 完全相同,
                # 只有 extract 能区分。records 里的 cmd 必须能自述查了什么,
                # 否则人复核时说不出这条证据对应哪个配置项
                command = f"GET {url}" if extract is None else f"GET {url} -> {extract}"
            else:
                url = ""
                extract = None
                command = self.command_template.format_map(values)
        except (KeyError, ValueError) as exc:
            raise DiagnosticConfigError(
                f"诊断动作模板不可渲染: {self.diagnostic_id}"
            ) from exc
        if any(
            character in text
            for text in (command, url, extract or "")
            for character in ("\n", "\r", "\x00")
        ):
            raise DiagnosticConfigError("诊断动作渲染后包含控制字符")
        return RenderedDiagnostic(
            diagnostic_id=self.diagnostic_id,
            command=command,
            timeout_seconds=self.timeout_seconds,
            transport=self.transport,
            url=url,
            extract=extract,
        )


class DiagnosticRegistry:
    def __init__(
        self,
        definitions: Dict[str, _Definition],
        approved_actions: List[Dict[str, Any]],
        log_catalog: Optional[LogCatalog] = None,
    ):
        self._definitions = definitions
        self._approved_actions = approved_actions
        self._log_catalog = log_catalog

    @classmethod
    def load(
        cls,
        path: Path,
        log_catalog: Optional[LogCatalog] = None,
        *,
        repo_root: Optional[Path] = None,
    ) -> "DiagnosticRegistry":
        """repo_root 定位环境包(平台上下文、fs-roots);生产须显式传代码仓根。

        不传时按旧布局从文件位置推断(``<根>/config/<白名单>``),只供自建临时仓的测试用。
        """
        try:
            root = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        except Exception as exc:
            raise DiagnosticConfigError(f"诊断白名单不可读: {type(exc).__name__}") from exc
        if not isinstance(root, dict) or root.get("version") != 1:
            raise DiagnosticConfigError("诊断白名单 version 必须是 1")
        raw_definitions = root.get("diagnostics")
        raw_actions = root.get("approved_actions")
        if not isinstance(raw_definitions, list) or not isinstance(raw_actions, list):
            raise DiagnosticConfigError("diagnostics/approved_actions 必须是列表")

        config_path = Path(path)
        if repo_root is None:
            repo_root = (
                config_path.parent.parent
                if config_path.parent.name == "config"
                else config_path.parent
            )
        needs_fs_roots = any(
            isinstance(raw, dict)
            and any(
                isinstance(schema, dict)
                and schema.get("enum_from") == "fs_roots"
                for schema in (raw.get("params") or {}).values()
            )
            for raw in raw_definitions
        )
        fs_roots = (
            _load_fs_roots(
                load_env_pack(repo_root).config_file("fs-roots.yaml"),
                load_env_pack(repo_root).cluster_keys("managed"),
            )
            if needs_fs_roots
            else None
        )

        definitions: Dict[str, _Definition] = {}
        for raw in raw_definitions:
            definition = _parse_definition(
                raw,
                log_catalog,
                fs_roots=fs_roots,
                repo_root=repo_root,
            )
            if definition.diagnostic_id in definitions:
                raise DiagnosticConfigError(
                    f"诊断动作 id 重复: {definition.diagnostic_id}"
                )
            definitions[definition.diagnostic_id] = definition
        return cls(definitions, raw_actions, log_catalog)

    def render(
        self,
        diagnostic_id: str,
        params: Dict[str, Any],
        *,
        platform: str,
    ) -> RenderedDiagnostic:
        definition = self._definitions.get(diagnostic_id)
        if definition is None or platform not in definition.platforms:
            raise self._not_registered(
                diagnostic_id, platform, known=definition is not None,
            )
        if not isinstance(params, dict):
            raise DiagnosticDenied("诊断动作 params 必须是 object")
        return definition.render(params, platform)

    def render_request(
        self,
        request: Dict[str, Any],
        *,
        platform: str,
    ) -> RenderedDiagnostic:
        if not isinstance(request, dict) or set(request) != {
            "diagnostic_id",
            "params",
        }:
            raise DiagnosticDenied("请求必须且只能包含 diagnostic_id 和 params")
        diagnostic_id = request["diagnostic_id"]
        if not isinstance(diagnostic_id, str):
            raise DiagnosticDenied("diagnostic_id 必须是字符串")
        return self.render(diagnostic_id, request["params"], platform=platform)

    def _not_registered(
        self, diagnostic_id: str, platform: str, *, known: bool,
    ) -> DiagnosticDenied:
        """未登记或不适用本平台的 ID 回传本平台相近候选,而不是只说「不行」。"""
        reason = (
            f"诊断动作 {diagnostic_id} 不适用于平台 {platform}"
            if known else f"诊断动作未登记: {diagnostic_id}"
        )
        available = [
            key for key in sorted(self._definitions)
            if platform in self._definitions[key].platforms
        ]
        candidates = _closest_ids(diagnostic_id, available)
        if not available:
            hint = f"平台 {platform} 没有登记诊断动作,请改用 run_command 自由只读命令采证"
        elif candidates:
            hint = "本平台相近的已登记动作: " + ", ".join(candidates)
        else:
            hint = "本平台没有相近的已登记动作;可用 ID 见 diagnostic_id 枚举,或改用 run_command"
        error = DiagnosticDenied(f"{reason}。{hint}")
        error.code = DIAGNOSTIC_NOT_REGISTERED
        return error

    def ids(self) -> List[str]:
        return sorted(self._definitions)

    def catalog(self, platform: Optional[str] = None) -> List[Dict[str, Any]]:
        """诊断清单;给定 platform 时只保留适用项。enum_from 参数不逐项展开
        (5 个 logs 动作 × 上百条路径会重复膨胀),合法值见 log_dirs()。"""
        items: List[Dict[str, Any]] = []
        for key in sorted(self._definitions):
            definition = self._definitions[key]
            if platform is not None and platform not in definition.platforms:
                continue
            entry: Dict[str, Any] = {
                "id": definition.diagnostic_id,
                "platforms": list(definition.platforms),
                "params": definition.params,
                "command": (
                    f"GET {definition.command_template}"
                    if definition.transport == "http"
                    else definition.command_template
                ),
            }
            if definition.note:
                entry["note"] = definition.note
            items.append(entry)
        return items

    def log_dirs(self, platform: Optional[str]) -> List[Dict[str, Any]]:
        """logs.* 动作 log_dir 参数的合法值清单(含服务归属与所在节点)。"""
        if self._log_catalog is None or not platform:
            return []
        return self._log_catalog.describe(platform)

    def approved_action_ids(self) -> List[str]:
        return sorted(
            action["id"]
            for action in self._approved_actions
            if isinstance(action, dict) and isinstance(action.get("id"), str)
        )


def _closest_ids(diagnostic_id: str, available: List[str]) -> List[str]:
    """同前缀(如 mysql.*)在前、字符相似度补位,各自按相似度排序,至多 _MAX_CANDIDATES 个。"""
    if not available:
        return []
    ranked = difflib.get_close_matches(
        diagnostic_id, available, n=len(available), cutoff=0,
    )
    prefix = diagnostic_id.split(".", 1)[0]
    same_prefix = [item for item in ranked if item.split(".", 1)[0] == prefix]
    similar = difflib.get_close_matches(
        diagnostic_id, available, n=_MAX_CANDIDATES, cutoff=0.5,
    )
    return list(dict.fromkeys([*same_prefix, *similar]))[:_MAX_CANDIDATES]


def _parse_definition(
    raw: Any,
    log_catalog: Optional[LogCatalog] = None,
    *,
    fs_roots: Optional[Dict[str, List[str]]] = None,
    repo_root: Optional[Path] = None,
) -> _Definition:
    if not isinstance(raw, dict):
        raise DiagnosticConfigError("诊断动作定义必须是 object")
    diagnostic_id = raw.get("id")
    platforms = raw.get("platforms")
    params = raw.get("params")
    timeout = raw.get("timeout_seconds")
    note = raw.get("note") or ""
    if not isinstance(note, str):
        raise DiagnosticConfigError(f"诊断动作 note 必须是字符串: {diagnostic_id}")
    if not isinstance(diagnostic_id, str) or not diagnostic_id:
        raise DiagnosticConfigError("诊断动作 id 不能为空")
    transport = raw.get("transport", "ssh")
    pack = load_env_pack(repo_root) if repo_root is not None else active_env_pack()
    allowed_platforms = set(pack.cluster_keys("managed"))
    if (
        not isinstance(platforms, list)
        or not platforms
        or any(item not in allowed_platforms for item in platforms)
    ):
        raise DiagnosticConfigError(f"诊断动作平台无效: {diagnostic_id}")
    if not isinstance(params, dict):
        raise DiagnosticConfigError(f"诊断动作 params 必须是 object: {diagnostic_id}")
    reserved_params = set(params) & _RESERVED_PLACEHOLDERS
    if reserved_params:
        raise DiagnosticConfigError(
            f"诊断动作参数使用保留名: {diagnostic_id}: {sorted(reserved_params)}"
        )
    if type(timeout) not in {int, float} or not 0 < float(timeout) <= 180:
        raise DiagnosticConfigError(f"诊断动作超时无效: {diagnostic_id}")

    # MySQL 查询一律经 MySQL 插件,诊断不再直连数据库
    if transport not in {"ssh", "http"}:
        raise DiagnosticConfigError(
            f"诊断动作 transport 无效: {diagnostic_id}: {transport}"
        )
    command = raw.get("command")
    command_template = raw.get("command_template")
    url_template = raw.get("url_template")
    declared_templates = sum(
        isinstance(value, str)
        for value in (command, command_template, url_template)
    )
    if declared_templates != 1:
        raise DiagnosticConfigError(
            "诊断动作 command/command_template/url_template 三者互斥且只能声明一个: "
            f"{diagnostic_id}"
        )
    if transport == "http" and not isinstance(url_template, str):
        raise DiagnosticConfigError(
            f"HTTP 诊断必须声明 url_template: {diagnostic_id}"
        )
    if transport == "ssh" and isinstance(url_template, str):
        raise DiagnosticConfigError(
            f"SSH 诊断不能声明 url_template: {diagnostic_id}"
        )
    template = (
        url_template
        if isinstance(url_template, str)
        else command if isinstance(command, str) else command_template
    )
    if not template:
        raise DiagnosticConfigError(f"诊断动作命令不能为空: {diagnostic_id}")

    extract_template = raw.get("extract")
    # 诊断不再持有任何上游账号(带认证的外部系统改由插件接入)
    if raw.get("auth") is not None:
        raise DiagnosticConfigError(f"诊断动作不支持 auth: {diagnostic_id}")
    if extract_template is not None and (
        transport != "http"
        or not isinstance(extract_template, str)
        or not extract_template
    ):
        raise DiagnosticConfigError(f"诊断动作 extract 无效: {diagnostic_id}")

    try:
        parsed_fields = list(string.Formatter().parse(template))
        extract_fields = (
            list(string.Formatter().parse(extract_template))
            if extract_template is not None
            else []
        )
    except ValueError as exc:
        raise DiagnosticConfigError(
            f"诊断动作模板语法无效: {diagnostic_id}"
        ) from exc
    all_fields = parsed_fields + extract_fields
    fields = {name for _, name, _, _ in all_fields if name is not None}
    endpoint_names = set()
    needs_cluster = False
    for _, name, format_spec, conversion in all_fields:
        if name == "endpoint":
            if (
                not format_spec
                or conversion is not None
                or re.fullmatch(r"[A-Za-z0-9_.-]+", format_spec) is None
            ):
                raise DiagnosticConfigError(
                    f"endpoint 占位符必须声明登记名: {diagnostic_id}"
                )
            endpoint_names.add(format_spec)
        elif name == "cluster":
            if format_spec or conversion is not None:
                raise DiagnosticConfigError(
                    f"cluster 占位符不接受格式参数: {diagnostic_id}"
                )
            needs_cluster = True
    if fields - _RESERVED_PLACEHOLDERS != set(params):
        raise DiagnosticConfigError(
            f"诊断动作模板参数不匹配: {diagnostic_id}"
        )
    _validate_param_schemas(params, template, diagnostic_id)
    value_maps = raw.get("value_maps") or {}
    if not isinstance(value_maps, dict):
        raise DiagnosticConfigError(f"value_maps 必须是 object: {diagnostic_id}")

    platform_enums: Dict[str, Dict[str, List[str]]] = {}
    for name, schema in params.items():
        if not (isinstance(schema, dict) and schema.get("enum_from")):
            continue
        source_name = schema.get("enum_from")
        resolver = _ENUM_SOURCE_RESOLVERS.get(source_name)
        if resolver is None:
            raise DiagnosticConfigError(
                f"参数 {name} 的 enum_from 未知: {source_name}: {diagnostic_id}"
            )
        platform_enums[name] = {}
        for platform in platforms:
            allowed = resolver(platform, log_catalog, fs_roots, diagnostic_id)
            if not allowed:
                raise DiagnosticConfigError(
                    f"诊断动作 {diagnostic_id} 的 enum_from {source_name} "
                    f"在平台 {platform} 没有数据"
                )
            platform_enums[name][platform] = allowed
    return _Definition(
        diagnostic_id=diagnostic_id,
        platforms=tuple(platforms),
        params=params,
        command_template=template,
        value_maps=value_maps,
        timeout_seconds=float(timeout),
        note=note,
        platform_enums=platform_enums,
        endpoint_names=tuple(sorted(endpoint_names)),
        needs_cluster=needs_cluster,
        repo_root=repo_root,
        transport=transport,
        extract_template=extract_template,
    )


class _EndpointValues:
    def __init__(self, values: Dict[str, str]):
        self._values = values

    def __format__(self, name: str) -> str:
        return self._values[name]


def _log_catalog_enum(
    platform: str,
    log_catalog: Optional[LogCatalog],
    _fs_roots: Optional[Dict[str, List[str]]],
    diagnostic_id: str,
) -> List[str]:
    if log_catalog is None:
        raise DiagnosticConfigError(
            f"诊断动作 {diagnostic_id} 需要 log-catalog,"
            "请先用环境包的日志目录采集脚本生成清单"
        )
    return log_catalog.paths(platform)


def _fs_roots_enum(
    platform: str,
    log_catalog: Optional[LogCatalog],
    fs_roots: Optional[Dict[str, List[str]]],
    diagnostic_id: str,
) -> List[str]:
    if fs_roots is None:
        raise DiagnosticConfigError(
            f"诊断动作 {diagnostic_id} 需要 config/fs-roots.yaml"
        )
    return sorted(
        set(_log_catalog_enum(platform, log_catalog, fs_roots, diagnostic_id))
        | set(fs_roots.get(platform) or [])
    )


_ENUM_SOURCE_RESOLVERS = {
    "log_catalog": _log_catalog_enum,
    "fs_roots": _fs_roots_enum,
}


def _load_fs_roots(path: Path, managed: tuple[str, ...]) -> Dict[str, List[str]]:
    try:
        root = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise DiagnosticConfigError("fs-roots.yaml 不可读") from exc
    if not isinstance(root, dict) or root.get("version") != 1:
        raise DiagnosticConfigError("fs-roots.yaml version 必须是 1")
    raw_platforms = root.get("platforms")
    if not isinstance(raw_platforms, dict):
        raise DiagnosticConfigError("fs-roots.yaml platforms 必须是 object")
    platforms: Dict[str, List[str]] = {}
    for platform, items in raw_platforms.items():
        if platform not in managed or not isinstance(items, list):
            raise DiagnosticConfigError(
                f"fs-roots.yaml 平台条目无效: {platform}"
            )
        paths = []
        for item in items:
            value = item.get("path") if isinstance(item, dict) else None
            if (
                not isinstance(value, str)
                or _SAFE_ABSOLUTE_PATH.fullmatch(value) is None
                or ".." in Path(value).parts
            ):
                raise DiagnosticConfigError(
                    f"fs-roots.yaml path 无效: {platform}: {value!r}"
                )
            paths.append(value.rstrip("/") or "/")
        platforms[platform] = sorted(set(paths))
    return platforms


def _validate_param_schemas(
    params: Dict[str, Any],
    template: str,
    diagnostic_id: str,
) -> None:
    for name, schema in params.items():
        if not isinstance(name, str) or not isinstance(schema, dict):
            raise DiagnosticConfigError(
                f"诊断动作参数 schema 无效: {diagnostic_id}.{name}"
            )
        kind = schema.get("type")
        if kind == "integer":
            _validate_integer_schema(name, schema)
        elif kind == "string":
            if schema.get("enum_from"):
                continue   # 动态枚举:合法值在 _parse_definition 由清单展开
            allowed = schema.get("enum")
            if (
                not isinstance(allowed, list)
                or not allowed
                or any(type(value) is not str for value in allowed)
            ):
                raise DiagnosticConfigError(f"参数 {name} 必须声明非空 enum")
        elif kind in {"token", "text"}:
            max_length = schema.get("max_length")
            if type(max_length) is not int or max_length <= 0:
                raise DiagnosticConfigError(
                    f"参数 {name} 必须声明正整数 max_length"
                )
            pattern = schema.get("pattern")
            if pattern is not None:
                try:
                    re.compile(pattern)
                except (re.error, TypeError) as exc:
                    raise DiagnosticConfigError(
                        f"参数 {name} pattern 不是有效正则"
                    ) from exc
            elif _placeholder_starts_unterminated_word(template, name):
                raise DiagnosticConfigError(
                    f"参数 {name} 作为位置实参时前面必须有 -- 终止符"
                )
        else:
            raise DiagnosticConfigError(f"参数 {name} type 不受支持: {kind}")


def _validate_integer_schema(name: str, schema: Dict[str, Any]) -> None:
    has_enum = "enum" in schema
    has_minimum = "min" in schema
    has_maximum = "max" in schema
    if has_enum and (has_minimum or has_maximum):
        raise DiagnosticConfigError(f"参数 {name} 的 enum 与 min/max 互斥")
    if has_enum:
        allowed = schema["enum"]
        if (
            not isinstance(allowed, list)
            or not allowed
            or any(type(value) is not int for value in allowed)
        ):
            raise DiagnosticConfigError(f"参数 {name} 必须声明非空 integer enum")
        return
    if not has_minimum and not has_maximum:
        raise DiagnosticConfigError(f"参数 {name} 必须声明 enum 或 min/max")
    if not has_minimum or not has_maximum:
        raise DiagnosticConfigError(f"参数 {name} 必须同时声明 min 和 max")
    minimum = schema["min"]
    maximum = schema["max"]
    if type(minimum) is not int or type(maximum) is not int:
        raise DiagnosticConfigError(f"参数 {name} 的 min/max 必须是 integer")
    if minimum > maximum:
        raise DiagnosticConfigError(f"参数 {name} 必须满足 min <= max")


def _placeholder_starts_unterminated_word(template: str, name: str) -> bool:
    lexer = shlex.shlex(template, posix=True, punctuation_chars="|&;")
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError as exc:
        raise DiagnosticConfigError("诊断动作模板 shell 引号不完整") from exc

    marker = "{" + name + "}"
    has_terminator = False
    for token in tokens:
        if token and all(character in "|&;" for character in token):
            has_terminator = False
        elif token == "--":
            has_terminator = True
        elif token.startswith(marker) and not has_terminator:
            return True
    return False
