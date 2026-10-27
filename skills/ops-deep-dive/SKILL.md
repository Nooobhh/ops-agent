---
name: ops-deep-dive
description: knowledge 覆盖不到时的逐步排查。每条命令敲 ts_exec 固定脚本(红线过滤+自动落档),WRITE 需人在环审批,收口敲 ts_conclude。LLM 只提命令与归因,不写代码。
---

# ops-deep-dive — 深挖 skill

> **执行前置**:CWD 必须在仓库根(宿主挂载时由宿主保证;手动跑先 cd 到仓库根)。
> 命令里 `$PY` = `.venv/bin/python`(Windows 宿主 = `.venv\Scripts\python.exe`)。

## 何时使用

由 `ops-triage` 转入:alert/三元组未命中文档、有文档无 playbook(照文档逐步跑)、
playbook verdict=`insufficient`。**session 已由 ops-triage 建好,深挖只累加,不重开。**

## 单条命令循环

LLM 每轮:
1. 陈述一个 hypothesis(如"内存问题的活跃换页情况")
2. 生成一条**只读**命令,敲:
   ```
   $PY scripts/ts_exec.py --run-id <run_id> --node <node> --cmd "<命令>"
   ```
3. 读输出 JSON:`ok=true` → 读 `stdout` 更新 hypothesis;结论成立则收口
4. `ok=false` 时按 `error` 处理:
   - `write_needs_approval` → 走下方写操作审批流程
   - `forbidden` → 该命令永不放行,换思路另生成
   - `exec_error` → 连接/节点问题,检查 node 名与凭证再试
5. 疑似慢命令(如全盘扫描)加 `--timeout 180`;单行超长输出(如大 JSON)先在节点落文件
   再 `python -m json.tool <file>` 多行化读取
6. **同一命令要跑多台节点**时用取数 CLI(仍走红线,只读,自动落档);MySQL 不走节点上的客户端,
   一律经中台 MySQL 插件查询:
   ```
   $PY scripts/kquery.py --platform <p> --nodes n1,n2,n3 --cmd "<命令>" --run-id <run_id>
   ```

## 写操作(人在环审批)

确需 WRITE(如 kill 僵尸进程、swapoff)时:
1. 向用户展示:**命令 + 影响 + 回滚方式**,等用户明确说"批准"
2. 批准后同一命令加 `--approved` 重敲:
   ```
   $PY scripts/ts_exec.py --run-id <run_id> --node <node> --cmd "<写命令>" --approved
   ```
3. 立刻按 `kb-write` 手动导入流程生成 oplog;收口 conclusion 的
   `oplogs` 字段必须引用它,否则 ts_conclude 报 `oplog_missing` 拒绝收口

## 收口

从模板起稿:`cp skills/ops-deep-dive/conclusion.template.json records/troubleshoot/<run_id>/artifacts/conclusion.json`,按下表改值(**类型错一个字段 ts_conclude 就整体打回**):

| 字段 | 类型 | 约束 |
|---|---|---|
| `verdict` | str 必填 | 封闭集 `conclusive` / `needs_llm` / `insufficient` |
| `severity` | str 必填 | 封闭集 `P0` / `P1` / `P2` / `ok` |
| `root_cause` | str 必填 | 一句话根因;阴性论据(「无 X 异常」)可写这里 |
| `evidence` | list 必填 | 每条引用 steps stdout 关键行,带数值与时刻;不得编造 |
| `remediation` | list 必填 | 只读建议与需审批建议分开列 |
| `oplogs` | list 可省 | 有 approved 步骤时必填 `knowledge/cluster/oplog/**.md` 路径(旧 `knowledge/oplog/` 仍可解析),否则拒收口 |
CLI 收口不再接受 `kb_doc`、`kb_draft` 或 `kb_doc_effective`；知识库只由中台排查生成，
或把本地材料按 `kb-write` 规范转换后导入。然后:
```
$PY scripts/ts_conclude.py --run-id <run_id> --conclusion-json-file records/troubleshoot/<run_id>/artifacts/conclusion.json
```

没走 session 的手工排查先用 `$PY scripts/ts_backfill.py --manifest <manifest.json>` 事后补录
成 open run，再走 `ts_conclude.py` 收口；需要入库时另走手动导入入口。

## 关键约束

- **禁止**内联 Python / subprocess / 直接 ssh;到集群的唯一通道就是 ts_exec
- 外部工具产物只放 `records/troubleshoot/<run_id>/artifacts/`;session.json/report.md
  永远由脚本产出,不手写
- 环境事实先查 `config/platform-*.yaml` endpoints 与 baseline 文档,再上节点验证
- 深挖中发现高频路径 → 告知 kb-write 沉淀成 playbook

## 引用

- 入口:`skills/ops-triage/SKILL.md` · 沉淀:`skills/kb-write/SKILL.md`
- 红线规则人读版:`knowledge/cluster/shared/procedures/redlines.md`
