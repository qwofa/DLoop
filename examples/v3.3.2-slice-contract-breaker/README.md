# v3.3.2 切片契约与实施熔断最小示例

本目录给出一条可复核路径：契约检查通过 → 进入实施 → 完整验证集合第一次失败只记录事实 → 同一验证集合连续第二次失败后系统熔断并生成报告 → 人工决定拆出前置切片 → 前置切片验收且报告条件逐项具备证据 → 原切片以新执行身份恢复。

示例假设已经初始化 `<root>/<feature-id>`、批准需求并确认实施输入，`<workspace>` 为目标工作区。命令都使用现有档案与切片执行账本：

| 系统观察到的事实 | 结果 |
|---|---|
| 契约未批准、七项检查失败或事实矛盾 | 保留失败原因，不取得修改租约 |
| 完整验证集合第一次失败，且没有硬边界失效 | 保持实施中，允许修复 |
| 同一验证失败集合连续第二次出现 | 自动熔断并生成报告 |
| 工作区出现授权范围外实际变化 | 单次立即熔断 |
| 业务结果、验收、强依赖、不变量、独立验收或回退点失效 | 单次立即熔断 |
| 全部验证通过且没有边界失效 | 保持实施中，可作为最终检查点 |
| 没有最终检查点、检查点未全通过、契约已变化或工作区已漂移 | 禁止提交候选 |
| 替代切片全部验收 | 原切片进入 `superseded` 终态 |

```powershell
python Tools/FeatureArchive/feature_archive.py prepare-slice-contract `
  --root <root> --feature-id <feature-id> `
  --package-file examples/v3.3.2-slice-contract-breaker/v3.3.2-task-package.json

python Tools/FeatureArchive/feature_archive.py check-slice-contract `
  --root <root> --feature-id <feature-id> `
  --package-file examples/v3.3.2-slice-contract-breaker/v3.3.2-task-package.json

python Tools/FeatureArchive/feature_archive.py start-slice `
  --root <root> --feature-id <feature-id> --execution-id example-exec-1 `
  --package-file examples/v3.3.2-slice-contract-breaker/v3.3.2-task-package.json `
  --workspace-root <workspace>

python Tools/FeatureArchive/feature_archive.py checkpoint-slice `
  --root <root> --feature-id <feature-id> --execution-id example-exec-1 `
  --checkpoint-file examples/v3.3.2-slice-contract-breaker/v3.3.2-checkpoint-1.json

python Tools/FeatureArchive/feature_archive.py checkpoint-slice `
  --root <root> --feature-id <feature-id> --execution-id example-exec-1 `
  --checkpoint-file examples/v3.3.2-slice-contract-breaker/v3.3.2-checkpoint-2-breaker.json
```

第二个检查点返回 `circuit_open` 和系统生成的完整报告。此后再次提交检查点会返回 `IMPLEMENTATION_HALTED`，不会出现第三次相同失败补丁。检查点不接收完成比例、主观改善状态、边界布尔值或人工熔断报告。

先人工保留需要的已验证成果，并将当前切片授权范围恢复到执行基线；工具不会代替人工删除或回退。随后记录决定：

```powershell
python Tools/FeatureArchive/feature_archive.py decide-slice-breaker `
  --root <root> --feature-id <feature-id> --package-id example-slice `
  --decision-file examples/v3.3.2-slice-contract-breaker/v3.3.2-decision-split-prerequisite.json
```

该决定进入 `waiting_dependency` 并明确等待 `example-prerequisite`。按正常契约流程创建、实施和验收此前置切片后，提交恢复证据：

```powershell
python Tools/FeatureArchive/feature_archive.py resume-slice-breaker `
  --root <root> --feature-id <feature-id> --package-id example-slice `
  --execution-id example-exec-2 `
  --recovery-file examples/v3.3.2-slice-contract-breaker/v3.3.2-recovery.json
```

工具会实测前置切片已经验收，并将原切片恢复为 `active`，同时保留契约、两个检查点、熔断报告、决定和恢复历史。若选择小范围修订，可用 `v3.3.2-task-package-revision-2.json` 配合 `amend_contract` 决定；系统会比较写入范围、责任边界、影响区域、依赖、不变量和回退点，只有未扩大边界且新契约检查通过时才进入 `resumable`。

业务结果、验收、依赖、不变量、独立验收或回退点失效仍由负责人提交明确事实与证据；授权范围外变化、契约版本、完整验证集合、连续失败、最终检查点、工作区一致性、依赖关系和恢复证据覆盖由工具确定性校验，不使用主观评分或 20%/50% 比例。
