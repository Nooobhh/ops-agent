---
name: kb-write
description: 知识文档创建、更新与晋升规范。中台排查提交结构化草稿；本地材料先改造成 manifest 再导入。禁止脚本或模型直接写 knowledge/。
---

# kb-write

知识库只有两个文档入口：中台排查生成、手动导入本地材料。模型负责内容判断，代码负责文档形状、路径、状态、证据和原子写入。

## 中台排查

- 报告完成后调用 `create_kb_draft`，只提交工具 schema 允许的结构化字段。
- `summary` 写成可独立理解的短摘要；`mechanism` 再展开因果机理，不要简单重复。
- 已有文档匹配本次结论时用 `append_case`；新故障模式才用 `new`。
- `evidence` 只能逐字引用本次真实采证输出。不要把知识文档、推断或处置建议当证据。
- 人工任务必须完成意图与全文两段审批；自动任务由代码直接落 draft。
- 后续独立排查满足代码晋升策略时自动晋升，模型不得自行修改 `status`。

## 手动导入

当用户给出本地 Markdown、会议记录或历史文档时：

1. 完整阅读原文。
2. 阅读 [文档契约](references/document-contract.md)，按对应类型整理内容。
3. 生成 JSON manifest，不直接生成或编辑 `knowledge/*.md`。
4. 运行：

```bash
.venv/bin/python scripts/kb_import.py --source <本地原文> --manifest <manifest.json>
```

新建使用 `mode=new`；更新同一路径文档使用 `mode=update`；存量文档不在约定路径（如组件子目录、旧文件名）时加 `--target <文档 ID>`，类型/平台/服务/problem 须与原文档一致。troubleshoot 更新会回到 draft，等待重新验证；knowledge（含 baseline/topology/reference）不设置 draft。

## 晋升

- 中台自动晋升：代码核对后续独立 run、`conclusive`、历史案例、平台/服务和内容呼应。
- 人工晋升：用户明确确认后运行：

```bash
.venv/bin/python -m lib.kb promote <doc> --reason "<人工确认理由>" [--run-id <关联排查>]
```

- procedure、oplog 永不晋升。
- 禁止手改 frontmatter.status、verified_by、verified_at 或 verified_reason。

## 内容职责

模型负责：

- 标题和摘要能让人快速判断是否适用。
- 症状、关键判据、机理、步骤和处置互相一致。
- 明确区分已验证事实、推断、建议和未验证边界。
- 保留原文的重要事实，不为了格式化删掉关键证据。

代码负责：schema v3、文档层/用途、章节顺序、目录、文件名、来源、排查/操作文档的 draft 状态、链接、告警唯一性、多 playbook 指针、lint、索引重建和失败回滚；knowledge 不进入 draft/verified 生命周期。

根、服务和组件 `_index.md` 的 `GENERATED:KB-INDEX` 区域由 rebuild 自动维护。不要编辑标记内内容；人工概述、架构、端口、依赖等写在标记外。

## 禁止

- 禁止采集器直接输出到 `knowledge/`。
- 禁止直接复制本地原文进知识库而不做结构改造。
- 禁止通过编辑器或临时脚本绕过统一 writer。
