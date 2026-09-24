---
name: dloop
description: 使用 DLoop v4.0.1 管理至少包含一个实施切片的大型功能交付流；普通调用不生成 Unity UI 专属产物。
disable-model-invocation: true
---

# DLoop v4.0.1 功能交付流入口

本技能只在用户显式调用 Codex 的 `$dloop` 或 Cursor 的 `/dloop`，且目标需要跨轮规划、至少一个实现切片、独立候选评审和最终集成验收时管理大型功能交付。不满足入口条件的事项直接按普通任务处理，不初始化交付档案；进行中确认不需要实现切片时停止 DLoop，不得用空候选集合完成验收。DloopUI 是独立的显式入口；界面相关自然语言不会让普通 DLoop 选择 UI 配置或生成 UI 证据。

本文件只供主协调者和跨回合恢复者完整读取。冷读、设计评审、实施、候选评审、返修和最终验收角色直接使用 `context-summary` 返回的闭合角色视图，不读取本入口或前序对话。

需求确认与最终验收直接用现有入口展示业务场景，具体规则随对应动作投递。普通 DLoop 可引用任务已有的场景和证据；这不启用 `$dloop-ui` 的专属模型、Prefab 采集或标注流水线。

## 默认路径

1. 从当前版索引按用户给出的自然标题或唯一业务上下文定位当前交付项：唯一匹配就恢复，没有匹配才创建；存在多个合理候选时请用户选择。当前对话只处理一个交付项。
2. 进入、切换或异常恢复时运行 `workflow-status` 与只读 `audit`。指定交付项时从唯一 `delivery_view` 读取状态和必要占用身份；仅排查历史、检查明细或恢复基线时加 `--include-details`，不把展开内容当作日常上下文。恢复时发现活动执行身份就把工作交还该身份并停止，不接管、替换或释放它；交接前只读准备材料不代表接管该角色。
3. 把当前业务动作映射为下表标识，用一次 `workflow-rules` 取得闭合规则集合，完整读取返回正文后行动。多个动作按业务顺序一次提交，工具会稳定去重。
4. 按 `delivery_view` 推进；事实不唯一由协调者决定，机器阻断原样返回，不拼接视图或手改状态。暂停、保存或回退先按 `snapshot` 规则处理；返修用 `slice-release`。
5. 创建下游上下文前调用 `prepare-handoff --feature-id <id> --action <action> --role <role>`，附上该角色所需临时输入。只有 `handoff_ready: true` 才交付工具生成的 `task_message` 与 `context_command` 并启动下游；正式事实不复制进任务消息。缺项由原上游上下文在授权范围内补齐，输入未变化时不重试；涉及业务决定或批准变化时交还协调者。下游自行执行返回的读取命令，独立核对完整性与业务正确性。

交付档案固定在 `.scratch/dloop-v3/v4.0.1/outputs/<feature-id>/`，阶段依次是需求、调查、设计、计划、实施、验证。已连接 DLoop MCP 时，先核对返回的项目根；状态用 `dloop_status`，四类输入用 `dloop_prepare_input` 取模板后调用对应提交工具，勿手写 JSON 文件。其余动作或未连接时用 `python Tools/FeatureArchive/feature_archive.py <command>`；写入位置以返回为准。

## 动作路由

```powershell
python Tools/FeatureArchive/feature_archive.py workflow-rules `
  --action <action>
```

下表是规则动作；角色视图中的阶段动作和命令返回的执行操作分别按当次合同使用。

执行使用返回的 `tool_call` 或 `next_action_contract`、`handoff_preparation`、`input_preparations`；身份不混填。设计评审用 `design/design-review`，候选评审用 `implementation/review` 与评审身份，分别提交 `review-issues`、`review-verification`。CLI 输入只用返回路径，临时分析用 `.scratch/outputs/`。

| 业务动作 | 固定动作标识 |
|---|---|
| 创建、继续、需求编写、候选术语或术语变更 | `create`、`resume`、`requirements`、`terminology` |
| 需求共识 | `requirements-review` |
| DloopUI 最终交互核对 | `ui-interaction-review` |
| 调查、设计、计划、文档、图示、依赖刷新 | `investigation`、`design`、`plan`、`documentation`、`diagram`、`dependency-refresh` |
| 重大架构确认、独立冷读 | `architecture-review`、`cold-read` |
| 切片方案、契约检查、实施检查点、熔断决策与恢复，或启动、提交、候选验收、返修和释放切片 | `slice-plan`、`slice-start`、`slice-submit`、`candidate-review`、`slice-rework`、`slice-release` |
| 导出只读分享 | `share` |
| 有明确额外劳动时补记，或复盘当前交付项的摩擦 | `friction` |
| 整体验证、交付评审、按需业务验收 | `validation`、`final-review` |
| 冻结、保留期检测、清理 | `freeze`、`retention`、`cleanup` |

只讨论工作流设计时，仅选择讨论实际涉及的动作，不为“相关”扩大规则集合。

## 关键不变量

- DloopUI 保留内部需求与设计；四类必要材料齐备后，展示开工清单并取得用户确认。缺项、歧义或确认失效时停止整个需求的实施。实施中补齐范围内交互；最终展示并等待用户验收。
- 当前动作只读取能改变判断或产出的最小工作集。实施从角色视图、授权项目事实和本轮验证证据开始；评审从固定候选和批准材料出发核对相关项目事实，不接收实施对话。
- `context-summary --feature-id <id> --action <action> --role <role>` 与 `workflow-status` 共享事实源。角色视图、字段合同、状态迁移、写入范围、修改租约、新鲜度、候选固定、评审身份、最终绑定和清理资格均以工具结果为准。
- 单线实施，使用项目的 Git 或 SVN 工作区；实施与候选评审身份独立。候选评审和最终验收只读，自审、候选漂移、缺材料或批准过期时停止并交还主协调者。
- 范围内测试失败保留活动棒次，实施者继续诊断、修复和重跑。只有业务结果、范围、强依赖、公共或存量数据、不变量、独立验收或安全回退等契约边界明确失效时才记录熔断，并在 `circuit_open` 后停止生产修改。
- 文档语义变化后按刷新队列更新下游材料并重新通过新鲜度门禁。AI 只判断语义是否变化；版本、传播和批准失效由工具维护。
- 清理先预览；只有用户本轮明确授权且预览允许时执行。发布、标签、安装、覆盖、删除和其他不可逆或高风险操作不包含在阶段批准中，需要各自专项授权。

## 完成与停止

- 对外只按 `delivery_summary.completion_claim` 的状态汇报；候选缺失、待评审、熔断或未最终验收时不得宣称整体完成。

- 用户指出重复劳动，或角色实际经历重复查找材料、格式试错和交接退回时，就近使用返回的 `friction_note` 入口；没有该返回时读取 `workflow-rules --action friction`。已有问题只补记，恢复后描述关键改动与已知代价；不要求每步记录、不为记录另开交接，也不以摩擦关闭作为交付条件。
- 协调动作在目标、输入边界、验收条件、当前阻塞和唯一下一动作可判断时停止扩展读取。
- 实施者完成登记验证后提交最终检查点和候选；提交成功后停止并交还协调者。提交缺项时保留原上下文、执行身份和成果，补齐后再提交，不能把失败当成完成。评审者成功提交独立结论后停止；下游核对发现缺项时不产生部分结论。
- 最终验收核对候选、独立评审、集成确认和工作区仍一致；绑定漂移或验收范围内必要验证未完成时阻塞。用户明确接受的延期与范围外未验证继续保留，不改写为通过。
- 普通 DLoop 交付按 `freeze` 自动封存；DloopUI 须先完成最终交互展示与用户验收；清空刷新队列，校验索引。SVN 冻结后执行 `sync-svn-changelist --feature-id <id>`；Git 跳过归组。清理仍须专项授权。
