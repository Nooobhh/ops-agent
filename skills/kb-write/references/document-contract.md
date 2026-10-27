# 知识文档契约

手动导入 manifest 是 JSON 对象，字段如下：

```json
{
  "mode": "new",
  "title": "人类可读标题",
  "type": "troubleshoot",
  "platform": "alpha",
  "service": "hdfs",
  "problem": "datanode-disk-failure",
  "summary": "一段可独立理解的摘要",
  "knowledge_layer": "manual_task",
  "entrypoints": ["manual_task"],
  "profile": "diagnosis",
  "execution_scope": "mixed",
  "confidence": "observed",
  "symptoms": ["用户或告警可观察到的症状"],
  "alerts": [],
  "playbooks": [],
  "diagnostic_paths": [],
  "relations": [],
  "related": [],
  "sections": {
    "适用现象": "Markdown",
    "关键判据": "Markdown",
    "机理": "Markdown",
    "排查步骤": "Markdown",
    "证据与结果": "Markdown",
    "处置建议": "Markdown",
    "坑与误导信号": "可省略",
    "历史案例": "可省略",
    "补充材料": "可省略"
  }
}
```

共同规则：

- `mode` 只能是 `new` 或 `update`。
- `problem` 是小写 kebab-case，也是目标文件名的一部分。
- `service` 必须取项目封闭词表中的值。
- 写入非默认库用 `knowledge_base`（如分组库）；分组库的任务文档带 `task_type` 落对应类型目录，不带则进 `common/`。
- 类型通用文档 `problem` 固定为 `<类型小写>-general`；pid/tid 特化文档带 `process_id`（可加 `task_id`），`problem` 以 `p<pid>[-t<tid>]-` 开头。
- 操作手册可写 `sop_kind`：`routine`（日常，缺省可不写）/`change`（变更调优）/`deploy`（部署）；其他类型不得声明。
- `summary` 不超过 500 字，直接说明“是什么、何时适用、关键结论”。
- `related` 中使用 `[[path/or/slug]]`，目标必须已存在。
- `platform` 写短键（环境包的集群键或环境级 `shared`）；落盘时代码改写为资源引用（`<环境>/<集群>`，环境级为 `<环境>`），文档统一使用 `schema_version: 3`；资源轮询快照保持独立的资源 schema。
- `knowledge_layer` 描述检索用途，`profile` 描述正文模板；一个文档可以同时出现在多个 `entrypoints`。
- `knowledge` 类型（包括 baseline、topology、reference）不写 `status`；它们用 `confidence` 和 `observed_at` 表达事实可信度与时效。`troubleshoot`、`procedure`、`oplog` 才由代码生成 `draft` 状态并按规则推进。
- troubleshoot 的 `playbooks` 按 `order` 从小到大执行；每个诊断方向可声明自己的 playbook 列表和确认信号。
- 不写 `schema_version/source/source_ref/source_sha256/status/author`；代码自动生成（作者取本机用户）。
- 不使用 `n/a`、模板占位符或“尚未确认”。未知信息要写清楚缺口及验证方式。

各类型章节：

- `troubleshoot`：适用现象、关键判据、机理、排查步骤、证据与结果、处置建议；坑与误导信号、历史案例、补充材料可省略。至少一个 symptom。
- `knowledge/profile=baseline`：适用范围、组件与节点、关键配置、最新观测、边界与时效、参考。
- `knowledge/profile=topology`：适用范围、实体与关系、关键事实、边界与时效、参考；关系必须有明确证据，不从同机部署推断。
- `knowledge/profile=reference`：适用范围、关键知识、边界与注意事项、参考。
- `procedure/profile=sop`：适用场景、前置条件、步骤、验证、回滚；v2 支持平台专属人工 SOP。
- `oplog`：操作步骤、验证过程、回滚；只记录实际发生的操作，不写计划。

内容判断：

- “关键判据”写能区分本问题与相似问题的信号，不写泛泛检查项。
- “机理”只写原文或证据支持的因果关系；推断必须标明。
- “处置建议”区分只读确认、需审批变更和禁止由 agent 执行的动作。
- 导入材料相互矛盾时保留矛盾并说明，不自行选择看起来更合理的一边。
