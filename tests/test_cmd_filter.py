import shlex
import pytest

from lib.cmd_filter import CmdFilter, CommandDenied, Level, default_rules_path

READONLY_CASES = [
    "free -m",
    "cat /proc/meminfo",
    "vmstat 1 3",
    "ps aux | grep java",
    "grep -c shutdown /var/log/messages",        # v3 误杀:shutdown 出现在参数
    "iptables -L",                                # v3 误杀:子串 tables
    "sudo -u hdfs jstat -gcutil 12345",
    "curl -s http://127.0.0.1:8088/ws/v1/cluster/apps",
    "mysql -e 'select 1' </dev/null",
    "echo hi > /tmp/opsagent/x.log",              # /tmp 重定向放行
    "ls /data 2>/dev/null",                       # /dev 重定向放行
    "mkdir -p /tmp/opsagent/run-1",               # agent 工作区白名单
    "awk 'BEGIN{print 1}'",
    "systemctl status nginx",                     # 服务只读诊断不该要审批
    "service nginx status",
    "crontab -l",
    "cat /etc/crontab",                           # 路径里的 crontab 不是命令
    "grep backup /etc/crontab",
    "which bash",
    "ls /bin/sh",
    "last reboot",                                # reboot 是 last 的参数不是命令
    "curl -sG 'http://192.0.2.53:9091/api/v1/query' --data-urlencode 'query=up'",
    # -G 强制 GET,--data* 全部转 query string,只读
    "find /data -name '*.sh' -exec ls -l {} \\;", # -exec 接只读命令
    "find /data -name '*.sh' -exec md5sum {} \\;",
    "grep -a -oE '(/bin/(ba)?sh|sh -c) cmd' /var/log/audit.log",
    # 参数字符串里的 "sh -c" 不是内联 shell
    "grep 'python -c import' /var/log/app.log",
]

WRITE_CASES = [
    "rm /tmp/x",                                  # v3 漏拦:裸 rm
    "kill -9 4854328",                            # v3 漏拦
    "hdfs dfs -rm /user/x",                       # v3 漏拦
    "systemctl restart node_exporter",
    "swapoff -a",
    "echo 1 > /proc/sys/vm/drop_caches",          # 绝对路径重定向出 /tmp,/dev
    "sed -i 's/a/b/' /etc/hosts",
    "curl -X POST http://manager:8080/api/v1/x",
    "mysql -e 'drop table t1'",
    "free -m\nrm /tmp/x",                         # 换行是天然分隔符,第二段必须被扫到
    "free -m & rm /tmp/x",                        # 单 & 后台分隔符
    "hadoop fs -rm -r /user/x",                   # hdfs dfs 的等价别名
    "find /data -name '*.log' -delete",           # find 自带删除
    "find /tmp -type f -exec rm {} \\;",
    "dd if=/dev/zero of=/data/x",                 # dd 写普通文件
    "dd if=/data/blk of=/dev/null",               # dd 基准测试:降为可审批 WRITE(不再 FORBIDDEN)
    "crontab /tmp/newcron",
    "mkdir -p /tmp/opsagent/x ; hdfs dfs -rm /user/y",   # allow 前缀不得短路后续段
    "mkdir -p /tmp/opsagent/x ; rm /tmp/y",
    "systemctl rescue",                           # 运行级切换
    "find /data -exec sh -c 'rm {}' \\;",         # -exec 接 shell 不豁免
    "curl --data-urlencode 'q=x' http://api/w",   # 无 -G 的 --data* 是真 POST
]

FORBIDDEN_CASES = [
    "reboot",
    "shutdown -h now",
    "sudo reboot",
    "mkfs.ext4 /dev/sdb1",
    "dd if=/dev/zero of=/dev/sda",
    "rm -rf /",
    "init 0",
    "rm -rf /*",                                  # 与 rm -rf / 等价的灾难
    "mkdir -p /tmp/opsagent/x ; rm -rf /",        # allow 永不豁免 forbidden
    "systemctl reboot",                           # 等价于裸 reboot,必须无审批通道
    # 内联 shell 让远端 shell 重新解析整串,逐 token 转义与结构化 argv 当场失效。
    # 批准它等于批准一个看不清内容的黑盒,故不给审批通道(不同于 python -c:
    # 后者不经 shell,argv 边界还在,由命令模式档位准入)
    "bash -c 'rm /tmp/x'",
    "sh -c 'kill 123'",
    "bash -lc 'systemctl restart nginx'",         # 带其他 flag
    "sudo bash -c 'echo 1 > /proc/x'",            # wrapper 剥离后仍识别
]


@pytest.fixture(scope="module")
def f() -> CmdFilter:
    return CmdFilter.from_yaml(default_rules_path())


@pytest.mark.parametrize("cmd", READONLY_CASES)
def test_readonly_allowed(f, cmd):
    assert f.classify(cmd).level is Level.READONLY


@pytest.mark.parametrize("cmd", WRITE_CASES)
def test_write_denied_without_approval(f, cmd):
    assert f.classify(cmd).level is Level.WRITE
    with pytest.raises(CommandDenied):
        f.check(cmd, approved=False)


@pytest.mark.parametrize("cmd", WRITE_CASES)
def test_write_allowed_with_approval(f, cmd):
    f.check(cmd, approved=True)  # 不抛异常即通过


@pytest.mark.parametrize("cmd", FORBIDDEN_CASES)
def test_forbidden_always(f, cmd):
    assert f.classify(cmd).level is Level.FORBIDDEN
    with pytest.raises(CommandDenied):
        f.check(cmd, approved=True)  # 审批也不放行


# --- 只读工具的单引号参数中和(诊断动作自由参数经 shlex.quote 后即此形态) ---

QUOTED_LITERAL_READONLY = [
    "grep -F -- 'systemctl stop' /var/log/messages",
    "grep -F -- 'service restart' /var/log/messages",
    "grep -F -- 'sed -i' /var/log/messages",
    "grep -F -- 'hdfs dfs -rm' /var/log/hadoop/hdfs/nn.log",
    "grep -F -- 'yarn application -kill' /var/log/hadoop-yarn/yarn/rm.log",
    "grep -F -- 'sysctl -w' /var/log/messages",
    "grep -F -- 'curl -X POST' /var/log/nginx/error.log",
    "grep -F -- 'rm -rf /' /var/log/messages",
    "tail -c 10485760 /var/log/x.log | grep -F -- 'systemctl stop' | tail -n 100",
]

# 引号只挡 shell,挡不住这些命令自己执行参数内容 —— 必须继续判 WRITE
QUOTED_STILL_WRITE = [
    "mysql -e 'insert into task values(1)'",
    "beeline -e 'insert into t select 1'",
]


@pytest.mark.parametrize("cmd", QUOTED_LITERAL_READONLY)
def test_literal_arg_of_readonly_tool_not_flagged_write(f, cmd):
    """搜索词里的运维关键词是数据不是命令,误判会让整个排查任务失败。"""
    assert f.classify(cmd).level is Level.READONLY


@pytest.mark.parametrize("cmd", QUOTED_STILL_WRITE)
def test_interpreter_quoted_payload_still_write(f, cmd):
    """中和只对只读工具生效:解释器会执行自己的引号参数,不得放松。"""
    assert f.classify(cmd).level is Level.WRITE


@pytest.mark.parametrize("cmd", [
    "python -c 'print(1)'",
    "python3 -c 'import json'",
    "perl -e 'print 1'",
    "jq -r .a /tmp/x.json",
])
def test_interpreters_are_not_graded_by_the_redline(f, cmd):
    """解释器不改变系统状态却能做任何事,按写/只读分不出去,改由命令模式档位
    准入(default 拒 / auto 过审核模型 / bypass 放行)。这里判 READONLY 不等于
    放行——它只是把判定交给了档位那一层。"""
    assert f.classify(cmd).level is Level.READONLY


def test_bare_write_command_unaffected_by_neutralization(f):
    """不带引号的真实写操作判定不受影响。"""
    for cmd in ("systemctl stop nginx", "sed -i 's/a/b/' /etc/hosts",
                "hdfs dfs -rm /data/x"):
        assert f.classify(cmd).level is Level.WRITE


# --- 中和不得成为绕过通道:嵌套命令上下文一律放弃中和 ---

NEUTRALIZE_BYPASS_ATTEMPTS = [
    # $(...) / 反引号 / 进程替换:段首是白名单命令,但引号内容由内层解释器执行
    "cat $(bash -c 'systemctl stop nginx')",
    "head -1 $(bash -c 'systemctl stop nginx')",
    "wc -l $(sh -c 'hdfs dfs -rm /data/important')",
    "cut -d: -f1 `bash -c 'systemctl stop nginx'`",
    "cat <(sh -c 'systemctl stop nginx')",
    "sort $(sh -c 'sysctl -w a=b')",
    # # 注释里的撇号会让朴素扫描器的引号状态与 shell 失配
    "grep 'a' /var/log/x # it's fine\nsystemctl stop nginx # don't",
    "cat 'a' # don't\nhdfs dfs -rm /data/x # won't",
    # $'...' ANSI-C 引用中的 \' 同样让引号配对错位
    "cat $'\\'' ; systemctl stop nginx # don't",
]


@pytest.mark.parametrize("cmd", NEUTRALIZE_BYPASS_ATTEMPTS)
def test_neutralization_never_hides_a_real_write(f, cmd):
    """扫描器建模不了的构造必须整串放弃中和,宁可误杀不可漏杀。

    只按段首命令词判断会把内层解释器的引号参数误当成外层只读工具的数据,
    使 write_patterns 整层失效。
    """
    assert f.classify(cmd).level is not Level.READONLY
    with pytest.raises(CommandDenied):
        f.check(cmd, approved=False)


# --- 第三轮:bash 在词法分析前处理的构造(黑名单枚举挡不住,故改用白名单文法) ---

GRAMMAR_ESCAPE_ATTEMPTS = [
    # 行继续符:bash 读入阶段先删掉 \<换行>,`$\<NL>(` 执行时就是 `$(`
    "cat $\\\n(sh -c 'systemctl stop nginx')",
    "wc -l $\\\n(sh -c 'hdfs dfs -rm /data/x')",
    "head <\\\n(sh -c 'yarn application -kill app_1')",
    "sort $\\\n(sh -c 'sysctl -w a=b')",
    # heredoc:body 对 shell 是数据,但朴素扫描会让引号跨过 EOF 配对
    "cat <<EOF\ncat it's fine\nEOF\nsystemctl stop nginx\ndon't\n",
    "cat <<EOF\ngrep won't\nEOF\nhdfs dfs -rm /data/x\ndon't\n",
]


@pytest.mark.parametrize("cmd", GRAMMAR_ESCAPE_ATTEMPTS)
def test_constructs_outside_safe_grammar_are_never_neutralized(f, cmd):
    """越出白名单文法的一律不中和 —— 未建模构造默认落安全侧。"""
    assert f.classify(cmd).level is not Level.READONLY
    with pytest.raises(CommandDenied):
        f.check(cmd, approved=False)


def test_rendered_diagnostics_stay_within_safe_grammar(f):
    """诊断动作渲染出的命令必须落在文法内,否则中和失效、误杀重现。"""
    import shlex
    from lib.cmd_filter import _NEUTRALIZE_SAFE_GRAMMAR
    rendered = [
        "free -m",
        "ps -eo pid,args",
        "top -bn1 | head -30",
        "ps -wweo pid,rss,args | sort -k2 -rn | head -20 | cut -c1-200",
        "tail -n 200 /var/log/hadoop/hdfs/nn.log",
        f"tail -c 10485760 /var/log/hadoop/hdfs/nn.log | grep -F -- "
        f"{shlex.quote('systemctl stop')} | tail -n 100",
        "hdfs dfsadmin -report | head -400",
        "yarn application -list -appStates RUNNING | head -60",
        "jstat -gcutil 12345",
    ]
    for cmd in rendered:
        assert _NEUTRALIZE_SAFE_GRAMMAR.fullmatch(cmd), cmd


def test_allowlisted_leaders_never_echo_args_to_interpreter(tmp_path):
    """中和白名单成员不得把参数回显到 stdout。

    `<成员> '<shell 代码>' | sh` 形态下,第一段的引号参数会被中和,若该成员
    把参数打到 stdout,载荷就进了下游解释器。现有成员均以参数为文件名/模式,
    故投毒打不通;本用例锁死这一点,防止未来往清单里加 echo/printf 一类命令。
    """
    import subprocess
    from lib.cmd_filter import _LITERAL_ARG_CMDS

    delivered = []
    for leader in sorted(_LITERAL_ARG_CMDS):
        canary = tmp_path / f"canary_{leader}"
        payload = f"touch {canary}"
        subprocess.run(
            f"{leader} {shlex.quote(payload)} 2>/dev/null | sh 2>/dev/null",
            shell=True, cwd=tmp_path, timeout=10,
        )
        if canary.exists():
            delivered.append(leader)
    assert delivered == [], f"这些成员把参数送达了下游解释器: {delivered}"


def test_echo_style_command_is_not_allowlisted():
    """对照组:能回显参数的命令必须不在清单里,否则上面的投毒即刻打通。"""
    from lib.cmd_filter import _LITERAL_ARG_CMDS
    for risky in ("echo", "printf", "basename", "dirname", "seq", "yes", "rev"):
        assert risky not in _LITERAL_ARG_CMDS


# --- 命令名看着只读、实则能落盘/执行的缺口 ---

SORT_AND_QUOTED_REDIRECT_WRITES = [
    "sort -o /etc/passwd /tmp/x",              # -o 直接写目标文件
    "sort --output=/etc/passwd /tmp/x",        # 长选项同义
    "sort -m -o /etc/cron.d/evil /tmp/a",      # 旗标簇里的 -o
    "sort --compress-program=/tmp/evil /tmp/x",  # 执行任意二进制
    "cat /tmp/a | sort -o /etc/passwd",        # 管道下游段
    "cat /tmp/x >'/etc/passwd'",               # 重定向目标带单引号
    'cat /tmp/x >"/etc/shadow"',               # 重定向目标带双引号
]

SORT_READONLY_USAGE = [
    "sort /tmp/x",
    "sort -u /var/log/x.log",
    "sort -k2 -rn /tmp/a",
    "ps -wweo pid,rss,args | sort -k2 -rn | head -20 | cut -c1-200",  # system.top_memory
    "du -sh /data/x | sort -h | tail -5",
]


@pytest.mark.parametrize("cmd", SORT_AND_QUOTED_REDIRECT_WRITES)
def test_sort_write_and_quoted_redirect_are_blocked(f, cmd):
    assert f.classify(cmd).level is Level.WRITE
    with pytest.raises(CommandDenied):
        f.check(cmd, approved=False)


@pytest.mark.parametrize("cmd", SORT_READONLY_USAGE)
def test_ordinary_sort_usage_not_flagged(f, cmd):
    """排序取 Top N 是排查常用手法,不能因为补 -o 规则被误杀。"""
    assert f.classify(cmd).level is Level.READONLY
