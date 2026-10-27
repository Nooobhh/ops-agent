"""写操作审批令牌:人工批准后由 Harness 签发,插件验签后才执行(与 ops-mcp core/approval.py 字节兼容)。

令牌 = base64url(json{plan_id, plan_hash, exp}) + "." + hex(HMAC-SHA256(secret, 前半段));
密钥取文本去首尾空白后的 UTF-8 字节,与插件读 approval.secret 文件的规则一致。
令牌只在签发它的那次调用里存在于内存,不落任何文件、事件或日志;落档只用 digest。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Optional

# 签完立即调用,不必用插件默认的 300s 上限
TOKEN_TTL_SECONDS = 120


def sign(secret: str, plan_id: str, plan_hash: str, *, ttl_s: int = TOKEN_TTL_SECONDS,
         now: Optional[float] = None) -> str:
    payload = json.dumps(
        {"plan_id": plan_id, "plan_hash": plan_hash, "exp": int((now or time.time()) + ttl_s)},
        sort_keys=True, separators=(",", ":"),
    ).encode()
    head = base64.urlsafe_b64encode(payload).rstrip(b"=")
    return head.decode() + "." + hmac.new(secret.strip().encode("utf-8"), head, hashlib.sha256).hexdigest()


def digest(token: str) -> str:
    """与插件审计日志的令牌摘要同算法,可据此对账,不可还原令牌。"""
    return hashlib.sha256(token.encode()).hexdigest()[:12]
