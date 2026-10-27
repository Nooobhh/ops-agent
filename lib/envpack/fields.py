"""pydantic 字段类型:合法值取活动环境包,JSON schema 的 enum 在生成时注入。

替代写死的 ``Literal[...]`` 平台枚举;enum 顺序即 env.yaml 声明顺序,
因此进模型的工具 schema 与原 Literal 生成的完全一致。
"""
from __future__ import annotations

from typing import Annotated, Any, Callable, Dict, Tuple

from pydantic import AfterValidator

from . import active_env_pack


class EnvEnum:
    def __init__(self, values: Callable[[], Tuple[str, ...]], label: str):
        self._values = values
        self._label = label

    def __call__(self, value: str) -> str:
        allowed = self._values()
        if value not in allowed:
            raise ValueError(f"{self._label}必须是 {list(allowed)} 之一")
        return value

    def __get_pydantic_json_schema__(self, schema: Any, handler: Any) -> Dict[str, Any]:
        result = handler(schema)
        result["enum"] = list(self._values())
        return result


_TARGET = EnvEnum(lambda: active_env_pack().cluster_keys(), "平台")
_KNOWLEDGE = EnvEnum(lambda: active_env_pack().knowledge_platforms(), "知识库平台")

# 排查实时目标:环境包声明的全部集群旧短键
TargetPlatform = Annotated[str, AfterValidator(_TARGET), _TARGET]
# 知识库 platform:knowledge 集群短键 + 环境级范围键
KnowledgePlatform = Annotated[str, AfterValidator(_KNOWLEDGE), _KNOWLEDGE]
