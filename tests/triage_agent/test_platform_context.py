from pathlib import Path

import pytest

from lib.triage_agent.platform_context import PlatformContext, PlatformContextError
from tests.envpack_helpers import as_env_pack


REPO_ROOT = Path(__file__).resolve().parents[2]


def _write_platform(tmp_path, platform="alpha", content=""):
    as_env_pack(tmp_path)
    config = tmp_path / "config"
    (config / f"platform-{platform}.yaml").write_text(content, encoding="utf-8")
    return tmp_path


def test_load_returns_only_explicit_endpoint_urls(tmp_path):
    repo_root = _write_platform(
        tmp_path,
        content="""
platform: alpha
cluster_name: example-cluster
endpoints:
  manager:
    url: http://198.51.100.101:8080
  hbase_master_info:
    host: 198.51.100.102
    port: 60010
  blank:
    url: ''
nodes: {}
""",
    )

    context = PlatformContext.load(repo_root, "alpha")

    assert context.endpoint("manager") == "http://198.51.100.101:8080"
    assert context.endpoint_names() == ["manager"]
    with pytest.raises(PlatformContextError, match="hbase_master_info"):
        context.endpoint("hbase_master_info")
    with pytest.raises(PlatformContextError, match="missing"):
        context.endpoint("missing")


def test_cluster_name_comes_from_registered_platform_value(tmp_path):
    repo_root = _write_platform(
        tmp_path,
        content="""
platform: alpha
cluster_name: example-cluster
endpoints: {}
nodes: {}
""",
    )

    assert PlatformContext.load(repo_root, "alpha").cluster_name() == "example-cluster"
    assert PlatformContext.load(REPO_ROOT, "alpha").cluster_name() == "alpha-cluster"


def test_missing_cluster_name_is_a_named_error(tmp_path):
    repo_root = _write_platform(
        tmp_path,
        content="""
platform: alpha
endpoints: {}
nodes: {}
""",
    )

    with pytest.raises(PlatformContextError, match="cluster_name"):
        PlatformContext.load(repo_root, "alpha").cluster_name()


@pytest.mark.parametrize("platform", ["", "Alpha", "other"])
def test_unsupported_platform_is_rejected(tmp_path, platform):
    with pytest.raises(PlatformContextError, match="platform"):
        PlatformContext.load(tmp_path, platform)


@pytest.mark.parametrize(
    "setup",
    [
        lambda tmp_path: as_env_pack(tmp_path),
        lambda tmp_path: _write_platform(tmp_path, content="endpoints: [\n"),
    ],
)
def test_unreadable_or_invalid_yaml_is_a_named_error(tmp_path, setup):
    repo_root = setup(tmp_path)

    with pytest.raises(PlatformContextError):
        PlatformContext.load(repo_root, "alpha")
