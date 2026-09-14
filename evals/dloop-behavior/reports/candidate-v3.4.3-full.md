# DLoop 行为评测：candidate-v3.4.3-full

- Skill 摘要：`sha256:d092a24832afc9a948b251aaaca846bdf2de2a471c760016894c9692304789bc`
- 执行模式：`model-protocol-dry-run`
- Trace 采集：`self-reported-semantic-trace`
- 静态材料表面：入口 5146 字节；references 36137 字节；合计 41532 字节（不等同于实际上下文 token）
- 场景：12；已运行：12；通过：12；失败：0；未运行：0；错误：0
- 硬门槛：通过
- 合规分：92.3
- 效率分：93.8

耗时、轮次和上下文成本只在执行器可靠提供时记录，当前不参与自动判分。人工评审保持独立，不替代机器可判定事实。

| 场景 | 可见性 | 状态 | 硬门槛 | 合规 | 效率 | 人工评审 |
| --- | --- | --- | --- | ---: | ---: | --- |
| 已有交付项选择恢复 | public | passed | 通过 | 100.0 | 100 | pending |
| 恢复既有执行身份 | public | passed | 通过 | 100.0 | 100 | pending |
| 实施角色最小读取并提交候选 | public | passed | 通过 | 100.0 | 100 | pending |
| 候选自审被机器拒绝 | public | passed | 通过 | 100.0 | 100 | pending |
| 范围内测试失败继续诊断 | public | passed | 通过 | 100.0 | 100 | pending |
| 真实契约变化触发熔断 | public | passed | 通过 | 80.0 | 70 | pending |
| 清理先预览后执行 | public | passed | 通过 | 100.0 | 100 | pending |
| 普通 DLoop 不产生 UI 证据 | public | passed | 通过 | 75.0 | 85 | pending |
| 过期批准阻塞推进 | public | passed | 通过 | 77.8 | 85 | pending |
| 最终评审识别候选漂移 | public | passed | 通过 | 100.0 | 100 | pending |
| 无匹配档案时创建交付项 | holdout | passed | 通过 | 100.0 | 100 | pending |
| 未授权清理只预览 | holdout | passed | 通过 | 75.0 | 85 | pending |
