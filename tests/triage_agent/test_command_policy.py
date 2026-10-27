import json
from pathlib import Path

import pytest

from lib.cmd_filter import CmdFilter, Level, default_rules_path
from lib.triage_agent.command_policy import (
    CommandPolicy,
    CommandPolicyError,
    command_signature,
    render_pipeline,
)
from lib.triage_agent.command_review import (
    REVIEW_SYSTEM_PROMPT,
    CommandReviewer,
    _parse_verdict,
)


# ---------------- 签名 ----------------

def test_signature_keeps_tool_and_options_but_drops_argument_values():
    """签名决定名单的泛化粒度:批过 `grep -c` 就不必为每个关键词重复批,
    但换成 `grep -r` 仍要另批——选项能改变命令的性质。"""
    a = command_signature([["grep", "-c", "ERROR", "/var/log/a.log"]])
    b = command_signature([["grep", "-c", "WARN", "/var/log/b.log"]])
    c = command_signature([["grep", "-r", "ERROR", "/var/log/a.log"]])

    assert a == b == "grep -c"
    assert c == "grep -r"


def test_signature_is_stable_across_option_order():
    first = command_signature([["ps", "-e", "-o", "args="]])
    second = command_signature([["ps", "-o", "args=", "-e"]])

    assert first == second == "ps -e -o"


def test_signature_keeps_the_executable_path_distinct():
    """工具名不做 basename:否则批准 `cat` 会连 `/tmp/cat` 一起放行,
    正表对路径的拒绝就被名单绕过了。"""
    bare = command_signature([["cat", "/etc/hosts"]])
    pathed = command_signature([["/tmp/cat", "/etc/hosts"]])

    assert bare != pathed


@pytest.mark.parametrize("tool,benign,hostile", [
    ("awk", ["{print $1}"], ['BEGIN{system("id")}']),
    ("sed", ["-e", "s/a/b/"], ["-e", "1e id"]),
    ("mysql", ["-e", "select 1"], ["-e", "drop table t"]),
])
def test_code_bearing_tools_sign_by_content(tool, benign, hostile):
    """awk/sed/mysql 的危险在参数内容里:签名必须绑定内容,否则批准一个
    `awk '{print $1}'` 等于批准 `awk 'BEGIN{system(...)}'`。"""
    a = command_signature([[tool, *benign, "/var/log/x.log"]])
    b = command_signature([[tool, *hostile, "/var/log/x.log"]])

    assert a != b
    # 同一条命令的签名必须稳定,否则批准立刻失效
    assert a == command_signature([[tool, *benign, "/var/log/x.log"]])


def test_plain_tools_still_generalise_across_argument_values():
    """非代码型工具仍按工具+选项泛化,不然名单会被同一命令的变体撑爆。"""
    assert (
        command_signature([["grep", "-c", "A", "/var/log/a.log"]])
        == command_signature([["grep", "-c", "B", "/var/log/b.log"]])
    )


def test_signature_covers_every_pipeline_segment():
    signature = command_signature(
        [["ps", "-eo", "args="], ["grep", "java"], ["head", "-20"]]
    )

    assert signature == "ps -eo | grep | head -N"


def test_long_option_with_value_signs_by_option_name():
    signature = command_signature([["grep", "--include=*.log", "-rn", "x", "/var/log/x.log"]])

    assert signature == "grep --include -rn"


# ---------------- 判定链 ----------------

@pytest.fixture
def policy(tmp_path):
    return CommandPolicy(tmp_path / "command-policy.json")


def _write(policy, **payload):
    policy.path.write_text(json.dumps(payload), encoding="utf-8")


def test_deny_wins_over_allow_when_a_signature_is_in_both(policy):
    """同签名同时进两个名单时拒绝优先——名单冲突不该以放行收场。"""
    _write(policy, mode="auto", allow=["grep -c"], deny=["grep -c"])

    verdict = policy.evaluate("grep -c")

    assert verdict.action == "deny"


def test_task_defaults_allow_only_within_that_task(policy):
    _write(policy, mode="auto", allow=[], deny=[])

    assert policy.evaluate("grep -c", task_defaults=["grep -c"]).action == "allow"
    assert policy.evaluate("grep -c", task_defaults=[]).action == "review"


def test_whitelist_mode_denies_anything_not_listed(policy):
    _write(policy, mode="whitelist", allow=["free -m"], deny=[])

    assert policy.evaluate("free -m").action == "allow"
    assert policy.evaluate("grep -c").action == "deny"


def test_auto_mode_sends_unlisted_signatures_to_review(policy):
    _write(policy, mode="auto", allow=[], deny=[])

    assert policy.evaluate("grep -c").action == "review"


def test_bypass_mode_allows_unlisted_but_still_honours_deny(policy):
    """bypass 只是跳过策略层,红线仍在;显式黑名单也依然拦得住。"""
    _write(policy, mode="bypass", allow=[], deny=["wget"])

    assert policy.evaluate("grep -c").action == "allow"
    assert policy.evaluate("wget").action == "deny"


def test_missing_policy_file_defaults_to_whitelist(policy):
    """没有策略文件时收窄到 whitelist,但正表内的基础只读工具仍可用。"""
    assert not policy.path.exists()

    assert policy.load().mode == "whitelist"
    assert policy.evaluate("ls", pipeline=[["ls"]]).action == "allow"
    assert policy.evaluate("grep -c").action == "deny"


def test_corrupt_policy_file_raises_instead_of_silently_allowing(policy):
    policy.path.write_text("{ not json", encoding="utf-8")

    with pytest.raises(CommandPolicyError):
        policy.evaluate("grep -c")


def test_invalid_mode_is_rejected(policy):
    _write(policy, mode="wide-open", allow=[], deny=[])

    with pytest.raises(CommandPolicyError):
        policy.evaluate("grep -c")


# ---------------- 名单写入 ----------------

def test_remember_moves_a_signature_between_lists(policy):
    policy.remember("wget", allow=False)
    assert policy.load().deny == ["wget"]

    policy.remember("wget", allow=True)
    config = policy.load()
    assert config.allow == ["wget"]
    assert config.deny == []


def test_remember_is_idempotent(policy):
    policy.remember("grep -c", allow=True)
    policy.remember("grep -c", allow=True)

    assert policy.load().allow == ["grep -c"]


def test_set_mode_persists_and_validates(policy):
    policy.set_mode("whitelist")
    assert policy.load().mode == "whitelist"

    with pytest.raises(CommandPolicyError):
        policy.set_mode("nope")


# ---------------- 与红线的分层 ----------------

@pytest.mark.parametrize("pipeline", [
    [["rm", "-rf", "/tmp/x"]],
    [["systemctl", "restart", "hdfs"]],
    [["reboot"]],
    [["sort", "-o", "/etc/pwn", "/etc/hosts"]],
])
def test_policy_allow_never_overrides_the_redline(policy, pipeline):
    """策略层只做减法:哪怕签名进了永久白名单,红线照拦不误。

    这条性质是整个分层设计成立的前提——放行策略是给只读命令做细分的,
    不是给危险命令开后门的。
    """
    signature = command_signature(pipeline)
    _write(policy, mode="bypass", allow=[signature], deny=[])
    assert policy.evaluate(signature).action == "allow"

    redline = CmdFilter.from_yaml(default_rules_path())
    assert redline.classify(render_pipeline(pipeline)).level is not Level.READONLY


# ---------------- 审核器 ----------------

def test_reviewer_without_a_model_never_reports_safe():
    reviewer = CommandReviewer(None)

    verdict = reviewer.review(command="grep -c x /var/log/a", signature="grep -c")

    assert reviewer.available is False
    assert verdict.safe is False
    assert verdict.reviewed is False


@pytest.mark.parametrize("content", [
    None,
    "",
    "大概是安全的吧",
    "{}",
    '{"safe": "yes"}',
    "{ broken json",
])
def test_unparseable_review_responses_count_as_unsafe(content):
    """解析不出结构一律当可疑:把解析失败当放行会让审核层变成假防线。"""
    verdict = _parse_verdict(content)

    assert verdict.safe is False
    assert verdict.reviewed is False


def test_reviewer_accepts_plain_and_fenced_json():
    assert _parse_verdict('{"safe": true, "reason": "只读"}').safe is True
    assert _parse_verdict(
        '```json\n{"safe": true, "reason": "只读"}\n```'
    ).safe is True
    assert _parse_verdict('{"safe": false, "reason": "会执行代码"}').safe is False


def test_reviewer_prompt_inspects_interpreter_intent_instead_of_tool_name():
    assert "Do not reject an interpreter merely because" in REVIEW_SYSTEM_PROMPT
    assert "python3 -c 'print(1)'" in REVIEW_SYSTEM_PROMPT
    assert "open(\"/tmp/x\", \"w\")" in REVIEW_SYSTEM_PROMPT


def test_model_failure_degrades_to_unsafe_rather_than_raising():
    class Boom:
        def complete(self, messages, tools):
            raise RuntimeError("upstream 502")

    verdict = CommandReviewer(Boom()).review(command="x", signature="x")

    assert verdict.safe is False
    assert verdict.reviewed is False


# ---------------- 三档的分工 ----------------

def _tri(tmp_path, pipeline):
    """同一条命令在三档下的判定。"""
    import yaml

    from lib.triage_agent.command_policy import PolicyConfig
    from lib.triage_agent.path_policy import load_path_policy

    root = Path(__file__).resolve().parents[2]
    policy = CommandPolicy(
        tmp_path / "p.json",
        path_policy=load_path_policy(
            yaml.safe_load((root / "envs" / "example" / "config" / "fs-roots.yaml").read_text()),
            catalog=yaml.safe_load(
                (root / "envs" / "example" / "config" / "log-catalog.yaml").read_text()
            ),
        ),
    )
    signature = command_signature(pipeline)
    return tuple(
        policy.evaluate(
            signature, config=PolicyConfig(mode=mode), pipeline=pipeline,
        ).action
        for mode in ("whitelist", "auto", "bypass")
    )


@pytest.mark.parametrize("pipeline,expected,why", [
    # 静态可验证只读:三档都免审
    ([["mysql", "db", "-N", "-e", "select 1"]],
     ("allow", "allow", "allow"), "只读 SQL"),
    ([["jq", "-r", ".ssql", "/opt/app/x/def.json"]],
     ("allow", "allow", "allow"), "jq 在窄表里,default 也能读结构化定义"),
    ([["ps", "-eo", "args="], ["grep", "java"], ["head", "-20"]],
     ("allow", "allow", "allow"), "寻常只读管道"),

    # 档位相关:default 拒 / auto 交审核模型 / bypass 放行
    ([["python", "-c", "import json"]],
     ("deny", "review", "allow"), "解释器"),
    ([["curl", "-s", "http://192.0.2.11:8080/api"]],
     ("deny", "review", "allow"), "curl"),
    ([["cat", "/etc/hive/conf/hive-site.xml"]],
     ("deny", "review", "allow"), "非凭据的路径越界"),

    # ask 断路器:三档都挂人工,bypass 也不例外
    ([["rm", "-rf", "/data/x"]], ("ask", "ask", "ask"), "删除"),
    ([["systemctl", "restart", "hive"]], ("ask", "ask", "ask"), "服务重启"),
    ([["hadoop-daemon.sh", "stop", "datanode"]], ("ask", "ask", "ask"), "组件启停"),
    ([["cat", "/etc/shadow"]], ("ask", "ask", "ask"), "凭据文件"),
    ([["cat", "/root/.ssh/id_rsa"]], ("ask", "ask", "ask"), "私钥"),
    ([["curl", "file:///root/.ssh/id_rsa"]],
     ("ask", "ask", "ask"), "URL 形态的凭据路径也要认出来"),
    ([["cat", "/var/log/../../root/.ssh/id_rsa"]],
     ("ask", "ask", "ask"), "normpath 之后才现形的凭据路径"),

    # 三档都拒:会瓦解判定机制本身,或内容里就带着执行构造
    ([["bash", "-c", "id"]], ("deny", "deny", "deny"), "内联 shell"),
    ([["shutdown", "-h", "now"]], ("deny", "deny", "deny"), "关机"),
    ([["awk", 'BEGIN{system("id")}']],
     ("deny", "deny", "deny"), "awk 程序含 system()"),
    ([["sort", "-o/tmp/pwn", "/etc/hosts"]],
     ("ask", "ask", "ask"), "sort -o 落盘属写操作"),
])
def test_three_tiers_differ_only_in_toolset_not_in_strictness(
    tmp_path, pipeline, expected, why,
):
    """三档的差别在工具集大小与谁来担保,不在检查的严格程度。

    放开工具集不等于放开约束:红线绝对禁止与内容含执行构造的,bypass 一样拒;
    删除、服务启停、凭据路径三档都要人看一眼。
    """
    assert _tri(tmp_path, pipeline) == expected, why


def test_permanent_allowlist_cannot_cover_the_ask_classes(tmp_path):
    """「永久加入白名单」是「以后都不问」,对 rm 和凭据太宽。这两类每次都要
    人看一眼——一次性券可以覆盖(那是人刚刚批准了这一条),永久名单不行。"""
    from lib.triage_agent.command_policy import PolicyConfig

    pipeline = [["rm", "-rf", "/data/x"]]
    signature = command_signature(pipeline)
    policy = CommandPolicy(tmp_path / "p.json")

    verdict = policy.evaluate(
        signature,
        config=PolicyConfig(mode="bypass", allow=[signature]),
        pipeline=pipeline,
    )

    assert verdict.action == "ask"
