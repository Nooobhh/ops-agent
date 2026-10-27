"""路径策略:命令能碰哪些路径。

按路径管而不是按工具管,因为能把文件内容打到 stdout 的工具有十几个,
路径是它们唯一的共同收口。
"""
import pytest

from lib.triage_agent.command_grammar import check_pipeline
from lib.triage_agent.command_policy import command_signature
from lib.triage_agent.path_policy import (
    GUIDANCE,
    PathPolicy,
    check_paths,
    offending_paths,
)


# ---------------- 读取范围 ----------------

@pytest.mark.parametrize("tool_argv", [
    ["cat", "/etc/shadow"],
    ["head", "-n", "50", "/etc/shadow"],
    ["tail", "-n", "50", "/etc/shadow"],
    ["grep", "-v", "zzz", "/etc/shadow"],
    ["awk", "{print}", "/etc/shadow"],
    ["sed", "-n", "p", "/etc/shadow"],
    ["nl", "/etc/shadow"],
    ["cut", "-d", ":", "-f", "1", "/etc/shadow"],
    ["zcat", "/etc/shadow"],
])
def test_every_reading_tool_is_stopped_at_the_same_path(tool_argv):
    """九种工具都能读同一个文件,所以必须在路径上收口而不是逐工具堵。"""
    assert check_pipeline([tool_argv]).ok is False


@pytest.mark.parametrize("path", [
    "/root/.ssh/id_rsa",
    "/home/deploy/.ssh/authorized_keys",
    "/etc/shadow",
    "/etc/sudoers",
    "/etc/ssh/ssh_host_rsa_key",
    "/opt/app/server.pem",
    "/opt/app/keystore.jks",
    "/proc/1234/environ",
    "/proc/kcore",
    "/home/x/.aws/credentials",
])
def test_credential_paths_are_refused_even_inside_allowed_roots(path):
    """deny 优先于 allow:/opt 是允许根,但它下面的 .pem 仍要拦。"""
    assert check_paths([["cat", path]]).ok is False


@pytest.mark.parametrize("path", [
    "/var/log/messages",
    "/var/log/hadoop/hdfs/namenode.log",
    "/usr/lib/datahub/logs/app.log",
    "/data1/hadoop/yarn/log/container_1/stdout",
    "/proc/meminfo",
    "/proc/1234/status",
    "/sys/block/sda/queue/scheduler",
    "/etc/hosts",
    "/etc/passwd",
    "/tmp/opsagent/scratch.txt",
])
def test_ordinary_investigation_paths_are_allowed(path):
    verdict = check_paths([["cat", path]])
    assert verdict.ok, path
    assert verdict.paths == [path]


def test_path_verdict_keeps_all_recognised_paths_even_when_one_is_out_of_bounds():
    verdict = check_paths([
        ["cat", "/var/log/messages", "/etc/hive/conf/hive-site.xml"],
    ])

    assert verdict.paths == ["/var/log/messages", "/etc/hive/conf/hive-site.xml"]


def test_relative_paths_are_refused():
    """相对路径的落点取决于远端 cwd,判不了就不放行。"""
    assert check_paths([["cat", "var/log/messages"]]).ok is False


def test_dotdot_escape_is_normalised_before_checking():
    assert check_paths([["cat", "/var/log/../../root/.ssh/id_rsa"]]).ok is False


# ---------------- 扫描范围 ----------------

def test_recursive_scan_from_filesystem_root_is_refused():
    assert check_paths([["grep", "-rn", "password", "/"]]).ok is False


def test_recursive_scan_from_an_allowed_root_is_still_too_broad():
    """/var/log 下 catalog 记了 103 个目录,拿它当递归起点等于扫全部。"""
    assert check_paths([["grep", "-r", "x", "/var/log"]]).ok is False
    assert check_paths([["grep", "-r", "x", "/var/log/hadoop"]]).ok


def test_find_from_an_allowed_root_needs_a_depth_bound():
    assert check_paths([["find", "/var/log", "-name", "*.log"]]).ok is False
    assert check_paths([["find", "/var/log", "-maxdepth", "2", "-name", "*.log"]]).ok
    assert check_paths([["find", "/var/log/hadoop", "-name", "*.log"]]).ok


# ---------------- 不同命名空间 ----------------

@pytest.mark.parametrize("argv", [
    ["hdfs", "dfs", "-ls", "/user/hive/warehouse"],
    ["hdfs", "dfs", "-du", "/apps/spark"],
    ["hadoop", "fs", "-ls", "/tmp/whatever"],
    ["hdfs", "dfsadmin", "-report"],
])
def test_hdfs_paths_are_not_judged_by_local_filesystem_roots(argv):
    """`/user/hive/warehouse` 在 HDFS 上,与本地 / 无关;拿本地允许根卡它是错的。
    HDFS 侧的范围由 hdfs 自己的子动作正表约束。"""
    assert check_pipeline([argv]).ok, argv


# ---------------- 程序文本不是路径 ----------------

@pytest.mark.parametrize("argv", [
    ["awk", "/error|warn/{print}", "/var/log/x.log"],
    ["sed", "-n", "/error/p", "/var/log/x.log"],
    ["sed", "s/a/b/g", "/var/log/x.log"],
    ["awk", "-F", ":", "{print $1}", "/etc/passwd"],
])
def test_regex_addresses_are_not_mistaken_for_paths(argv):
    """awk 的 `/error|warn/` 与 sed 的 `/error/p` 都以 / 开头,却不是路径。"""
    assert check_pipeline([argv]).ok, argv


def test_awk_and_sed_still_have_their_input_files_checked():
    """跳过的只是程序文本,它们读的文件照查不误。"""
    assert check_pipeline([["awk", "{print}", "/etc/shadow"]]).ok is False
    assert check_pipeline([["sed", "-n", "p", "/root/.ssh/id_rsa"]]).ok is False


# ---------------- 拒绝必须可操作 ----------------

def test_refusal_tells_the_model_what_to_do_instead():
    """拒绝不能是死胡同。没有出路的话模型只会像没有自由命令时那样直接放弃,
    退回到「读两篇文档就 verdict=insufficient」。"""
    verdict = check_paths([["cat", "/etc/hive/conf/hive-site.xml"]])

    assert verdict.ok is False
    assert GUIDANCE in verdict.reason
    assert "配置查询工具" in verdict.reason, "要指出配置项的正路"
    assert "人工裁决" in verdict.reason, "要说明确需读文件时怎么办"


def test_refusal_names_the_offending_path():
    verdict = check_paths([["cat", "/etc/hive/conf/hive-site.xml"]])

    assert verdict.offending == "/etc/hive/conf/hive-site.xml"


# ---------------- 与签名的耦合 ----------------

def test_out_of_bounds_path_enters_the_signature():
    """批准读某个越界文件,不能等于批准该工具读一切。

    签名若只是 `cat`,人看到的是一个文件、批出去的是整个工具——这与
    Codex 报的「签名丢信息导致批准范围扩大」是同一类问题。
    """
    approved = command_signature([["cat", "/etc/hive/conf/hive-site.xml"]])
    other = command_signature([["cat", "/root/.ssh/id_rsa"]])

    assert approved != other
    assert "/etc/hive/conf/hive-site.xml" in approved


def test_in_bounds_paths_do_not_bloat_the_signature():
    """允许范围内的路径不进签名,泛化能力照旧,名单不会被同一命令的变体撑爆。"""
    a = command_signature([["cat", "/var/log/messages"]])
    b = command_signature([["cat", "/var/log/secure"]])

    assert a == b == "cat"


def test_offending_paths_ignores_foreign_namespaces():
    """HDFS 路径不该污染签名。"""
    assert offending_paths([["hdfs", "dfs", "-ls", "/user/hive/warehouse"]]) == []


# ---------------- 配置可覆盖 ----------------

def test_custom_allow_roots_are_honoured():
    policy = PathPolicy(allow_roots=("/srv/app",))

    assert check_paths([["cat", "/srv/app/x.log"]], policy).ok
    assert check_paths([["cat", "/var/log/messages"]], policy).ok is False


def test_deny_still_applies_under_custom_roots():
    policy = PathPolicy(allow_roots=("/srv",))

    assert check_paths([["cat", "/srv/keys/server.pem"]], policy).ok is False


# ---------------- 日志目录以 管理面 为准 ----------------

def test_log_roots_come_from_the_collected_catalog():
    """日志目录的真源是 管理面(经 collect_log_catalog 落到 log-catalog.yaml)。

    组件升级换路径时 管理面 会跟着变,硬编码不会——实测就有 4 个 自研组件
    把日志放在 /usr/lib/*/logs 而非 /opt/app。
    """
    from lib.triage_agent.path_policy import load_path_policy, log_roots_from_catalog

    catalog = {"platforms": {"alpha": {"services": {
        "datahub": {"dirs": [{"path": "/usr/lib/datahub/logs"}]},
    }}}}
    roots = log_roots_from_catalog(catalog)
    policy = load_path_policy(catalog=catalog)

    assert "/usr/lib/datahub/logs" in roots
    assert check_paths([["cat", "/usr/lib/datahub/logs/app.log"]], policy).ok


def test_missing_catalog_falls_back_to_bounded_roots_not_everything():
    """catalog 读不到时要能兜底,但兜底项仍是有界的几个根,不是放开 /。"""
    from lib.triage_agent.path_policy import BASE_ALLOW_ROOTS, load_path_policy

    policy = load_path_policy(catalog=None)

    assert check_paths([["cat", "/var/log/messages"]], policy).ok
    assert check_paths([["cat", "/etc/shadow"]], policy).ok is False
    assert check_paths([["cat", "/some/random/place"]], policy).ok is False
    assert set(BASE_ALLOW_ROOTS).issubset(set(policy.allow_roots))


def test_refusal_mentions_a_stale_catalog_as_a_possible_cause():
    """日志目录被拒时要提醒可能是 catalog 未重采,而不是让模型以为查不了。

    用 catalog 驱动的策略:它精确到已登记目录,组件升级后的新版本目录会落空,
    这正是最容易被误读成「查不了」的情形。
    """
    from lib.triage_agent.path_policy import load_path_policy

    policy = load_path_policy(catalog={"platforms": {"alpha": {"services": {
        "datahub": {"dirs": [{"path": "/srv/app/4.0.5/datahub/logs"}]},
    }}}})

    assert check_paths([["cat", "/srv/app/4.0.5/datahub/logs/a.log"]], policy).ok
    verdict = check_paths(
        [["cat", "/srv/app/9.9.9/datahub/logs/a.log"]], policy
    )
    assert verdict.ok is False
    assert "log-catalog" in verdict.reason


def test_log_roots_are_var_log_union_demo():
    """日志允许根 = /var/log ∪ 管理面 采集到的目录。

    只用 catalog 会挡住 messages/secure/cron 这些不归 管理面 管的系统日志;
    只用 /var/log 又会漏掉 /usr/lib/*/logs 与 /data*/hadoop/yarn/log。
    """
    from lib.triage_agent.path_policy import load_path_policy

    policy = load_path_policy(catalog={"platforms": {"alpha": {"services": {
        "datahub": {"dirs": [{"path": "/usr/lib/datahub/logs"}]},
    }}}})

    # /var/log 侧:catalog 里没有,照样放行
    assert check_paths([["cat", "/var/log/messages"]], policy).ok
    assert check_paths([["cat", "/var/log/secure"]], policy).ok
    # 管理面 侧:不在 /var/log 下,靠 catalog 才进得来
    assert check_paths([["cat", "/usr/lib/datahub/logs/app.log"]], policy).ok
    # 两边都不沾的仍然拒(/opt 是基础根,不能拿它当反例)
    assert check_paths([["cat", "/srv/other/x.log"]], policy).ok is False


def test_var_log_stays_allowed_even_with_an_empty_catalog():
    """采集失败或 catalog 为空时,系统日志不能跟着一起失守。"""
    from lib.triage_agent.path_policy import load_path_policy

    policy = load_path_policy(catalog={})

    assert check_paths([["cat", "/var/log/messages"]], policy).ok


def test_environment_deployment_roots_come_from_fs_roots_not_the_core_fallback():
    """环境专有的部署根(组件安装根)只经环境包 fs-roots 并入,核心兜底根不写死。"""
    from lib.triage_agent.path_policy import load_path_policy

    path = "/srv/vendor/app/logs/app.log"
    assert check_paths([["cat", path]]).ok is False
    policy = load_path_policy({"platforms": {"alpha": [{"path": "/srv/vendor", "note": "部署根"}]}})
    assert check_paths([["cat", path]], policy).ok is True
