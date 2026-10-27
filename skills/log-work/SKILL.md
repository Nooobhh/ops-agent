---
name: log-work
description: 触发条件:/log、/log --add、记录工作、日报。支持从当前对话自动提取和手动补录两种模式。
---

# log-work — 工作日志记录

## 目的

将每日工作以结构化格式写入独立的 worklog 文件，供日报和周期汇总解析。

| 存储 | 用途 | 路径 |
|------|------|------|
| **worklog** | 日报及周期汇总数据源，仅记录工作任务 | `<记录根>/worklog/YYYY-MM-DD.md` |

**关键原则：**
- `/log` 只写 worklog
- 日报及周期汇总只读 worklog
- 个人项目和无关技术讨论不写入本项目 worklog
- worklog 是用户主动确认后才写入的"精选工作记录"

## 自动 emit（读者说明）

`/log` 主要用于补录。常规排查已经由 `ts_conclude`、`patrol_runner`、`alert_poll`
在收口时自动 emit（实现见 `lib/worklog.py`）。用户只需使用 `/log --add` 补记会议、
线下沟通和手动运维等未经过自动排查链路的工作。

## 集中日志路径

**一律经 `scripts/worklog_add.py` 写入，不直接编辑日志文件：**

```
<记录根>/worklog/YYYY-MM-DD.md    # 记录根 = paths.local.yaml 的 records_root,缺省 <数据根>/records
```

- YYYY-MM-DD 默认取当天日期，可通过 `--date` 参数覆盖
- 脚本按数据根定位，不依赖调用者当前目录；目录、日期文件与 `# YYYY-MM-DD` 标题行由脚本创建
- 作者行由脚本按本机用户(设置 → 本机用户)自动写入，不要手填

## `--date` 参数

两种模式均支持 `--date YYYY-MM-DD` 指定写入的日期文件：

```
/log --date 2026-05-26                    # 自动提取, 写入 2026-05-26.md
/log --add "线上巡检" --date 2026-05-26    # 手动补录, 写入 2026-05-26.md
```

- 不加 `--date`：默认当天日期
- 日期必须为有效格式 `YYYY-MM-DD`
- 日期不能是未来日期（报错提示）

## 两种模式

### 模式 1: `/log` — 自动提取

从当前对话中提取已完成的工作内容，生成结构化条目。

**执行流程：**

1. **扫描对话**：回顾当前 session 的对话历史，识别以下信号：
   - `##` 标题段落（讨论的主题）
   - 文件创建/修改操作
   - 问题 → 诊断 → 解决的完整闭环
   - 关键结论和产出

2. **提炼条目**：为每组相关工作生成一条日志，格式见下方模板。一条 session 可能有 1~3 条日志。

3. **向用户确认**：展示提炼结果（含来源/产出/关联/过程），逐条询问：
   - 标题是否准确
   - 状态确认：`[DONE]` / `[WIP]` / `[BLOCKED]`
   - 类型确认：`[BUGFIX]` / `[FEATURE]` / `[RESEARCH]` / `[OPS]` / `[DOC]` / `[MEETING]`
   - 过程步骤是否准确、是否需要补充

4. **挂工作项**：先跑 `scripts/work_items.py list --date <日期>` 取该日计划窗口内的未关闭事项，
   为每条日志挑一个对应事项连同确认稿给用户看；用户确认或改选后带 `--work-item`，对不上的不挂。

5. **写入**：确认后逐条调用 `scripts/worklog_add.py`(见下方「写入命令」)，读其 JSON 回执确认 `written: true`。

### 模式 2: `/log --add "标题"` — 手动补录

补录未通过 WorkBuddy 完成的工作（如会议、手动运维、线下沟通等）。

**执行流程：**

1. 解析标题参数
2. 询问用户：
   - 状态：`[DONE]` / `[WIP]` / `[BLOCKED]`
   - 类型：`[BUGFIX]` / `[FEATURE]` / `[RESEARCH]` / `[OPS]` / `[DOC]` / `[MEETING]`
   - 过程（可选；填写时按行概括，最多 8 条）
   - 关联文件/产出（可选）
   - 挂到哪个工作项（可选；同模式 1 第 4 步，从 `scripts/work_items.py list` 里选）
3. 确认后同样经 `scripts/worklog_add.py` 写入

## 日志条目格式

每条日志严格遵循以下模板：

```markdown
## [状态] [类型] 标题
- **来源**: {来源说明}
- **作者**: {用户 ID,脚本自动写入}
- **产出**: {关键产出}
- **关联**: {关联项, 逗号分隔}
- **过程**: {概括性步骤描述, 编号列表}
```

**格式规则（重要）：**
- `[状态]` 和 `[类型]` 在 `##` 标题行内，用空格分隔
- 来源行必填：`auto: {session主题简述}` 或 `manual: {用户手填}`
- 产出列文件名、脚本名、文档名等具体产出物
- 关联列相关文件路径或上游依赖
- 自动提取的过程列用 `1. xxx  2. xxx` 编号概括关键步骤，每条 20 字以内，3-8 条即可
- 手动补录的过程可为空；无过程、产出或关联项时写 `-`，不可省略行
- 每条日志末尾加一个空行
- `- **周报**: 忽略` 是可选元数据：缺省表示参与周报，只有用户在中台点击隐藏后才写入

用户屏蔽某条日志参与周报时，在该条目的来源行后增加：

```markdown
- **周报**: 忽略
```

**完整示例：**

```markdown
## [DONE] [BUGFIX] Doris VARCHAR 双规则修正
- **来源**: auto: doris_ddl-fix/varchar
- **产出**: fix_varchar_v2.py, ddl_tksjzx_v2.sql (6420列)
- **关联**: 参数生成.xlsx
- **过程**: 1. 发现VARCHAR双规则冲突导致DDL生成异常  2. 排查MySQL与Doris的VARCHAR长度映射差异  3. 修正fix_varchar.py为三倍规则  4. 重新生成6420列DDL并验证

## [DONE] [OPS] HD集群月度巡检
- **来源**: manual: 手动巡检
- **产出**: -
- **关联**: node1~17 all green
- **过程**: 1. 逐节点检查集群服务状态  2. 确认各节点核心服务均Started  3. 记录已停用服务清单(非异常)  4. 生成巡检汇总

## [WIP] [FEATURE] SeaTunnel 任务重试机制
- **来源**: auto: retry-mechanism
- **产出**: hive_to_doris.sh v2.1 (新增retry函数)
- **关联**: SocketTimeout根因分析报告.md
- **过程**: 1. 分析SocketTimeout根因  2. 设计指数退避重试机制  3. 实现retry函数并集成到v2.1脚本  4. 待实际数据验证
```

## 分类标签说明

| 标签 | 含义 | 适用场景 |
|------|------|----------|
| `[BUGFIX]` | 故障修复 | 修bug、改配置、hotfix |
| `[FEATURE]` | 功能开发 | 新脚本、新工具、新能力 |
| `[RESEARCH]` | 调研分析 | 根因分析、技术选型、性能测试 |
| `[OPS]` | 运维操作 | 巡检、发布、告警处理、配置变更 |
| `[DOC]` | 文档建设 | SOP编写、知识库更新、PPT制作 |
| `[MEETING]` | 会议沟通 | 评审、汇报、跨组协调 |

## 写入规则

1. **追加，不覆写**：始终将条目追加到当日文件末尾
2. **不修改已有条目**：如果用户想修改之前的日志，追加一条修正说明；唯一例外是中台可原子添加或移除 `- **周报**: 忽略` 元数据
3. **空行分隔**：每个条目之后保证一个空行
4. **当天文件首次写入**：如果日期文件不存在或为空，先写入 `# YYYY-MM-DD` 作为文件头
5. **不做去重**：同一项工作在不同时间 `/log` 多次都会被记录（保留时间线）

## 并发写入保护

脚本经数据层(`lib.datastore`)写入，文件锁 + 原子替换保护并发，避免多个 session 同时 `/log` 产生写冲突。

## 错误处理

- 脚本返回 `write_failed`：报错并建议用户检查 `<记录根>/worklog/` 是否可写
- 脚本返回 `bad_work_item`：工作项 ID 不存在，重新 `list` 后改选或不挂
- 脚本返回 `link_failed`：日志已写入但没挂上(多为该条已属别的工作项)，告知用户到中台工作项页处理，不要重写日志
- 用户取消确认：不写入，告知已取消
- 标题包含特殊字符：不转义，保持原样

## 写入命令

每条日志一次调用，stdout 为 JSON(`written`/`path`/`author`/`preview`)；先加 `--dry-run` 可只看渲染结果：

```bash
.venv/bin/python scripts/worklog_add.py --title "..." --status DONE --category MEETING \
  --source "manual: 线下沟通" --output "-" --related "-" \
  --process "整理议题" --process "完成沟通" --process "记录结论" [--date YYYY-MM-DD] [--work-item wi-…]
```

查可挂的工作项(只读，stdout 为 JSON `items`)：

```bash
.venv/bin/python scripts/work_items.py list --date YYYY-MM-DD
```

参数非法(状态/类型越界、未来日期、过程超过 8 条)返回 `{"ok": false, "error": "bad_args"}`,按 message 修正后重试。
