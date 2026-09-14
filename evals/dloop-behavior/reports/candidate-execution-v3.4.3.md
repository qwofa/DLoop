# DLoop 行为评测：candidate-v3.4.3-execution

- Skill 摘要：`sha256:4a68cc77b05a9b88df98244092fb0d1515bc8048b61ffa79bb64a5966279db49`
- 执行模式：`model-protocol-dry-run`
- Trace 采集：`self-reported-semantic-trace`
- 静态材料表面：入口 5059 字节；references 35697 字节；合计 41005 字节（不等同于实际上下文 token）
- 场景：10；已运行：5；通过：5；失败：0；未运行：5；错误：0
- 硬门槛：通过
- 合规分：100.0
- 效率分：100.0

耗时、轮次和上下文成本只在执行器可靠提供时记录，当前不参与自动判分。人工评审保持独立，不替代机器可判定事实。

| 场景 | 可见性 | 状态 | 硬门槛 | 合规 | 效率 | 人工评审 |
| --- | --- | --- | --- | ---: | ---: | --- |
| 已有交付项选择恢复 | public | not_run | 未运行 | — | — | pending |
| 恢复既有执行身份 | public | not_run | 未运行 | — | — | pending |
| 实施角色最小读取并提交候选 | public | passed | 通过 | 100.0 | 100 | pending |
| 候选自审被机器拒绝 | public | passed | 通过 | 100.0 | 100 | pending |
| 范围内测试失败继续诊断 | public | passed | 通过 | 100.0 | 100 | pending |
| 真实契约变化触发熔断 | public | passed | 通过 | 100.0 | 100 | pending |
| 清理先预览后执行 | public | not_run | 未运行 | — | — | pending |
| 普通 DLoop 不产生 UI 证据 | public | not_run | 未运行 | — | — | pending |
| 过期批准阻塞推进 | public | not_run | 未运行 | — | — | pending |
| 最终评审识别候选漂移 | public | passed | 通过 | 100.0 | 100 | pending |
