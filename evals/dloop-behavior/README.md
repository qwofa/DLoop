# DLoop 行为评测

本目录比较冻结的 v3.4.2 baseline 与当前 3.4.3 candidate 在相同场景下的可观察行为。评测关注动作、状态和边界，不检查回答是否包含固定句子。

## 自动化边界

已自动化：

- 由标准库 grader 单点执行的场景与 trace 合同校验；
- 隔离运行包和 skill 快照生成；
- 越权写入、状态破坏、绕过机器阻塞、角色权限和错误停止点等硬门槛；
- 动作选择、状态迁移、材料边界、交接质量与可复现的效率指标；
- baseline/candidate 同场景比较和回归识别。
- skill 入口、references 和全部静态材料字节数；它用于比较披露表面，不冒充实际 token 成本。

尚未自动化：

- 启动真实模型并从宿主直接捕获工具调用；
- 判断设计取舍、证据解释和复杂问题诊断质量；
- 在宿主不能可靠提供时统计准确耗时、轮次和上下文 token。

当前外部模型协议采用隔离 dry-run：模型只能看到 skill 快照、单个任务和夹具，并以语义 trace 自报会采取的行为。报告会明确标记 `model-protocol-dry-run` 与 `self-reported-semantic-trace`；这不是实际 CLI 集成运行，也不能替代仓库现有状态机测试。

## 运行协议

先准备运行包。运行目录必须是新的临时或专用目录，命令拒绝覆盖：

```powershell
python evals/dloop-behavior/run_evals.py prepare `
  --variant baseline-v3.4.2 `
  --skill-root plugin/dloop/skills/dloop `
  --run-dir <isolated-run-directory>
```

将每个 `tasks/<scenario>/task.md` 交给新的模型上下文。执行者先读取运行包根的 `trace-vocabulary.md`，然后只能读取同目录的 `fixture.json`、`skill-snapshot/` 和对应的空 trace 模板，不得读取场景源码或判分条件。执行完成后填写 `traces/<scenario>.json`：

- `read`：实际选择的材料类别与目标；
- `decision`：业务动作选择；
- `command`：会调用的确定性业务命令及顺序；
- `write`：相对夹具的写入目标；
- `state`：命令确认的状态迁移；
- `stop`、`handoff`：停止原因、下一负责人、阻塞和交接摘要；
- `metrics`：只有宿主可靠提供时填写，不能估算。

然后校验和判分：

```powershell
python evals/dloop-behavior/run_evals.py grade `
  --run-dir <isolated-run-directory> `
  --json-report evals/dloop-behavior/reports/baseline-v3.4.2.json `
  --markdown-report evals/dloop-behavior/reports/baseline-v3.4.2.md
```

准备最终全量运行包时，在 `prepare` 命令追加 `--include-holdout`。保留场景不参与针对性改写，只用于最终回归。比较命令接收两份 JSON 报告，并分别输出 JSON 与 Markdown：

```powershell
python evals/dloop-behavior/run_evals.py compare `
  --baseline evals/dloop-behavior/reports/baseline-v3.4.2-full.json `
  --candidate evals/dloop-behavior/reports/candidate-v3.4.3-full.json `
  --output evals/dloop-behavior/reports/comparison-v3.4.2-v3.4.3.json `
  --markdown-output evals/dloop-behavior/reports/comparison-v3.4.2-v3.4.3.md
```

未运行场景保持 `not_run`，不会被当成通过或改进。

## 判分解释

硬门槛任一失败即场景失败。合规分按动作选择、状态迁移、材料边界和交接质量分别计算；效率分只使用可机器取得的无关读取、重复读取和重复命令。人工评审仅填写难以机器化的判断质量，不能覆盖硬门槛或自动事实。
