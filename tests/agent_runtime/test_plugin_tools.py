import copy

from lib.agent_runtime import AgentRuntime
from lib.agent_runtime.tools import TOOL_NAMES
from lib.triage_agent.models import TaskStatus

from .conftest import FakeActions, ScriptedModel, call, new_task, reply


def plugin_tool(plugin_id="manager-alpha", cluster="alpha"):
    name = f"mcp__{plugin_id}__get_config"
    return {"name": name, "plugin": plugin_id, "tool": "get_config", "trusted": True, "cluster": cluster,
            "hash": "h1", "schema": {"type": "function", "function": {
                "name": name, "description": "[插件 Alpha 管理面] 读配置",
                "parameters": {"type": "object", "properties": {"service": {"type": "string"}},
                               "required": ["service"]}}}}


class PluginActions(FakeActions):
    def __init__(self, root, tools):
        super().__init__(root)
        self.snapshot = {"version": 1, "notices": [], "tools": tools,
                         "plugins": [{"id": "manager-alpha", "title": "Alpha 管理面", "cluster": "alpha",
                                      "trusted": True, "version": "0.1.0"}]}
        self.health = {"state": "ok"}
        self.plugin_calls = []

    def plugin_snapshot(self, platform, writable=False):
        return copy.deepcopy(self.snapshot)

    def plugin_health(self, plugin_id):
        return self.health

    def run_plugin_tool(self, run_id, *, plugin_id, tool, arguments, content_hash):
        self.plugin_calls.append((plugin_id, tool, arguments, content_hash))
        self.sessions[run_id]["steps"].append({
            "cmd": f"[mcp:{plugin_id}] {tool}", "exit_code": 0, "stderr": "", "approved": False,
            "stdout": '{"data": {"dfs.replication": "3"}}',
        })
        return {"plugin": plugin_id, "tool": tool, "trust": "official", "ok": True,
                "data": {"dfs.replication": "3"}, "warnings": [], "error": None, "meta": None}


def _runtime(env, replies):
    model = ScriptedModel(replies)
    return AgentRuntime(store=env.store, model=model, actions=env.actions,
                        retry_delays=(), sleep=lambda _: None), model


def _finish(match):
    return call("finish", {"answer": "副本数为 3", "confidence": "high",
                           "facts": [{"ref": "step:1", "match": match}]}, "f1")


def test_plugin_tools_are_frozen_offered_and_cited(env):
    env.actions = PluginActions(env.tmp, [plugin_tool()])
    task = new_task(env.store, message="HDFS 副本数是多少?")
    runtime, model = _runtime(env, [
        reply(call("mcp__manager-alpha__get_config", {"service": "HDFS"}, "p1")),
        reply(_finish("dfs.replication")),
    ])
    assert runtime.run(task.task_id).status is TaskStatus.WAITING_INPUT

    messages, tools = model.requests[0]
    assert "mcp__manager-alpha__get_config" in tools
    assert "mcp__ 开头" in messages[0]["content"], "有插件工具时系统提示带不可信数据说明"
    assert env.actions.plugin_calls == [("manager-alpha", "get_config", {"service": "HDFS"}, "h1")]
    stored = env.store.get(task.task_id)
    assert stored.plugin_tools["tools"][0]["name"] == "mcp__manager-alpha__get_config"
    assert stored.diagnostic_calls == 1
    result = next(event.payload["result"] for event in env.store.events(task.task_id)
                  if event.type == "tool_result" and event.payload["name"].startswith("mcp__"))
    assert result["model_result"]["ref"] == "step:1"
    assert result["target"]["kind"] == "plugin" and result["evidence"]["provenance"] == "runtime_tool"
    assert any("step:1" in line and "dfs.replication" in line for line in env.actions.finishes[-1]["evidence"])

    # 插件页之后停用了插件:已开始的任务工具清单不变(前缀缓存),续查仍是同一份
    env.actions.snapshot = {"version": 1, "plugins": [], "tools": [], "notices": []}
    env.store.resume_with_message(task.task_id, "再确认一次")
    env.store.claim_next("w1", lease_seconds=60)
    runtime, model = _runtime(env, [reply(content="仍为 3")])
    runtime.run(task.task_id)
    assert "mcp__manager-alpha__get_config" in model.requests[0][1]


def test_bad_arguments_and_other_cluster_are_rejected_without_calling(env):
    env.actions = PluginActions(env.tmp, [plugin_tool(), plugin_tool("manager-beta", "beta")])
    task = new_task(env.store)
    runtime, _ = _runtime(env, [
        reply(call("mcp__manager-alpha__get_config", {}, "p1"),
              call("mcp__manager-beta__get_config", {"service": "HDFS"}, "p2")),
        reply(content="查不了"),
    ])
    runtime.run(task.task_id)

    assert env.actions.plugin_calls == []
    errors = {event.payload["tool_call_id"]: str(event.payload["result"].get("data", {}).get("error"))
              for event in env.store.events(task.task_id) if event.type == "tool_result"}
    assert "缺少必填参数" in errors["p1"]
    assert "一个任务只对应一个集群" in errors["p2"]


def test_without_plugins_tools_and_prompt_are_unchanged(env):
    task = new_task(env.store)
    runtime, model = _runtime(env, [reply(content="正常")])
    runtime.run(task.task_id)

    messages, tools = model.requests[0]
    assert set(tools) == TOOL_NAMES
    assert "mcp__" not in messages[0]["content"]
    assert env.store.get(task.task_id).plugin_tools is None, "网关不支持插件时不冻结快照"


def test_unavailable_or_uncatalogued_plugins_are_announced_at_opening(env):
    env.actions = PluginActions(env.tmp, [plugin_tool()])
    env.actions.snapshot["notices"] = ["插件 Beta 管理面 已启用但还没有工具清单,请在插件页做连接测试"]
    env.actions.health = {"state": "unreachable", "detail": "连接失败"}
    task = new_task(env.store)
    runtime, _ = _runtime(env, [reply(content="正常")])
    runtime.run(task.task_id)

    opening = [event.payload["message"] for event in env.store.events(task.task_id)
               if event.type == "harness_notice"][0]
    assert "【插件】插件 Beta 管理面 已启用但还没有工具清单" in opening
    assert "【插件】插件 Alpha 管理面 当前不可用(连接失败)" in opening


def test_broken_plugin_config_does_not_block_the_task(env):
    env.actions = PluginActions(env.tmp, [])

    def broken(_platform, writable=False):
        raise ValueError("plugins.local.yaml 不可读")

    env.actions.plugin_snapshot = broken
    task = new_task(env.store)
    runtime, model = _runtime(env, [reply(content="正常")])
    runtime.run(task.task_id)

    assert set(model.requests[0][1]) == TOOL_NAMES
    assert "插件配置读取失败" in env.store.get(task.task_id).plugin_tools["notices"][0]


def test_global_plugin_tools_are_pinned_to_the_task_cluster(env):
    scope = {"role": "cluster", "argument": "filters", "label": "platform", "clusters": {"alpha": "alpha", "beta": "beta"}}
    tool = plugin_tool("prometheus", None)
    tool["schema"]["function"]["parameters"] = {"type": "object", "properties": {"filters": {"type": "object"}}}
    tool["scope"] = scope
    env.actions = PluginActions(env.tmp, [tool])
    task = new_task(env.store)
    runtime, _ = _runtime(env, [
        reply(call("mcp__prometheus__get_config", {}, "p1"),
              call("mcp__prometheus__get_config", {"filters": {"platform": "beta"}}, "p2")),
        reply(content="完成"),
    ])
    runtime.run(task.task_id)

    assert env.actions.plugin_calls == [("prometheus", "get_config", {"filters": {"platform": "alpha"}}, "h1")]
    errors = {event.payload["tool_call_id"]: str(event.payload["result"].get("data", {}).get("error"))
              for event in env.store.events(task.task_id) if event.type == "tool_result"}
    assert "一个任务只对应一个集群" in errors["p2"]
