"""账号页凭据项的通用存取:凭据项由环境包声明,这里用假声明验证存取、迁移、脱敏与校验语义。"""
import json

import pytest

from lib.credentials import (
    CredentialField,
    CredentialMissing,
    CredentialProfileSpec,
    CredentialStore,
    CredentialStoreError,
)


def _user_password(value):
    user, password = str(value.get("user") or "").strip(), str(value.get("password") or "")
    if not user or not password:
        raise CredentialStoreError("必须包含 user 和 password")
    return {"user": user, "password": password}


def _gateway(value):
    if any(not str(value.get(key) or "").strip() for key in ("username", "password", "otp")):
        raise CredentialStoreError("网关凭证不完整")
    return {key: str(value[key]).strip() for key in ("username", "password", "otp")}


FIELDS = (CredentialField("user", "账号"), CredentialField("password", "密码", kind="secret"))


def specs(tmp_path=None):
    return (
        CredentialProfileSpec(
            id="gateway", title="网关", validate=_gateway, username_field="username",
            fields=(CredentialField("username", "用户名"), CredentialField("password", "密码", kind="secret"),
                    CredentialField("otp", "OTP", kind="secret")),
            legacy_file=(tmp_path / "gateway-auth.json") if tmp_path else None,
        ),
        CredentialProfileSpec(id="acct:a", title="A 账号", fields=FIELDS, validate=_user_password,
                              username_field="user"),
        CredentialProfileSpec(id="acct:b", title="B 账号", fields=FIELDS, validate=_user_password,
                              username_field="user"),
    )


def store_at(tmp_path, **kwargs):
    return CredentialStore(tmp_path / "auth-profiles.json", merge_home=False, specs=specs(tmp_path), **kwargs)


def test_store_saves_profiles_atomically_and_public_status_is_redacted(tmp_path):
    store = store_at(tmp_path, legacy_credentials_path=tmp_path / "credentials.json")
    store.save_profile("gateway", {"username": "gateway-user", "password": "gateway-password", "otp": "JBSWY3DP"})
    store.save_profile("acct:a", {"user": "a-admin", "password": "a-password"})

    status = store.public_status()
    assert status["schema_version"] == 1
    assert status["profiles"]["gateway"]["configured"] is True
    assert status["profiles"]["gateway"]["username_hint"] == "ga***r"
    assert status["profiles"]["acct:a"]["configured"] is True
    assert status["profiles"]["acct:b"]["configured"] is False
    encoded = json.dumps(status, ensure_ascii=False)
    for secret in ("gateway-password", "JBSWY3DP", "a-password"):
        assert secret not in encoded
    assert (tmp_path / "auth-profiles.json").stat().st_mode & 0o077 == 0

    assert store.get_profile("acct:a")["user"] == "a-admin"
    with pytest.raises(CredentialMissing):
        store.get_profile("acct:b")
    with pytest.raises(CredentialStoreError, match="未知凭证"):
        store.get_profile("nope")


def test_store_prefers_new_profile_over_legacy_values(tmp_path):
    legacy = tmp_path / "credentials.json"
    legacy.write_text(json.dumps({"acct:a": {"user": "old", "password": "old-password"}}), encoding="utf-8")
    store = store_at(tmp_path, legacy_credentials_path=legacy)
    assert store.get_profile("acct:a")["user"] == "old"
    store.save_profile("acct:a", {"user": "new", "password": "new-password"})
    assert store.get_profile("acct:a")["user"] == "new"


def test_store_partial_update_preserves_legacy_profile_fields(tmp_path):
    legacy = tmp_path / "credentials.json"
    legacy.write_text(json.dumps({"acct:a": {"user": "old", "password": "old-password"}}), encoding="utf-8")
    store = store_at(tmp_path, legacy_credentials_path=legacy)
    store.save_profile("acct:a", {"password": "new-password"})
    assert store.get_profile("acct:a") == {"user": "old", "password": "new-password"}


def test_declared_legacy_file_is_read_until_profile_saved(tmp_path):
    (tmp_path / "gateway-auth.json").write_text(json.dumps({
        "username": "legacy-user", "password": "p", "otp": "o",
    }), encoding="utf-8")
    store = store_at(tmp_path)
    assert store.get_profile("gateway")["username"] == "legacy-user"
    assert store.public_status()["profiles"]["gateway"]["configured"] is True
    store.save_profile("gateway", {"password": "p2"})  # 只改密码,其余沿用旧文件
    assert store.get_profile("gateway") == {"username": "legacy-user", "password": "p2", "otp": "o"}
    (tmp_path / "gateway-auth.json").write_text("{broken", encoding="utf-8")
    assert store.get_profile("gateway")["password"] == "p2"  # 已保存即不再读旧文件


def test_merged_for_test_overlays_input_without_persisting(tmp_path):
    store = store_at(tmp_path)
    store.save_profile("acct:a", {"user": "admin", "password": "secret"})
    assert store.merged_for_test("acct:a", {"password": "try"}) == {"user": "admin", "password": "try"}
    assert store.get_profile("acct:a")["password"] == "secret"
    with pytest.raises(CredentialStoreError):
        store.merged_for_test("acct:b", {"user": "only-user"})


def test_store_rejects_incomplete_updates_without_overwriting_existing_profile(tmp_path):
    store = store_at(tmp_path)
    store.save_profile("acct:a", {"user": "admin", "password": "secret"})
    with pytest.raises(CredentialStoreError):
        store.save_profile("acct:a", {"password": ""})
    assert store.get_profile("acct:a")["password"] == "secret"


def test_store_delete_removes_profile_and_metadata(tmp_path):
    store = store_at(tmp_path)
    store.save_profile("acct:b", {"user": "admin", "password": "secret"})
    store.record_test("acct:b", ok=True)
    store.delete_profile("acct:b")
    assert store.public_status()["profiles"]["acct:b"]["configured"] is False
    assert store.public_status()["profiles"]["acct:b"]["last_test"] is None


def test_store_rejects_corrupt_profile_file_without_overwriting(tmp_path):
    path = tmp_path / "auth-profiles.json"
    path.write_text("{broken", encoding="utf-8")
    store = store_at(tmp_path)
    with pytest.raises(CredentialStoreError):
        store.save_profile("acct:a", {"user": "admin", "password": "secret"})
    assert path.read_text(encoding="utf-8") == "{broken"


def test_store_without_declared_profiles_still_serves_plugins(tmp_path):
    store = CredentialStore(tmp_path / "auth-profiles.json", merge_home=False, specs=())
    assert store.public_status() == {"schema_version": 1, "profiles": {}}
    store.save_plugin("demo", "bearer", {"token": "t0ken"})
    assert store.get_plugin_secret("demo", "bearer")["token"] == "t0ken"
