# Trace 语义词汇

本文件只解释适配层的稳定语义值，不包含场景答案、判分条件或第二份结构合同；字段、类型、执行模式和采集来源由标准库 grader 单点校验。

每个事件使用严格递增的 `sequence`。共同字段是 `type`，其余字段如下：

- `read`：`category`、`target`。材料类别可用 `skill_entry`、`workflow_rules`、`archive_index`、`status`、`audit`、`role_view`、`role_blocker`、`project_fact`、`test_evidence`、`contract`、`candidate`、`cleanup_preview`、`requirements`、`freshness_blocker`、`ui_evidence`。目标以 `delivery_view` 开头时，即使另用更细的类别描述字段，判分仍把它视为同一份 `role_view` 材料。夹具声明 `allowed_project_facts` 时，`project_fact` 的目标必须精确属于该清单。
- `decision`：`name`。常用业务值为 `create_new_delivery`、`resume_existing_delivery`、`continue_existing_execution`、`take_over_execution`、`diagnose_in_scope_failure`、`open_circuit_breaker`、`select_dloop_ui_configuration`。
- `command`：`name`，可选 `mode` 与 `args`。命令名使用 DLoop 公开命令，如 `workflow-rules`、`workflow-status`、`audit`、`context-summary`、`init`、`assert-fresh`、`start-slice`、`checkpoint-slice`、`submit-slice`、`review-slice`、`purge`、`ui-investigate`。`purge` 的模式使用 `preview` 或 `execute`；普通初始化可用 `mode: ordinary`。
- `write`：`target`，可选 `category`。目标必须是相对夹具路径。
- `state`：`from`、`to`。只记录确定性命令确认的变化；没有变化时不要猜测。
- `stop`：`reason`，可选 `handoff_to`。
- `handoff`：`to`、`summary`，可选 `blockers`。

`final` 汇总最后状态、停止原因、下一负责人、摘要与阻塞。常用状态为 `coordinator_ready`、`requirements_in_progress`、`execution_active`、`candidate_fixed`、`circuit_open`、`blocked`、`pending_cleanup`、`removed`。停止原因使用能区分业务停止点的稳定小写英文短语；交接给主协调者使用 `coordinator`，交接给执行身份使用 `implementation:<execution-id>`。

只有实际运行完成后才把 `execution.status` 设为 `completed`。执行器、开始和完成时间按实际填写；宿主无法可靠提供的耗时、轮次和上下文成本保留 `null`，不得估算。`manual_review` 默认保持 `pending`。
