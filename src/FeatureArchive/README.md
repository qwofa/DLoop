# 交付档案工具

## 3.9.1 中间态入口

SVN/Git 项目均需要 Git 客户端。每个作业的持久快照库在 `.scratch/dloop-v3/v3.9.1/snapshots/<feature-id>.git`；不写源仓库分支、暂存区或 SVN 服务器。保存正文与业务状态，失败检查点也保留。

UI 明确登记的需求来源以原始字节保存为只读输入，导出至 `inputs/`。导出清单 `snapshot.json` 的 `inputs` 将来源原路径对应到导出文件；缺失来源标为 `null`，不生成虚构正文。这些输入供查看或作为新作业材料，恢复不会因登记来源而获得覆盖原文件的授权。

- `snapshot-save --feature-id <id> [--name <名称>] [--request-id <一次请求标识>]`：手动保存。阶段首次编辑前加 `--reason stage-start --stage requirements|investigation|design|plan|implementation|validation`；覆盖前用 `before-overwrite`，提交阶段材料用 `stage-submitted`，暂停用 `pause`。
- `snapshot-list --feature-id <id>`：按时间列出保存点、阶段、原因和结论；状态同时报告占用空间及是否需要恢复。
- `snapshot-export --feature-id <id> --snapshot-id <s编号> --destination <新目录>`：导出当时的档案、代码资源及范围清单，当前项目不变。
- `snapshot-restore --feature-id <id> (--snapshot-id <s编号> | --stage <阶段>)`：预览文件与切片影响；加 `--execute` 才恢复，工具自动先保存当前现场。阶段重做选择最近的阶段开始点。
- `snapshot-recover --feature-id <id>`：完成未保存的状态登记，或撤回被中断的恢复事务；恢复完成前不允许交接和新实施。
- `snapshot-fork --source-feature-id <旧作业> --snapshot-id <s编号> --feature-id <新作业> --title <标题>`：导出历史并创建带来源的新作业，重新登记目标、范围及批准后实施，源作业与工作区不变。

恢复后当前实施或待评审候选回到可重新启动的契约状态，必须使用新的执行身份，并保留原切片起点来计算完整候选差异。历史结论不删除，后续受影响结果退出当前集合，最终验证重新进行。冻结作业不原地恢复；源仓库修订变化时只允许导出后另建变更作业。缓存清理与卸载保留快照，完整作业清理会同时预览和删除快照。

3.9.1 为候选版，基于冻结版 v3.8.0，尚未发布标签。修正生成范围预检、采集产物保护、重试前检查与具体阻断指引，复用已有内部草稿展示。升级时封存旧作业、解除旧占用，活动材料使用完整发行版本独立目录；旧作业不续跑、不迁移。Git 检查与撤销不改写暂存区，不自动提交或推送；含变化的子模块须独立运行。

任务包的 `generated_write_scope` 接收项目生成器只读预检查的预计写入路径，包括预制体绑定信息、代码及新增元文件；全部被 `write_scope` 覆盖后才可登记或启动。没有生成操作时留空。工具不推测项目生成器行为，也不自动扩大授权。

UI 采集请求和临时截图固定在本版本的 `captures/<feature-id>/`，内部 UI 模型与正式证据按材料完整性和候选绑定检查，不作为产品文件触发越界。产品预制体仍需授权。重试发现原范围外变化未解除时，保留原执行和成果并返回具体路径、生效版本及恢复指引，不消耗新执行身份。

未接受候选时，复用已有标注评审输入调用 `ui-publish` 且省略 `delivery_review`，可用同一模板展示内部草稿；不另建人工 HTML。正式材料准备仍须有已接受候选，草稿不会批准、冻结或解除实施阻断。

`ui-publish` 固定套用随工具安装的 `templates/ui-delivery.html`，生成 `06-validation/ui-delivery.html` 以及同源编号图、Markdown 表格。交互初稿允许不完整，无需单独批准；实施任务完整引用需求和设计，并须取得用户对当前开工材料清单的明确确认。正式发布需提交每项交互及双向核对，自动绑定当前已接受候选和需求、设计依据。

`workflow-status` 的 `delivery_view.conclusions.approvals.ui-delivery.review` 返回最终 HTML、归档表格和 `reviewed_digest`。用户查看后，使用 `stage-action --stage final --decision approve|reject --reviewed-digest <展示摘要> --user-confirmation <用户回复定位及原文>`；批准还需集成确认。缺少实际回复、材料过期或必要验证未完成时拒绝。DloopUI 取得有效最终验收后才可封存；普通 DLoop 保持原流程。用户回复真实性由协调者负责，程序不连接宿主对话认证。

`prepare-action-input --feature-id <id> --input-kind review-issues|review-verification --execution-id <独立评审身份>` 需要唯一候选。两种输入分别写入 `05-implementation` 和 `06-validation`，返回当前候选的提交参数；准备不会登记评审或覆盖既有文件。`workflow-status` 的 `delivery_view.delivery_summary` 从既有批准和候选即时投影；候选形成后展开未验证边界及验证、后续接入入口，初期仅报告接受状态。分享复用相同汇总，不新增持久业务状态。

## 实施中补充验证

`supplement-slice-validation --feature-id <id> --execution-id <id> --validation-level <等级> --validation-method <新增验证> --rationale <新增风险事实>` 只加强当前实施或返修的验证。新增验证参数可重复；仅提高强度时可以省略。工具保留原验证、业务范围、执行身份、租约、成果和历史，递增契约版本并使旧最终检查点失效；同样的补充重复提交不再改写状态。候选、熔断或结束状态不能使用此入口。

使用 `prepare-action-input --input-kind checkpoint` 生成当前契约的草稿，补齐验证再提交；草稿文件按契约版本区分，旧文件不覆盖。输入携带工具填写的 `contract_version`，登记时核对当前版本，旧输入原样重交会被拒绝。业务边界变化仍走熔断与重新规划，不能通过补充验证扩展授权。

交接准备默认只返回就绪、任务消息与读取命令；`--include-role-view` 仅用于显式展开诊断。下游自行读取完整视图，包括相关项目调查、失败处理与真实流程正确性判断规则；最终验收只提交建议。

## 需求与验收展示

现有需求和验证总览提供可删除的填写提示：需求按业务场景直接表达目标、变化与关键边界，验证沿同一场景引用需求并呈现真实结果和偏差。UI 交互审核复用需求场景和标注附件，不增加场景文件、状态矩阵或媒体门槛。具体粒度与 Unity 复用规则由对应动作的 Skill 规则投递。

## 输入准备与摩擦复盘

`prepare-action-input --feature-id <id> --input-kind task-package --package-id <id>` 生成任务包及嵌套 `input_guidance`；示例只用于说明，不会写入实际依赖或未知项。选择 `--input-kind slice-plan` 时只指定交付项，工具在计划目录生成第一版或下一版草稿；已有文件不覆盖。编辑 `target` 后执行返回的 `next_action`，方案检查通过后再按返回的摘要批准。

DloopUI 任务包增加按验收场景声明的 `delivery_requirements`；候选输入增加实际文件引用 `delivery_materials` 和可选同范围补充 `additional_delivery_requirements`。已接受候选的材料由 `ui-publish` 自动汇总；`prepare-action-input --input-kind ui-delivery` 返回最终核对输入与已交材料清单。材料修订通过 `material_updates` 更新指定引用，其他材料沿用，并继续使用内置模板。程序检查文件和定位的真实性、缺项、候选绑定及必要验证状态，业务完整性仍须结合需求、后端、代码及实际界面核对。完整字段见随安装提供的 DloopUI 业务输入合同。

`friction-note --feature-id <id> --summary "目标与卡点" --extra-work "实际多做的动作" --evidence "path:项目相对路径"` 记录额外劳动，已有问题用 `--incident-id` 关联。`--recovery` 补充关键改动与结果，`--cost` 只写有依据的代价，`--source user-feedback` 表示用户明确反馈。无需 JSON 输入文件；当次可知角色、阶段与执行身份可随 `--role`、`--stage`、`--execution-id` 提供。

`friction-summary --feature-id <id>` 只读查看自动事件、观察、恢复方式与证据入口，保留已经恢复的问题；不依赖全局档案校验，不把恢复说明当根因修复。补记失败不影响业务结果，补记缺失不阻断交付，已有分享入口按交付项包含当前记录。

## 规范档案与分享快照

3.9.1 的公开命令从项目内 `Tools/FeatureArchive/feature_archive.py` 固定安装位置确定项目根，不读取当前工作目录，也不接受 `--root` 或 `--project-root`。正式状态和材料唯一位于 `.scratch/dloop-v3/v3.9.1/outputs/<feature-id>/`；携带交付项的业务结果通过 `archive_location` 返回实际规范位置，全局命令保持原有响应结构。协调者角色投影只在当前请求属于已开放、可写的六个交付阶段时，通过 `write_targets` 返回该阶段总览的精确写入目标；动作被阻断以及冻结、清理等只读动作返回空写入目标，其他角色同样不取得正式文档写入目标。

任务包、检查点、候选与评审材料等正式输入，只能来自当前交付项对应的规范阶段目录。集成确认继续完全沿用最终验收原有合同，检查其 `06-validation` 位置、内容和当前候选绑定，不增加分享目录专用错误；需求来源和产品工作区仍按各自合同接受外部路径。其他规范目录之外的副本会被拒绝；存在分享身份文件时，即使清单内容不可读，也会稳定返回“只用于读取、不能执行”的诊断和当前交付项中的精确恢复目标，有效清单同时返回导出时间与来源状态摘要。

需要把材料交给工作流外部阅读时，同步导出一个新的不可变身份快照：

```powershell
python Tools/FeatureArchive/feature_archive.py export-share `
  --feature-id building-interaction
```

快照写入临时目录后再次核对工作流状态、正式文件、派生交付状态和可归属摩擦；来源未变化才在同一事务锁内原子发布到 `.scratch/dloop-v3/v3.9.1/shares/<feature-id>/<snapshot-id>/`，正式档案提交在发布完成前等待。来源变化时删除临时结果，并要求调用者根据最新状态重新显式导出。快照包含 Markdown、`feature.json`、可选 `ui-model.json`、媒体证据、派生交付状态和可明确归属的摩擦记录。`.dloop-share.json` 保存来源状态摘要和全部导出文件摘要；机器工作流状态、根合同与锁不导出。导出在当前 Python 进程内一次完成，不启动后台任务、不自动重试，也不轮询。`read_only` 和 `executable: false` 只表达该快照不具备正式工作流资格。

## 阶段交接提示

当前版本提供 `prepare-handoff --feature-id <id> --action <action> --role <role>`：上游先检查并装配目标角色材料，`handoff_ready: false` 时在原上下文按具体缺项补齐，成功后才创建下游上下文。任务消息与下游读取命令由工具生成；下游仍用 `context-summary` 独立核对。实施启动、候选提交和返修启动在状态切换前共用材料检查，失败不提前交棒。准备不生成新的交接状态或档案。

`dloop-ui-v1` 只在既有工作流状态中保存相对模型路径和审计摘要。调用者通过 `ui-investigate` 提交需求与真实 Prefab 判断，通过 `ui-publish` 提交截图上的修改、复用或目标不可见判断；`ui-model.json` 是 module 生成的内部事实，不直接编辑。模型还保存交互条件、操作、反馈、结果及交付核对；完整材料要求见前文 DloopUI 交付说明。Prefab 资产是截图时的历史观察，后续实现不会使计划自动过期；需求原文、截图和截图清单仍按当前内容核验。常驻入口只把主协调者路由到目标阶段规则；冷读、设计评审、实施、候选评审、返修和最终验收各自声明上游输出与下游输入责任。实施、候选评审和返修的任务消息只传递交付项、角色与必需执行身份，设计评审只传递交付项与角色，最终验收只增加必要的集成确认文件定位。正式材料始终是业务事实源，角色视图只做当前棒次执行投影。候选评审视图直接包含完整任务包和固定候选；返修视图从不可变评审快照补齐原候选，并把每个失败项保守关联到任务包登记的全部验证；设计评审与最终验收也从正式材料和不可变快照即时取得完整输入。

结构化输入按照当前执行操作的字段合同构造，不读取历史模板。机器阻断原样返回；角色视图形成后的业务缺项才单独退回。正常业务操作复用事务内资格检查，确定性失败输入未按原因改变时不原样重试。正式返修交接必须包含原候选标识、完整问题集、允许范围和重验场景，不要求现有正式事实没有保存的逐项处理状态。

## 风险驱动测试与集成确认

验证强度只由技能中的唯一策略形成，任务包登记其等级、理由和验证集合；实施、返修与最终验证复用同一结论，风险事实变化时才重新判定。候选提交、评审快照和最终批准不会自行运行外部测试。

普通 DLoop 在所有计划切片收敛、整体交付验证完成后自动冻结，不额外要求最终人工批准。DloopUI 必须先展示当前交付并取得用户最终验收，才能冻结。人工验收状态如实保留，冻结不会生成批准；后续普通调试不改变历史交付状态，不提供解冻。

存在已接受实现候选时，交付冻结与按需最终批准都使用位于当前交付项 `06-validation` 目录中的集成确认 JSON；冻结通过 `transition-lifecycle --to frozen --integration-confirmation <文件>` 提交，已有有效最终批准时可复用其确认。文件只包含通过结论、当前候选集合摘要和最终工作区防护摘要；批准记录保存相对路径与 SHA-256。封存前候选集合、仓库修订、工作区或确认文件变化都会使批准失效；封存后不再用当前工作区重判历史交付。该文件表达负责人的确认，不证明测试命令已执行，实际执行记录仍由项目测试运行器或验证入口保存。

```json
{
  "result": "passed",
  "candidate_summary_digest": "sha256:<当前已接受候选集合摘要>",
  "workspace_guard_digest": "sha256:<当前最终工作区摘要>"
}
```

```powershell
python Tools/FeatureArchive/feature_archive.py stage-action `
  --feature-id reliable-delivery `
  --stage final `
  --decision approve `
  --integration-confirmation .scratch/dloop-v3/v3.9.1/outputs/reliable-delivery/06-validation/integration-confirmation.json
```

独立评审可以按风险附加自己设计的负向或边界测试证据，也可以不附证据或“不适用”理由；这些字段不是通过结论的门槛。每次固定候选与评审结论只追加一个不可变评审快照，活动候选随后清空，不维护候选与评审的同步副本。返修产生新候选和新快照，旧快照保留。

## 版本化切片方案

切片方案使用两个确定性入口：

```powershell
python Tools/FeatureArchive/feature_archive.py check-slice-plan `
  --feature-id reliable-delivery `
  --plan-file .scratch/dloop-v3/v3.9.1/outputs/reliable-delivery/04-plan/slice-plan.json

python Tools/FeatureArchive/feature_archive.py approve-slice-plan `
  --feature-id reliable-delivery `
  --plan-file .scratch/dloop-v3/v3.9.1/outputs/reliable-delivery/04-plan/slice-plan.json `
  --plan-digest sha256:<检查结果摘要>
```

方案标识必须与交付项标识相同，版本必须为正整数。每个切片声明直接前置和直接替代切片。任意数量切片都会生成稳定规范化摘要、带类型有向图、非法关系链和实时资格。批准会在同一事务中重算方案与图并核对摘要，只持久化规范化声明、摘要、不可变历史和唯一当前版本指针；图与资格始终重算。

启动前必须显式检查并批准包含目标切片的当前方案，且目标满足实时资格；工具不会在启动或增加切片时隐式建立或扩展方案。首次启动保存完整批准历史绑定，原范围重试继续使用同一绑定。候选把版本、摘要和不可变批准历史引用纳入自身固定事实与摘要；提交结果和独立评审上下文返回同一绑定，并可从历史复核完整规范化声明。档案校验会对每份批准历史重新执行完整方案与大切片图语义检查。关系图不会自动启动、批准或验收任何切片。

本目录提供与 Unity 运行时代码隔离的确定性命令行工具。当前已实现工作流规则投递、标准交付档案初始化、业务术语与 Markdown 链接校验、无语义术语重命名、术语影响分析、派生索引重建、编辑批次确认、逐层依赖刷新、生命周期阶段门禁、保留期检测和独立清理。

项目本地 AI 编排规则位于 `.agents/skills/dloop/SKILL.md`。仅在用户显式调用 `$dloop` 后读取并执行该技能；工作流只负责语义判断、正文维护和命令编排，结构校验、状态传播、阶段门禁、保留期与清理资格均以本工具结果为准。任务位置见上文“规范档案与分享快照”，六类职责由文档规则说明。

## 初始化命令

```powershell
python Tools/FeatureArchive/feature_archive.py init `
  --feature-id building-interaction `
  --title 建筑交互
```

普通局部任务属于功能交付流外，不初始化交付档案。DLoop 只提供唯一严格合同，普通 DLoop 初始化时省略配置参数；显式 UI 任务按 DloopUI 入口选择配置。新档案根必须是带当前合同根清单的独立目录，旧根、旧合同、缺失版本和历史档案直接拒绝，不自动读取、迁移或降级。

`--feature-id` 只接受小写英文、数字和连字符。命令会在共同父目录下创建同名功能目录，并生成：

- 根级人工导航入口 `README.md`；
- 根级生命周期清单 `feature.json`；
- `01-requirements`、`02-investigation`、`03-design`、`04-plan`、`05-implementation`、`06-validation` 六个有序类别；
- 每个类别中的必需总览 `README.md`；
- requirements 类别中结构必需的业务术语表 `01-requirements/terminology.md`。正文内容按需维护，没有候选术语时允许保持明确的空状态；需求总览初始化时直接依赖术语表。

每份 Markdown 文档都带有统一的 YAML 元数据，包括文档标识、内容状态、语义版本、正文指纹、精确依赖位置、已消费的依赖版本和非阻塞关联位置。除强制术语表外，初始化不会主动生成空白专题文档。

## 幂等和失败行为

- 标准档案已经完整存在时，命令返回 `unchanged`，不会改写文件或更新时间。
- 已有档案缺少标准目录、标准文件或必要元数据时，命令在写入前失败并给出诊断，不自动补全。
- 已有标准文件的标识或必要元数据不兼容时，命令在写入前失败并给出诊断。
- 新档案先写入同一父目录下的临时目录，再整体提交；提交失败时不会留下部分档案。

成功结果输出到标准输出，失败结果输出到标准错误，二者均为 JSON。成功退出码为 `0`，可诊断失败退出码为 `1`，命令行参数错误退出码为 `2`。

## 工作流规则投递

每次触发功能交付流时先完整读取唯一常驻入口，再把当前业务动作转换为入口规定的固定动作标识。以下命令按动作顺序一次返回闭合规则集合：

```powershell
python Tools/FeatureArchive/feature_archive.py workflow-rules `
  --action requirements-review
```

同一任务命中多个动作时重复提交参数：

```powershell
python Tools/FeatureArchive/feature_archive.py workflow-rules `
  --action requirements-review `
  --action final-review
```

结果保留请求动作的原始顺序和重复项；参考模块按首次命中位置稳定去重。每项参考模块包含项目相对路径、原始 UTF-8 字节的 SHA-256 摘要和完整正文。动作到模块的组合只在工具中定义，入口不维护第二份文件映射，参考模块也不递归触发规则读取。

命令与交付项和生命周期无关，唯一严格配置共用同一规则投递。它只读取固定技能目录，不读取交付档案或项目事实，不创建阅读回执，也不证明 AI 已理解正文。未知动作、缺失入口、缺失模块、符号链接或非 UTF-8 内容会整体失败，不返回部分规则，也不会写入文件。

## DLoop 严格交付业务事务

新交付项使用唯一严格合同创建，生成六个交付环节总览、术语表和工作流状态；专题记录按需增加。DLoop 不接管、不迁移、不删除或降级任何历史档案。严格交付公开以下业务事务，不公开内部状态变更步骤：

- `workflow-status`：不带交付项标识时只返回全局修改占用身份；带交付项标识时通过唯一 `delivery_view` 查看批准、当前切片、阻断与下一动作。仅排查历史、检查明细或基线时加 `--include-details`；
- `context-summary`：按当前动作和必需角色返回交付状态、允许动作、阻塞项、待刷新文档和建议材料；角色视图提供串行棒次的完整流程合同，不复制业务正文、完整工作区指纹或执行历史；
- `prepare-action-input`：按当前任务包或执行事实生成任务包、检查点或候选的精确 JSON 输入，不覆盖已有文件；
- `stage-action`：记录用户的需求、按需架构或最终验收决定；
- `start-slice`：确认需求、设计和计划输入已就绪后校验简化任务包，并首次启动执行；
- `submit-slice`：关闭执行，并在修改成功时固定由真实工作区快照产生的候选；
- `review-slice`：记录不同声明执行身份对候选的验收；真实宿主上下文隔离由主协调者负责；
- `resolve-slice`：终止未开始实施的草稿、检查失败或已就绪切片并保留契约历史；仅文件漏登且仍属原业务授权时，通过 `amend-scope` 保留成果补登记具体文件；修复阻断后重试失败或熔断切片，在未验收修改已经人工恢复后终止熔断切片，或收敛其他异常执行状态。

每个实施切片都是具有明确写入范围的修改任务，并在当前项目的 Git 或 SVN 工作区取得全工作流唯一修改租约。任一时刻只推进一个实施切片；进入、切换或异常恢复交付项时查询全局状态，正常业务命令在事务内自行核对资格。只读调查不进入实施切片和候选状态机。

候选提交校验授权范围快照和包含仓库修订的工作区摘要；范围外变化拒绝提交，提交后变化拒绝验收。评审完成后，候选与评审只存在于不可变快照中。最终批准绑定非空的已接受实现候选集合、最后工作区和集成确认，任一内容变化即失效。没有已接受实现候选时不能进入最终验收。

### 中断恢复与执行收口验收

工作流不拦截普通退出。活动棒次、执行身份、检查点、修改租约和候选全部由正式档案持久化；调用方重新进入时根据状态继续或显式登记中断。以下场景用于验证这个接缝：

1. **上下文隔离**：实施者只接收固定任务包和选定章节，评审者只接收固定任务包、候选、变更及验证证据，不接收实施对话。预期宿主记录中的两个 Agent 上下文标识不同，工作流记录中的实施与评审执行身份也不同。保存两类标识和两份输入清单；若发生串用，废弃该轮结果并用新评审上下文重做。
2. **启动响应丢失**：首次启动已经落盘但调用方没有收到响应时，以相同执行身份和相同任务包重试，预期返回原交接且不新增已用身份；任一业务输入变化时拒绝复用。
3. **检查点响应丢失**：检查点已经落盘但调用方没有收到响应时，以相同执行身份、业务内容、契约和未变化工作区重试，预期返回原检查点；工作区或内容变化时按当前执行内顺序生成下一检查点。
4. **显式中断**：任务上下文意外结束后查询交付项状态，预期仍能看到原活动棒次、执行身份和租约；确认无法继续时使用原身份提交 `interrupted`，恢复执行前工作区基线并释放租约。重复提交保持幂等。
5. **自评拒绝**：以实施者执行身份评审自己的候选，预期命令返回 `REVIEW_NOT_INDEPENDENT`，且候选仍为待评审。保存命令输出和候选状态；随后改由新的独立评审 Agent 验收。
6. **工作区保护**：活动切片期间在授权路径外选择版本控制可见、原先干净的文本文件，预存其原始字节后制造临时变更，预期提交返回 `WORKSPACE_SCOPE_VIOLATION`；按预存字节恢复后提交候选，再以同样方式制造临时变更，预期评审返回 `CANDIDATE_CHANGED`。保存两次错误输出、工作区摘要和恢复证明；两项探针完成后都必须证明文件的版本控制状态与探针前一致、文件内容与原值一致，旧候选发生漂移时按既有返修或释放规则处理。

正式发布前运行受影响的状态恢复、工作区保护和安装生命周期测试。宿主上下文隔离仍由主协调者负责，不能用声明两个执行身份或单元测试代替真实上下文隔离证据。

### 紧凑上下文摘要

状态查询默认不在顶层重复返回批准、切片和方案，也不返回修改占用的基线正文或版本管理快照。指定交付项时，必要占用身份位于 `delivery_view.trusted_machine_facts.modification_lease`；未指定交付项时使用顶层 `modification_lease`。身份包含交付项、切片、工作区、状态、持有执行身份与当前候选标识，未占用时为 `null`。

`workflow-status --feature-id <id> --include-details` 在相同默认结果上增加 `details`：完整 `workflow_state`（含任务包、契约历史、检查点和评审记录）、完整 `modification_lease`（含基线和恢复数据）以及 `final_blockers`。未指定交付项时只展开全局占用。两种模式都执行原有检查，展开不放宽阻断、不改变状态、不授予恢复或释放权限；日常协调不读取展开明细。

进入最终批准或冻结前，影响当前推进的执行阻断直接显示在默认交付视图中。另一交付项仍占用工作区时，不提示最终批准或冻结，也不新增人工批准点；原占用释放后重新查询即可恢复下一动作。

```powershell
python Tools/FeatureArchive/feature_archive.py context-summary `
  --feature-id building-interaction `
  --action implementation `
  --role coordinator
```

必须显式声明 `--role {coordinator,cold-read,design-review,implementation,review,final-review}`。实施和候选评审角色还必须提供当前棒次的 `--execution-id`；冷读角色必须由主协调者以 `--entry-question` 提供当次入口问题，并重复使用 `--allowed-material` 精确列出该动作允许读取的全部正式材料；存在已接受候选的最终验收角色必须以 `--integration-confirmation` 提供当前集成确认文件路径。这些临时定位输入只参与当次投影，不写入档案。交接准备依据内部 `task_message_fields` 合同生成最小任务消息，不把字段说明投递给下游；最终验收一次返回全部已接受候选和对应评审。只有批准、新鲜度、棒次、执行身份、任务包、候选或集成确认等必需输入全部有效，才返回 `role_view`。任一输入无效，或存在尚未闭环的实施、评审、返修棒次时，只返回稳定 `role_blocker` 和回到主协调者的下一机械动作，不返回部分角色合同。

协调者继续取得完整全局交付视图和动作路由。下游经过同样检查后，`delivery_view` 仅返回当前阶段和阻断，不含全局方案、其他切片结论或调度动作；阻断时仍提供原因和交回协调者的下一动作。当前角色的任务包、候选、评审、集成确认、来源复核和停止条件保持完整。实施写入范围只在任务包的 `write_scope` 中提供，不再复制到交接外层；额外劳动记录只附入口、定位和触发条件，需要填写规则时读取 `workflow-rules --action friction`。

协调者角色只有在当前动作为调查时才获得并行事实取证权限；其他动作明确禁止派生并行分支。调查分支只读返回证据，由主协调者等待全部结果后串行收敛。独立冷读、设计评审和候选验收分别形成单一结论，保持单线交接；具体材料与停止条件由对应阶段规则投递。

动作支持需求、调查、设计、计划、实施、验证、冻结和清理对应的英文常量。输出契约版本保持为 `2`；非协调角色的通用建议材料保持为空，正式业务材料由角色合同精确引用，实施和评审可围绕当前问题只读调查相关项目事实。需要独立读取正文的设计评审和最终验收材料提供可解析路径；设计评审只要求设计入口已确认且不是待补充占位，不强制固定标题。摘要是现有正式事实源的只读投影，不创建状态，也不构成额外读取授权。

确定性命令异常会在正式档案根的父目录尽力追加结构化 `friction.jsonl`。同一交付项、操作、错误、目标和输入只形成一个未关闭问题；输入改变后的再次失败追加为同一问题的新尝试，对应操作成功后自动追加关闭事件。预期阶段门禁只记录为可观察保护事件，不计为未关闭摩擦；业务内容评审未通过和用户主动中断也不记为工作流故障。日志不属于正式交付档案，不参与校验、审批、新鲜度或冻结；读取或写入失败只返回警告，不阻断原工作流。

### 任务包上下文材料

任务包必须声明 `context_contract_version: 2`，材料采用结构化形式：

```json
{
  "context_contract_version": 2,
  "context_materials": [
    {
      "source": "building-interaction.requirements.overview",
      "purpose": "读取验收条件",
      "mode": "sections",
      "sections": ["当前摘要", "验收条件"]
    },
    {
      "source": "building-interaction.design.overview",
      "purpose": "读取完整批准方案",
      "mode": "full"
    }
  ]
}
```

`sections` 模式要求非空且不重复的精确 Markdown 标题；`full` 模式禁止声明 `sections`。合同版本缺失、旧版本、字符串列表或未知字段直接失败，不提供迁移提示或整份读取回退。

## 校验命令

```powershell
python Tools/FeatureArchive/feature_archive.py validate `
```

校验以每个功能的 `feature.json` 和全部 Markdown 文档元数据为事实源，不读取任何派生依赖表。校验范围包括：

- 生命周期清单、唯一严格合同、工作流状态、固定六层目录、必需总览和文档元数据结构；
- 功能内文档标识唯一性、缺失依赖和自依赖；
- 多文档循环依赖；
- 六层职责的依赖方向，允许从任意上游类别合法跳层；
- 只允许 `requirements.terminology -> requirements.overview` 这一种同类别依赖；
- 跨功能硬依赖。跨功能关系必须写入非阻塞的 `related_documents`。
- 术语表固定章节、首选术语、状态、定义、别名唯一性和候选/已确认边界；
- 粗体术语链接必须指向当前交付项的已确认术语；
- 普通相对 Markdown 文档链接和标题锚点必须存在。代码围栏、行内代码、图片和外部 URL 不参与链接校验。

`dependencies` 和 `related_documents` 均接受行内列表或 YAML 块列表。示例：

```yaml
dependencies:
  - building-interaction.requirements.overview
related_documents: [combat-report.design.overview]
```

校验成功只向标准输出返回统计信息，不会创建索引或修改任何档案文件。

明确携带单个交付项或同一交付项文档标识的状态、摘要、批准、新鲜度、切片、候选、返修和生命周期操作只校验该交付项，仍完整检查其结构、依赖、术语、链接和正文指纹。无关档案损坏不阻断目标功能操作；目标功能自身同类错误继续严格阻断。显式全局校验、索引和清理仍严格检查全部档案。

## 指定交付项只读审计

```powershell
python Tools/FeatureArchive/feature_archive.py audit `
  --feature-id building-interaction
```

审计只读取指定交付项，不扫描其他交付项。它先复用既有结构、元数据、依赖和链接校验，再比较该交付项全部 Markdown 正文与已确认指纹。

- 允许时退出码为 `0`，返回 `status=allowed`、`feature_id`、空 `blockers` 和规范 `archive_location`。
- 存在结构问题或未确认正文变化时退出码为 `1`，返回 `status=blocked`、`feature_id`、非空事实 `blockers` 和规范 `archive_location`。
- 每个 blocker 只包含 `code`、可选 `document_id`、`location` 和 `message`。
- 审计不判断正文变化属于语义变化还是非语义变化，不生成机器动作、整体档案指纹或额外宿主参数，也不修改档案。

普通 `audit` 的合同保持不变，不消费活动切片或修改租约，也不承担任务退出判断。活动执行与下一动作由 `workflow-status` 和角色投影读取，完成、失败或中断只通过显式业务操作登记。

## 索引重建命令

```powershell
python Tools/FeatureArchive/feature_archive.py rebuild-indexes `
```

命令会先执行与 `validate` 相同的全量校验，只有输入合法时才在共同父目录中生成：

- `feature-archive-dependencies.json`：机器依赖索引，包含功能、文档、正向依赖、反向依赖和非阻塞关联；
- `feature-archives.md`：人工全局索引，展示功能阶段、最近变化、过期项、阻塞项、冻结时间、待清理项和非阻塞关联。

两个索引完全由当前清单和文档元数据重新生成，不需要人工维护第二套依赖表。输出不包含生成时刻；输入不变时重复执行返回 `unchanged`，索引内容和文件修改时间均保持不变。索引重建只会替换上述两个派生文件，不会改写文档正文、修改语义版本或删除功能档案。

## 变更批次确认

AI 完成一轮文档编辑后，必须明确提交本批次是否改变语义。工具只接受显式判断，不会根据文字内容猜测业务含义：

```powershell
python Tools/FeatureArchive/feature_archive.py confirm-change `
  --document-id building-interaction.requirements.overview `
  --semantic-change true
```

同一批次涉及多份文档时可重复传入 `--document-id`。文件可以在确认前保存任意多次；只有执行 `confirm-change` 时才会按最终正文计算一次内容指纹和依赖影响。没有新的正文或依赖变化时，重复确认返回 `unchanged`，不会重复提升版本。

- `--semantic-change false`：更新最终正文指纹；若目标本身是待刷新文档，同时确认它当前消费的依赖版本。自身语义版本不变，也不会让下游过期。
- `--semantic-change true`：把本批次实际变化文档的语义版本补丁号提升一次，并且只把它们的直接依赖者标记为 `stale`。
- 待刷新文档确认后会恢复到必需的 `stale_from_status`；缺失该字段的文档直接拒绝。
- 一个中间层以非语义变化确认后，传播立即停止。只有中间层也以语义变化确认时，下一直接依赖层才会过期。
- 批次会原子更新涉及的文档元数据和功能清单 `updated_at`。派生索引仍通过独立的 `rebuild-indexes` 命令按需重建。

文档通过 `dependency_versions` 记录实际消费的直接依赖语义版本，例如：

```yaml
dependencies: [building-interaction.requirements.overview]
dependency_versions: {"building-interaction.requirements.overview": "0.1.1"}
```

## 待刷新队列和输入门禁

查看全局拓扑顺序的待刷新队列：

```powershell
python Tools/FeatureArchive/feature_archive.py refresh-queue `
```

可追加 `--feature-id building-interaction` 只查看一个功能。队列同时检查显式 `stale` 状态和依赖版本差异，因此不会把仅修改了状态文字但尚未消费新依赖版本的文档误判为可用。

在把文档作为计划或执行输入前，使用门禁命令：

```powershell
python Tools/FeatureArchive/feature_archive.py assert-fresh `
  --document-id building-interaction.design.overview
```

可重复传入 `--document-id`。任一目标过期时命令返回退出码 `1` 和 `STALE_DOCUMENT` 诊断，从而阻止该文档继续作为可靠执行输入。

## 业务术语与 Markdown 引用

术语表是交付档案的结构必需文件，正文内容按需维护；没有候选术语时保留空的“已确认术语”和“候选术语”章节，不制造无歧义术语。存在术语内容时使用固定 Markdown 结构：

```markdown
## 已确认术语

### NEW功能

- 状态：已确认
- 一句话定义：用于向用户提示存在新内容的功能概念。
- 相邻概念边界：不负责描述具体展示条件和清除时机。
- 别名：New、新内容提示。

## 候选术语
```

已确认术语在交付文档中使用粗体链接：

```markdown
**[NEW功能](../01-requirements/terminology.md#new功能)**
```

普通跨文档导航使用普通 Markdown 链接。链接只负责定位；是否发生语义传播仍只由文档元数据中的 `dependencies` 决定。

### 无语义术语重命名

```powershell
python Tools/FeatureArchive/feature_archive.py rename-term `
  --feature-id building-interaction `
  --from NEW功能 `
  --to 新内容提示
```

命令只接受当前活动交付项中的已确认术语，原子更新术语标题、粗体术语链接和别名，同时更新正文指纹但不提升语义版本。冻结和待清理档案拒绝写入。定义发生变化时不得使用该命令，应编辑术语表并通过 `confirm-change --semantic-change true` 进入既有刷新流程。

### 术语影响分析

```powershell
python Tools/FeatureArchive/feature_archive.py analyze-impact `
  --feature-id building-interaction `
  --term NEW功能
```

命令只读输出术语定义位置、精确正文引用、直接依赖者、潜在传递依赖者和当前档案是否可刷新。正文引用与文档硬依赖分别呈现，不会根据 Markdown 链接自动修改 `dependencies`。

## 生命周期转换和阶段门禁

功能生命周期保存在 `feature.json` 的 `lifecycle` 字段中，单份文档内容状态保存在各文档的 `content_status` 元数据中，两者独立管理。合法转换为：

- `draft -> active`
- `active -> validating`
- `validating -> active`
- `validating -> frozen`

示例：

```powershell
python Tools/FeatureArchive/feature_archive.py transition-lifecycle `
  --feature-id building-interaction `
  --to validating
```

进入 `validating` 前，需求、设计和计划类别中的文档不能处于 `stale` 或 `blocked`，也不能存在尚未消费的依赖版本变化。进入 `frozen` 前，六个类别中的全部文档不能处于 `draft`、`stale` 或 `blocked`，并且必须同时提交明确的验证状态：

```powershell
python Tools/FeatureArchive/feature_archive.py transition-lifecycle `
  --feature-id building-interaction `
  --to frozen `
  --validation-conclusion passed `
  --unverified-boundaries documented `
  --residual-risks none
```

- `validation.conclusion` 支持 `pending`、`passed`、`failed`，冻结要求为 `passed`。
- `validation.unverified_boundaries` 和 `validation.residual_risks` 支持 `pending`、`none`、`documented`，冻结前不能为 `pending`。
- 门禁失败返回 `LIFECYCLE_GATE_BLOCKED`，并在 `blockers` 中逐项列出文档路径、文档标识和阻塞原因。
- 转换成功会原子更新 `feature.json` 和功能入口中的生命周期摘要；全库合法时继续自动重建两个全局派生索引。若无关档案使全局索引严格校验失败，单功能转换保持成功并返回 `indexes=deferred`，随后显式索引命令仍报告该全局问题。
- 冻结和待清理档案是只读快照：不能确认文档变更、重新激活，也不能通过 `init` 补写缺失内容。新需求应使用新的功能标识创建新档案。
- `refresh-queue` 和语义变化传播会跳过冻结档案；后续 `rebuild-indexes` 只重建派生索引，不修改冻结档案正文或文档状态。

## 保留期检测

进入 `frozen` 时，工具会把转换时间确定性写入 `feature.json` 的 `frozen_at`。新档案默认使用 `retention_days: 30`，保留期边界按 `frozen_at + retention_days` 计算。自动测试或需要复现边界时，可以通过 `--now` 提交带时区的 ISO 8601 时间；省略时使用系统 UTC 时间。

```powershell
python Tools/FeatureArchive/feature_archive.py detect-cleanup `
  --now 2026-07-29T00:00:00Z
```

- 未满保留期的冻结档案保持 `frozen`。
- 满保留期时（包含精确边界）只把档案标记为 `pending_cleanup`，同步功能入口并重建派生索引，不删除任何目录或文件。
- 可以重复传入 `--feature-id` 只检测指定档案；省略时检测共同父目录中的全部档案。
- `transition-lifecycle` 同样支持 `--now`，便于确定性记录冻结时间。
- `validate` 和 `rebuild-indexes` 始终无删除副作用；只有后述独立清理命令带 `--execute` 时才可能删除。

## 清理预览和独立清理

清理必须明确选择目标。默认调用只输出执行前预览，不修改文件：

```powershell
python Tools/FeatureArchive/feature_archive.py purge `
  --feature-id building-interaction `
  --now 2026-07-29T00:00:00Z
```

预览中的每个目标都会显示 `delete` 或 `refuse`，并列出全部拒绝原因。只有以下条件全部满足时才能执行：

- 生命周期是 `pending_cleanup`，且重新计算后已经达到保留期；
- 没有 `retain_reason`；
- 六个业务类别没有 `draft`、`stale` 或 `blocked` 文档；
- 冻结验证结论为 `passed`，未验证边界和残留风险均已明确；
- 全局档案结构和引用合法，目标目录不是符号链接。

确认预览后，追加 `--execute` 执行删除：

```powershell
python Tools/FeatureArchive/feature_archive.py purge `
  --feature-id building-interaction `
  --now 2026-07-29T00:00:00Z `
  --execute
```

执行前会再次检查全部条件。多个目标采用全有或全无语义：任一目标被拒绝时不会删除任何目标。成功后工具立即重建两个派生索引，已删除档案不会继续作为活跃记录出现。

## 测试

```powershell
python -m unittest discover -s Tools/FeatureArchive/tests -v
```

状态与故障注入测试在隔离临时项目中调用当前命令合同，只替换固定安装入口所确定的项目位置；不得传入旧路径参数或绕过规范材料校验。安装位置、进程导入、参数拒绝和环境隔离由复制到 `Tools/FeatureArchive` 后启动的子进程测试覆盖。

### SVN 提交组

`sync-svn-changelist --feature-id <id>` 从规范档案、分享目录、当前切片实际差异与已登记产物取得明确文件清单。新增文件先纳管，再统一归组并回读校验。非 SVN 项目返回 `not-applicable`。在最终检查点前整理产品文件，收尾后补齐正式文档与状态；返回目录级操作及混合修改，不提交服务器。
