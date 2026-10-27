"""演示告警源(alert_sources 扩展点):按集群各一个源,固定不产告警。

真实环境在这里接自己的告警系统:fetch 返回 lib.envpack.ext.AlertEvent 列表,取数失败抛 SourceError。
"""
from __future__ import annotations


class DemoSource:
    def __init__(self, context) -> None:
        self.context = context

    def fetch(self) -> list:
        return []


def create_source(context) -> DemoSource:
    return DemoSource(context)
