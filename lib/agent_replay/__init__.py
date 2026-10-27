"""Agent 回放评估:用历史任务录制的环境结果重放排查,度量 harness + 模型行为是否退化。

只替换「观察环境」的 I/O(诊断/命令/playbook/资源风险/资源快照/任务依赖),
知识检索与 harness 内部工具照常现跑。设计见本地 docs/specs/2026-10-21-agent-replay-eval.md。
"""
