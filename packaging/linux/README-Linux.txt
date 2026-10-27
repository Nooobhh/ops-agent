ops-agent 中台 Linux 版(x86_64,glibc ≥ 2.17,自带 Python 运行时)

【安装】
  tar -xzf OpsAgent-<版本>-linux-x64.tar.gz -C ~      # 解出 ~/opsagent
  ~/opsagent/bin/opsagent start                        # 后台拉起,就绪后打印 JSON(含控制台地址)
  首次启动会建立知识索引,约十几秒。包内不带环境数据与知识库,接真实环境见下方【环境数据】。

【目录】
  bin/opsagent     命令入口(可软链进 PATH)
  app/<版本>/      程序本体,升级整体替换;app/current 指向当前版本
  data/            本机数据(env/knowledge/records/state/build/config),升级永不触碰;
                   可用环境变量 OPS_AGENT_DATA_ROOT 指到别处

【命令】
  opsagent serve [--host 0.0.0.0] [--port 8420]   前台运行(给 systemd 用,模板见 app/current/packaging/linux/opsagent.service)
  opsagent start | stop | restart | status        后台托管
  opsagent prepare                                 重建索引(start 会自动做)
  opsagent kb lint | rebuild                       知识库 lint / 重建派生物
  opsagent run <脚本> [参数]                       运行 scripts/<脚本>.py,如 opsagent run worklog_add --help
  opsagent version
  opsagent upgrade <新版 tarball>                  离线升级:先停中台;切换后拉起验证,3 分钟内不就绪自动切回

【环境数据】
  包内只带虚构的示例环境(控制台顶部提示「当前为示例环境」)。接真实环境:把环境包目录放到 data/env/,
  知识库放到 data/knowledge/(git clone 或从旧机器整目录拷贝),再 opsagent restart。升级不碰这两个目录。
  从旧机器搬配置与设置的完整清单:app/current/docs/migration.md。

【注意】
- 控制台缺省只听 127.0.0.1;多人访问用 serve --host 0.0.0.0 并自行做好网络隔离(一阶段无登录)。
- 中台日志 data/state/web_server.log。
