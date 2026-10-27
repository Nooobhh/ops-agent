"""工具静态元数据查询(ToolResult envelope 用);A3.7 起结构档的按 Agent/阶段准入已随旧编排器删除。"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .tool_contracts import ToolMetadata


class ToolAdmissionError(ValueError):
    """A deterministic, model-recoverable admission rejection."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "TOOL_NOT_ADMITTED",
        phase: str = "admission",
        disposition: str = "agent_retry",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.phase = phase
        self.disposition = disposition
        self.retryable = disposition == "agent_retry"


class ToolRegistry:
    """按工具名取静态元数据;未登记的工具抛 ToolAdmissionError,调用方退回无元数据的 envelope。"""

    def __init__(self, specs: Optional[Dict[str, Any]] = None) -> None:
        if specs is None:
            from .tools import TOOLS

            specs = TOOLS
        self._specs = dict(specs)

    def metadata(self, name: str) -> ToolMetadata:
        spec = self._specs.get(name)
        metadata = getattr(spec, "metadata", None)
        if metadata is None:
            raise ToolAdmissionError(
                f"工具未登记元数据: {name}", code="TOOL_METADATA_MISSING",
                disposition="task_fail",
            )
        return metadata
