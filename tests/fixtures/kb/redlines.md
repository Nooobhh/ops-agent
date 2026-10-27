---
schema_version: 3
source: manual_import
source_ref: legacy-migration:shared/procedures/redlines.md
title: 命令红线(cmd_filter 人可读镜像)
type: procedure
platform: example
service: system
problem: redlines
summary: '到集群的所有命令必须过 SSHPool.run() 里的 CmdFilter。三级:'
knowledge_layer: manual_task
entrypoints:
- manual_task
profile: sop
execution_scope: human_only
confidence: historical
related: []
status: draft
source_sha256: 23546d882480e59345d02f6c1c281dbc352768968af05e8aedd514c8fac548c0
filter_rules_sha256: {filter_rules_sha256}
---
# 命令红线(cmd_filter 人可读镜像)

## 摘要

到集群的所有命令必须过 SSHPool.run() 里的 CmdFilter。三级:

## 适用场景

- 当前未提供可直接验证的独立机理说明；需要结合实际采证结果确认。

## 前置条件

- 前置条件：执行前必须完成只读盘点并获得人工变更批准。

## 步骤

### 变更流程

1. 修 `lib/data/filter_rules.yaml`(添/删/改规则)
2. 跑全量回归:`.venv/bin/python -m pytest tests/test_cmd_filter.py`
3. 用例全绿后同步本文档规则清单段
4. `sha256sum lib/data/filter_rules.yaml` → 更新 frontmatter
5. `.venv/bin/python -m pytest tests/test_kb_redlines.py` 校验一致

## 验证

- 验证：按前置条件和补充材料中的健康门禁逐项验收。

## 回滚

- 回滚：任一验收门禁失败时暂停后续步骤，按变更指挥人的回退路径执行。

## 补充材料

### 意图

到集群的所有命令必须过 `SSHPool.run()` 里的 `CmdFilter`。三级:

- **READONLY**:默认放行(free/vmstat/cat/ps/grep/awk 等诊断命令)
- **WRITE**:默认拒绝,人在环审批令牌可放行(rm/kill/systemctl restart/hdfs dfs -rm 等)
- **FORBIDDEN**:无条件拒绝(shutdown/reboot/mkfs/dd of=/dev/…/rm -rf / 等)

### 规则真源

机器消费的规则文件是 `lib/data/filter_rules.yaml`。**本文档是它的人可读镜像**——每次改
`filter_rules.yaml` 必须同步:

1. 更新本文档的规则说明段落
2. 重算 sha256 → 更新 frontmatter `filter_rules_sha256`
3. kb-lint 校验两者一致,不一致拒入库

### 三级规则清单(简版)

### FORBIDDEN(无审批通道)

- 关机 / 重启 / halt / poweroff / kexec / init 0|6
- `systemctl reboot/poweroff/halt/kexec/exit/emergency`
- 磁盘格式化 / 分区(mkfs.*/fdisk/parted)
- `dd of=/dev/…`(除 /dev/null)
- `rm -rf /` 及其等价形态(`/*` / `/.` / `.. ; rm -rf /`)
- Shell 内联:`bash/sh/zsh/ksh/dash -c`——让远端 shell 重新解析整串,逐 token 转义与结构化 argv 当场失效,静态判定全部落空;批了也看不清真实执行什么,故不给审批通道(段级命令词+旗标判定,参数字符串里出现 "sh -c" 不误伤)

### WRITE(要审批)

- 文件写:rm/mv/chown/chmod/touch/tee/truncate/shred/ln/mkdir(工作区外)
- 进程写:kill/pkill/killall
- 服务写:`systemctl/service <state-changing 动词>`(start/stop/restart/reload/enable 等);`supervisorctl <start|stop|restart|reload|shutdown|…>`;组件启停脚本 `<any>.sh <start|stop|restart|reload>`(hadoop-daemon.sh / zkServer.sh / kafka-server-*.sh 等,systemctl 之外的第二条服务生命周期通道)
- HDFS 写:`hdfs dfs -{rm,mv,put,cp,chmod,chown,mkdir,...}` 与 `hadoop fs` 别名
- YARN 写:`yarn app -kill`
- 内核写:`sysctl -w` / `sed -i` / 绝对路径重定向出 /tmp、/dev(目标带引号亦算)
- 只读命令的写旁路:`sort -o/--output <file>`(排序结果落盘)、`sort --compress-program`(执行任意二进制)
- 数据库写:`mysql/hive/beeline/impala-shell -e '<insert/update/delete/drop/alter/truncate>'`;`psql -c/-tAc '<同上 + create/grant/revoke>'`
- 间接执行:`eval`;`xargs` 转发写命令(rm/mv/kill/chmod/chown/tee/truncate/dd/shred/ln);命令替换 `$(…)` 与反引号——内容对过滤器是黑盒,一律落安全侧(单引号里的字面量已被中和,`grep '$(x)' f` 不误伤)
- crontab 写:`crontab -` 或 `crontab <file>`(`-l` 放行)
- 变更请求:`curl -X POST/PUT/DELETE` / `--data` / `-d`(带 `-G/--get` 时豁免:--data* 转 query string 是只读 GET)
- 查找即删:`find … -delete/-exec/-execdir`(`-exec` 接 ls/cat/grep/stat/md5sum 等只读白名单命令豁免)
- 数据写:`dd of=<非 /dev/null 目标>`

### READONLY(默认放行)

- 所有诊断命令(free/vmstat/cat/head/tail/ps/top/netstat/ss/iostat/pidstat/lsof/…)
- `systemctl status/is-active/show/cat/list-*` / `journalctl`
- `supervisorctl status`(状态查询不含变更动词)
- `crontab -l`(含 `-u <user> -l`)
- `curl` GET / `curl -s`(默认动词)
- `mkdir -p /tmp/opsagent/*`(agent 工作区豁免)

### 不由红线判级的:解释器

`python -c` / `perl|ruby|node -e` / `php -r` 不改变系统状态,却能做任何事,按「写/只读」
分不出去。它们改由**命令模式档位**准入(见 `lib/triage_agent/command_policy.py`):

| 档位 | 解释器 |
|---|---|
| default | 拒——窄正表只收结构上可静态验证只读的工具 |
| auto | 过审核模型;判安全即执行,判危险挂人工审核 |
| bypass | 直接执行 |

放开之后,红线只能拦明文可见的危险构造:`python -c "__import__('os').system('r'+'m -rf /')"`
这类拼接混淆,正则拦不住。auto 档靠审核模型看意图兜底,bypass 档靠信任——这是 bypass
的固有属性,不是可修的缺口。`bash -c` 例外,见 FORBIDDEN 段。
