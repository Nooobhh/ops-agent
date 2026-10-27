# 迁移清单:把一台中台搬到新机器

> 适用:换机、从源码运行改为安装包、macOS 与 Windows 之间互迁。程序本身不用迁——新机装同版本(或更新版本)的发布包即可;
> 要搬的是数据目录里的应用数据。

## 1. 数据目录里有什么

数据根:安装形态是 `<安装根>/data/`;源码运行是仓库根,设了 `OPS_AGENT_DATA_ROOT` 则是它指向的目录。

| 位置 | 内容 | 迁移方式 |
|---|---|---|
| `env/` | 环境数据(环境包):集群清单、拓扑、告警与资源映射、playbook、周报模板、环境扩展 | 环境包若是 git 仓,新机 clone;否则整目录拷贝 |
| `knowledge/` | 知识库 | 同上(clone 或拷贝) |
| `records/` | 工作日志、排查记录、周报、工作项、告警与资源历史 | 同上;不要历史可不迁 |
| `config/` | 本机配置(见第 3 节) | 拷贝,或在新机设置页重填 |
| `state/` | 运行状态与界面设置(见第 4 节) | 只拷设置类文件,其余新机自行生成 |
| `build/` | 知识索引等派生物 | 不迁,新机首启自动重建 |

`config/paths.local.yaml` 可把环境包、知识库、记录指到数据根之外(`env_pack` / `knowledge_root` / `records_root`,
绝对路径或相对数据根);没有这个文件时就用上表的缺省位置。

## 2. 步骤

- [ ] 新机装好发布包,首次启动一次(生成数据目录骨架),再停掉中台
- [ ] 放环境数据:`data/env/`(git clone 环境包仓,或从旧机拷贝 `env_pack` 指向的目录)
- [ ] 放知识库:`data/knowledge/`;需要历史时放记录:`data/records/`
- [ ] 本机配置与设置:按第 3、4 节拷贝,或跳过、启动后在设置页重填
- [ ] 旧机的 `paths.local.yaml` 若写了绝对路径:新机改成新路径,或删掉改用缺省位置
- [ ] 启动中台,核对下一节
- [ ] 旧机停中台或在「采集与存储」关掉采集,免得两台重复推送告警

## 3. 本机配置(`config/`)

| 文件 | 内容 | 建议 |
|---|---|---|
| `model.local.yaml` | 模型 profile 与 API key | 拷贝,或在「设置 → 模型」重填 |
| `auth-profiles.json` | 账号页凭据、插件凭据、写操作审批密钥 | 含密钥,经可信渠道拷贝,或重填 |
| `plugins.local.yaml` | 插件地址覆盖、开关、自定义插件 | 拷贝 |
| `user.local.yaml` | 本机用户(记录作者) | 拷贝;多人各用一台时各自设置 |
| `vector.local.yaml` | 向量/重排设置 | 拷贝;用本地模型时连同模型目录一起搬,路径按新机改 |
| `weekly-report.local.yaml` | 设置页改过的周报周期 | 拷贝 |
| `credentials.json` | 节点凭据缓存 | 不拷,新机在「设置 → 账号」重新生成 |
| `paths.local.yaml` | 外部目录位置 | 见第 2 节 |
| `local-dev.yaml` | 源码开发用的跳板配置 | 安装形态不需要 |

## 4. 界面设置(`state/`)

只拷这些(都是设置页写的,缺了就是缺省值):`acquisition-settings.json`、`poll-enabled.json`、`notify-channels.json`、
`notify-settings.json`、`alert-source-notify.json`、`alert-rule-overrides.json`、`resource-alert-settings.json`、
`command-policy.json`。

可选:`triage-tasks/`(Agent-Ops 任务历史)。不拷:`*.lock`、日志、`alerts.json`、`timeline.db`(可由
`records/alerts/` 重建)、`graphs/`(图谱可在知识页刷新)及其余运行状态。

## 5. 核对

- [ ] 顶栏没有「当前为示例环境」提示;「设置 → 关于」显示的环境、路径与提交号是你搬过来的那份
- [ ] 知识库页能检索到文档;插件页各插件健康
- [ ] 告警轮询状态为运行中(若本机负责采集);通知渠道发一条测试消息
- [ ] 工作日志能看到历史(若迁了记录)
