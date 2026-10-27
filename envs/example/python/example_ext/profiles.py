"""账号页凭据项(credential_profiles 扩展点):每个托管集群一个管理面账号,演示声明式凭据表单。

真实环境可在 test 里做只读登录测试(成功返回提示语,失败抛 CredentialTestFailed)。
"""
from __future__ import annotations

from typing import Any, Dict, List

from lib.envpack import EnvPack
from lib.envpack.ext import CredentialField, CredentialProfileSpec, CredentialStoreError


def profiles(pack: EnvPack) -> List[CredentialProfileSpec]:
    return [
        CredentialProfileSpec(
            id=f"manager:{platform}",
            title=f"{pack.short_name(platform)} 管理面",
            description="该集群管理面插件与采集器使用的账号。",
            fields=(
                CredentialField("user", "账号", help="首次配置必填。"),
                CredentialField("password", "密码", kind="secret", help="留空保持已保存密码；输入新值会替换。"),
            ),
            validate=validate_user_password,
            username_field="user",
        )
        for platform in pack.cluster_keys("managed")
    ]


def validate_user_password(value: Dict[str, Any]) -> Dict[str, Any]:
    user = str(value.get("user") or "").strip()
    password = str(value.get("password") or "")
    if not user or not password:
        raise CredentialStoreError("管理面凭证必须包含 user 和 password")
    return {"user": user, "password": password}
