import assert from "node:assert/strict"
import test from "node:test"

import {
  assertSchemaV1,
  consultedKnowledgeDocs,
  executedCommands,
  isTerminalTask,
  mergeTaskEvents,
  modelUsageSummary,
  toConversationBlocks,
} from "../src/lib/triageEvents.ts"
import type { TriageEvent } from "../src/types.ts"


function event(event_id: string, type = "status", payload = {}): TriageEvent {
  return {
    schema_version: 1,
    event_id,
    task_id: "tr-1",
    ts: `2026-07-26T00:00:0${event_id.at(-1)}Z`,
    type,
    payload,
  }
}


test("deduplicates events and preserves server order", () => {
  const merged = mergeTaskEvents(
    [event("ev-1"), event("ev-2")],
    [event("ev-2"), event("ev-3")],
  )

  assert.deepEqual(
    merged.map((item) => item.event_id),
    ["ev-1", "ev-2", "ev-3"],
  )
})


test("projects missing task context as a user question", () => {
  const blocks = toConversationBlocks([event("ev-1", "task_context_clarification_requested", {
    kind: "task_context",
    question: "请确认本任务所属平台",
    options: ["Alpha 集群", "Beta 集群"],
    task_type: "investigation",
  })])
  assert.equal(blocks[0].kind, "task_type_question")
  if (blocks[0].kind === "task_type_question") {
    assert.equal(blocks[0].request.question, "请确认本任务所属平台")
    assert.deepEqual(blocks[0].request.options, ["Alpha 集群", "Beta 集群"])
  }
})


test("hides provider reasoning blocks while keeping the final assistant text", () => {
  const blocks = toConversationBlocks([
    event("ev-1", "assistant_message", {
      message: {
        role: "assistant",
        content: "<thinking>private chain of thought</thinking>结论已整理。<analysis>also private</analysis>",
      },
    }),
  ])

  assert.deepEqual(blocks, [{
    kind: "message",
    eventId: "ev-1",
    role: "assistant",
    text: "结论已整理。",
  }])
})


test("hides an unclosed provider thinking block", () => {
  const blocks = toConversationBlocks([
    event("ev-1", "assistant_message", {
      message: {
        role: "assistant",
        content: "<thinking>Computing optimization deltas from recorded output",
      },
    }),
  ])

  assert.equal(blocks[0].kind, "message")
  if (blocks[0].kind === "message") assert.equal(blocks[0].text, "")
})


test("groups legacy tool events by the assistant tool-call batch", () => {
  const blocks = toConversationBlocks([
    event("ev-1", "assistant_message", {
      message: {
        role: "assistant",
        content: null,
        tool_calls: [
          {id: "invoke-1", type: "function", function: {name: "run_command", arguments: "{}"}},
          {id: "invoke-2", type: "function", function: {name: "run_command", arguments: "{}"}},
        ],
      },
    }),
    event("ev-2", "tool_call", {
      tool_call_id: "invoke-1",
      name: "run_command",
      arguments: {pipeline: [["hostname"]]},
    }),
    event("ev-3", "tool_result", {
      tool_call_id: "invoke-1",
      name: "run_command",
      result: {command: "hostname", node: "node1", stdout: "node1"},
    }),
    event("ev-4", "tool_result", {
      tool_call_id: "invoke-2",
      name: "run_command",
      result: {skipped: true, reason: "前序调用失败"},
    }),
  ])
  const tools = blocks.filter((block) => block.kind === "tool")
  assert.equal(tools.length, 2)
  assert.equal(tools[0].batchId, tools[1].batchId)
  assert.notEqual(tools[0].batchId, undefined)
})


test("joins a tool call and result into one safe activity", () => {
  const blocks = toConversationBlocks([
    event("ev-1", "tool_call", {
      tool_call_id: "invoke-1",
      name: "run_diagnostic",
      arguments: {
        diagnostic_id: "system.memory_summary",
        params: {},
        hidden_prompt: "must not render",
      },
    }),
    event("ev-2", "tool_result", {
      tool_call_id: "invoke-1",
      name: "run_diagnostic",
      result: {
        diagnostic_id: "system.memory_summary",
        node: "node12",
        stdout: "ok",
        model_payload: "must not render",
      },
    }),
  ])

  assert.deepEqual(blocks, [{
    kind: "tool",
    eventId: "ev-1",
    invocationId: "invoke-1",
    toolName: "run_diagnostic",
    actionId: "system.memory_summary",
    label: "system.memory_summary",
    scope: "node12",
    node: "node12",
    status: "done",
    stdout: "ok",
    stderr: "",
    commands: [],
    documentKind: null,
    knowledgeDoc: null,
    durationSeconds: null,
    argumentsText: JSON.stringify({
      diagnostic_id: "system.memory_summary",
      params: {},
      hidden_prompt: "must not render",
    }, null, 2),
    resultText: JSON.stringify({
      diagnostic_id: "system.memory_summary",
      node: "node12",
      stdout: "ok",
      model_payload: "must not render",
    }, null, 2),
  }])
})


test("keeps a pending tool call when no result has arrived", () => {
  const blocks = toConversationBlocks([
    event("ev-1", "tool_call", {
      tool_call_id: "invoke-1",
      name: "run_registered_playbook",
      arguments: {playbook: "playbooks/alpha/system/check_memory_pressure.sh"},
    }),
  ])

  assert.equal(blocks[0].kind, "tool")
  if (blocks[0].kind === "tool") assert.equal(blocks[0].status, "running")
})


test("projects canonical ToolResult execution data and partial outcome", () => {
  const blocks = toConversationBlocks([
    event("ev-1", "tool_call", {
      tool_call_id: "invoke-1",
      name: "run_command",
      arguments: {pipeline: [["df", "-P"]]},
    }),
    event("ev-2", "tool_result", {
      tool_call_id: "invoke-1",
      name: "run_command",
      result: {
        schema_version: 1,
        tool: {name: "run_command", version: "1.0"},
        call_id: "invoke-1",
        ok: true,
        outcome: "completed_partial",
        target: {kind: "registered_node", platform: "alpha", node: "node1", scope: "node"},
        data: {
          command: "df -P",
          execution: {exit_code: 0, stdout: "Filesystem", stderr: "", duration: 0.2, truncated: true},
        },
        connection: {status: "reused", route: "direct", channel: "exec", reused: true},
        evidence: {eligible: true, refs: ["invoke-1"], provenance: "runtime_tool"},
        metrics: {duration_ms: 200, output_bytes: 10},
        warnings: [],
        error: null,
        tool_result: {
          schema_version: 1,
          tool: {name: "run_command", version: "1.0"},
          call_id: "invoke-1",
          ok: true,
          outcome: "completed_partial",
          target: {kind: "registered_node", platform: "alpha", node: "node1", scope: "node"},
          data: {command: "df -P", execution: {stdout: "Filesystem", stderr: "", exit_code: 0}},
          connection: null,
          evidence: {eligible: true, refs: ["invoke-1"], provenance: "runtime_tool"},
          metrics: {duration_ms: 200, output_bytes: 10, attempts: 1},
          warnings: [],
          error: null,
        },
      },
    }),
  ])
  assert.equal(blocks[0].kind, "tool")
  if (blocks[0].kind === "tool") {
    assert.equal(blocks[0].status, "partial")
    assert.equal(blocks[0].stdout, "Filesystem")
    assert.equal(blocks[0].durationSeconds, 0.2)
  }
})


test("projects command approval events into a command review block", () => {
  const blocks = toConversationBlocks([
    event("ev-1", "approval_requested", {
      approval_id: "cm-1",
      kind: "command",
      command: "cat /var/log/messages\ngrep ERROR",
      signature: "grep -c @/etc/hive/conf/hive-site.xml",
      node: "node12",
      mode: "whitelist",
      reason: "命令未登记",
      review: {safe: false, reason: "未参与", reviewed: false},
      targets: {
        paths: ["/var/log/messages", "/etc/hive/conf/hive-site.xml"],
        mysql: [],
        mysql_parsed: true,
      },
    }),
    event("ev-2", "approval_decided", {
      approval_id: "cm-1",
      decision: "approve",
      scope: "task",
    }),
  ])

  assert.deepEqual(blocks[0], {
    kind: "command_review",
    eventId: "ev-1",
    approvalId: "cm-1",
    command: "cat /var/log/messages\ngrep ERROR",
    signature: "grep -c @/etc/hive/conf/hive-site.xml",
    node: "node12",
    mode: "whitelist",
    reason: "命令未登记",
    review: {safe: false, reason: "未参与", reviewed: false},
    targets: {
      paths: ["/var/log/messages", "/etc/hive/conf/hive-site.xml"],
      mysql: [],
      mysql_parsed: true,
    },
    outcome: {decision: "approve", feedback: "", scope: "task"},
  })
})


test("terminal unpaired calls are interrupted and use task node fallback", () => {
  const blocks = toConversationBlocks([
    event("ev-1", "tool_call", {
      tool_call_id: "invoke-1",
      name: "run_registered_playbook",
      arguments: {playbook: "playbooks/alpha/system/check_memory_pressure.sh"},
    }),
  ], {
    taskStatus: "failed",
    taskNode: "node12",
  })

  assert.equal(blocks[0].kind, "tool")
  if (blocks[0].kind === "tool") {
    assert.equal(blocks[0].status, "interrupted")
    assert.equal(blocks[0].scope, "node12")
  }
})


test("knowledge activities use knowledge scope and redact sensitive details", () => {
  const blocks = toConversationBlocks([
    event("ev-1", "tool_call", {
      tool_call_id: "invoke-1",
      name: "read_kb_document",
      arguments: {
        doc: "alpha/system/memory.md",
        api_key: "sk-secret",
        nested: {authorization: "Bearer private", safe: "visible"},
      },
    }),
    event("ev-2", "tool_result", {
      tool_call_id: "invoke-1",
      name: "read_kb_document",
      result: {
        content: "# Memory",
        password: "private",
        stdout: "Authorization: Bearer top-secret-token",
      },
    }),
  ], {
    taskStatus: "completed",
    taskNode: "node12",
  })

  assert.equal(blocks[0].kind, "tool")
  if (blocks[0].kind === "tool") {
    assert.equal(blocks[0].scope, "知识库")
    assert.match(blocks[0].argumentsText, /\[已隐藏\]/)
    assert.match(blocks[0].argumentsText, /visible/)
    assert.doesNotMatch(blocks[0].argumentsText, /sk-secret|Bearer private/)
    assert.doesNotMatch(blocks[0].resultText, /private|top-secret-token/)
  }
})


test("collects executed commands from diagnostics and playbook steps", () => {
  const blocks = toConversationBlocks([
    event("ev-1", "tool_call", {
      tool_call_id: "invoke-1",
      name: "run_diagnostic",
      arguments: {diagnostic_id: "system.disk_summary", params: {}},
    }),
    event("ev-2", "tool_result", {
      tool_call_id: "invoke-1",
      name: "run_diagnostic",
      result: {node: "node1", command: "df -P", stdout: "ok"},
    }),
    event("ev-3", "tool_call", {
      tool_call_id: "invoke-2",
      name: "run_registered_playbook",
      arguments: {playbook: "playbooks/alpha/system/check.sh"},
    }),
    event("ev-4", "tool_result", {
      tool_call_id: "invoke-2",
      name: "run_registered_playbook",
      result: {
        node: "node1",
        steps: [{cmd: "uptime"}, {cmd: "vmstat 1 3"}],
      },
    }),
  ])

  assert.deepEqual(
    executedCommands(blocks).map((item) => item.command),
    ["df -P", "uptime", "vmstat 1 3"],
  )
  assert.equal(executedCommands(blocks)[0].node, "node1")
})


test("collects consulted knowledge documents in order without duplicates", () => {
  const blocks = toConversationBlocks([
    event("ev-1", "tool_call", {
      tool_call_id: "invoke-search",
      name: "search_kb_candidates",
      arguments: {doc: "shared/hdfs/search-only.md"},
    }),
    event("ev-2", "tool_call", {
      tool_call_id: "invoke-read-1",
      name: "read_kb_document",
      arguments: {doc: "shared/hdfs/hdfs-datanode-storage.md"},
    }),
    event("ev-3", "tool_call", {
      tool_call_id: "invoke-read-2",
      name: "read_kb_document",
      arguments: {},
    }),
    event("ev-4", "tool_result", {
      tool_call_id: "invoke-read-2",
      name: "read_kb_document",
      result: {doc: "beta/hdfs/beta-hdfs-datanode-storage-percent.md"},
    }),
    event("ev-5", "tool_call", {
      tool_call_id: "invoke-read-3",
      name: "read_kb_document",
      arguments: {doc: "shared/hdfs/hdfs-datanode-storage.md"},
    }),
    event("ev-6", "tool_call", {
      tool_call_id: "invoke-draft",
      name: "create_kb_draft",
      arguments: {doc: "beta/hdfs/draft-only.md"},
    }),
    event("ev-7", "tool_result", {
      tool_call_id: "invoke-read-1",
      name: "read_kb_document",
      result: {doc: "shared/hdfs/hdfs-datanode-storage.md"},
    }),
    event("ev-8", "tool_result", {
      tool_call_id: "invoke-read-3",
      name: "read_kb_document",
      result: {doc: "shared/hdfs/hdfs-datanode-storage.md"},
    }),
    event("ev-9", "tool_call", {
      tool_call_id: "invoke-denied-read",
      name: "read_kb_document",
      arguments: {doc: "beta/hdfs/denied.md"},
    }),
    event("ev-10", "tool_result", {
      tool_call_id: "invoke-denied-read",
      name: "read_kb_document",
      result: {error: "知识文档未登记", failed: true},
    }),
    event("ev-11", "tool_call", {
      tool_call_id: "invoke-running-read",
      name: "read_kb_document",
      arguments: {doc: "beta/hdfs/still-running.md"},
    }),
  ], {taskStatus: "failed"})

  const unfinishedRead = blocks.find(
    (block) => block.kind === "tool"
      && block.invocationId === "invoke-running-read",
  )
  assert.equal(unfinishedRead?.kind, "tool")
  if (unfinishedRead?.kind === "tool") {
    assert.equal(unfinishedRead.status, "interrupted")
  }
  assert.deepEqual(consultedKnowledgeDocs(blocks), [
    "shared/hdfs/hdfs-datanode-storage.md",
    "beta/hdfs/beta-hdfs-datanode-storage-percent.md",
  ])
})


test("archived report and knowledge draft render as documents", () => {
  const blocks = toConversationBlocks([
    event("ev-1", "tool_call", {
      tool_call_id: "invoke-1",
      name: "finalize_report",
      arguments: {verdict: "conclusive"},
    }),
    event("ev-2", "tool_result", {
      tool_call_id: "invoke-1",
      name: "finalize_report",
      result: {report: "records/troubleshoot/ts-1/report.md"},
    }),
    event("ev-3", "tool_call", {
      tool_call_id: "invoke-2",
      name: "create_kb_draft",
      arguments: {proposal: {mode: "new"}},
    }),
    event("ev-4", "tool_result", {
      tool_call_id: "invoke-2",
      name: "create_kb_draft",
      result: {kb_persisted: false, revision_requested: true},
    }),
  ])

  const kinds = blocks.map(
    (block) => block.kind === "tool" ? block.documentKind : "other",
  )
  assert.deepEqual(kinds, ["report", null])
})


test("knowledge review requests carry the rendered draft and decision", () => {
  const blocks = toConversationBlocks([
    event("ev-1", "approval_requested", {
      approval_id: "kb-1",
      kind: "kb_draft",
      doc: "knowledge/alpha/system/memory.md",
      mode: "new",
      markdown: "# 草稿",
    }),
    event("ev-2", "approval_decided", {
      approval_id: "kb-1",
      approved: false,
      decision: "revise",
      feedback: "标题要带节点名",
    }),
  ])

  assert.equal(blocks[0].kind, "kb_review")
  if (blocks[0].kind === "kb_review") {
    assert.equal(blocks[0].request.markdown, "# 草稿")
    assert.deepEqual(blocks[0].outcome, {
      decision: "revise",
      feedback: "标题要带节点名",
    })
  }
})


test("write-action approvals stay on the legacy approval block", () => {
  const blocks = toConversationBlocks([
    event("ev-1", "approval_requested", {
      approval_id: "wa-1",
      action_id: "system.restart",
      command: "systemctl restart x",
    }),
    event("ev-2", "approval_decided", {approval_id: "wa-1", approved: true}),
  ])

  assert.equal(blocks[0].kind, "approval")
  if (blocks[0].kind === "approval") assert.equal(blocks[0].decided, true)
})


test("rejects unsupported schema versions and identifies terminal states", () => {
  assert.throws(() => assertSchemaV1({schema_version: 2}), /不支持/)
  assert.equal(isTerminalTask("completed"), true)
  assert.equal(isTerminalTask("kb_failed"), true)
  assert.equal(isTerminalTask("waiting_input"), false)
})


test("projects model timeout retries as a retry notice", () => {
  const blocks = toConversationBlocks([event("ev-1", "model_retry", {
    attempt: 2,
    max_attempts: 3,
    wait_seconds: 5,
    error: "模型请求失败: ReadTimeout",
  })])
  assert.deepEqual(blocks, [{
    kind: "model_retry", eventId: "ev-1", attempt: 2, maxAttempts: 3, waitSeconds: 5,
  }])
})


test("projects a multi-node shell call with per-node output and partial status", () => {
  const blocks = toConversationBlocks([
    event("ev-1", "tool_call", {
      tool_call_id: "c1", name: "shell", arguments: {nodes: ["node1", "node2"], command: "free -h\nuptime"},
      batch_id: "b1", batch_index: 0,
    }),
    event("ev-2", "tool_result", {
      tool_call_id: "c1", name: "shell",
      result: {command: "free -h\nuptime", results: [
        {node: "node1", ref: "step:1", exit_code: 0, stdout: "Mem: 125G\n", stderr: ""},
        {node: "node2", error: "SSH 连接失败"},
      ]},
    }),
  ])
  assert.equal(blocks[0].kind, "tool")
  if (blocks[0].kind === "tool") {
    assert.equal(blocks[0].label, "free -h")
    assert.equal(blocks[0].scope, "node1, node2")
    assert.equal(blocks[0].status, "partial")
    assert.equal(blocks[0].commandStatus, "zero")
    assert.match(blocks[0].stdout, /## node1 · 退出 0 · step:1\nMem: 125G/)
    assert.match(blocks[0].stdout, /## node2 · 执行失败\nSSH 连接失败/)
    assert.deepEqual(blocks[0].commands, ["free -h\nuptime"])
  }
})


test("projects a plugin tool call as plugin · tool with its data or error", () => {
  const blocks = toConversationBlocks([
    event("ev-1", "tool_call", {tool_call_id: "p1", name: "mcp__manager-alpha__get_config", arguments: {service: "HDFS"}}),
    event("ev-2", "tool_result", {tool_call_id: "p1", name: "mcp__manager-alpha__get_config", result: {
      ref: "step:1", plugin: "manager-alpha", tool: "get_config", ok: true, data: {"dfs.replication": "3"},
    }}),
    event("ev-3", "tool_call", {tool_call_id: "p2", name: "mcp__manager-alpha__list_alerts", arguments: {}}),
    event("ev-4", "tool_result", {tool_call_id: "p2", name: "mcp__manager-alpha__list_alerts", result: {
      plugin: "manager-alpha", tool: "list_alerts", ok: false, data: null,
      error: {code: "PLUGIN_UNAVAILABLE", message: "连接失败"},
    }}),
  ])
  const tools = blocks.filter((block) => block.kind === "tool")
  assert.equal(tools.length, 2)
  if (tools[0].kind === "tool" && tools[1].kind === "tool") {
    assert.equal(tools[0].label, "manager-alpha · get_config")
    assert.equal(tools[0].scope, "插件 manager-alpha")
    assert.match(tools[0].stdout, /"dfs.replication": "3"/)
    assert.equal(tools[1].stderr, "PLUGIN_UNAVAILABLE: 连接失败")
  }
})


test("projects finish verification as a conclusion block, failed finish stays a tool", () => {
  const verified = toConversationBlocks([
    event("ev-1", "tool_call", {tool_call_id: "f1", name: "finish", arguments: {answer: "x"}}),
    event("ev-2", "tool_result", {tool_call_id: "f1", name: "finish", result: {
      verdict: "conclusive", grounded: true, report: "records/troubleshoot/r1/report.md",
      hypotheses: [{
        statement: "DN 心跳超时", status: "supported", declared: "supported", downgraded: [],
        evidence: [{role: "observation", ref: "step:3", found: true, text: "heartbeat took 30012 ms"}],
        related: [],
      }, {
        statement: "磁盘满", status: "insufficient", declared: "refuted", downgraded: ["推翻需要两个不同命令的反证"],
        evidence: [{role: "absence", ref: "step:9", found: false, reason: "引用的步骤不存在"}], related: [],
      }],
      facts: [],
    }}),
  ])
  assert.equal(verified[0].kind, "finish")
  if (verified[0].kind === "finish") {
    assert.equal(verified[0].summary.grounded, true)
    assert.equal(verified[0].summary.hypotheses[0].evidence[0].text, "heartbeat took 30012 ms")
    assert.equal(verified[0].summary.hypotheses[1].declared, "refuted")
    assert.deepEqual(verified[0].summary.hypotheses[1].reasons, ["推翻需要两个不同命令的反证"])
  }
  const failed = toConversationBlocks([
    event("ev-1", "tool_call", {tool_call_id: "f1", name: "finish", arguments: {}}),
    event("ev-2", "tool_result", {tool_call_id: "f1", name: "finish", result: {error: "参数不合法", failed: true}}),
  ])
  assert.equal(failed[0].kind, "tool")
})


test("projects harness notices by the marker event in front of them", () => {
  const blocks = toConversationBlocks([
    event("ev-1", "agent_opening", {tier: "guided"}),
    event("ev-2", "harness_notice", {message: "【任务上下文】{}"}),
    event("ev-3", "budget_reminder", {turns: 25}),
    event("ev-4", "harness_notice", {message: "[预算提醒] 本档软预算已过半"}),
    event("ev-5", "closing_round", {reason: "模型轮次达到硬上限"}),
    event("ev-6", "harness_notice", {message: "[收口回合] 模型轮次达到硬上限。"}),
    event("ev-7", "closing_round_unanswered", {reason: "收口回合未调用 finish"}),
    event("ev-8", "harness_tier_changed", {from: "autonomous", to: "guided"}),
    event("ev-9", "model_usage", {prompt_tokens: 100}),
  ])
  assert.deepEqual(
    blocks.map((block) => block.kind === "notice" ? block.variant : block.kind),
    ["opening", "reminder", "closing", "unanswered", "tier"],
  )
  const tier = blocks[4]
  assert.ok(tier.kind === "notice" && tier.text.includes("自主档 → 引导档"))
})


test("report gate shows hypotheses for the new runtime and keeps the legacy shape", () => {
  const [modern] = toConversationBlocks([event("ev-1", "approval_requested", {
    approval_id: "rg-1", kind: "report_gate", tool_call_id: "f1", reason: "结论没有服务器事实依据",
    proposal: {
      finish: {answer: "可能是心跳超时", remediation: ["观察"]},
      verification: {hypotheses: [{
        statement: "DN 心跳超时", declared: "supported", status: "insufficient",
        reasons: ["缺少摘录成功的判据(阈值/配置/预期值)"],
        evidence: [{role: "observation", ref: "step:1", found: true, text: "heartbeat took 30012 ms", match: "x"}],
        related: [],
      }]},
    },
  })])
  assert.ok(modern.kind === "report_review")
  if (modern.kind === "report_review") {
    assert.equal(modern.request.answer, "可能是心跳超时")
    assert.equal(modern.request.hypotheses?.[0].status, "insufficient")
    assert.deepEqual(modern.request.remediation, ["观察"])
  }
  const [legacy] = toConversationBlocks([event("ev-1", "approval_requested", {
    approval_id: "rg-2", kind: "report_gate", reason: "零采证",
    proposal: {verdict: "conclusive", severity: "P2", root_cause: "x", evidence: ["a"], remediation: []},
  })])
  assert.ok(legacy.kind === "report_review" && legacy.request.hypotheses === undefined)
})


test("summarizes model usage cache hits and counts kb_read as consulted", () => {
  const usage = modelUsageSummary([
    event("ev-1", "model_usage", {prompt_tokens: 1000, cached_tokens: 900, completion_tokens: 50}),
    event("ev-2", "model_usage", {prompt_tokens: 1000, cached_tokens: 700, completion_tokens: 30}),
  ])
  assert.deepEqual(usage, {requests: 2, promptTokens: 2000, cachedTokens: 1600, completionTokens: 80, hitRate: 0.8})
  assert.equal(modelUsageSummary([event("ev-1")]), null)
  const blocks = toConversationBlocks([
    event("ev-1", "tool_call", {tool_call_id: "k1", name: "kb_read", arguments: {doc: "cluster/alpha/x.md"}}),
    event("ev-2", "tool_result", {tool_call_id: "k1", name: "kb_read", result: {doc: "cluster/alpha/x.md", content: "..."}}),
  ])
  assert.deepEqual(consultedKnowledgeDocs(blocks), ["cluster/alpha/x.md"])
})


test("projects a write operation into one card that follows execution to its outcome", () => {
  const approval = {
    approval_id: "wo-1", kind: "operation", tool_call_id: "w1", plugin: "manager-alpha", cluster: "alpha",
    title: "重启 LOGFEEDER@node11", reason: "配置生效", plan_id: "p-1", plan_hash: "abcdef0123456789xyz",
    created_at: "2026-10-22T10:00:00+00:00", expires_at: "2026-10-22T10:05:00+00:00",
    expected_max_duration_s: 1200,
    plan: {
      summary: ["重启:LogFeeder@node11"],
      current: [{host: "node11", component: "LOGFEEDER", state: "STARTED", desired_state: "STARTED", maintenance: "ON"}],
      pending_config_changes: [{instance: "LOGFEEDER@node11", type: "logfeeder-env", key: "heap"}],
      warnings: ["处于维护模式"],
      request: {method: "POST", path: "clusters/c/requests", body: {RequestInfo: {command: "RESTART"}}},
    },
    impact: {ok: true, downstream: ["YARN"], upstream: []},
  }
  const events = [
    event("ev-1", "approval_requested", approval),
    event("ev-2", "approval_decided", {approval_id: "wo-1", decision: "approve", decided_by: {id: "alice", name: "alice"}}),
    event("ev-3", "operation_executing", {approval_id: "wo-1", attempt: 1}),
    event("ev-4", "operation_executed", {approval_id: "wo-1", token_digest: "d1g3st", request_id: 3301}),
    event("ev-5", "operation_submitted", {approval_id: "wo-1", request_id: 3301, token_digest: "d1g3st"}),
    event("ev-6", "operation_progress", {approval_id: "wo-1", request_status: "IN_PROGRESS", progress_pct: 40,
      tasks: [{host: "node11", component: "LOGFEEDER", status: "IN_PROGRESS", exit_code: 0, duration_s: 3}]}),
  ]
  const [card] = toConversationBlocks(events).filter((block) => block.kind === "operation_review")
  assert.equal(card.kind, "operation_review")
  if (card.kind !== "operation_review") return
  assert.equal(card.request.title, "重启 LOGFEEDER@node11")
  assert.deepEqual(card.request.pendingChanges, ["LOGFEEDER@node11 · logfeeder-env · heap"])
  assert.deepEqual(card.request.impact?.downstream, ["YARN"])
  assert.match(card.request.request.body, /RESTART/)
  assert.equal(card.outcome?.decision, "approve")
  assert.equal(card.decidedBy, "alice")
  assert.equal(card.execution?.phase, "submitted")
  assert.equal(card.execution?.requestId, 3301)
  assert.equal(card.execution?.progressPct, 40)

  const finished = toConversationBlocks([...events, event("ev-7", "operation_finished", {
    approval_id: "wo-1", outcome: "succeeded", reason: "复核一致", oplog: "knowledge/oplog/alpha/x.md",
  })]).find((block) => block.kind === "operation_review")
  assert.ok(finished && finished.kind === "operation_review")
  if (finished?.kind !== "operation_review") return
  assert.equal(finished.execution?.phase, "finished")
  assert.equal(finished.execution?.outcome, "succeeded")
  assert.equal(finished.execution?.oplog, "knowledge/oplog/alpha/x.md")
})


test("marks maybe-delivered executions as reconciling and replans as such", () => {
  const events = [
    event("ev-1", "approval_requested", {approval_id: "wo-1", kind: "operation", plan: {}, title: "x"}),
    event("ev-2", "operation_executed", {approval_id: "wo-1", maybe_delivered: true}),
  ]
  const card = toConversationBlocks(events).find((block) => block.kind === "operation_review")
  assert.ok(card?.kind === "operation_review" && card.execution?.phase === "reconciling")
  const replanned = toConversationBlocks([...events, event("ev-3", "operation_finished", {
    approval_id: "wo-1", outcome: "not_executed", replanned: true,
  })]).find((block) => block.kind === "operation_review")
  assert.ok(replanned?.kind === "operation_review" && replanned.execution?.replanned)
})
