import pytest

from lib.sanitize import REDACTED, redact_config_value, redact_secrets

REDACT_CASES = [
    # properties 形态(7-21 事故的实际键名形态)
    ("mqtt.ak=AKID12345", f"mqtt.ak={REDACTED}"),
    ("mqtt.sk=SKVAL678", f"mqtt.sk={REDACTED}"),
    ("register.secret_key=abcdef", f"register.secret_key={REDACTED}"),
    ("spring.datasource.password=Pa5s!w0rd", f"spring.datasource.password={REDACTED}"),
    # yaml / json 形态
    ("password: hunter2", f"password: {REDACTED}"),
    ('"password": "p@ss"', f'"password": "{REDACTED}"'),
    ('"apiKey": "xyz"', f'"apiKey": "{REDACTED}"'),
    ("access_key = AK999", f"access_key = {REDACTED}"),
    # URL 内嵌凭证与鉴权头
    ("jdbc:mysql://root:pw123@node1:3306/db", f"jdbc:mysql://root:{REDACTED}@node1:3306/db"),
    ("Authorization: Bearer eyJhbGciOi", f"Authorization: Bearer {REDACTED}"),
    # URL query 里的 token,值止于 &
    ("http://x/api?token=abc&x=1", f"http://x/api?token={REDACTED}&x=1"),
    # 管理面 配置里未被 SECRET: 打码的实际键名形态(2026-10-12 实测漏网)
    ("super_pass=abc123", f"super_pass={REDACTED}"),
    ("appmanager.pass=abc123", f"appmanager.pass={REDACTED}"),
    ("export KMS_SSL_KEYSTORE_PASS=changeit", f"export KMS_SSL_KEYSTORE_PASS={REDACTED}"),
    ("mqtt.secret.key=xyz", f"mqtt.secret.key={REDACTED}"),
    ('"super_pass": "abc"', f'"super_pass": "{REDACTED}"'),
    # 命令行长选项空格隔开的值(sqoop/DataX 任务命令,2026-10 实测漏网)
    ("sqoop import --username 'u1' --password 'Tab c1'", f"sqoop import --username 'u1' --password '{REDACTED}'"),
    ("datax --password Secret99 --x 1", f"datax --password {REDACTED} --x 1"),
    ('cmd --db-password "p w" -v', f'cmd --db-password "{REDACTED}" -v'),
    ("cli --api-key abc123;echo", f"cli --api-key {REDACTED};echo"),
]

KEEP_CASES = [
    "task=123",                 # sk/ak 是词尾但前缀非分隔符
    "peak=5",
    "port: 8080",
    "disk=50%",
    "host=node1.example.internal",
    "swap_used_pct: 100",
    "jdbc:mysql://node1:3306/db",   # 无凭证 URL
    "bypass=1",                 # pass 须是整段,不误伤 bypass/passive/compass
    "passive.mode=on",
    "compass=north",
    "rsync --passive host",     # 长选项名含 pass 片段但不是凭证键
]


@pytest.mark.parametrize("raw,expected", REDACT_CASES)
def test_redacts(raw, expected):
    assert redact_secrets(raw) == expected


@pytest.mark.parametrize("raw", KEEP_CASES)
def test_keeps(raw):
    assert redact_secrets(raw) == raw


@pytest.mark.parametrize("key,value,expected", [
    ("super_pass", "abc123", REDACTED),                       # 键名敏感:整值替换
    ("hive.password", "SECRET:hive-site:3:x", "SECRET:hive-site:3:x"),  # 管理面 掩码保留
    ("content", "a.password=pw1\nport=1", f"a.password={REDACTED}\nport=1"),
    ("appmanager.host", "node1", "node1"),
    ("dfs.replication", 3, 3),                                 # 非字符串原样
])
def test_redact_config_value(key, value, expected):
    assert redact_config_value(key, value) == expected


def test_multiline_config_dump():
    raw = "host=node5\nmqtt.password=secret1\nport=1883\nmqtt.ak=AK1"
    out = redact_secrets(raw)
    assert "secret1" not in out and "AK1" not in out
    assert "host=node5" in out and "port=1883" in out
