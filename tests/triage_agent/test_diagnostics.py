import inspect
import shlex
from dataclasses import fields
from pathlib import Path

import pytest
import yaml

from lib.cmd_filter import CmdFilter, Level, default_rules_path
from lib.log_catalog import LogCatalog, LogDirEntry
from lib.triage_agent.diagnostics import (
    DIAGNOSTIC_NOT_REGISTERED,
    DiagnosticConfigError,
    DiagnosticDenied,
    DiagnosticRegistry,
    RenderedDiagnostic,
    _Definition,
)
from tests.envpack_helpers import as_env_pack


@pytest.fixture
def registry(tmp_path):
    path = tmp_path / "diagnostics.yaml"
    path.write_text(
        """
version: 1
diagnostics:
  - id: system.memory_summary
    platforms: [alpha, beta]
    params: {}
    command: "free -m"
    timeout_seconds: 15
  - id: system.vmstat_sample
    platforms: [alpha, beta]
    params:
      samples: {type: integer, enum: [3, 5]}
    command_template: "vmstat 1 {samples}"
    timeout_seconds: 15
  - id: logs.tail
    platforms: [alpha]
    params:
      log_key: {type: string, enum: [datanode]}
    value_maps:
      log_key:
        datanode: "/var/log/hadoop-hdfs/datanode.log"
    command_template: "tail -n 50 {log_key}"
    timeout_seconds: 30
approved_actions: []
""",
        encoding="utf-8",
    )
    return DiagnosticRegistry.load(path)


@pytest.fixture
def open_param_registry(tmp_path):
    path = tmp_path / "open-params.yaml"
    path.write_text(
        """
version: 1
diagnostics:
  - id: range.sample
    platforms: [alpha, beta]
    params:
      lines: {type: integer, min: 1, max: 500}
    command_template: "head -n {lines}"
    timeout_seconds: 10
  - id: token.read
    platforms: [alpha, beta]
    params:
      filename: {type: token, max_length: 16}
    value_maps:
      filename:
        safe.log: mapped.log
    command_template: "cat -- {filename}"
    timeout_seconds: 10
  - id: token.pattern
    platforms: [alpha, beta]
    params:
      app_id: {type: token, max_length: 64, pattern: '^application_[0-9]+_[0-9]+$'}
    command_template: "show {app_id}"
    timeout_seconds: 10
  - id: text.search
    platforms: [alpha, beta]
    params:
      keyword: {type: text, max_length: 16}
    value_maps:
      keyword:
        safe: mapped
    command_template: "grep -F -- {keyword}"
    timeout_seconds: 10
approved_actions: []
""",
        encoding="utf-8",
    )
    return DiagnosticRegistry.load(path)


def _registry_path(tmp_path, schema, command_template="show {value}"):
    path = tmp_path / "schema.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "diagnostics": [
                    {
                        "id": "schema.test",
                        "platforms": ["alpha", "beta"],
                        "params": {"value": schema},
                        "command_template": command_template,
                        "timeout_seconds": 10,
                    }
                ],
                "approved_actions": [],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def _context_registry(tmp_path, diagnostic, *, platform_yaml, fs_roots=None, log_catalog=None):
    as_env_pack(tmp_path)
    config = tmp_path / "config"
    (config / "diagnostics.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "diagnostics": [diagnostic],
                "approved_actions": [],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (config / "platform-alpha.yaml").write_text(platform_yaml, encoding="utf-8")
    if fs_roots is not None:
        (config / "fs-roots.yaml").write_text(fs_roots, encoding="utf-8")
    return DiagnosticRegistry.load(config / "diagnostics.yaml", log_catalog=log_catalog)


@pytest.fixture(scope="module")
def repository_registry():
    root = Path(__file__).resolve().parents[2]
    return DiagnosticRegistry.load(
        root / "envs" / "example" / "config" / "agent-diagnostics.yaml",
        log_catalog=LogCatalog.load(root / "envs" / "example" / "config" / "log-catalog.yaml"),
    )


@pytest.fixture(scope="module")
def command_filter():
    return CmdFilter.from_yaml(default_rules_path())


def test_unknown_diagnostic_is_rejected(registry):
    with pytest.raises(DiagnosticDenied, match="未登记"):
        registry.render("system.arbitrary", {}, platform="alpha")


def test_unknown_diagnostic_returns_nearest_registered_candidates(registry):
    with pytest.raises(DiagnosticDenied) as excinfo:
        registry.render("system.memory", {}, platform="alpha")

    assert excinfo.value.code == DIAGNOSTIC_NOT_REGISTERED
    hint = str(excinfo.value).split("。", 1)[1]
    assert hint.endswith("system.memory_summary, system.vmstat_sample")


def test_wrong_platform_lists_candidates_of_the_task_platform(registry):
    with pytest.raises(DiagnosticDenied, match="不适用于平台 beta") as excinfo:
        registry.render("logs.tail", {"log_key": "datanode"}, platform="beta")

    assert excinfo.value.code == DIAGNOSTIC_NOT_REGISTERED
    assert "logs.tail" not in str(excinfo.value).split("。", 1)[1]


def test_platform_without_diagnostics_points_to_run_command(registry):
    with pytest.raises(DiagnosticDenied, match="run_command") as excinfo:
        registry.render("mysql.replication_status", {}, platform="mysql")

    assert excinfo.value.code == DIAGNOSTIC_NOT_REGISTERED


def test_catalog_lists_registered_diagnostics_sorted(registry):
    catalog = registry.catalog()

    assert [entry["id"] for entry in catalog] == [
        "logs.tail",
        "system.memory_summary",
        "system.vmstat_sample",
    ]
    vmstat = next(
        entry for entry in catalog if entry["id"] == "system.vmstat_sample"
    )
    assert vmstat["command"] == "vmstat 1 {samples}"
    assert vmstat["params"] == {"samples": {"type": "integer", "enum": [3, 5]}}
    assert vmstat["platforms"] == ["alpha", "beta"]


@pytest.mark.parametrize("value", ["5; id", "$(id)", "`id`", "5 | sh", "5"])
def test_parameter_shell_injection_and_string_coercion_are_rejected(registry, value):
    with pytest.raises(DiagnosticDenied):
        registry.render("system.vmstat_sample", {"samples": value}, platform="alpha")


def test_model_cannot_submit_a_raw_command(registry):
    with pytest.raises(DiagnosticDenied, match="diagnostic_id"):
        registry.render_request({"cmd": "free -m"}, platform="alpha")


def test_registered_enum_renders_literal_command(registry):
    action = registry.render(
        "system.vmstat_sample",
        {"samples": 3},
        platform="alpha",
    )

    assert action.command == "vmstat 1 3"
    assert action.timeout_seconds == 15


def test_string_parameter_is_mapped_to_a_fixed_value(registry):
    action = registry.render(
        "logs.tail",
        {"log_key": "datanode"},
        platform="alpha",
    )

    assert action.command == "tail -n 50 /var/log/hadoop-hdfs/datanode.log"


def test_extra_parameter_and_wrong_platform_are_rejected(registry):
    with pytest.raises(DiagnosticDenied, match="参数"):
        registry.render(
            "system.memory_summary",
            {"extra": True},
            platform="alpha",
        )
    with pytest.raises(DiagnosticDenied, match="平台"):
        registry.render("logs.tail", {"log_key": "datanode"}, platform="beta")


def test_duplicate_ids_make_the_registry_invalid(tmp_path):
    path = tmp_path / "duplicates.yaml"
    path.write_text(
        """
version: 1
diagnostics:
  - {id: same, platforms: [alpha], params: {}, command: uptime, timeout_seconds: 10}
  - {id: same, platforms: [beta], params: {}, command: uptime, timeout_seconds: 10}
approved_actions: []
""",
        encoding="utf-8",
    )

    with pytest.raises(DiagnosticConfigError, match="重复"):
        DiagnosticRegistry.load(path)


def test_repository_whitelist_loads_all_registered_actions(repository_registry):
    assert set(repository_registry.ids()) == {
        "system.uptime",
        "system.memory_summary",
        "system.vmstat_sample",
        "system.disk_summary",
        "system.process_list",
        "system.top_cpu",
        "system.top_memory",
        "system.oom_history",
        "component.process_check",
        "logs.list",
        "logs.tail",
        "logs.grep",
        "logs.find",
        "logs.grep_files",
        "hdfs.dfsadmin_report",
        "yarn.app_list",
        "yarn.app_status",
        "jvm.gc_stat",
        "fs.list_dir",
        "net.port_check",
        "system.cluster_time",
        "mysql.error_log_files",
    }, "MySQL 查询(含 demo_db)一律经 MySQL 插件,诊断不再直连数据库"
    assert repository_registry.approved_action_ids() == []


@pytest.mark.parametrize(
    ("diagnostic_id", "params", "expected"),
    [
        ("system.uptime", {}, "uptime"),
        ("system.memory_summary", {}, "free -m"),
        ("system.vmstat_sample", {"samples": 3}, "vmstat 1 3"),
        ("system.disk_summary", {}, "df -P"),
        ("system.process_list", {}, "ps -eo pid,args | head -200"),
        ("system.top_cpu", {}, "top -bn1 | head -30"),
        (
            "system.top_memory",
            {},
            "ps -wweo pid,rss,args | sort -k2 -rn | head -20 | cut -c1-200",
        ),
        (
            "system.oom_history",
            {},
            "dmesg -T | grep -iE 'killed process|out of memory' | tail -20",
        ),
        (
            "component.process_check",
            {"component": "datanode"},
            "ps -wweo pid,args | grep -E 'proc_datanode' | grep -v grep | cut -c1-200",
        ),
        (
            "logs.list",
            {"log_dir": "/var/log/hadoop/hdfs", "subdir": ".", "lines": 15},
            "ls -lt /var/log/hadoop/hdfs/. | head -15",
        ),
        (
            "mysql.error_log_files",
            {},
            "find /var/lib/mysql -maxdepth 1 -type f -name '*.err' -print | head -20",
        ),
    ],
)
def test_existing_diagnostic_commands_remain_byte_for_byte_unchanged(
    repository_registry,
    diagnostic_id,
    params,
    expected,
):
    rendered = repository_registry.render(diagnostic_id, params, platform="alpha")

    assert rendered.command == expected


@pytest.mark.parametrize("lines", [1, 500])
def test_integer_range_accepts_closed_boundaries(open_param_registry, lines):
    rendered = open_param_registry.render("range.sample", {"lines": lines}, platform="alpha")

    assert rendered.command == f"head -n {lines}"


@pytest.mark.parametrize("lines", [0, 501, True, 1.0, "1"])
def test_integer_range_rejects_out_of_range_and_non_integer_values(
    open_param_registry,
    lines,
):
    with pytest.raises(DiagnosticDenied, match=r"参数 lines.*(?:区间|integer)"):
        open_param_registry.render("range.sample", {"lines": lines}, platform="alpha")


@pytest.mark.parametrize("keyword", ["quo'te", 'quo"te', "a;b", "`id`", "$(id)", "a|b", "a&b"])
def test_text_shell_metacharacters_are_quoted_as_one_literal_argument(
    open_param_registry,
    keyword,
):
    rendered = open_param_registry.render(
        "text.search",
        {"keyword": keyword},
        platform="alpha",
    )

    assert shlex.split(rendered.command) == ["grep", "-F", "--", keyword]


@pytest.mark.parametrize("keyword", ["line\nfeed", "carriage\rreturn", "nul\x00byte", "tab\tbyte"])
def test_text_rejects_non_printable_characters(open_param_registry, keyword):
    with pytest.raises(DiagnosticDenied, match=r"参数 keyword.*可打印"):
        open_param_registry.render(
            "text.search",
            {"keyword": keyword},
            platform="alpha",
        )


@pytest.mark.parametrize("keyword", ["-v", "-e", "--include=*"])
def test_text_option_like_values_follow_the_option_terminator(
    open_param_registry,
    keyword,
):
    rendered = open_param_registry.render(
        "text.search",
        {"keyword": keyword},
        platform="alpha",
    )

    assert shlex.split(rendered.command) == ["grep", "-F", "--", keyword]


@pytest.mark.parametrize("filename", ["-v", "-e", "--include"])
def test_token_option_like_values_follow_the_option_terminator(
    open_param_registry,
    filename,
):
    rendered = open_param_registry.render(
        "token.read",
        {"filename": filename},
        platform="alpha",
    )

    assert shlex.split(rendered.command) == ["cat", "--", filename]


@pytest.mark.parametrize("filename", ["/", "..", "safe..log", "../../etc/passwd", "safe/name"])
def test_token_rejects_path_traversal_and_path_separators(
    open_param_registry,
    filename,
):
    with pytest.raises(DiagnosticDenied, match=r"参数 filename"):
        open_param_registry.render(
            "token.read",
            {"filename": filename},
            platform="alpha",
        )


def test_token_and_text_max_length_boundaries(open_param_registry):
    token = open_param_registry.render(
        "token.read",
        {"filename": "1234567890123456"},
        platform="alpha",
    )
    text = open_param_registry.render(
        "text.search",
        {"keyword": "1234567890123456"},
        platform="alpha",
    )

    assert shlex.split(token.command)[-1] == "1234567890123456"
    assert shlex.split(text.command)[-1] == "1234567890123456"
    for diagnostic_id, name, value in [
        ("token.read", "filename", "12345678901234567"),
        ("text.search", "keyword", "12345678901234567"),
    ]:
        with pytest.raises(DiagnosticDenied, match=rf"参数 {name}.*长度"):
            open_param_registry.render(
                diagnostic_id,
                {name: value},
                platform="alpha",
            )


def test_token_pattern_only_tightens_the_default_character_set(open_param_registry):
    rendered = open_param_registry.render(
        "token.pattern",
        {"app_id": "application_1_1"},
        platform="alpha",
    )

    assert rendered.command == "show application_1_1"
    for app_id in ["app", "application_1", "application_1_1.extra"]:
        with pytest.raises(DiagnosticDenied, match=r"参数 app_id.*pattern"):
            open_param_registry.render(
                "token.pattern",
                {"app_id": app_id},
                platform="alpha",
            )


@pytest.mark.parametrize(
    ("diagnostic_id", "name", "value"),
    [
        ("token.read", "filename", None),
        ("token.read", "filename", "bad*name"),
        ("token.read", "filename", "--include=*"),
        ("text.search", "keyword", None),
    ],
)
def test_open_parameter_value_errors_are_denied_not_config_errors(
    open_param_registry,
    diagnostic_id,
    name,
    value,
):
    with pytest.raises(DiagnosticDenied, match=rf"参数 {name}"):
        open_param_registry.render(
            diagnostic_id,
            {name: value},
            platform="alpha",
        )


def test_value_maps_apply_only_to_string_parameters(open_param_registry):
    token = open_param_registry.render(
        "token.read",
        {"filename": "safe.log"},
        platform="alpha",
    )
    text = open_param_registry.render(
        "text.search",
        {"keyword": "safe"},
        platform="alpha",
    )

    assert token.command == "cat -- safe.log"
    assert text.command == "grep -F -- safe"


def test_public_render_interface_and_result_fields_are_unchanged():
    assert list(inspect.signature(_Definition.render).parameters) == [
        "self",
        "raw_params",
        "platform",
    ]
    assert [field.name for field in fields(RenderedDiagnostic)] == [
        "diagnostic_id",
        "command",
        "timeout_seconds",
        "transport",
        "url",
        "extract",
    ]


def test_integer_schema_rejects_enum_combined_with_range(tmp_path):
    path = _registry_path(
        tmp_path,
        {"type": "integer", "enum": [3, 5], "min": 1, "max": 10},
    )

    with pytest.raises(DiagnosticConfigError, match=r"参数 value.*互斥"):
        DiagnosticRegistry.load(path)


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "integer", "min": 1},
        {"type": "integer", "max": 10},
        {"type": "integer"},
    ],
)
def test_integer_schema_requires_enum_or_complete_range(tmp_path, schema):
    path = _registry_path(tmp_path, schema)

    with pytest.raises(DiagnosticConfigError, match=r"参数 value.*(?:同时|enum)"):
        DiagnosticRegistry.load(path)


def test_integer_schema_rejects_reversed_range(tmp_path):
    path = _registry_path(
        tmp_path,
        {"type": "integer", "min": 10, "max": 1},
    )

    with pytest.raises(DiagnosticConfigError, match=r"参数 value.*min.*max"):
        DiagnosticRegistry.load(path)


@pytest.mark.parametrize("kind", ["token", "text"])
def test_open_positional_parameter_requires_option_terminator(tmp_path, kind):
    path = _registry_path(
        tmp_path,
        {"type": kind, "max_length": 32},
        "show {value}",
    )

    with pytest.raises(DiagnosticConfigError, match=r"参数 value.*--"):
        DiagnosticRegistry.load(path)


@pytest.mark.parametrize("kind", ["token", "text"])
def test_open_positional_parameter_is_allowed_after_option_terminator(tmp_path, kind):
    path = _registry_path(
        tmp_path,
        {"type": kind, "max_length": 32},
        "show -- {value}",
    )

    loaded = DiagnosticRegistry.load(path)

    assert loaded.ids() == ["schema.test"]


def test_patterned_token_is_exempt_from_option_terminator(tmp_path):
    path = _registry_path(
        tmp_path,
        {"type": "token", "max_length": 32, "pattern": "^[A-Za-z]+$"},
        "show {value}",
    )

    loaded = DiagnosticRegistry.load(path)

    assert loaded.ids() == ["schema.test"]


def test_invalid_parameter_pattern_rejects_registry_load(tmp_path):
    path = _registry_path(
        tmp_path,
        {"type": "token", "max_length": 32, "pattern": "["},
        "show {value}",
    )

    with pytest.raises(DiagnosticConfigError, match=r"参数 value.*pattern.*正则"):
        DiagnosticRegistry.load(path)


@pytest.mark.parametrize(
    ("kind", "max_length"),
    [
        ("token", None),
        ("text", None),
        ("token", 0),
        ("text", -1),
        ("token", 1.5),
        ("text", True),
    ],
)
def test_open_parameter_requires_positive_integer_max_length(
    tmp_path,
    kind,
    max_length,
):
    schema = {"type": kind}
    if max_length is not None:
        schema["max_length"] = max_length
    path = _registry_path(tmp_path, schema, "show -- {value}")

    with pytest.raises(DiagnosticConfigError, match=r"参数 value.*max_length"):
        DiagnosticRegistry.load(path)


def test_string_schema_still_requires_non_empty_enum(tmp_path):
    path = _registry_path(tmp_path, {"type": "string", "enum": []})

    with pytest.raises(DiagnosticConfigError, match=r"参数 value.*非空 enum"):
        DiagnosticRegistry.load(path)


def test_registry_public_method_signatures_are_unchanged():
    # repo_root 只作关键字参数追加(环境包定位),不影响既有调用
    assert list(inspect.signature(DiagnosticRegistry.load).parameters) == [
        "path",
        "log_catalog",
        "repo_root",
    ]
    assert inspect.signature(DiagnosticRegistry.load).parameters["repo_root"].kind is inspect.Parameter.KEYWORD_ONLY
    assert list(inspect.signature(DiagnosticRegistry.render).parameters) == [
        "self",
        "diagnostic_id",
        "params",
        "platform",
    ]
    assert list(inspect.signature(DiagnosticRegistry.catalog).parameters) == [
        "self",
        "platform",
    ]
    assert list(inspect.signature(DiagnosticRegistry.ids).parameters) == ["self"]


def test_reserved_endpoint_and_cluster_placeholders_are_injected(tmp_path):
    registry = _context_registry(
        tmp_path,
        {
            "id": "context.show",
            "platforms": ["alpha"],
            "params": {},
            "command": "show {endpoint:manager}/clusters/{cluster}",
            "timeout_seconds": 10,
        },
        platform_yaml="""
platform: alpha
cluster_name: demo-cluster
endpoints:
  manager: {url: 'http://198.51.100.101:8080'}
nodes: {}
""",
    )

    rendered = registry.render("context.show", {}, platform="alpha")

    assert rendered.command == "show http://198.51.100.101:8080/clusters/demo-cluster"


def test_missing_reserved_endpoint_is_denied_not_a_config_error(tmp_path):
    registry = _context_registry(
        tmp_path,
        {
            "id": "context.show",
            "platforms": ["alpha"],
            "params": {},
            "command": "show {endpoint:manager}",
            "timeout_seconds": 10,
        },
        platform_yaml="""
platform: alpha
cluster_name: demo-cluster
endpoints: {}
nodes: {}
""",
    )

    with pytest.raises(DiagnosticDenied, match="manager"):
        registry.render("context.show", {}, platform="alpha")


@pytest.mark.parametrize("reserved_name", ["endpoint", "cluster"])
def test_reserved_placeholder_names_cannot_be_model_parameters(tmp_path, reserved_name):
    path = _registry_path(
        tmp_path,
        {"type": "string", "enum": ["value"]},
        command_template=f"show {{{reserved_name}}}",
    )
    root = yaml.safe_load(path.read_text(encoding="utf-8"))
    root["diagnostics"][0]["params"] = {
        reserved_name: {"type": "string", "enum": ["value"]}
    }
    path.write_text(yaml.safe_dump(root, sort_keys=False), encoding="utf-8")

    with pytest.raises(DiagnosticConfigError, match="保留"):
        DiagnosticRegistry.load(path)


def test_fs_roots_enum_combines_log_catalog_and_manual_roots(tmp_path):
    log_catalog = LogCatalog(
        {
            "alpha": [
                LogDirEntry(
                    path="/var/log/example",
                    service="EXAMPLE",
                    nodes="all",
                    source="test",
                )
            ]
        }
    )
    registry = _context_registry(
        tmp_path,
        {
            "id": "fs.show",
            "platforms": ["alpha"],
            "params": {"root": {"type": "string", "enum_from": "fs_roots"}},
            "command_template": "ls {root}",
            "timeout_seconds": 10,
        },
        platform_yaml="platform: alpha\ncluster_name: c\nendpoints: {}\nnodes: {}\n",
        fs_roots="""
version: 1
platforms:
  alpha:
    - {path: /opt, note: deployment root}
""",
        log_catalog=log_catalog,
    )

    assert registry.render("fs.show", {"root": "/opt"}, platform="alpha").command == "ls /opt"
    assert (
        registry.render(
            "fs.show", {"root": "/var/log/example"}, platform="alpha"
        ).command
        == "ls /var/log/example"
    )
    with pytest.raises(DiagnosticDenied):
        registry.render("fs.show", {"root": "/etc"}, platform="alpha")


def test_unknown_enum_source_is_a_configuration_error(tmp_path):
    path = _registry_path(
        tmp_path,
        {"type": "string", "enum_from": "unknown_source"},
    )

    with pytest.raises(DiagnosticConfigError, match="unknown_source"):
        DiagnosticRegistry.load(path)


def test_fs_roots_requires_a_valid_versioned_source(tmp_path):
    log_catalog = LogCatalog({"alpha": []})

    with pytest.raises(DiagnosticConfigError, match="fs-roots.*version"):
        _context_registry(
            tmp_path,
            {
                "id": "fs.show",
                "platforms": ["alpha"],
                "params": {
                    "root": {"type": "string", "enum_from": "fs_roots"}
                },
                "command_template": "ls {root}",
                "timeout_seconds": 10,
            },
            platform_yaml="platform: alpha\ncluster_name: c\nendpoints: {}\nnodes: {}\n",
            fs_roots="version: 2\nplatforms: {}\n",
            log_catalog=log_catalog,
        )


NEW_DIAGNOSTIC_CASES = [
    (
        "logs.tail",
        {"log_dir": "/var/log/hadoop/hdfs", "file": "namenode.log", "lines": 50},
        "tail -n 50 /var/log/hadoop/hdfs/namenode.log",
        30,
    ),
    (
        "logs.grep",
        {
            "log_dir": "/var/log",
            "file": "messages",
            "keyword": "OutOfMemoryError",
            "before": 0,
            "after": 0,
            "lines": 20,
        },
        "grep -n -F -B 0 -A 0 -- OutOfMemoryError /var/log/messages "
        "| tail -n 20",
        45,
    ),
    (
        "logs.find",
        {
            "log_dir": "/var/log/hadoop/hdfs",
            "subdir": ".",
            "name": "hadoop-hdfs-*.log*",
            "depth": 1,
            "days": 2,
        },
        "find /var/log/hadoop/hdfs/. -maxdepth 1 -name 'hadoop-hdfs-*.log*' "
        "-mtime -2 2>/dev/null | head -60",
        45,
    ),
    (
        "logs.grep_files",
        {
            "log_dir": "/var/log/hadoop/hdfs",
            "subdir": ".",
            "keyword": "Exception",
            "days": 2,
        },
        "find /var/log/hadoop/hdfs/. -maxdepth 1 -name '*.log' -mtime -2 "
        "2>/dev/null | head -200 | xargs -r grep -l -F -- Exception "
        "2>/dev/null | head -20",
        60,
    ),
    (
        "hdfs.dfsadmin_report",
        {},
        "sudo -n -u hdfs hdfs dfsadmin -report | head -400",
        60,
    ),
    (
        "yarn.app_list",
        {"state": "RUNNING"},
        "yarn application -list -appStates RUNNING 2>/dev/null | head -60",
        60,
    ),
    (
        "yarn.app_status",
        {"app_id": "application_1_1"},
        "yarn application -status application_1_1 2>/dev/null",
        30,
    ),
    (
        "jvm.gc_stat",
        {"pid": 12345, "mode": "gcutil"},
        "jstat -gcutil 12345",
        20,
    ),
]


@pytest.mark.parametrize(
    ("diagnostic_id", "params", "expected_command", "expected_timeout"),
    NEW_DIAGNOSTIC_CASES,
)
def test_new_diagnostic_commands_render_exactly(
    repository_registry,
    diagnostic_id,
    params,
    expected_command,
    expected_timeout,
):
    rendered = repository_registry.render(diagnostic_id, params, platform="alpha")

    assert rendered.command == expected_command
    assert rendered.timeout_seconds == expected_timeout


def test_new_diagnostics_support_both_platforms(repository_registry):
    new_ids = {case[0] for case in NEW_DIAGNOSTIC_CASES}
    catalog = {
        entry["id"]: entry
        for entry in repository_registry.catalog()
        if entry["id"] in new_ids
    }

    assert set(catalog) == new_ids
    assert all(entry["platforms"] == ["alpha", "beta"] for entry in catalog.values())


def test_fs_list_dir_quotes_pattern_after_option_terminator(repository_registry):
    rendered = repository_registry.render(
        "fs.list_dir",
        {"dir": "/opt", "pattern": "*.log", "limit": 5},
        platform="alpha",
    )

    assert "'*.log'" in rendered.command
    tokens = shlex.split(rendered.command)
    assert tokens.index("--") < tokens.index("*.log")
    assert tokens[tokens.index("*.log") - 1] == "-name"
    assert "head -n 5" in rendered.command


def test_fs_list_dir_rejects_unregistered_paths_and_patterns(repository_registry):
    with pytest.raises(DiagnosticDenied):
        repository_registry.render(
            "fs.list_dir",
            {"dir": "/etc", "pattern": "*.log", "limit": 5},
            platform="alpha",
        )
    for pattern in ["..", "a/b", "a;b"]:
        with pytest.raises(DiagnosticDenied):
            repository_registry.render(
                "fs.list_dir",
                {"dir": "/opt", "pattern": pattern, "limit": 5},
                platform="alpha",
            )


@pytest.mark.parametrize("limit", [0, 101])
def test_fs_list_dir_limit_is_bounded(repository_registry, limit):
    with pytest.raises(DiagnosticDenied):
        repository_registry.render(
            "fs.list_dir",
            {"dir": "/opt", "pattern": "*", "limit": limit},
            platform="alpha",
        )


@pytest.mark.parametrize("port", [0, 65536])
def test_net_port_check_rejects_out_of_range_ports(repository_registry, port):
    with pytest.raises(DiagnosticDenied):
        repository_registry.render(
            "net.port_check", {"port": port}, platform="alpha"
        )


def test_cluster_time_includes_epoch_and_process_list_has_limit(repository_registry):
    rendered_time = repository_registry.render(
        "system.cluster_time", {}, platform="alpha"
    )
    rendered_processes = repository_registry.render(
        "system.process_list", {}, platform="alpha"
    )

    assert "date +%s" in rendered_time.command
    assert "head -200" in rendered_processes.command


def test_net_port_check_groups_grep_failure_to_print_not_listening_notice(
    repository_registry,
):
    rendered = repository_registry.render(
        "net.port_check", {"port": 65535}, platform="alpha"
    )

    assert "| { grep -E --" in rendered.command
    assert "|| echo port 65535 is not listening; } | head -20" in rendered.command


@pytest.mark.parametrize(
    ("diagnostic_id", "params", "_expected_command", "_expected_timeout"),
    NEW_DIAGNOSTIC_CASES,
)
def test_new_diagnostic_commands_are_readonly_under_real_rules(
    repository_registry,
    command_filter,
    diagnostic_id,
    params,
    _expected_command,
    _expected_timeout,
):
    rendered = repository_registry.render(diagnostic_id, params, platform="beta")

    assert command_filter.classify(rendered.command).level is Level.READONLY


@pytest.mark.parametrize(
    "log_dir",
    [
        "/var/log/hadoop/hdfs",
        "/var/log/hbase",
        "/var/log/kafka",
        "/var/log/zookeeper",
        "/var/log/storm",
        "/var/log/hive",
        "/var/log",
        "/opt/app/jobs/logs",
    ],
)
def test_log_actions_share_log_catalog_paths(repository_registry, log_dir):
    """logs.* 动作共享同一份 log-catalog 合法值:同一路径三个动作都能渲染。"""
    listed = repository_registry.render(
        "logs.list",
        {"log_dir": log_dir, "subdir": ".", "lines": 15},
        platform="alpha",
    )
    tailed = repository_registry.render(
        "logs.tail",
        {"log_dir": log_dir, "file": "service.log", "lines": 1},
        platform="alpha",
    )
    searched = repository_registry.render(
        "logs.grep",
        {
            "log_dir": log_dir,
            "file": "service.log",
            "keyword": "error",
            "before": 0,
            "after": 0,
            "lines": 1,
        },
        platform="alpha",
    )

    assert listed.command == f"ls -lt {log_dir}/. | head -15"
    assert tailed.command == f"tail -n 1 {log_dir}/service.log"
    assert searched.command.startswith(
        f"grep -n -F -B 0 -A 0 -- error {log_dir}/service.log"
    )


@pytest.mark.parametrize("app_id", ["-v", "app", "application_1"])
def test_yarn_app_status_rejects_invalid_app_ids(repository_registry, app_id):
    with pytest.raises(DiagnosticDenied, match=r"参数 app_id"):
        repository_registry.render(
            "yarn.app_status",
            {"app_id": app_id},
            platform="alpha",
        )


def test_logs_grep_shell_metacharacters_are_quoted_then_literal(
    repository_registry,
    command_filter,
):
    """转义是第一道防线(断言 shlex.quote 生效);红线按引号上下文判命令替换:

    含单引号的 keyword 经 shlex.quote 产出 `'a'"'"'b'` 形态,`$(id)` 与反引号
    全部落在单引号里,shell 不展开,红线判只读(A2 §3.2)。裸露的命令替换
    仍判 WRITE,见 test_cmd_filter 与 test_command_guard。
    """
    keyword = "error'; $(id) | `id` &"
    rendered = repository_registry.render(
        "logs.grep",
        {
            "log_dir": "/var/log",
            "file": "messages",
            "keyword": keyword,
            "before": 0,
            "after": 0,
            "lines": 10,
        },
        platform="alpha",
    )

    assert f"grep -n -F -B 0 -A 0 -- {shlex.quote(keyword)}" in rendered.command
    assert command_filter.classify(rendered.command).level is Level.READONLY
    assert command_filter.classify(f"grep -F -- {keyword} /var/log/messages").level is Level.WRITE


@pytest.mark.parametrize("diagnostic_id", ["logs.tail", "logs.grep"])
def test_nginx_log_directory_maps_to_verified_path(repository_registry, diagnostic_id):
    """nginx 目录经集群实测确认为 /var/log/nginx 后登记(beta node1)。"""
    params = {"log_dir": "/var/log/nginx", "file": "error.log", "lines": 10}
    if diagnostic_id == "logs.grep":
        params.update(keyword="geteventlist", before=0, after=0)

    rendered = repository_registry.render(diagnostic_id, params, platform="beta")
    assert "/var/log/nginx/error.log" in rendered.command


@pytest.mark.parametrize("diagnostic_id", ["logs.tail", "logs.grep"])
@pytest.mark.parametrize("archive", ["access.log-20260802.gz", "old.log.bz2", "a.zip"])
def test_compressed_archive_filename_rejected(
        repository_registry, diagnostic_id, archive):
    """压缩归档读出来是二进制,grep 只回「Binary file matches」,易被当成有结论。"""
    params = {"log_dir": "/var/log/nginx", "file": archive, "lines": 10}
    if diagnostic_id == "logs.grep":
        params.update(keyword="error", before=0, after=0)

    with pytest.raises(DiagnosticDenied, match=r"参数 file"):
        repository_registry.render(diagnostic_id, params, platform="beta")


@pytest.mark.parametrize(
    ("params", "match"),
    [
        # 选项注入:find 的 name/subdir 禁首横线
        ({"log_dir": "/var/log", "subdir": ".", "name": "-delete",
          "depth": 1, "days": 1}, r"参数 name"),
        ({"log_dir": "/var/log", "subdir": "-P", "name": "x.log",
          "depth": 1, "days": 1}, r"参数 subdir"),
        # 路径穿越
        ({"log_dir": "/var/log", "subdir": "../etc", "name": "x.log",
          "depth": 1, "days": 1}, r"参数 subdir"),
    ],
)
def test_logs_find_rejects_option_injection_and_traversal(
    repository_registry, params, match,
):
    with pytest.raises(DiagnosticDenied, match=match):
        repository_registry.render("logs.find", params, platform="alpha")


def test_logs_tail_rejects_path_traversal_in_file(repository_registry):
    with pytest.raises(DiagnosticDenied, match=r"参数 file"):
        repository_registry.render(
            "logs.tail",
            {"log_dir": "/var/log", "file": "../../etc/passwd", "lines": 5},
            platform="alpha",
        )


def test_unregistered_log_dir_is_rejected_per_platform(repository_registry):
    with pytest.raises(DiagnosticDenied, match="登记的日志目录"):
        repository_registry.render(
            "logs.list",
            {"log_dir": "/etc", "subdir": ".", "lines": 15},
            platform="alpha",
        )
    # alpha 专属路径在 beta 平台同样必须拒绝
    with pytest.raises(DiagnosticDenied, match="登记的日志目录"):
        repository_registry.render(
            "logs.list",
            {"log_dir": "/opt/app/jobs/logs",
             "subdir": ".", "lines": 15},
            platform="beta",
        )


def test_log_dirs_view_exposes_service_and_nodes(repository_registry):
    dirs = repository_registry.log_dirs("alpha")
    by_path = {entry["path"]: entry for entry in dirs}

    ts = by_path["/opt/app/jobs/logs"]
    assert ts["service"] == "jobs"
    assert isinstance(ts["nodes"], list) and "node3" in ts["nodes"]
    assert by_path["/var/log"]["nodes"] == "all"
    assert repository_registry.log_dirs(None) == []
