"""内存版 ops-mcp 写协议(plan / execute / track / reconcile / impact),顶替 call_plugin_tool。

验签与 ops-mcp core/approval.py 同逻辑:令牌 = base64url(json{plan_id, plan_hash, exp}).hex(HMAC);
execute 顺序与 ops-mcp 管理面插件 一致:计划不存在 → 已执行幂等 → 过期 → 验签 → 状态指纹。
"""
import base64
import hashlib
import hmac
import json
import time

from lib.plugins import PluginCallError

PLUGIN = "manager-alpha"
TERMINAL = {"COMPLETED", "FAILED", "ABORTED", "TIMEDOUT"}


class Clock:
    def __init__(self, now=None):
        self.now = now or time.time()

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def verify(secret: bytes, token: str, plan_id: str, plan_hash: str, now: float) -> str:
    """返回空串为通过,否则为 APPROVAL_INVALID 的原因(与 ops-mcp verify 同判定)。"""
    if not secret:
        return "实例未配置审批密钥"
    try:
        head, sig = token.strip().split(".", 1)
        if not hmac.compare_digest(hmac.new(secret, head.encode(), hashlib.sha256).hexdigest(), sig):
            return "签名不符"
        payload = json.loads(base64.urlsafe_b64decode(head + "=" * (-len(head) % 4)))
    except (ValueError, json.JSONDecodeError):
        return "令牌格式无效"
    if payload.get("plan_id") != plan_id or payload.get("plan_hash") != plan_hash:
        return "令牌与该计划不匹配"
    if int(payload.get("exp", 0)) < now:
        return "令牌已过期"
    return ""


class FakeOpsMcp:
    def __init__(self, secret: str, clock: Clock):
        self.secret = secret.strip().encode()
        self.clock = clock
        self.plan_ttl = 300
        self.plans = {}
        self.requests = {}
        self.calls = []
        self.tokens = []
        self.next_request = 3301
        self.noop = False
        self.plan_error = None
        self.execute_error = None
        self.state_changed = False
        # execute 时抛的传输错误;submit_first=True 表示插件其实已经下发(读超时)
        self.execute_raises = None
        self.submit_first = False
        self.track_script = ["IN_PROGRESS", "COMPLETED"]
        self.verification = {"succeeded": True, "mismatched": []}
        self.lost_verification = False

    def __call__(self, url, headers, timeout, name, arguments):
        assert headers.get("Authorization", "").startswith("Basic "), "写协议调用也要带插件凭据"
        self.calls.append((name, {key: value for key, value in arguments.items() if key != "approval_token"}))
        return _wrap(getattr(self, f"_{name}")(**arguments))

    def executed(self):
        return [args for name, args in self.calls if name == "execute_operation"]

    # ---- 工具 ----

    def _plan_operation(self, action, scope, service=None, component=None, host=None, reason=""):
        if self.plan_error:
            return _error(self.plan_error, "预检失败")
        index = len(self.plans) + 1
        plan_id = f"p-{index:010x}"
        target = {"action": action, "scope": scope, "service": service, "component": component, "host": host}
        plan_hash = hashlib.sha256(json.dumps({**target, "n": index}, sort_keys=True).encode()).hexdigest()
        self.plans[plan_id] = {"hash": plan_hash, "expires": self.clock() + self.plan_ttl, "result": None,
                               "target": target}
        return {
            "plan_id": plan_id, "plan_hash": plan_hash, "noop": self.noop, "expires_in_s": self.plan_ttl,
            "summary": [f"{action}:{component or service}@{host or '*'}"],
            "request": {"method": "POST", "path": "clusters/c/requests", "body": {"RequestInfo": {"command": "RESTART"}}},
            "current": [{"host": f"{host or 'node1'}.example.internal", "component": component or "X", "state": "STARTED",
                         "desired_state": "STARTED", "maintenance": "OFF"}],
            "pending_config_changes": [{"instance": "x@node11", "type": "logfeeder-env", "key": "k"}],
            "expected_max_duration_s": 1200, "warnings": ["该实例处于维护模式"],
        }

    def _execute_operation(self, plan_id, approval_token):
        self.tokens.append(approval_token)
        plan = self.plans.get(plan_id)
        if plan is None:
            return _error("PLAN_EXPIRED", "计划不存在或已过期")
        if plan["result"] is not None:
            return {**plan["result"], "idempotent_replay": True}
        if self.clock() > plan["expires"]:
            return _error("PLAN_EXPIRED", "计划已超过有效期")
        # 令牌由 Harness 签发时取真实时间;计划有效期走可拨的测试时钟
        reason = verify(self.secret, approval_token, plan_id, plan["hash"], time.time())
        if reason:
            return _error("APPROVAL_INVALID", f"审批令牌无效({reason})")
        if self.state_changed:
            return _error("PLAN_STALE", "目标状态在批准后发生了变化")
        if self.execute_error:
            return _error(self.execute_error, "插件拒绝")
        if self.noop:
            plan["result"] = {"plan_id": plan_id, "status": "NOOP", "request_id": None}
            return plan["result"]
        request_id = self.next_request
        self.next_request += 1
        self.requests[request_id] = {"plan_id": plan_id, "polls": 0}
        plan["result"] = {"plan_id": plan_id, "status": "SUBMITTED", "request_id": request_id}
        if self.execute_raises:
            error, self.execute_raises = self.execute_raises, None
            if not self.submit_first:
                del self.requests[request_id]
                plan["result"] = None
            raise error
        return {**plan["result"], "next": "用 get_operation 跟踪"}

    def _get_operation(self, request_id, task_id=None, tail_lines=200):
        request = self.requests[int(request_id)]
        status = self.track_script[min(request["polls"], len(self.track_script) - 1)]
        request["polls"] += 1
        final = status in TERMINAL
        tasks = [{"task_id": 1, "host": "node11.example.internal", "component": "LOGSEARCH_LOGFEEDER", "command": "RESTART",
                  "status": "FAILED" if status == "FAILED" else status if final else "IN_PROGRESS",
                  "exit_code": 1 if status == "FAILED" else 0, "duration_s": 12.0}]
        data = {"request_id": int(request_id), "status": status, "final": final,
                "progress_pct": 100 if final else 40, "context": f"[ops-agent] 重启 (plan {request['plan_id']})",
                "tasks": tasks}
        if status == "FAILED":
            data["logs"] = [{"task_id": 1, "host": "node11.example.internal", "component": "LOGSEARCH_LOGFEEDER",
                             "stdout_tail": "", "stderr_tail": "Traceback: start failed password=hunter2"}]
        if final and not self.lost_verification:
            data["verification"] = {"plan_id": request["plan_id"], "expected_state": "STARTED",
                                    **self.verification}
        return data

    def _list_operations(self, limit=20, status=None, since=None):
        return {"operations": [
            {"request_id": rid, "context": f"[ops-agent] 重启 x (plan {item['plan_id']})", "status": "IN_PROGRESS"}
            for rid, item in sorted(self.requests.items(), reverse=True)
        ]}

    def _get_topology(self, service=None):
        return {"focus": {"service": service, "upstream": ["ZOOKEEPER"], "downstream": ["YARN", "HBASE"]}}

    def _get_service(self, service):
        return {"service": service, "state": "STARTED"}


def _error(code, message):
    return _ErrorEnvelope(code, message)


class _ErrorEnvelope:
    def __init__(self, code, message):
        self.code, self.message = code, message


def _wrap(data):
    if isinstance(data, _ErrorEnvelope):
        envelope = {"schema_version": 1, "ok": False, "instance": PLUGIN, "meta": {}, "data": None, "warnings": [],
                    "error": {"code": data.code, "message": data.message, "retryable": False, "details": {}}}
    else:
        envelope = {"schema_version": 1, "ok": True, "instance": PLUGIN, "meta": {}, "data": data, "warnings": [],
                    "error": None}
    return {"content": [{"type": "text", "text": json.dumps(envelope, ensure_ascii=False)}]}


def transport_timeout():
    return PluginCallError("PLUGIN_UNAVAILABLE", "请求超时", maybe_delivered=True)


PLUGIN_TOOLS = [
    {"name": "plan_operation", "description": "生成启停/重启计划(只读预检,不执行)。",
     "annotations": {"readOnlyHint": True},
     "inputSchema": {"type": "object", "properties": {
         "action": {"type": "string"}, "scope": {"type": "string"}, "service": {"type": "string"},
         "component": {"type": "string"}, "host": {"type": "string"}, "reason": {"type": "string"}},
         "required": ["action", "scope"]}},
    {"name": "execute_operation", "description": "执行已批准的计划。", "annotations": {"readOnlyHint": False},
     "inputSchema": {"type": "object", "properties": {"plan_id": {"type": "string"},
                                                      "approval_token": {"type": "string"}},
                     "required": ["plan_id", "approval_token"]}},
    {"name": "get_operation", "description": "某个操作的逐主机任务状态。", "annotations": {"readOnlyHint": True},
     "inputSchema": {"type": "object", "properties": {"request_id": {"type": "integer"}},
                     "required": ["request_id"]}},
    {"name": "list_operations", "description": "最近的操作。", "annotations": {"readOnlyHint": True},
     "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}}}},
    {"name": "get_topology", "description": "栈依赖与拓扑。", "annotations": {"readOnlyHint": True},
     "inputSchema": {"type": "object", "properties": {"service": {"type": "string"}}}},
]
MANIFEST_READ_ONLY = {"plan_operation": True, "execute_operation": False, "get_operation": True,
                      "list_operations": True, "get_topology": True}


def install_plugin(service, *, secret=None, plan_enabled=True):
    """在 PluginService 里装好 manager-alpha:启用、凭据、工具缓存、(可选)审批密钥与 plan 工具开关。"""
    from lib.plugins.catalog import normalize_tool

    service.registry.update(PLUGIN, enabled=True)
    service.credentials.save_plugin(PLUGIN, "basic", {"user": "admin", "password": "test-pw"})
    tools = [normalize_tool(item, MANIFEST_READ_ONLY[item["name"]]) for item in PLUGIN_TOOLS]
    service.catalog.put(PLUGIN, {"url": "http://127.0.0.1:30161/mcp", "server": {}, "manifest": {"name": PLUGIN},
                                 "version": "0.1.0", "tools": tools})
    if secret:
        service.credentials.save_plugin_approval_secret(PLUGIN, secret)
    if plan_enabled:
        service.registry.set_tool(PLUGIN, "plan_operation", enabled=True)
    return {item["name"]: item for item in tools}
