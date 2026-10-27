"""按环境包生成清单;进程级缓存,任一来源文件变化(mtime/size)即重建,不落盘。"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional, Tuple

from lib.envpack import EnvPack, load_env_pack

from .model import Inventory

_Signature = Tuple[Tuple[str, Optional[int], Optional[int]], ...]
_CACHE: Dict[Tuple[Path, Path], Tuple[EnvPack, _Signature, Inventory]] = {}


def load_inventory(repo_root: Path) -> Inventory:
    from .sources.legacy import build_inventory, source_paths

    repo_root = Path(repo_root).resolve()
    pack = load_env_pack(repo_root)
    signature = tuple(_stat(path) for path in source_paths(pack))
    key = (pack.root, repo_root)
    cached = _CACHE.get(key)
    if cached is not None and cached[0] is pack and cached[1] == signature:
        return cached[2]
    inventory = build_inventory(pack)
    _CACHE[key] = (pack, signature, inventory)
    return inventory


def _stat(path: Path) -> Tuple[str, Optional[int], Optional[int]]:
    try:
        stat = path.stat()
    except OSError:
        return str(path), None, None
    return str(path), stat.st_mtime_ns, stat.st_size
