from lib.agent_runtime.finish import (
    INSUFFICIENT,
    REFUTED,
    SUPPORTED,
    FinishArgs,
    extract,
    parse_time,
    report_fields,
    verify,
)

WINDOW = {"start": "2026-10-21 13:18", "end": "2026-10-21 13:22"}
STEPS = [
    {"cmd": "[node3] grep -i heartbeat dn.log", "stdout": "2026-10-21 13:19:02 WARN heartbeat took 30012 ms\n"},
    {"cmd": "[node3] grep -A1 heartbeat hdfs-site.xml",
     "stdout": "<name>dfs.heartbeat.timeout</name>\n<value>5000</value>\n"},
    {"cmd": "[node3] grep -c Exception dn.log", "stdout": "26/10/21 13:20:11 WARN slow BlockReceiver write took 3200ms\n"},
    {"cmd": "[node3] sed -n '/13:18/,/13:22/p' dn.log",
     "stdout": "2026-10-21 13:18:30 INFO heartbeat ok in 3 ms\n2026-10-21 13:21:30 INFO heartbeat ok in 2 ms\n"},
    {"cmd": "[node3] hdfs dfsadmin -report", "stdout": "Name: 198.51.100.103:1019\nLast contact: Wed Oct 21 13:20:01 CST 2026\n"},
    {"cmd": "[node4] sed -n '/13:18/,/13:22/p' dn.log", "stdout": "2026-10-21 13:19:30 INFO heartbeat ok in 4 ms\n"},
]


def _args(*hypotheses, window=WINDOW, **extra):
    return FinishArgs.model_validate({"answer": "x", "confidence": "high", "incident_window": window,
                                      "hypotheses": list(hypotheses), **extra})


def _hypothesis(status, *evidence, related=()):
    return {"statement": "DN 心跳超时", "status": status,
            "evidence": [dict(zip(("role", "ref", "match", "value"), item)) for item in evidence],
            "related": [{"ref": ref, "match": match} for ref, match in related]}


OBSERVATION = ("observation", "step:1", "heartbeat took 30012", "30012")
CRITERION = ("criterion", "step:2", "dfs.heartbeat.timeout</name> <value>5000", "5000")


def test_observation_in_window_plus_criterion_is_supported():
    result = verify(_args(_hypothesis("supported", OBSERVATION, CRITERION)), STEPS)
    assert result["hypotheses"][0]["status"] == SUPPORTED and result["grounded"]
    # 跨行片段按窗口摘录原始行
    assert result["hypotheses"][0]["evidence"][1]["text"] == "<name>dfs.heartbeat.timeout</name>\n<value>5000</value>"


def test_supported_without_criterion_or_window_is_downgraded():
    no_criterion = verify(_args(_hypothesis("supported", OBSERVATION)), STEPS)["hypotheses"][0]
    assert no_criterion["status"] == INSUFFICIENT and "判据" in no_criterion["reasons"][0]
    no_window = verify(_args(_hypothesis("supported", OBSERVATION, CRITERION), window=None), STEPS)
    assert no_window["hypotheses"][0]["status"] == INSUFFICIENT and not no_window["grounded"]


def test_observation_outside_window_is_downgraded():
    late = {"start": "2026-10-21 15:00", "end": "2026-10-21 15:05"}
    result = verify(_args(_hypothesis("supported", OBSERVATION, CRITERION), window=late), STEPS)
    assert result["hypotheses"][0]["status"] == INSUFFICIENT
    assert "对不上" in result["hypotheses"][0]["reasons"][0]


def test_value_must_appear_in_extracted_line():
    wrong = ("observation", "step:1", "heartbeat took 30012", "45s")
    result = verify(_args(_hypothesis("supported", wrong, CRITERION)), STEPS)
    check = result["hypotheses"][0]["evidence"][0]
    assert not check["found"] and "45s" in check["reason"]
    assert result["hypotheses"][0]["status"] == INSUFFICIENT


def test_unrelated_errors_only_stay_insufficient():
    hypothesis = _hypothesis("supported", related=[("step:3", "slow BlockReceiver write")])
    result = verify(_args(hypothesis), STEPS)["hypotheses"][0]
    assert result["status"] == INSUFFICIENT and result["related"][0]["found"]


def test_refuted_needs_two_independent_commands_with_one_in_window():
    log = ("absence", "step:4", "heartbeat ok in 3 ms")
    report = ("absence", "step:5", "Last contact: Wed Oct 21 13:20:01")
    assert verify(_args(_hypothesis("refuted", log, report)), STEPS)["hypotheses"][0]["status"] == REFUTED
    # 同一命令在两个节点上不算两个来源
    same_command = ("absence", "step:6", "heartbeat ok in 4 ms")
    single = verify(_args(_hypothesis("refuted", log, same_command)), STEPS)["hypotheses"][0]
    assert single["status"] == INSUFFICIENT and "两个不同命令" in single["reasons"][0]


def test_refuted_alone_does_not_ground_the_conclusion():
    log = ("absence", "step:4", "heartbeat ok in 3 ms")
    report = ("absence", "step:5", "Last contact: Wed Oct 21 13:20:01")
    result = verify(_args(_hypothesis("refuted", log, report)), STEPS)
    assert not result["grounded"]
    assert report_fields(_args(_hypothesis("refuted", log, report)), result)["verdict"] == "insufficient"


def test_kb_short_and_missing_matches_are_not_extracted():
    assert extract(STEPS, "kb:cluster/alpha/hdfs/x.md", "heartbeat timeout default")["reason"].startswith("知识库")
    assert "过短" in extract(STEPS, "step:1", "WARN")["reason"]
    assert not extract(STEPS, "step:9", "heartbeat took 30012")["found"]


def test_facts_ground_question_answers_without_hypotheses():
    args = FinishArgs.model_validate({"answer": "心跳正常", "confidence": "high",
                                      "facts": [{"ref": "step:4", "match": "heartbeat ok in 2 ms"}]})
    result = verify(args, STEPS)
    assert result["grounded"]
    fields = report_fields(args, result)
    assert fields["verdict"] == "conclusive" and fields["severity"] == "ok"
    assert fields["evidence"][1] == "  [事实·step:4] 2026-10-21 13:21:30 INFO heartbeat ok in 2 ms"


def test_downgrade_is_written_into_report_and_uncertain():
    args = _args(_hypothesis("supported", OBSERVATION))
    fields = report_fields(args, verify(args, STEPS))
    assert fields["evidence"][0].startswith("[缺少证据] DN 心跳超时(模型判为成立,降级:")
    assert any("判据" in item for item in fields["finish"]["uncertain"])


def test_parse_time_formats():
    assert parse_time("26/10/21 13:20:11 WARN").hour == 13
    assert parse_time("Wed Oct 21 13:20:01 CST 2026", year=2026).minute == 20
    assert parse_time("2026-10-21T13:19:02.123+08:00").second == 2
    assert parse_time("no time here") is None


def test_value_on_the_next_line_is_extracted_with_the_match():
    item = ("criterion", "step:2", "dfs.heartbeat.timeout", "5000")
    result = verify(_args(_hypothesis("supported", OBSERVATION, item)), STEPS)
    check = result["hypotheses"][0]["evidence"][1]
    assert check["found"] and check["text"] == "<name>dfs.heartbeat.timeout</name>\n<value>5000</value>"
    assert result["hypotheses"][0]["status"] == SUPPORTED


NOW = {"start": "2026-10-22 14:40", "end": "2026-10-22 14:50"}
LIVE = [
    {"cmd": "[node12] vmstat 1 3", "recorded_at": "2026-10-22T14:46:35+08:00",
     "stdout": " r  b   swpd   free  si   so\n 2  0 11920 610768  950  1200\n"},
    {"cmd": "[node12] sysctl vm.swappiness", "recorded_at": "2026-10-22T14:47:01+08:00",
     "stdout": "vm.swappiness = 60\n"},
    {"cmd": "[node12] grep -c oom-kill /var/log/messages", "recorded_at": "2026-10-22T14:47:30+08:00",
     "stdout": "oom_kill_lines=0\n"},
]


def test_live_output_without_timestamp_uses_step_execution_time():
    observation = ("observation", "step:1", "2 0 11920 610768 950 1200", "950")
    criterion = ("criterion", "step:2", "vm.swappiness = 60", "60")
    result = verify(_args(_hypothesis("supported", observation, criterion), window=NOW), LIVE)
    hypothesis = result["hypotheses"][0]
    assert hypothesis["status"] == SUPPORTED and result["grounded"]
    assert hypothesis["evidence"][0]["time_source"] == "executed"
    # 现状输出的执行时间不在历史故障窗口内:照样降级
    historical = verify(_args(_hypothesis("supported", observation, criterion), window=WINDOW), LIVE)
    assert historical["hypotheses"][0]["status"] == INSUFFICIENT
    assert "执行时间" in historical["hypotheses"][0]["reasons"][0]


def test_refuted_counts_live_absence_by_execution_time_and_excerpt_time_wins():
    absence_log = ("absence", "step:4", "heartbeat ok in 3 ms")
    sources = {**{f"step:{i}": step for i, step in enumerate(STEPS, 1)},
               "step:4": {**STEPS[3], "recorded_at": "2026-10-22T14:47:30+08:00"}, "step:7": LIVE[2]}
    result = verify(_args(_hypothesis("refuted", ("absence", "step:7", "oom_kill_lines=0"), absence_log)), sources)
    checks = result["hypotheses"][0]["evidence"]
    # 日志行自带时间(13:18:30,窗口内)优先于执行时间;现状输出按执行时间(10-22,窗口外)
    assert [check["time_source"] for check in checks] == ["executed", "excerpt"]
    assert [check["in_window"] for check in checks] == [False, True]
    assert result["hypotheses"][0]["status"] == REFUTED
