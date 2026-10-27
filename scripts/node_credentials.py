"""Initialize or explicitly replace project-local node SSH credentials.

生成由环境包的节点凭据提供方(``node_credentials`` 扩展)完成;环境包未声明提供方时
无事可做,返回 ok 并注明 skipped。不带 ``--replace-existing`` 是登录初始化路径:已缓存的凭据原样复用,
不删除服务端已有凭据;替换只留给显式的人工调用。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from lib.data_root import local_config_path
from lib.credentials import (
    CredentialGenerateFailed,
    CredentialProviderUnavailable,
    FileCredentials,
    node_credential_provider,
)
from lib.envpack import load_env_pack

try:
    from scripts._common import emit_ok
except ImportError:  # direct script execution
    from _common import emit_ok


def _emit_failure(code: str, detail: str) -> int:
    print(json.dumps({"ok": False, "code": code, "detail": detail}, ensure_ascii=False))
    return 1


def run(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ip-prefix", default=None)
    parser.add_argument("--credentials", default=str(local_config_path(REPO_ROOT, "credentials.json")))
    parser.add_argument("--profile", default=None)
    parser.add_argument(
        "--replace-existing",
        action="store_true",
        help="显式替换已有凭据(由提供方删除旧凭据并重新生成)",
    )
    args = parser.parse_args(argv)

    try:
        provider = node_credential_provider(load_env_pack(REPO_ROOT))
    except Exception as exc:
        return _emit_failure("provider_unavailable", f"{type(exc).__name__}: {exc}")
    if provider is None:  # 只用密钥直连的环境无需生成,中台启动时也走这里
        return emit_ok(skipped="环境包未声明节点凭据提供方(node_credentials)", generated=[], cached=[],
                       failed=[], total=0, success=0)
    try:
        credential_kwargs = {"merge_home": True}
        if args.profile:
            credential_kwargs["profile_path"] = Path(args.profile)
        credentials = FileCredentials(Path(args.credentials), **credential_kwargs)
        result = (
            provider.generate_all(credentials, args.ip_prefix, replace_existing=True)
            if args.replace_existing
            else provider.initialize(credentials, args.ip_prefix)
        )
    except CredentialProviderUnavailable as exc:
        return _emit_failure("provider_unavailable", str(exc))
    except CredentialGenerateFailed as exc:
        return _emit_failure("generate_failed", str(exc))
    except Exception as exc:
        return _emit_failure("generate_failed", f"{type(exc).__name__}: {exc}")

    generated = sorted(
        ip for ip, item in result.items()
        if item.get("ok") is True and item.get("status") == "generated"
    )
    cached = sorted(
        ip for ip, item in result.items()
        if item.get("ok") is True and item.get("status") == "cached"
    )
    failed = [
        {"ip": ip, "error": str(item.get("error") or "unknown")}
        for ip, item in sorted(result.items())
        if item.get("ok") is not True
    ]
    total = len(result)
    success = len(generated) + len(cached)
    if success == 0:
        detail = failed[0]["error"] if failed else f"未找到匹配 {args.ip_prefix} 的节点"
        return _emit_failure("generate_failed", detail)
    return emit_ok(
        generated=generated,
        cached=cached,
        failed=failed,
        total=total,
        success=success,
    )


if __name__ == "__main__":
    sys.exit(run())
