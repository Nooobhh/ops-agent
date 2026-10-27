"""本机用户:数据根所有者,存 ``<数据根>/config/user.local.yaml``(id + display_name)。"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple

import yaml

from lib.atomic_file import atomic_write_text
from lib.file_lock import FileLock

from .models import Author, DataStoreError, validate_author_id

DISPLAY_NAME_MAX = 64


class LocalUserDirectory:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock_path = self.path.with_name(f".{self.path.name}.lock")

    def load(self) -> Tuple[Optional[Author], Optional[str]]:
        """返回 (本机用户, 配置错误);未配置时两者皆 None。写路径据此不因坏配置中断。"""
        if not self.path.is_file():
            return None, None
        try:
            data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict):
                raise DataStoreError("顶层必须是映射")
            user_id = validate_author_id(data.get("id"))
            return Author(id=user_id, display_name=_display_name(data.get("display_name"), user_id)), None
        except (OSError, yaml.YAMLError, DataStoreError) as exc:
            return None, f"{self.path.name} 无效: {exc}"

    def local_user(self) -> Optional[Author]:
        return self.load()[0]

    def owner_id(self) -> Optional[str]:
        user = self.local_user()
        return user.id if user is not None else None

    def save_local_user(self, user_id: str, display_name: str) -> Author:
        author = Author(id=validate_author_id(user_id), display_name=_display_name(display_name, user_id))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self._lock_path)):
            atomic_write_text(self.path, yaml.safe_dump(
                {"id": author.id, "display_name": author.display_name},
                allow_unicode=True, sort_keys=False,
            ))
        return author

    def resolve(self, author_id: Optional[str]) -> Optional[Author]:
        owner = self.local_user()
        if not author_id:
            if owner is None:
                return None
            return Author(id=owner.id, display_name=owner.display_name, inferred=True)
        if owner is not None and owner.id == author_id:
            return owner
        # 一阶段没有他人目录:显示名即 ID;二阶段由共享服务的用户目录补全
        return Author(id=author_id, display_name=author_id)


def _display_name(value: object, user_id: str) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > DISPLAY_NAME_MAX:
        raise DataStoreError(f"显示名最长 {DISPLAY_NAME_MAX} 个字符")
    return text or user_id
