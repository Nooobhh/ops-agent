"""Alert event model and host normalization helpers."""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Any, Mapping, Optional


@dataclass
class AlertEvent:
    source: str
    platform: str
    alertname: str
    severity: str
    host: Optional[str]
    host_raw: str
    service: str
    state: str
    detail: str
    maintenance: bool = False
    # 源端本轮触发时间(监控源自己记录的触发时刻),仅展示层用
    source_active_at: Optional[str] = None
    # firing 段末的源端 sample 时间
    source_last_seen_at: Optional[str] = None
    # resolved 被源端观察到的时间,仅 state="ok" 事件使用
    source_resolved_at: Optional[str] = None

    @property
    def key(self) -> str:
        return f"{self.source}:{self.platform}:{self.alertname}:{self.host or self.host_raw}"


def normalize_host(raw: str, nodes: Mapping[str, Any]) -> Optional[str]:
    if not raw:
        return None
    value = str(raw).strip()
    if not value:
        return None

    host_part = _strip_port(value)
    try:
        ip = str(ipaddress.ip_address(host_part))
    except ValueError:
        ip = None
    if ip is not None:
        for name, info in nodes.items():
            node_ip = _node_ip(info)
            if node_ip == ip:
                return name
        return None

    short = host_part.split(".", 1)[0]
    if short in nodes:
        return short
    if host_part in nodes:
        return host_part
    return None


def _strip_port(value: str) -> str:
    if value.count(":") == 1:
        host, port = value.rsplit(":", 1)
        if port.isdigit():
            return host
    return value


def _node_ip(info: Any) -> Optional[str]:
    if isinstance(info, Mapping):
        value = info.get("ip") or info.get("host")
    else:
        value = getattr(info, "ip", None) or getattr(info, "host", None)
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(str(value)))
    except ValueError:
        return str(value)
