"""环境包 Python 扩展:按 env.yaml ``extensions`` 的 ``模块:对象`` 引用导入环境包提供的实现。

环境包代码与核心同等信任(运维方自己的私有包,随签名发布包分发)。导入时把环境包的
``python`` 目录追加到 ``sys.path`` 末尾:与核心模块重名时核心优先,扩展模块请用环境包专属前缀。
导入失败、对象不存在一律 ``EnvPackError`` 显式失败,不静默降级。
"""
from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path
from typing import Any

from . import EXTENSION_POINTS, EnvPack, EnvPackError

_REF_RE = re.compile(r"^[A-Za-z_][\w.]*:[A-Za-z_][\w.]*$")


def extension_decl(pack: EnvPack, point: str) -> Any:
    """扩展点的原始声明;未声明返回 None。"""
    if point not in EXTENSION_POINTS:
        raise EnvPackError(f"未知扩展点 {point!r}")
    return pack.extensions.get(point)


def load_object(pack: EnvPack, ref: str) -> Any:
    """``模块:对象``(对象可带点号取属性)→ 环境包 python 目录下导入得到的对象。"""
    text = str(ref or "").strip()
    if not _REF_RE.match(text):
        raise EnvPackError(f"扩展引用须形如 模块:对象: {ref!r}")
    if pack.python_dir is None:
        raise EnvPackError(f"环境包 {pack.id} 未声明 python 目录,无法加载扩展 {text}")
    if not pack.python_dir.is_dir():
        raise EnvPackError(f"环境包 python 目录不存在: {pack.python_dir}")
    entry = str(pack.python_dir)
    if entry not in sys.path:
        sys.path.append(entry)
    module_name, _, attr_path = text.partition(":")
    try:
        target: Any = importlib.import_module(module_name)
    except Exception as exc:  # 扩展模块里的任何导入错误都要带着引用名显式报出
        raise EnvPackError(f"扩展 {text} 导入失败: {type(exc).__name__}: {exc}") from exc
    origin = getattr(target, "__file__", None) or ""
    if not origin or not Path(origin).resolve().is_relative_to(Path(entry).resolve()):
        raise EnvPackError(f"扩展 {text} 解析到环境包之外的模块 {origin or module_name},请换用环境包专属模块名")
    for attr in attr_path.split("."):
        try:
            target = getattr(target, attr)
        except AttributeError as exc:
            raise EnvPackError(f"扩展 {text} 不存在属性 {attr}") from exc
    return target


__all__ = ["extension_decl", "load_object"]
