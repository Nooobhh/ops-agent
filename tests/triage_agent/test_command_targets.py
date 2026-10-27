from lib.triage_agent.command_targets import extract_targets


def test_extract_targets_keeps_all_read_paths_across_pipeline():
    targets = extract_targets([
        ["cat", "/var/log/messages"],
        ["grep", "-n", "ERROR", "/tmp/diagnostic.log"],
    ])

    assert targets["paths"] == ["/var/log/messages", "/tmp/diagnostic.log"]
    assert targets["mysql"] == []
    assert targets["mysql_parsed"] is True


def test_extract_targets_parses_mysql_database_and_table():
    targets = extract_targets([
        ["mysql", "-D", "demo_db", "-e", "SELECT * FROM P_TASK_RUN"],
    ])

    assert targets["mysql"] == [{"database": "demo_db", "tables": ["P_TASK_RUN"]}]
    assert targets["mysql_parsed"] is True


def test_extract_targets_marks_unrecognised_mysql_shape_as_unparsed():
    targets = extract_targets([["mysql", "-D", "demo_db", "-e", "SELECT 1"]])

    assert targets["mysql_parsed"] is False
    assert targets["mysql"][0]["tables"] == []


def test_extract_targets_reports_no_mysql_as_parsed():
    targets = extract_targets([["ls", "/tmp"]])

    assert targets == {"paths": ["/tmp"], "mysql": [], "mysql_parsed": True}
