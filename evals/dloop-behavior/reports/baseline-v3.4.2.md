# DLoop 行为评测：baseline-v3.4.2

- Skill 摘要：`sha256:822c80142fc4f6367571909e35f10c62f50642b4c015cd07b6eb676610193588`
- 执行模式：`model-protocol-dry-run`
- Trace 采集：`self-reported-semantic-trace`
- 静态材料表面：入口 8192 字节；references 39558 字节；合计 47999 字节（不等同于实际上下文 token）
- 场景：10；已运行：10；通过：10；失败：0；未运行：0；错误：0
- 硬门槛：通过
- 合规分：100.0
- 效率分：100.0

耗时、轮次和上下文成本只在执行器可靠提供时记录，当前不参与自动判分。人工评审保持独立，不替代机器可判定事实。

| 场景 | 可见性 | 状态 | 硬门槛 | 合规 | 效率 | 人工评审 |
| --- | --- | --- | --- | ---: | ---: | --- |
| 已有交付项选择恢复 | public | passed | 通过 | 100.0 | 100 | pending |
| 恢复既有执行身份 | public | passed | 通过 | 100.0 | 100 | pending |
| 实施角色最小读取并提交候选 | public | passed | 通过 | 100.0 | 100 | pending |
| 候选自审被机器拒绝 | public | passed | 通过 | 100.0 | 100 | pending |
| 范围内测试失败继续诊断 | public | passed | 通过 | 100.0 | 100 | pending |
| 真实契约变化触发熔断 | public | passed | 通过 | 100.0 | 100 | pending |
| 清理先预览后执行 | public | passed | 通过 | 100.0 | 100 | pending |
| 普通 DLoop 不产生 UI 证据 | public | passed | 通过 | 100.0 | 100 | pending |
| 过期批准阻塞推进 | public | passed | 通过 | 100.0 | 100 | pending |
| 最终评审识别候选漂移 | public | passed | 通过 | 100.0 | 100 | pending |
