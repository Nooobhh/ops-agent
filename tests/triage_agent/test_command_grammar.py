"""内置工具正表。核心契约:未登记的选项/工具一律拒绝。

语料有两组来源,都不是凭空想的:
  GOOD —— 一次真实重放里模型实际敲过的命令,加常见排查组合
  BAD  —— Codex 两轮审查报出的绕过,加已知危险形态
"""
import pytest

from lib.triage_agent.command_grammar import TOOLS, check_pipeline

# 真实排查语料:这些必须能过,否则正表等于把功能废了
GOOD = [
    [["mysql", "demo_db", "-N", "-e", "show tables"]],
    [["mysql", "demo_db", "-N", "-B", "-e",
      "select TASK_TYPE,count(*) from P_TASK_RUN group by EXEC_DATE"]],
    [["ls", "-lh", "/tmp/x.tsv"]],
    [["find", "/usr/lib", "-maxdepth", "7", "-type", "d", "-path", "*proc*"]],
    [["grep", "-c", "ERROR", "/var/log/n.log"]],
    [["grep", "-rn", "--include=*.log", "OutOfMemory", "/var/log/hadoop/"]],
    [["tail", "-n", "500", "/var/log/messages"], ["grep", "-i", "oom"]],
    [["awk", "{print $1}", "/var/log/x.log"], ["sort"], ["uniq", "-c"],
     ["sort", "-rn"], ["head", "-n", "20"]],
    [["ps", "-eo", "args="], ["grep", "java"], ["head", "-n", "20"]],
    [["ps", "-ef"], ["grep", "-v", "grep"], ["wc", "-l"]],
    [["free", "-m"]],
    [["df", "-h"]],
    [["netstat", "-anp"], ["grep", "8020"]],
    [["hdfs", "dfs", "-ls", "/user/hive/warehouse"]],
    [["hdfs", "dfsadmin", "-report"]],
    [["yarn", "application", "-list"]],
    [["jstat", "-gcutil", "12345"]],
    [["systemctl", "status", "hadoop-hdfs-namenode", "--no-pager"]],
    [["journalctl", "-u", "hive", "--since", "1 hour ago", "--no-pager"],
     ["tail", "-n", "100"]],
    [["sed", "-n", "100,200p", "/var/log/x.log"]],
]

# Codex 两轮报的绕过 + 已知危险形态:一条都不能放行
BAD = [
    ([["command", "sort", "-o", "/etc/pwn", "/etc/hosts"]], "wrapper 掩盖命令词"),
    ([["/tmp/cat", "/etc/hosts"]], "路径直指非标准二进制"),
    ([["wget", "http://host/x"]], "默认写文件"),
    ([["tar", "-xvf", "/tmp/a.tar"]], "解包写文件"),
    ([["sort", "-o/tmp/pwn", "/etc/hosts"]], "短选项附值写文件"),
    ([["sort", "--compress-program", "/tmp/evil", "/var/log/x.log"]], "执行任意二进制"),
    ([["curl", "-dfoo", "http://host/"]], "提交数据"),
    ([["sed", "-i.bak", "s/a/b/", "/etc/hosts"]], "原地改写"),
    ([["xargs", "sh", "-c", "touch /tmp/x"]], "转发执行"),
    ([["env", "LC_ALL=C", "rm", "-f", "/tmp/x"]], "wrapper 执行任意命令"),
    ([["awk", 'BEGIN{system("rm -f /tmp/x")}']], "awk system()"),
    ([["awk", '{print > "/etc/pwn"}', "/var/log/x.log"]], "awk 重定向写文件"),
    ([["sed", "-n", "1e rm /tmp/x", "/var/log/x.log"]], "sed e 执行"),
    ([["sed", "s/a/b/w /etc/pwn", "/var/log/x.log"]], "sed w 写文件"),
    ([["tail", "-f", "/var/log/messages"]], "永不退出"),
    ([["mysql", "-e", "create table t(i int)"]], "DDL"),
    ([["mysql", "-e", "select * into outfile '/tmp/x' from t"]], "SQL 写文件"),
    ([["jmap", "-dump:format=b,file=/tmp/heap", "1234"]], "写巨型 dump"),
    ([["hdfs", "dfs", "-copyFromLocal", "/tmp/x", "/data/x"]], "写 HDFS"),
    ([["hdfs", "dfsadmin", "-safemode", "enter"]], "改集群状态"),
    ([["yarn", "application", "-kill", "app_1"]], "杀任务"),
    ([["systemctl", "restart", "hdfs"]], "重启服务"),
    ([["ss", "-K", "dst", "1.2.3.4"]], "强杀连接"),
    ([["dmesg", "-C"]], "清空内核缓冲"),
    ([["date", "-s", "2020-01-01"]], "改系统时间"),
    ([["hbase", "shell"]], "交互式 shell"),
    ([["perl", "-e", "unlink '/tmp/x'"]], "解释器"),
    ([["python", "-c", "import os;os.system('id')"]], "解释器"),
    ([["find", "/var/log", "-name", "*.log", "-delete"]], "find -delete"),
    ([["find", "/tmp", "-exec", "rm", "{}", ";"]], "find -exec"),
]


@pytest.mark.parametrize("pipeline", GOOD, ids=lambda p: p[0][0])
def test_real_investigation_commands_are_allowed(pipeline):
    verdict = check_pipeline(pipeline)
    assert verdict.ok, verdict.reason


@pytest.mark.parametrize("pipeline,why", BAD, ids=lambda x: x if isinstance(x, str) else "")
def test_dangerous_forms_are_refused(pipeline, why):
    assert check_pipeline(pipeline).ok is False, f"应拒绝({why}): {pipeline}"


# ---------------- 选项解析的边界 ----------------

def test_unknown_option_is_refused_not_ignored():
    """白名单的立身之本:未登记选项必须拒。默认放行就退化成黑名单了。"""
    assert check_pipeline([["grep", "--zzz", "x", "/var/log/x.log"]]).ok is False
    assert check_pipeline([["ls", "-Z"]]).ok is False


def test_short_option_clusters_are_understood():
    """`-lh` / `-anp` 是簇不是内联附值,拆错会把日常命令全拒掉。"""
    assert check_pipeline([["ls", "-lh", "/tmp"]]).ok
    assert check_pipeline([["netstat", "-anp"]]).ok
    # 簇里混入未登记字母仍要拒
    assert check_pipeline([["ls", "-lhZ", "/tmp"]]).ok is False


def test_cluster_may_end_with_a_value_taking_option():
    """`ps -eo args=`:簇尾是带值选项,值在下一个 token。"""
    assert check_pipeline([["ps", "-eo", "args="]]).ok


def test_single_dash_long_options_are_not_split_into_clusters():
    """`-maxdepth` 是 find 的单横线长选项,不能按 `-m`+`axdepth` 拆。"""
    assert check_pipeline([["find", "/tmp", "-maxdepth", "2"]]).ok


def test_inline_value_only_allowed_for_value_taking_options():
    assert check_pipeline([["head", "-n50", "/var/log/x.log"]]).ok
    # -o 不在 sort 的正表里,附值写法也救不回来
    assert check_pipeline([["sort", "-o/tmp/pwn", "/var/log/x.log"]]).ok is False


def test_value_taking_option_needs_its_value():
    assert check_pipeline([["grep", "-A"]]).ok is False


def test_tool_name_must_be_bare_not_a_path():
    assert check_pipeline([["/usr/bin/cat", "/etc/hosts"]]).ok is False
    assert check_pipeline([["./cat", "/etc/hosts"]]).ok is False
    assert check_pipeline([["cat", "/etc/hosts"]]).ok


def test_double_dash_ends_option_parsing():
    assert check_pipeline([["grep", "-F", "--", "-weird-pattern", "/var/log/x.log"]]).ok


# ---------------- 子命令与动作 ----------------

def test_subcommand_must_be_registered():
    assert check_pipeline([["hdfs", "namenode", "-format"]]).ok is False
    assert check_pipeline([["yarn", "rmadmin", "-refreshNodes"]]).ok is False


def test_sub_action_must_be_registered():
    assert check_pipeline([["hdfs", "dfs", "-ls", "/var/log/x.log"]]).ok
    assert check_pipeline([["hdfs", "dfs", "-rm", "/var/log/x.log"]]).ok is False


def test_missing_subcommand_is_refused():
    assert check_pipeline([["hdfs"]]).ok is False


# ---------------- 参数即代码 ----------------

@pytest.mark.parametrize("program", [
    'BEGIN{system("id")}',
    '{print > "/tmp/x"}',
    '{printf "%s" > "/tmp/x"}',
    '{while(("id"|&getline line)>0) print line}',
    '{getline < "/etc/shadow"}',
    'BEGIN{print ENVIRON["AWS_SECRET"]}',
])
def test_awk_programs_that_escape_read_only_are_refused(program):
    assert check_pipeline([["awk", program, "/var/log/x.log"]]).ok is False


def test_ordinary_awk_programs_still_work():
    assert check_pipeline([["awk", "{print $1}", "/var/log/x.log"]]).ok
    assert check_pipeline([["awk", "-F", ":", "{print $1,$3}", "/etc/passwd"]]).ok
    assert check_pipeline([["awk", "$3>100{c++}END{print c}", "/var/log/x.log"]]).ok


def test_sed_scripts_that_execute_or_write_are_refused():
    assert check_pipeline([["sed", "-e", "1e id", "/var/log/x.log"]]).ok is False
    assert check_pipeline([["sed", "s/a/b/w /tmp/out", "/var/log/x.log"]]).ok is False


def test_ordinary_sed_scripts_still_work():
    assert check_pipeline([["sed", "-n", "10,20p", "/var/log/x.log"]]).ok
    assert check_pipeline([["sed", "s/foo/bar/g", "/var/log/x.log"]]).ok


@pytest.mark.parametrize("sql", [
    "insert into t values(1)",
    "update t set a=1",
    "delete from t",
    "drop table t",
    "create table t(i int)",
    "select 1 into outfile '/tmp/x'",
    "select load_file('/etc/shadow')",
    "select sleep(100)",
])
def test_non_read_only_sql_is_refused(sql):
    assert check_pipeline([["mysql", "db", "-e", sql]]).ok is False


def test_read_only_sql_is_allowed():
    for sql in [
        "select count(*) from t",
        "show tables",
        "describe t",
        "explain select * from t",
        "with x as (select 1) select * from x",
    ]:
        assert check_pipeline([["mysql", "db", "-N", "-e", sql]]).ok, sql


def test_mysql_without_an_explicit_statement_is_refused():
    """不给 -e 就意味着走 stdin 或交互,内容无从校验。"""
    assert check_pipeline([["mysql", "db"]]).ok is False


@pytest.mark.parametrize("args", [
    ["-NBe", "SHOW SLAVE STATUS\\G"],
    ["-eSELECT 1"],
    ["--execute=show tables"],
    ["-NBe", "SHOW VARIABLES LIKE 'log_error'; SHOW VARIABLES LIKE 'datadir'"],
])
def test_mysql_clustered_and_inline_statements_are_accepted(args):
    assert check_pipeline([["mysql", *args]]).ok, args


@pytest.mark.parametrize("args", [
    ["-e", "select 1", "-Be", "drop table t"],
    ["-e", "select 1", "-edrop table t"],
    ["-e", "select 1", "--execute=drop table t"],
    ["-NBe", "delete from t"],
])
def test_every_mysql_statement_is_checked_whatever_the_option_form(args):
    """簇写/内联的 -e 取值也是要执行的语句:只认独立 -e 时,第二条可绕过只读检查。"""
    assert check_pipeline([["mysql", *args]]).ok is False, args


def test_multi_statement_is_checked_per_statement():
    ok = check_pipeline([["mysql", "db", "-e", "select 1; show tables"]])
    bad = check_pipeline([["mysql", "db", "-e", "select 1; drop table t"]])

    assert ok.ok is True
    assert bad.ok is False


# ---------------- 正表自身的约束 ----------------

def test_no_interpreter_or_wrapper_leaked_into_the_table():
    """解释器与 wrapper 一旦进表,整张表就失去意义。"""
    forbidden = {
        "bash", "sh", "zsh", "python", "python3", "perl", "ruby", "node",
        "php", "eval", "exec", "command", "sudo", "env", "xargs", "nohup",
        "timeout", "watch", "tee", "wget", "curl", "tar", "jmap", "hbase",
    }
    assert forbidden.isdisjoint(TOOLS.keys())


# ---------------- 第三轮审查修复的回归 ----------------

@pytest.mark.parametrize("program", [
    'BEGIN{print "x" | "id"}',          # 单管道执行外部程序
    'BEGIN{"id" | getline v; print v}',  # 管道进 getline
    'BEGIN{@include "/tmp/evil"}',
    '@load "filefuncs"; BEGIN{}',
])
def test_awk_pipes_and_gawk_extensions_are_refused(program):
    assert check_pipeline([["awk", program, "/var/log/x.log"]]).ok is False


def test_awk_regex_alternation_is_not_mistaken_for_a_pipe():
    """`/error|warn/` 里的 | 是正则交替,拦它会废掉最常用的 awk 写法。"""
    assert check_pipeline([["awk", "/error|warn/{print}", "/var/log/x.log"]]).ok


@pytest.mark.parametrize("script", [
    "1,3w /tmp/out",     # 带逗号地址的写文件
    "1,3e id",           # 带逗号地址的执行
    "/error/w /tmp/out",  # 正则地址
    "$!e id",            # $ 地址 + 取反
    "s/a/b/w /tmp/out",  # 替换式的 w 标志
    "s/a/b/e",           # 替换式的 e 标志
])
def test_sed_dangerous_commands_in_every_address_form_are_refused(script):
    assert check_pipeline([["sed", script, "/var/log/x.log"]]).ok is False


def test_sed_script_file_option_is_not_available():
    """`-f` 指向远端文件,内容对本地校验完全不可见。"""
    assert check_pipeline([["sed", "-f", "/tmp/evil.sed", "/var/log/x.log"]]).ok is False


def test_jstat_sampling_interval_requires_a_count():
    assert check_pipeline([["jstat", "-gcutil", "1234"]]).ok
    assert check_pipeline([["jstat", "-gcutil", "1234", "1000"]]).ok is False
    assert check_pipeline([["jstat", "-gcutil", "1234", "1000", "5"]]).ok


@pytest.mark.parametrize("sql", [
    "select * from t for update",
    "select a from t for share",
    "select a from t lock in share mode",
    "select get_lock('x',10)",
    "select benchmark(1000000,md5('x'))",
    "select 1 into @v",
])
def test_select_statements_that_take_locks_or_load_are_refused(sql):
    """以 select 开头不等于只读:取锁会阻塞生产库的业务。"""
    assert check_pipeline([["mysql", "db", "-e", sql]]).ok is False


def test_a_column_named_like_a_forbidden_function_still_works():
    assert check_pipeline([["mysql", "db", "-e", "select sleeping from t"]]).ok


def test_stdin_placeholder_and_empty_arguments_are_refused():
    """grammar 层自己也要挡住,不能依赖调用方先过 Pydantic。"""
    assert check_pipeline([["cat", "-"]]).ok is False
    assert check_pipeline([["grep", "-c", "x", ""]]).ok is False
    assert check_pipeline([["grep", "-c", "a\nb", "/var/log/x.log"]]).ok is False


def test_stdin_placeholder_is_allowed_downstream_of_a_pipe():
    """首段的 stdin 无人喂会挂住,下游段的 stdin 正是上一段的输出。"""
    assert check_pipeline([
        ["mysql", "db", "-N", "-e", "select id from t"],
        ["paste", "-sd,", "-"],
    ]).ok


@pytest.mark.parametrize("segment", [
    ["head", "-15"],
    ["tail", "-200", "/var/log/messages"],
])
def test_line_count_written_as_a_bare_number_is_allowed(segment):
    """`head -15` 是最常见的截断写法,不认它等于把正表变成摆设。"""
    assert check_pipeline([segment]).ok


def test_bare_numbers_are_not_options_for_tools_that_never_took_them():
    assert check_pipeline([["cat", "-15", "/var/log/x.log"]]).ok is False


def test_find_can_format_to_stdout_but_not_to_a_file():
    """-printf 只写 stdout;写文件的是 -fprintf。"""
    assert check_pipeline([
        ["find", "/tmp", "-maxdepth", "1", "-printf", "%f %s\\n"]
    ]).ok
    assert check_pipeline([
        ["find", "/tmp", "-fprintf", "/tmp/out", "%f"]
    ]).ok is False


def test_awk_logical_or_is_not_mistaken_for_a_pipe():
    """`||$4` 里的第二个 | 后面跟着字段引用,不是管道到 shell。
    拦它的话聚合类 awk 程序基本全过不去。"""
    assert check_pipeline([
        ["awk", "{if(!(k in mn)||$4<mn[k])mn[k]=$4} END{for(k in mn)print k}"]
    ]).ok
    assert check_pipeline([["awk", '{print $1 | "id"}']]).ok is False


def test_option_cluster_with_an_inline_value_is_parsed_as_one_unit():
    """`-sd,` = -s 加 -d 内联逗号。拆成「先纯簇再内联附值」两步会漏掉它。"""
    assert check_pipeline([["ls", "-lh", "/tmp"]]).ok
    assert check_pipeline([["grep", "-A5", "ERROR", "/var/log/x.log"]]).ok
    assert check_pipeline([["sort", "-t,", "-k1,1nr"]]).ok


def test_rewritten_cluster_parsing_still_refuses_the_known_bypasses():
    """簇解析重写不得放松这两条:sort -o 写文件、ps 未登记选项。"""
    assert check_pipeline([["sort", "-o/tmp/pwn", "/etc/hosts"]]).ok is False
    assert check_pipeline([["sort", "-no", "/tmp/pwn"]]).ok is False
