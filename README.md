# ops-agent

通用 OPS 中台:知识库路由 + playbook 脚本优先 + LLM 按需介入;环境专有内容装进环境包,数据源与执行经 MCP 插件接入。

[English](README.en.md) · [架构总览](docs/architecture.md) · [更新记录](CHANGELOG.md) · [路线](ROADMAP.md)

## 是什么

一个给运维团队用的中台:告警汇聚与分级、资源风险、排查 Agent、知识库、工作项与工作日志/周报,常驻在内网办公机或服务器上。

- **知识库是路由真源**:Markdown 文档的 frontmatter 声明适用告警、症状与 playbook;中台按它路由告警、组装检索上下文
- **脚本优先**:标准排查路径编译成节点上一次执行的只读 playbook(stdout 纯 JSON),零 LLM 也能出结论
- **Agent 按需介入**:单一 Agent 运行时负责规划与证据推理;能力边界、命令红线、审批、恢复与落档由 Harness 负责
- **安全边界不随模型变化**:到集群的命令唯一出口是 `SSHPool.run()`,红线与命令闸挡在这一层;写操作走人工审批与 oplog
- **环境包**:集群清单、拓扑、playbook、告警与资源映射、Python 扩展都在环境包里,核心代码不写任何环境名
- **插件(MCP)**:Prometheus、MySQL、集群管理面等数据源经 MCP 服务接入,只读工具交给 Agent,写操作走两阶段审批

## 结构

| 目录 | 内容 |
|---|---|
| `lib/` | 核心:SSH 池与命令红线、playbook runner、排查记录、Agent 运行时与 Harness、知识库检索、告警与资源采集、插件客户端、资源清单、环境包加载 |
| `web/` | 中台:`server/` FastAPI 后端,`frontend/` React + Vite 前端 |
| `scripts/` | CLI:中台入口、排查与采证、知识库构建与评估、打包发版 |
| `playbooks/_lib/` | playbook 公共函数(具体 playbook 在环境包) |
| `skills/` | Agent 技能说明(排查、深挖、知识写入、工作日志、周报) |
| `envs/example/` | 虚构的示例环境包(核心测试与开箱体验用) |
| `desktop/`、`packaging/` | Windows 外壳(托盘、签名更新器)与 Linux 打包 |
| `tests/` | 核心测试(挂示例环境包) |

> Python + Node 双栈:后端与工具链纯 Python,只有 `web/frontend/` 需要 Node。

## 快速开始(示例环境)

```bash
git clone <本仓> ops-agent && cd ops-agent
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cd web/frontend && npm ci && npm run build && cd ../..
.venv/bin/python -m pytest                     # 核心测试,挂示例环境包
.venv/bin/python scripts/web_server.py         # 中台,默认 http://127.0.0.1:8420
```

没挂载环境数据时中台用程序自带的示例包 `envs/example`,顶栏提示「当前为示例环境」。示例环境的集群、地址与插件都是虚构/本机占位,
用来看界面与流程;接真实环境请按下文自建环境包。模型在「设置 → 模型」里配(写入数据根下 `config/model.local.yaml`)。

## 环境包

环境包是一个目录,装一个环境特有的全部内容,通常放在私有仓里:

```
env.yaml          环境 ID、时区、集群(短键、显示名、角色、清单来源)、知识库词表、官方插件、扩展点
config/           主机清单、拓扑快照、告警源/画像/去重、资源指标映射、诊断登记、日志目录、周报配置…
playbooks/        节点上执行的只读排查脚本
policy/redlines.yaml   命令红线补充(只许收紧)
lint/nouns.yaml   本环境专有名词词表(核心仓扫描用)
agent/facts.md    给 Agent 的环境事实
python/           Python 扩展
skills/           技能文件覆盖(如周报模板与风格)
tests/            扩展与采集器测试
```

环境包是应用数据,不进代码仓、不进发布包。中台按序定位:`<数据根>/config/paths.local.yaml` 的 `env_pack`(绝对路径,
或相对数据根)→ `<数据根>/env/` → 程序自带的示例包;程序只读环境目录。集群角色决定参与哪些功能:
`managed`(平台诊断、告警归属、资源采集)、`knowledge`(知识库分目录)、`mysql_direct`(只经 MySQL 插件的集群)。
资源清单(`lib/inventory`)按环境包声明在内存生成「环境 → 集群 → 主机 → 服务 → 组件 → 实例」,资源 ID 形如
`example/alpha/host/node1`,不同集群同名节点不会撞。

**Python 扩展点**(`env.yaml` 的 `extensions`,`模块:对象`,经 `lib/envpack/ext.py` 使用核心 API):

| 扩展点 | 作用 | 示例包里的演示 |
|---|---|---|
| `node_credentials` | 节点凭据提供方(如堡垒机签发的临时口令) | — |
| `credential_profiles` | 账号页凭据项(声明式表单、校验与登录测试) | `example_ext.profiles` |
| `alert_sources` | 按集群的告警源(源 id `类型:集群`) | `example_ext.demo_alerts` |
| `graphs` | 业务图谱采集器(知识页视图、分组库组装、任务依赖) | `example_ext.demo_graph` |

环境包不得出现在核心仓:`scripts/noun_lint.py` 用环境包词表与内置通用模式(私网地址、邮箱、凭据形态)扫描核心仓入库文件,
pre-commit 只扫暂存内容,CI 全量扫。

## 插件(MCP)

插件是 MCP 服务:官方插件由环境包 `plugins:` 声明,自定义插件在「设置 → 插件」添加。只读工具经插件页启用后交给 Agent,
调用结果脱敏落档并可作证据;写操作只能经环境包声明的两阶段写(计划 → 人工审批 → Harness 执行 → 跟踪),
模型只看得到计划工具。详见下文「插件(MCP 服务)」。

## 数据根与外部仓

运行时数据落在「数据根」:`env/`(环境数据)、`state/`、`records/`、`build/`、`knowledge/` 与 `config/` 下的本机私有配置。
换机时要搬哪些、怎么搬见 [docs/migration.md](docs/migration.md)。
设环境变量 `OPS_AGENT_DATA_ROOT` 后全部读写该目录,代码目录只读;不设时数据根就是仓库根。

知识库与记录各是独立的 git 仓,经 `paths.local.yaml` 的 `knowledge_root` / `records_root` 定位(缺省数据根下):

```bash
cp config/paths.example.yaml config/paths.local.yaml   # knowledge_root / records_root / env_pack
bash scripts/install_hooks.sh                          # 装好本仓、知识库/记录仓与环境包仓的 pre-commit
.venv/bin/python scripts/build_wiki.py                 # 生成 routing、wiki、拓扑与检索快照
```

知识库根下每个带 `kb.yaml` 的子目录是一个库(`platform_service` 布局按 `<集群>/<服务>/` 分目录,flat 布局可按
`task_type` 分组)。文档按运维视角分 5 类:故障处理、操作手册、集群信息、参考知识、操作记录。

## Windows 外壳与 Linux CLI

- **Windows**:首装包解压到可写目录,双击 `OpsAgent.exe`;托盘启停中台,`app\<版本>\` 只读、`data\` 即数据根。
  更新只认 ed25519 签名的 manifest,新版不健康 180s 内自动回退并还原 state
- **Linux**:解开 tarball 后用 `bin/opsagent serve|start|stop|status|upgrade` 管理(自带 Python,glibc ≥ 2.17)
- **发版**:`scripts/build_package.py` 产出全部产物(只装程序与示例环境包,所有环境通用),`scripts/release.py`
  写 manifest、本机私钥签名并发到发布仓

## 常用命令

- `.venv/bin/python scripts/web_server.py [--port 8420] [--state-dir state]` — 起中台(常驻,单实例);内嵌 scheduler 调度告警/资源 tick、每日维护与图谱周更,模型配置存在时守护 triage worker
- `.venv/bin/python scripts/opsagent.py serve|start|stop|restart|status|prepare|kb|run|version|upgrade` — 应用形态统一入口(Linux 安装后即 `bin/opsagent`)
- `.venv/bin/python -m pytest` — 测试(核心挂示例环境包;拆仓前也跑 `envs/` 下环境包自带的测试)
- `cd web/frontend && npm ci && npm run build && npm test` — 构建并测试前端(部署前必做,`dist/` 不入库)
- `.venv/bin/python scripts/noun_lint.py [--details] [--json] [--check] [--staged]` — 专有名词与敏感信息扫描(`--check` 有命中退出 1)
- `.venv/bin/python -m lib.kb` — 知识库 lint;`-m lib.kb promote <doc> --reason "<理由>"` 人工晋升 draft→verified
- `.venv/bin/python -m lib.record lint <记录根>/troubleshoot` — 排查记录 lint
- `.venv/bin/python scripts/build_wiki.py` — 重建 routing、wiki、拓扑与召回/检索/向量索引
- `.venv/bin/python scripts/build_kb_vectors.py [--full]` — 检索段向量化(内置向量库或 Milvus)
- `.venv/bin/python scripts/retrieve_context.py --platform <集群> --profile investigation "<问题>"` — 从检索快照生成 ContextPack
- `.venv/bin/python scripts/eval_kb_recall.py [--corpus <语料>]` — 召回评估(含线上 Top 5 与 ContextPack 必读清单;调排序须同看 dev 与 holdout)
- `.venv/bin/python scripts/group_docs.py -p <pid> -t <tid> [--task-type <类型>]` — 分组库任务固定必读(通用 → 类型通用 → 特化)
- `.venv/bin/python scripts/graph_deps.py -p <pid> [-t <tid>]` — 图谱任务读写的表、同表上下游与流程依赖
- `.venv/bin/python scripts/collect_graph.py --graph <id> --mode full|incremental` — 采集一张环境包图谱
- `.venv/bin/python scripts/kb_import.py --source <文档> --manifest <JSON> [--target <文档ID>]` — 手动导入或更新知识文档
- `.venv/bin/python scripts/migrate_kb_v1.py --knowledge-root <根> [--apply]` — 知识库 1.0 格式迁移(缺省 dry-run)
- `.venv/bin/python scripts/migrate_records_v2.py --records-root <记录根>/troubleshoot` — 历史排查记录补 schema 并重渲染 report
- `.venv/bin/python scripts/triage_match.py --alert "<告警名>" [--platform <集群>]` — 告警/症状路由匹配
- `.venv/bin/python scripts/ts_session.py start --node <node> --trigger "<触发>" --platform <集群>` — 建排查 session;`ts_playbook.py` / `ts_exec.py` 采证,`ts_conclude.py` 收口,`ts_backfill.py` 事后补录
- `.venv/bin/python scripts/kquery.py --platform <集群> --nodes n1,n2 --cmd "<命令>"` — 多节点只读取数
- `.venv/bin/python scripts/patrol_runner.py --playbook <path> --run-id auto` — 单次巡检(仅人工)
- `.venv/bin/python scripts/alert_poll.py --dry-run` / `scripts/resource_collect.py --dry-run` — 告警/资源 tick 预演(常态由中台调度)
- `.venv/bin/python scripts/rebuild_timeline.py` — 从 `records/alerts/*.jsonl` 重建告警时间线
- `.venv/bin/python scripts/triage_worker.py --once` — 领取并执行至多一个排查任务(常驻由中台守护)
- `.venv/bin/python scripts/node_credentials.py` — 节点凭据初始化(经环境包提供方,只补缺失缓存)
- `.venv/bin/python scripts/inventory.py show [--cluster <集群>] | resolve <别名> | export` — 资源清单与别名归一
- `.venv/bin/python scripts/worklog_add.py --title ... --status DONE --category OPS [--work-item wi-…]` — 补录工作日志
- `.venv/bin/python scripts/work_items.py list [--date YYYY-MM-DD]` — 列出未关闭工作项(只读)
- `.venv/bin/python scripts/eval_agent_replay.py run --cases smoke|all [--baseline <report.json>]` — Agent 回放评估(真模型)
- `.venv/bin/python scripts/runtime_replay.py <transcript.jsonl>... [--mode whitelist|auto|bypass]` — 运行时零成本回放
- `.venv/bin/python scripts/command_guard_replay.py <transcript.jsonl>... [--details]` — 命令闸离线回放
- `.venv/bin/python scripts/run_a3_eval.py --case <id> --live` — 评估实跑(连生产只读并耗额度)
- `.venv/bin/python scripts/model_probe.py --config config/model.local.yaml` — 验证模型原生 tool call 回传
- `.venv/bin/python scripts/build_package.py [--target windows|linux] [--version <预发布号>]` — 构建发布产物
- `.venv/bin/python scripts/release.py --repo <发布仓> [--version <预发布号> --notes ...] [--dry-run]` — 签名发版
- `cd desktop && go test ./... && GOOS=windows go vet ./...` — 外壳单测;`packaging/windows/e2e/remote.sh` 实机验收
- `bash scripts/install_hooks.sh` — 安装本仓(专有名词扫描 + 红线锁)、知识库/记录仓与环境包仓(独立 git 仓时跑包内测试)的 pre-commit
- `.venv/bin/python tests/smoke_ssh.py --node <node>` — SSH 冒烟(手动)

## 功能说明

### Agent-Ops 任务链路

所有任务由同一个 Agent 运行时(`lib/agent_runtime/`)执行:模型自己决定先查知识库还是先上节点、查几台、
何时收口;Harness 只守能力边界、操作安全、审批、预算、状态恢复与审计。任务类型只决定开场材料和一段
提示片段,不决定能用哪些工具:

| 类型 | 开场材料(Harness 预先准备,零 LLM) |
|---|---|
| `qa` | 本地资源轮询快照(参考,不是实时证据) |
| `investigation` | ContextPack 引用清单;资源风险任务另带 RiskSeed |
| `alert` | 精确路由命中的登记 playbook 由 Harness 先跑(每个最多一次,遇成立即停),结果放进开场 |
| `artifact` | 本任务执行报告与引用清单 |
| `default` | 无 |

```text
输入/告警
  → 程序预判 + 同模型无工具分类/标题/实体提取(冲突取程序判断,程序判 default 时取模型)
  → 缺平台/节点/服务时先澄清
  → 稳定前缀(基础 + 安全规则 + 类型片段 + 环境事实) + 开场材料
  → 循环:模型 → 工具批次 → 结果(只追加,前缀缓存友好)
  → finish:回答 + 猜测三态 + 证据位置,Harness 从 records 摘录原文核验
  → 落报告;人工任务停「可以输入」,用户点「完成任务」才 completed
```

工具:`shell`(裸 shell,可多节点,先过命令闸)、`diagnostic_catalog`/`diagnostic`(登记诊断捷径)、
`kb_search`/`kb_grep`/`kb_read`、`read_task_evidence`、`read_resource_snapshot`/`read_resource_risk`、
`read_task_dependencies`、`lookup_alert_route`、`set_task_context`、`create_task_artifact` 与 `finish`;
右侧「生成知识草稿」那一轮只开放 `create_kb_draft`。

`finish` 只给证据位置(`ref` + 该步输出里的特征片段 `match`),原文由 Harness 从 records 摘录。每个猜测
三态核验、只降不升、不驳回:成立需故障窗口内的异常观测 + 判据(阈值/配置/预期值),推翻需两个不同命令的
反证且至少一条在窗口内,其余为缺少证据;观测时间取摘录行里的时间戳,没有时取该步骤的执行时间。人工任务
的结论没有服务器事实依据时挂报告闸,由发起人放行/打回/终止。

软预算(轮次 60 / 40 分钟 / 节点命令 150)到顶时人工任务先问续期;硬上限(轮次 80 / 60 分钟 / 节点命令
200 / 输入 600 万 token)或拒绝续期即进入收口回合,只开放 `finish`,仍不收口则用已采步骤写未收口报告。
自动任务没有人工通道:追问转收口回合,写操作与凭据审批直接驳回,收口即落 worklog。

用户触发报告、知识库、worklog 或 artifact 不会重新分类。任务列表和对话头部优先显示模型生成的
`display_title`,原始首条消息始终保留在任务详情中。Composer 在发送后立即清空消息,提交中和 Agent
`queued/running` 期间统一显示「Agent运行中」并禁用输入,`waiting_input` 显示「可以输入」。工具活动按
assistant tool-call 批次默认折叠,点击批次后查看调用,再点击具体调用查看命令、参数、输出和阶段耗时;
`finish` 投影为收口核验卡,展示猜测三态、降级原因与摘录原文。

A3.7 之前由旧编排器(五类 Agent)执行的任务只读:可以查看,不能追加消息、重试或改运行时控制项。
后续路线见 [ROADMAP](ROADMAP.md)。

### 状态获取层

Web 中台启动后由同进程内的跨平台 scheduler 分别维护告警、资源和每日维护任务。告警与资源
默认每 300 秒运行一次，彼此失败互不阻塞且单任务不重入；宿主不再注册 cron、launchd 或
Windows schtasks 周期任务，但仍须保证 Web 进程常驻。首页顶部按「数据源 / 告警 / 资源」显示
健康数、总数与红黄绿圆点，点击告警或资源状态可查看详细数据源/平台运行情况。

直连 HTTP 的告警巡检(环境包扩展源)使用进程级、按 origin 隔离的持久 HTTP 连接池；请求不读取
环境代理，新建连接 3 秒超时并仅对 GET 建连失败重试 2 次，响应读取仍保留动作自己的 30 秒
上限。管理面 API 应直连具体节点,避免把共享 VIP 当真源(具体地址由环境包登记)。

资源采集经中央 Prometheus 插件(MCP)的资源快照、采集目标与具名查询取数，不走 SSH、不调用 LLM，
也不修改生产监控规则。插件按新标签(platform / system / component / node)给数，
`config/resource-metrics.yaml` 把它映射到中台资源模型：平台名取 platform 标签(如各托管集群与 MySQL、
Edge,排除 infra),登记过的 IP 归一到节点名；JVM 与服务目录按 system/component 映射到中台服务与
管理面组件名；业务侧采集 MySQL 主从延迟等具名查询。插件没给的字段(如 Swap 总量、inode、老年代
GC 后占用、MySQL 连接使用率等)保留空值与明确采集状态，ops-mcp 补上具名查询后只改映射即生效。

快照按环境时区每天写入 `records/telemetry/resources/YYYY-MM-DD.jsonl`，当天文件仅追加；
跨日维护校验后原子封存为 `.jsonl.gz`。告警历史与资源快照默认各保留 90 天，可在
「设置→采集与存储」分别调整启停、周期、路径和保留期；相对路径以仓库根解析，页面同时显示
解析后的绝对路径、可写状态、文件数和当前占用空间。修改路径不会迁移历史数据。

「资源状态」分概览、节点资源、服务内存、业务指标四个子页，均支持筛选和排序。点击节点、服务
或业务指标可按 24 小时/7 天/30 天打开快照图表；风险项可反向定位具体资源。风险分析覆盖 CPU、
可用内存、磁盘容量趋势、JVM GC 后 Old Gen 增长与 ETA，以及达到阈值的业务指标；采集状态与
指标风险级别分列。异常持续时间只累计连续有效样本，超过 15 分钟的采集缺口不计入持续时间。

资源告警阈值的默认值来自 `config/resource-metrics.yaml`，可在「设置→资源→资源告警」按 CPU、内存、
磁盘、JVM 和业务指标填写手动覆盖；留空即跟随默认，保存后下一轮本地风险分析生效。JVM 高位风险
按最近 3 个采集点的中位数判定，历史窗口曾经偏高但近期已低于阈值不会继续生成当前告警。

告警详情的「事件时间线」以生命周期区间为唯一数据源，每行显示一次告警周期、活跃/已恢复状态和
持续时长；不再重复展示独立的持续时间列表。时间线图表只读详情接口返回的完整事件流，9-3、9-4
的历史告警风暴记录已从源 JSONL 和可重建时间线中清理，当前活跃快照不受影响。

资源风险通知默认关闭，可在「设置→资源→采集与存储」显式开启。每条风险以分析器生成的稳定 `finding key` 作为连续异常段身份：同一 key 持续存在时只通知一次，风险消失后再次出现才重新通知；无可用渠道时不会误记为已通知，发送失败继续进入对应渠道的 24 小时补发队列。设置页显示当前异常、已通知和待通知数量。

通知模板保存在 `state/notify-settings.json`，通过「设置→通知」编辑。模板使用单花括号变量，例如 `{title}`、`{platform}`、`{value}`、`{description}`；未知变量、属性访问和格式化修饰会被拒绝。支持标题、加粗、引用、列表、代码等标准 Markdown。普通换行可能在段落中合并为空格，需要行尾两个空格形成强制换行；空行会开始新段落。预览使用示例资源异常数据实时渲染，企微保留 Markdown，飞书降级为纯文本，原始 HTML（如 `<span style="color:red">`）不作为通用模板语法解析。

### 知识库阅读器

中台顶部「知识库」是知识文档的阅读入口：左栏按平台/服务浏览，支持全文搜索；正文里的
`[[wikilink]]` 可点击跳转，`[[report:<run_id>|查看完整排查报告]]` 可打开对应执行报告，目标不存在的灰显；文档末尾列出谁引用过它——路由到它的告警、
引用过它的排查任务与收口结论、以及文档自述的历史案例。反向引用对中台任务是完整的，
对命令行排查只覆盖留下 `conclusion.json` 的那部分，页面会如实标注覆盖比例。

`/knowledge` 默认进入文档视图，Tab 顺序为文档、拓扑,其后是环境包声明的图谱(如作业调度)。有多个库时左栏顶部可切换库，首页为当前库的根索引，打开文档会自动切到其所属库；文档树支持按排查、知识、流程筛选；平台、服务和组件只由标题前箭头控制折叠，服务名称打开对应概览，knowledge 文档显示书本图标。正文、树和搜索统一使用“平台 - 服务/组件 - 问题描述”展示标题，健康度面板的文档链接会自动定位到正文状态。

文档树、搜索结果与正文统一显示人类标题；旧文档缺少合格 frontmatter title 时兼容读取正文
H1 或按平台/服务/problem 生成保底标题。健康度面板中的晋升候选不是“被打开过就算验证”：
只有后续独立排查得到 conclusive 报告、历史案例已回链、平台/服务一致且报告与正文关键内容
呼应时才具备自动晋升资格；中台排查写入时使用同一套代码策略自动裁决。

服务树与正文直读知识库源文件，改完刷新即见；只有全文搜索走 `build/kb-recall.sqlite`，
所以索引比源文件旧时页面会提示"新写入的内容暂时搜不到"，跑一次 `build_wiki.py` 即可。

统一 rebuild 还会同步人类可读索引：根 `_index.md` 生成全平台服务/告警路由，每个有文档的
服务和组件都会拥有 `_index.md`。系统只替换 `GENERATED:KB-INDEX` 标记内的目录表，概述、
架构、端口和依赖等人工章节原样保留；新增或变更的 index 必须与业务文档一起暂存提交。
服务概览的「组件分布」（节点与端口）和「服务依赖」（上下游、关系、依据）也在生成区内，由入库的
环境包登记的主机清单、拓扑快照与 `config/topology/manual-dependencies.yaml` 渲染；
人工章节不写节点、端口与依赖（lint 会拦），采集不到的依赖登记到 manual-dependencies，时点指标放「历史快照」章节。
排查类检索会沿服务依赖向上游扩展（如 HBase → HDFS / ZooKeeper），上游的故障文档只在与问题内容相关时加分。

顶部可展开「知识库健康度」：草稿/已验证分布、各平台覆盖与告警绑定数、因缺少 `symptoms`
与 `alerts` 而检索召不回的文档清单，以及每篇草稿的自动晋升资格和阻塞原因。浏览 API
保持只读；写入只发生在中台排查内部审批链和本地 `kb_import.py`，人工晋升必须填写理由。

### Agent-Ops 核心

模型本地配置从 `config/model.local.yaml` 读取，也可在中台「设置→模型配置」管理多个模型、
告警自动排查/手动排查/周报生成默认值、各工作流独立超时、auto 档可选审核模型与思考强度，示例见
`config/model.example.yaml`；真实 API key 已由 gitignore 排除且不会由设置 API 回传。普通模型请求默认超时
为 120 秒，周报各阶段默认超时为 300 秒；具体阶段、耗时与异常写入对应中台/worker 日志，提示词、工具参数和密钥不落日志。

节点动作有两条路。一条是 `config/agent-diagnostics.yaml` 中登记的诊断:参数或是封闭枚举,或是受长度、
字符集、正则三重约束并强制转义的开放型。另一条是裸 shell:先过 `lib/command_guard.py`(红线 → 结构 →
逐段 → 凭据 → 未登记,多条结论取最严),整文件读取前还会探测大小,≥1GiB 拒绝并给出截取建议。两条路
渲染出的命令最终都经过 `SSHPool.run()` 的 `cmd_filter`。

模型可见工具清单在 `lib/agent_runtime/tools.py`,只读接口 `GET /api/triage-tools` 返回同一份清单(含当前启用的插件工具)。

### 插件(MCP 服务)

外部系统的只读查询以插件接入,一个插件就是一个 MCP 服务(Streamable HTTP)。「官方」插件由环境包
`env.yaml` 的 `plugins:` 声明(如集群管理面、Prometheus、MySQL 的 MCP 服务),「自定义」插件在「设置 → 插件」页添加任意
MCP 地址与认证方式(无 / Basic / Bearer / 自定义请求头 / 连接列表)。有免登录 `/manifest` 的按它展示读写标记,
没有的用 `tools/list` 的工具注解兜底(未标 `readOnlyHint` 一律按写)。

- 插件页:健康状态(`/healthz` 或一次 MCP 握手)、版本、工具清单与读写标记;连接测试会刷新工具清单
- 凭据只在插件页填,保存在本机 `auth-profiles.json`,接口只回「是否已配置」与打码用户名
- 只读工具按「插件启用 + 工具启用」交给 Agent,模型可见名 `mcp__<插件id>__<工具>`;写工具不交给模型(写操作见下)。
  第三方插件的工具默认停用,描述清洗限长,内容变化后自动停用待复核
- 任务首次执行时冻结插件工具,任务内不变;插件页的改动只影响之后新建的任务
- 每次调用结果逐值脱敏后记为排查报告的一个步骤(`[mcp:<插件id>] ...`),可被收口引用为证据;
  插件连不上或认证失败时返回显式错误,任务开场也会写明哪些插件当前不可用
- 中台不自带管理面配置 / 告警诊断,这些查询由对应插件提供
- MySQL 一律经 MySQL 插件:插件页逐条添加连接(地址 / 端口 / 账号 / 密码 / 库 / 所属集群),
  密码只写不回显,编辑时账号密码留空沿用原值;连接测试逐条试连并显示版本、角色与缺少的权限。中台不再直连 MySQL,
  Agent 在节点上跑 mysql 客户端会被命令闸拒绝。两个拓扑采集查询默认不给 Agent,只供后台采集

**写操作(计划 → 人工审批 → Harness 执行)**:官方插件可在环境包 `write:` 声明两阶段写的工具配对
(如集群管理面的组件启动 / 停止 / 重启)。只有计划工具会交给模型,且要在插件页手动启用并先填审批密钥(与插件服务
目录的 `approval.secret` 同一串)、只对人工任务开放:

- 模型调用计划工具 = 提出写操作:中台先做只读预检,对话里出现审批卡片(目标、预检时状态、会生效的配置、
  下游服务、警告、计划有效期),可批准 / 打回 / 拒绝,审批人记入审计
- 批准后由中台签发一次性审批令牌并调用插件执行;令牌与密钥不交给模型、不落记录。计划过期会重新预检出新卡片
- 已下发的操作由中台跟踪到结束(任务显示「Agent运行中」,期间不可取消),结论(成功 / 部分成功 / 失败 /
  未复核 / 未知)写进排查报告的「写操作」一节,并自动生成 oplog 草稿;中断后重启会先对账,不会重复下发
- 自动任务没有人工通道,看不到计划工具

本机地址覆盖与开关存 `<数据根>/config/plugins.local.yaml`,拉到的工具清单缓存在 `state/plugin-catalog.json`。
每次调用的完整审计结果统一为版本化 `ToolResult`,模型历史只拿有界 projection(单次 64KB,超出保留头尾)。

命令档位由 `state/command-policy.json` 的 `mode` 决定,只作用于命令闸判为「未登记」的命令段:

| 档位 | 未登记的命令 |
|---|---|
| `whitelist`(全局默认档) | 挂人工审批 |
| `auto` | 交审核模型看意图,判安全即执行,否则转人工 |
| `bypass` | 直接执行 |

全局默认档在中台设置页可改。单个排查任务还能临时升档(对话框下方的档位选择器),
只对该任务生效、下一轮对话开始生效;覆盖的是档位不是名单,永久白/黑名单始终取全局。

有两类命令三档都绕不过。一类由命令闸直接拒绝并给出改写提示:命令替换 `$(…)`/反引号、`sudo`、
解释器、`xargs`、`bash -c`、`shutdown`、全量重查询等。另一类是删除、服务启停与读取凭据文件
(`/etc/shadow`、私钥、keytab),一律挂人工审核,**`bypass` 也不例外**;批准分四档:放行一次 /
本次排查放行 / 永久加白名单 / 永久拉黑。`bypass` 放行未登记命令只靠信任:命令闸按明文判定,
拼接混淆拦不住,这是 `bypass` 的固有属性,不是待修的缺口。

`auto` 审核模型只处理静态规则无法直接裁决、落入 `review` 的命令；红线 `deny` 或静态
`ask` 不会送模型。审核模型明确返回 `safe=true` 才自动执行，返回不安全、未配置、超时或
解析失败都会转人工审核；自动任务没有人工通道时则驳回。纯计算、解析、打印的解释器命令
（例如 `python3 -c 'print(1)'`）可自动放行；嵌套写文件、删除/修改、启动子进程或联网的解释器
命令不会自动放行。

诊断分两种执行形态：`ssh` 类在节点上跑命令（走上述红线闸）；`http` 类是只读 REST 查询，
由中台本地直连不经节点，URL 只能由 `config/platform-*.yaml` 已登记的 endpoint 与集群名拼装，
凭证在发起请求时才注入、不进命令、不进记录。可列目录的根白名单在 `config/fs-roots.yaml`
人工维护，与采集器产出的 `config/log-catalog.yaml` 取并集。

中台顶部「Agent-Ops」进入三栏工作台：左侧切换人工/自动任务，中间查看持续对话与操作记录，
右侧查看任务上下文、执行报告、任务产物、知识草稿和 worklog。左右侧栏可独立收起/展开，任务更多菜单支持重命名、指定、标记未读和归档。新建人工任务只要求任务需求，
平台和节点可留空；平台目标支持环境包登记的托管集群、MySQL 集群与零散节点，节点输入支持登记名、IP、别名和 FQDN 自动对应；Agent 自行判断是直接回答、服务器调查、故障排查还是生成文档/脚本，并且
每轮都必须在对话里给出结果，不能只落文件。用户回复会把任务从 `waiting_input` 原子恢复为
`queued`；自动任务只读。

新版人工任务不再由模型自动完成：Agent 回复后回到等待用户，只有空闲态可点击「完成任务」；
完成后仍可继续对话，发送新消息会自动重新排队。只要执行 SSH、诊断、playbook 或登记的集群
HTTP 查询，就会自动创建并持续更新执行报告；纯对话和本地产物不生成报告。知识草稿只有存在
执行报告时才可由人手动发起。任务可生成 `.md/.sh/.py` 到
`records/troubleshoot/<run_id>/artifacts/`，同名自动追加版本号，脚本只供审阅且不自动执行。
新建任务会应用设置默认值，也可当次覆盖模型和思考强度；失败任务允许补充信息或重试。新建或追加消息提交时，
对话会先显示用户气泡，主状态归并为「Agent运行中」/「可以输入」，收到正式事件后自动去重；失败提示右侧提供回退样式的重试按钮，
刷新当前任务则使用独立的刷新图标。对话、设置长页和资源页均提供统一的回到顶部入口。
对话框使用 `Enter` 发送、`Shift+Enter` 换行。

worker 中断或重启后，恢复路径会为同批次里“执行中”和“尚未开始”的全部工具调用补齐中断
结果，并在发给模型前按原 assistant 批次重排 tool result；因此重试不会再因缺少 tool output
陷入 HTTP 400。Agent 阶段总结写入执行报告前与命令输出一样经过 `redact_secrets` 脱敏。

报告、命令、调用参数与执行结果详情都在弹窗内容区内自动换行；长文本不应产生底部横向滚动条。

人工描述通常没有精确告警名。Agent 用 `kb_search` 在本地 SQLite FTS5 召回索引里检索,再用 `kb_read`
读取文档全文、`kb_grep` 做正则全文搜索。Markdown 仍是唯一知识真源;索引缺失、损坏或版本不兼容时
不会在查询链路偷建或退回无排序列表,运行 `.venv/bin/python scripts/build_wiki.py` 即可重建。

`GET /api/triage-health` 返回模型、worker 和队列状态。只有模型已配置且 worker 为 `running`
时允许新建；worker 停止时未完成任务明确显示等待，不伪装为运行中。Web 只通过
`TriageTaskService` 写 `state/triage-tasks/`，节点动作仍由独立 worker 发起，
最终唯一经过 `SSHPool`。`alert_poll` 自动任务入队仍是后续独立计划，不随本功能开启。

### 工作项与计划

工作项是「一件要做的事」，工作日志是「发生了什么」。中台顶部「工作项」页：

- **登记**：顶部一行快速登记（标题 + 截止）即建，适合每周临时的运维或开发任务；也可新建时填描述、优先级、开始/截止和所属计划节点。
- **计划**：把阶段性计划表（如下半年运维计划）从在线表格整块复制，用「导入计划」粘贴：表头至少要有「具体事项」，
  有「项目节点」列即导入为计划（合并单元格留空的节点沿用上一行），节点写「产物验证」的行作为该节点的验收项；
  没有节点列就逐行建成单条事项。再次粘贴同一计划只更新开始/截止，不覆盖中台里已改的进度、不删项。
  计划页按节点分组显示时间条、今天线、完成数与逾期。
- **挂载**：工作项可挂 Agent-Ops 任务、命令行排查记录（run ID）与工作日志条目。任务的报告、产物、审批与写操作随之可见；
  任务产生的工作日志按来源自动归到任务所属工作项。从工作项页「发起 Agent-Ops 任务」建成即挂上；任务右侧也可挂到已有或新建。
  告警详情「建工作项」，同一告警已有未关闭工作项时直接打开它。Agent 不建、不改工作项。
- **存储**：每项一个 JSON 落 `records/work-items/`（计划在 `plans/`），变更追加历史，只取消不删除。

### 工作日志与周报

中台顶部「工作日志」按环境时区的周周期逐日展示 `records/worklog/YYYY-MM-DD.md`。周期默认值在环境包
`config/weekly-report.yaml`（缺省周四至下周三），「设置 → 周报」改的值存本机 `config/weekly-report.local.yaml`；
页面可向前翻周，但不能越过当前周期。
手动补录要求日期、状态、类型和标题，产出、关联、过程均可留空，可选挂到工作项。每条日志右侧显示所属工作项，
未挂的可直接挂上；「匹配建议」让周报默认模型为本期未挂载的日志推荐工作项，勾选确认后才挂。点击日志主体可查看完整字段；
自动排查日志还会按来源加载关联的 `records/troubleshoot/<run_id>/report.md`，因此日报中的摘要
即使带省略号也能在弹窗查看完整根因、证据、命令和处置建议。

新版 Agent-Ops worklog 只由右侧按钮触发：任务模型根据上次确认游标之后的对话、操作、报告与
产物一次生成 1 至 3 条完整预览，标题不再复制原始任务长句；用户可填写意见重新生成，确认后
只追加不回改历史。模型请求失败时反馈和事件截止游标会保留，重试不会重新汇总旧历史。任务后续继续工作时按钮变为「更新 worklog」，仅概括新增内容。

工作日志、排查记录与新建任务都带作者:在「设置 → 本机用户」填用户 ID 与显示名后,新记录以此为作者
(Agent 任务的记录以任务创建人为作者);设置前写入的旧记录读取时按本机用户归属,不回写历史。
记录读写统一经数据层 `lib/datastore/`,一阶段只有本地实现,二阶段在其上接公网共享服务。

每条日志右侧可切换「在周报中隐藏/恢复到周报」，该状态以 `- **周报**: 忽略` 元数据落在
对应条目内。周报生成会先排除这些条目，再调用模型设置里的 weekly 默认模型，按
`skills/weekly-report/SKILL.md`(环境包同名文件优先)依次执行事实聚合、初稿、规则审查和二次改写：完整 worklog、
关联排查报告及上一期待办均带来源 ID，模型先发现跨日同主题关系并按业务线聚合，最终正文仍由
LLM 生成，代码只校验来源和写入固定模板 DOCX(文件名标题、模板、固定第 1 条与业务线取 `weekly-report.yaml` 的 `report` 段)。本期有工作项时，代码按状态与日期把工作项分成本周完成 / 进行中 / 下期排期
三类（附挂载的日志来源 ID）一并交给模型，并加载 `references/WORK_ITEMS.md` 规则：同一工作项的日志合并成一条、本周完成的必写、
下周待办先取日志与上期待办里写明的后续动作，不足时再用排期落在下期的事项补足；DOCX 仍是「本周工作总结 / 下周待办」两段。本期没有工作项时模型输入与以前完全一致。已生成周期可在中台预览与表格
一致的内容，也可在文件夹中定位已生成的 DOCX、重新生成或删除；侧栏展示最近 5 份，全部历史周报弹窗支持日期筛选。周报生成遵循 `weekly-report` skill 的 7-10 条总结规则；当有效日报较少时，仅基于已有日志过程、证据、结论和产出做事实性扩写，不虚构成果。
周报生成请求会立即返回后台任务 ID，页面按任务接口显示当前阶段和已完成阶段；切换或刷新页面不会中断任务，只要中台进程保持运行即可。

生产部署前仍须执行 `cd web/frontend && npm run build`，再重启默认 `8420` 端口的中台进程。

## 许可证

Apache-2.0,见 [LICENSE](LICENSE) 与 [NOTICE](NOTICE)。`desktop/internal/webview2/` 为第三方代码,保留其 MIT 许可。
