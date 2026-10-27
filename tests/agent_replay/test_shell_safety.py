import pytest

from lib.agent_replay.shell_safety import check_shell
from lib.cmd_filter import CmdFilter, default_rules_path

FILTER = CmdFilter.from_yaml(default_rules_path())

ALLOWED = [
    "free -h; vmstat 1 3",
    "ps -eo pid,args | grep NodeManager | grep -v grep",
    "grep -E 'pmem-check|vmem-check' -A1 /etc/hadoop/conf/yarn-site.xml",
    'mysql demo_db -t -e "select TASK_ID,START_TIME from P_TASK_RUN where TASK_ID=10167 limit 5"',
    "mysql --defaults-extra-file=/opt/mysql_exporter/mysqld_exporter.cnf -e \"show variables like 'innodb%'\"",
    "zcat /var/log/nginx/access.log-20261020.gz | grep -c geteventlist",
    "awk '{print $NF}' /var/log/nginx/access.log | sort | uniq -c | sort -rn | head",
    "tail -n 200 /var/log/hadoop/yarn/nm.log 2>/dev/null | grep -i error",
    "ls /data >/dev/null 2>&1 && echo ok",
    "nginx -T 2>&1 | grep -n proxy_read_timeout",
    "hdfs dfsadmin -report | head -30",
    "hdfs dfs -ls /tmp",
    "yarn application -list -appStates RUNNING",
    "jstat -gcutil 12345 1 1",
    "jmap -heap 12345",
    "jcmd 12345 VM.flags",
    "curl -s http://192.0.2.53:9091/api/v1/query?query=up",
    "timeout 30 sed -n '/ERROR/p' /var/log/messages",
    "crontab -l",
    "top -b -n 1 | head -20",
    "find /opt/app -name '*.log' -mmin -60 | head",
]

DENIED = [
    ("jstat -gcutil $(pgrep -f NodeManager) 1 1", "write"),
    ("cat /opt/mysql_exporter/mysqld_exporter.cnf", "credential"),
    ("grep password /etc/my.cnf", "credential"),
    ("cat < /etc/shadow", "credential"),
    ("ls /root/.ssh", "credential"),
    ("grep x /var/log/messages > /tmp/out.txt", "write"),
    ("echo hi >> /tmp/a", "write"),
    ("cat <<EOF\nx\nEOF", "write"),
    ("sleep 100 &", "write"),
    ("curl -o /tmp/f http://x/y", "write"),
    ("curl -X DELETE http://x/api/v1/clusters/c", "write"),
    ("wget http://x/y", "write"),
    ("mysql -e 'select 1; drop table t'", "write"),
    ("mysql demo_db", "unsupported"),
    ("python3 -c 'print(1)'", "write"),
    ("cp /etc/hosts /tmp/h", "write"),
    ("ssh node2 ls", "write"),
    ("sudo ls", "write"),
    ("find /tmp -exec cat {} ;", "write"),
    ("awk '{print > \"/tmp/o\"}' /etc/hosts", "write"),
    ("sed -n 's/a/b/w /tmp/x' /etc/hosts", "write"),
    ("hdfs dfs -rm /tmp/x", "write"),
    ("hdfs dfs -put a /tmp/x", "write"),
    ("yarn application -kill application_1", "write"),
    ("yarn rmadmin -refreshQueues", "write"),
    ("jmap -dump:format=b,file=/tmp/h 1", "write"),
    ("jmap -histo:live 1", "write"),
    ("jcmd 1 GC.heap_dump /tmp/x", "write"),
    ("tail -f /var/log/messages", "unsupported"),
    ("bash -c 'ls'", "forbidden"),
    ("rm -f /tmp/x", "write"),
    ("systemctl restart nginx", "write"),
    ("ls | xargs cat", "write"),
    ("cat <(ls)", "unsupported"),
    ("echo 'unterminated", "unsupported"),
]


@pytest.mark.parametrize("command", ALLOWED)
def test_read_only_commands_pass(command):
    verdict = check_shell(command, FILTER)
    assert verdict.allowed, verdict


@pytest.mark.parametrize("command,category", DENIED)
def test_dangerous_commands_are_refused(command, category):
    verdict = check_shell(command, FILTER)
    assert not verdict.allowed
    assert verdict.category == category, verdict


def test_quoted_operators_are_data_not_syntax():
    # 引号里的 > | ; & 是 SQL/正则内容,不是 shell 操作符
    assert check_shell("grep -E 'a>b|c;d&e' /var/log/messages", FILTER).allowed
    assert check_shell('mysql demo_db -e "select a from t where b > 1"', FILTER).allowed
