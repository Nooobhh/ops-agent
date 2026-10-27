"""平台枚举改走环境包后,模型可见 schema 与校验行为保持原样。"""
import pytest
from pydantic import TypeAdapter, ValidationError

from lib.envpack import active_env_pack
from lib.envpack.fields import KnowledgePlatform, TargetPlatform
from lib.triage_agent import tools
from lib.triage_agent.task_types import AlertEnvelope


def test_tool_schema_platform_enums_follow_env_pack_order():
    pack = active_env_pack()
    target = TypeAdapter(TargetPlatform).json_schema()
    assert target["enum"] == list(pack.cluster_keys())
    assert target["enum"] == ["alpha", "beta", "db", "shared", "cache", "edge"]
    draft = tools.TOOLS["create_kb_draft"].arguments.model_json_schema()
    proposal = draft["$defs"]["KnowledgeProposal"]["properties"]["platform"]
    # 知识库键:misc 集群的旧短键是 shared(与环境级同名),知识库另用 misc
    assert proposal["anyOf"][0] == {
        "enum": ["alpha", "beta", "db", "misc", "cache", "edge", "shared"], "type": "string",
    }


def test_platform_fields_validate_against_env_pack():
    assert AlertEnvelope(platform="Beta").platform == "beta"
    with pytest.raises(ValidationError):
        AlertEnvelope(platform="staging")
    with pytest.raises(ValidationError):
        TypeAdapter(TargetPlatform).validate_python("staging")
    knowledge = TypeAdapter(KnowledgePlatform)
    assert knowledge.validate_python("shared") == "shared"
    assert knowledge.validate_python("misc") == "misc"
    with pytest.raises(ValidationError):
        knowledge.validate_python("staging")


@pytest.mark.parametrize("message, expected", [
    ("看 alpha 的", "alpha"), ("Beta集群 node3", "beta"), ("数据库集群", "db"),
    ("数据库集群主库", "db"), ("平台=db", "db"), ("平台: shared", "shared"),
    ("零散节点上", "shared"), ("hdfs 问题", None), ("随便", None),
])
def test_reply_platform_parsing_follows_env_pack(message, expected):
    from lib.triage_agent.service import _platform_from_reply

    assert _platform_from_reply(message) == expected


def test_business_timezone_comes_from_env_pack(tmp_path, monkeypatch):
    from lib.acquisition.settings import AcquisitionSettingsError, default_settings, validate_settings
    from lib.envpack import env_timezone

    assert env_timezone().key == "UTC"
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "env.yaml").write_text("version: 1\nid: lab\ntimezone: Europe/Berlin\nclusters: []\n", encoding="utf-8")
    data_root = tmp_path / "data"
    (data_root / "config").mkdir(parents=True)
    (data_root / "config" / "paths.local.yaml").write_text(f"env_pack: {pack}\n", encoding="utf-8")
    monkeypatch.setenv("OPS_AGENT_DATA_ROOT", str(data_root))

    assert env_timezone().key == "Europe/Berlin"
    assert default_settings()["timezone"] == "Europe/Berlin"
    with pytest.raises(AcquisitionSettingsError, match="Europe/Berlin"):
        validate_settings({"timezone": "UTC"})
