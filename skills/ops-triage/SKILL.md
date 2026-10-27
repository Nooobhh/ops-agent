---
name: ops-triage
description: 大数据运维排查入口。接告警名或症状,敲 triage_match 匹配知识文档,命中则跑 playbook,否则转 ops-deep-dive。全程固定命令 + 读 JSON,LLM 不写代码不自由取数。
---

# ops-triage — 排查入口 skill

> **执行前置**:CWD 必须在仓库根(宿主挂载时由宿主保证;手动跑先 cd 到仓库根)。
> 命令里 `$PY` = `.venv/bin/python`(Windows 宿主 = `.venv\Scripts\python.exe`)。
> 所有脚本 stdout 是单个 JSON:`ok=true` 成功;`ok=false` 时读 `error`/`message` 处理。

## 何时使用

- 用户丢来一个告警(如 `集群监控-内存告警` on node12)
- 用户描述一个症状(如 "node12 内存快满了")
- 用户点名要跑某个已知 playbook

## 如何做(严格顺序)

### 1. 提取三元组(LLM 唯一自由点之一)

从用户输入抽取:`alertname`(精确告警名,可无)/ `platform`(hd|iot,必须)/
`service`(可无)/ `problem_hint`(一句话症状)/ `node`(triage 必须)。
platform 或 triage 的 node 无法确定 → **先问用户,不猜**。无 alertname 且属集群级调查
(多节点/盘点/全景类)→ 第 4 步改用 `ts_session.py start --type survey --nodes n1,n2,...`,
trigger 写 `survey:<主题>`。

### 2. 匹配 routing(纯命令)

```
$PY scripts/triage_match.py --alert "<alertname>" --platform <hd|iot> --service <svc>
```
(无 alertname 就省 `--alert`;无 service 就省 `--service`。
**无 alertname 时必须把用户原始症状传 `--query "<problem_hint>"`**——走 kb-recall
排序召回,Top5 按 score 排;`recall_index_error` 时先跑 `$PY scripts/build_wiki.py` 再重试。)

读 JSON 分支:
- `hit` 非 null → 记下 `hit.doc`、`hit.playbook` 与有序 `hit.playbooks`,直接进第 4 步
- `hit` null 且 `candidates` 非空 → 第 3 步
- 两者皆空 → 第 4 步走 4c(深挖)

### 3. 候选选择题(LLM 唯一自由点之二)

对 `candidates`(通常 2-8 条)读每条 `title` + `symptoms`,对比 `problem_hint`,
选**至多一篇**;全对不上就答"无匹配"走 4c。
**禁止**:生成候选之外的路径、对候选加权评分。选中候选的 `playbooks` 字段进第 4 步。

### 4. 建 session + 分派(纯命令)

```
$PY scripts/ts_session.py start --node <node> --trigger "<alertname 或 manual:<hint>>" --platform <p> --service <s>
```
survey 分流改用 `$PY scripts/ts_session.py start --type survey --nodes n1,n2,...
--trigger "survey:<主题>" --platform <p> --service <s>`。
读 JSON 记下 `run_id`,后续所有命令都带它。

**4a. 有 playbook**(命中的 `playbooks` 按 `order` 升序执行；旧文档退回单值 `playbook`):
```
$PY scripts/ts_playbook.py --run-id <run_id> --node <node> --playbook <playbook>
```
按 JSON `verdict` 分支。每个诊断方向执行对应 playbook；`conclusive` 停止，`needs_llm` 交由模型处理，`insufficient` 继续下一个方向；全部方向都无法确认时再转深挖:
- `conclusive` → 写 conclusion 文件后收口,**LLM 零归因**:
  `root_cause` 直接抄 `conclusion_hint`,`evidence` 逐条抄 `steps[].name: actual`。
  conclusion JSON 落 `records/troubleshoot/<run_id>/artifacts/conclusion.json`
  (从 `skills/ops-deep-dive/conclusion.template.json` 抄骨架,字段契约见 ops-deep-dive
  「收口」表;值取 playbook 输出),然后:
  `$PY scripts/ts_conclude.py --run-id <run_id> --conclusion-json-file records/troubleshoot/<run_id>/artifacts/conclusion.json`
- `needs_llm` → LLM 介入归因:读 `steps`+`metrics`+命中文档(**不跑新命令**),
  自写 conclusion JSON(evidence 必须引用 steps 的 actual 关键行) → ts_conclude
- `insufficient` → 转 `ops-deep-dive`(带同一 run_id,不重开 session)

CLI 排查只生成 report/records，不直接写知识库。需要沉淀时在中台排查任务中生成；
已有本地报告需要导入时走 `kb-write` 的手动导入流程。

**4b. 有文档无 playbook**(过渡态):LLM 读文档「排查步骤」章节,转 `ops-deep-dive`
用 ts_exec 逐条执行(同一 run_id)。收口时 kb-write 提醒固化 playbook 回填 frontmatter。

**4c. 无文档**:转 `ops-deep-dive` 自由排查(同一 run_id)。收口后可在中台生成知识草稿。

## 关键约束

- 全程只允许敲本 skill 与 ops-deep-dive 列出的 `scripts/` 命令;**禁止**内联 Python、
  禁止直接 ssh/subprocess、禁止手写 session.json / report.md
- playbook 路径只能来自 triage_match 输出(ts_playbook 会硬拒未登记路径)
- conclusion JSON 一律落 artifacts/ 文件传参,不内联命令行(引号转义坑)
- 环境事实先查 `config/platform-*.yaml` endpoints 与 baseline/connectivity 文档,再上节点

## 引用

- 深挖 skill:`skills/ops-deep-dive/SKILL.md` · 沉淀 skill:`skills/kb-write/SKILL.md`
- routing 编译:`$PY scripts/build_routing.py` · 知识库 lint:`$PY -m lib.kb`
