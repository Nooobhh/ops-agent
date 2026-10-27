# 示例环境包

虚构环境,供开箱体验与核心测试:两个托管 Hadoop 集群 `alpha` / `beta`(节点名都是 `node1~3`)、
独立数据库集群 `db`、零散节点 `misc`(旧短键 `shared`)、缓存集群 `cache` 与边缘节点 `edge`。
地址只用文档地址段,域名只用 `example.internal`,插件地址是本机占位。

数据根下没有 `env/`、也没配 `env_pack` 时中台挂本包,顶栏提示「当前为示例环境」。真实环境请参照本目录另建环境包,
放到 `<数据根>/env/`,或在 `<数据根>/config/paths.local.yaml` 写 `env_pack: <路径>`(绝对或相对数据根)。
