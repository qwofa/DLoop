# DloopUI 业务输入合同

调用者提交需求匹配、交互说明及可复核的来源定位。稳定 ID、证据摘要、截图请求、截图清单、数量和 `ui-model.json` 都由 module 管理。

## 当前编辑器采集

`ui-investigate` 默认只在临时输出目录生成当前请求；使用返回的 `capture_request`，不要自行填写 Prefab 身份或改写内部模型。在 Unity MCP 中确认打开的是返回的 `project_root`，然后执行已有截图入口，临时环境变量必须在结束或异常时恢复：

```csharp
var previous = System.Environment.GetEnvironmentVariable("DLOOP_UI_CAPTURE_REQUEST");
try
{
    System.Environment.SetEnvironmentVariable("DLOOP_UI_CAPTURE_REQUEST", @"<capture_request 的绝对路径>");
    Dloop.Editor.DloopUiCapture.CaptureFromEnvironment();
}
finally
{
    System.Environment.SetEnvironmentVariable("DLOOP_UI_CAPTURE_REQUEST", previous);
}
```

截图器负责临时采集场景并恢复原活动场景。采集后确认项目和原场景仍保持，再把真实生成的 `capture_manifest` 交给同一调研入口。单项失败保留截图器的真实错误；整体调用失败时停止，不构造清单冒充成功。规划截图不能证明实现后的交互或运行效果。

输入字段采用严格当前版本合同；不识别额外字段，不读取或迁移旧格式。

## 界面调研简报

修订时可在顶层增加 `refresh_prefabs: ["Assets/UI/ExamplePanel.prefab"]`，只对列出的本次已匹配 UI 发出采集请求，其余复用已有且身份、原图及定位依据有效的截图与节点区域。空列表表示全部复用；资产变化且未提交定位未变的核对时，会明确报出需要补拍或补核对的 UI。字段省略时按原流程采集全部已匹配 UI。简报仍提交完整需求集合；嵌套资源、画布或预览条件影响哪些页面由调用者核实后指定，不用单个 Prefab 摘要推断所有依赖未变。

仅修改文案或业务逻辑且截图定位未受影响时，先核对节点、布局、画布及相关依赖，再在调研简报中同时提交 `refresh_prefabs` 和 `unchanged_layout: [{"prefab": "Assets/UI/ExamplePanel.prefab", "reason": "具体改动及定位未变的核对依据"}]`。这是一份 AI 实施核对记录，不是用户批准门；不能用它掩盖位置变化。工具保留原图、节点区域及原始采集资产摘要，绑定本次已核对资产；HTML 和归档明确展示历史位置参考及原因。再次修改资产必须重新核对，身份变化、图片缺失或图片变化仍需补拍。正式材料中的交互与验证结论仍须跟随实际行为更新。

顶层格式：

~~~json
{
  "input_version": 1,
  "requirements": []
}
~~~

每条需求包含：

~~~json
{
  "key": "reward.receive",
  "name": "领取奖励",
  "statement": "用户点击领取后能看到最新剩余次数。",
  "source": {
    "path": "docs/reward.md",
    "locator": "领取奖励 / 第 18-20 行"
  },
  "target_clues": ["领取", "奖励界面"],
  "inference_level": "explicit",
  "confidence": "high",
  "match": {}
}
~~~

- `key`：本次交付项内唯一的小写业务键，可含数字、点、斜线、连字符和下划线。
- `source.path`：必须是初始化时已经登记的来源文件；相对路径以 Unity 项目根为准。
- `source.locator`：能在原材料中复核该需求的标题、段落、行号或 JSON 路径。
- `target_clues`：页面名、文案、功能名等搜索线索，可以为空数组。
- `inference_level`：`explicit` 或 `inferred`。
- `confidence`：`high` 或 `medium`。

`match` 必须是以下三种形式之一。

明确匹配到一个或多个真实 Prefab：

~~~json
{
  "status": "matched",
  "prefabs": [
    {
      "path": "Assets/UI/Reward/RewardPanel.prefab",
      "name": "奖励界面",
      "reason": "需求名称、界面标题和资产引用唯一对应。"
    }
  ]
}
~~~

没有找到：

~~~json
{
  "status": "not-found",
  "detail": "项目中没有奖励记录界面 Prefab。",
  "searched_paths": ["Assets/UI/Reward"]
}
~~~

存在至少两个真实候选但无法唯一选择：

~~~json
{
  "status": "ambiguous",
  "detail": "需求没有提供足以区分两个详情界面的上下文。",
  "candidates": [
    "Assets/UI/Season/SeasonDetail.prefab",
    "Assets/UI/Event/SeasonDetail.prefab"
  ]
}
~~~

`matched` 路径如果在执行时已经不存在，会自动转成 `prefab-not-found`；不会创建资产。候选或匹配路径越过当前项目 `Assets` 属于输入错误。

## 界面标注评审

顶层格式：

~~~json
{
  "input_version": 1,
  "investigation_token": "sha256:<调研结果摘要>",
  "outcomes": []
}
~~~

调研 token 绑定当前需求和 Prefab 匹配结果，以及已登记需求原文、截图清单、原始截图的内容摘要；任一内容变化时旧 token 失效，相同内容重复调研时保持稳定。`outcomes` 是当前完整判断集；删除一项表示撤销该判断。一个需求匹配多个成功截图的 Prefab 时，每个“需求 × Prefab”组合都必须分别形成修改、复用或目标不可见结果。

### 修改

~~~json
{
  "key": "reward.receive.button",
  "requirements": ["reward.receive"],
  "prefab": "Assets/UI/Reward/RewardPanel.prefab",
  "result": "change",
  "title": "领取按钮",
  "target": {
    "region": "<调研结果中的 object_id>",
    "label": "领取按钮区域"
  },
  "instruction": "接入领取意图，并在成功结果返回后刷新剩余次数。",
  "expected": "用户点击领取后能看到最新剩余次数。",
  "inference_level": "explicit",
  "confidence": "high"
}
~~~

`target` 也可使用截图坐标：

~~~json
{
  "rect": {"x": 100, "y": 220, "width": 180, "height": 72},
  "label": "领取按钮区域"
}
~~~

矩形必须完全位于真实截图内。

### 复用

~~~json
{
  "key": "reward.title.reuse",
  "requirements": ["reward.title"],
  "prefab": "Assets/UI/Reward/RewardPanel.prefab",
  "result": "reuse",
  "title": "奖励标题",
  "target": {
    "region": "<调研结果中的 object_id>",
    "label": "奖励标题区域"
  },
  "inference_level": "explicit",
  "confidence": "high"
}
~~~

复用不接收 `instruction`、`expected` 或保留行为列表。module 从关联需求生成可观察结论，并固定“无需产品修改”。

### 目标不可见

~~~json
{
  "key": "reward.notice.not-visible",
  "requirements": ["reward.notice"],
  "prefab": "Assets/UI/Reward/RewardPanel.prefab",
  "result": "target-not-visible",
  "title": "奖励提示",
  "detail": "真实截图中没有可可靠定位的提示区域。"
}
~~~

目标不可见只能引用已经成功截图、且与需求匹配的 Prefab。同一需求中的部分目标不可见，不影响其他可见操作分别标注；这一缺口记录不能代替最终交互说明。

## 交互补全与最终交付

每个独立操作或可观察触发单独提交一条 `change` 或 `reuse`，保留各自的条件与结果。同一功能点的多条操作使用相同的 `feature_point: {"key": "reward.action", "title": "领取奖励"}`，输出时共用一个编号与说明卡片。例如翻译和还原归为同一功能点。未指定时，相同目标节点或矩形自动归组；不同功能点用不同的 key 区分。内部草稿可以不完整；最终交付每条必须增加以下 `interaction`：

~~~json
{
  "conditions": "服务端返回可领取，且剩余次数大于零。",
  "action": "点击领取按钮。",
  "feedback": "请求期间禁用按钮，失败后恢复并显示原因。",
  "status": "verified",
  "detail": "已核对成功刷新、重复点击和失败恢复的实际验证记录。",
  "required_for_acceptance": true,
  "evidence": [
    {"path": "Assets/Scripts/RewardPanel.cs", "locator": "领取请求与回调处理"},
    {"path": "docs/validation/reward.md", "locator": "成功、失败与重复点击验证"}
  ]
}
~~~

结果继续使用 `expected`；复用项由关联需求生成结果。状态只能是 `verified`（已实现并验证）、`unverified`（尚未验证）、`not-implemented`（尚未实现），必须按真实证据填写。`unverified` 本身不证明实现已完成，`detail` 应分别说明已知实现情况、尚缺实现依据及未完成的验证；代码存在不等于运行验证通过。必要验收项尚未实现或未验证时只能发布草稿，不能发布最终就绪材料。只有已明确延期或不属于本期必要验收的项才能设 `required_for_acceptance: false`，并在依据与说明中记录原因。

隐藏、动态生成、无可靠位置或截图失败的交互仍保留完整文字，用以下目标代替矩形：

~~~json
{"label": "领取成功提示", "unavailable_reason": "成功返回后才显示，当前静态截图无法定位。"}
~~~

无匹配 Prefab 且已登记调研缺口的需求可用 `prefab: ""`。不能为了得到编号而虚构坐标。已有截图的定位受布局变化影响时更新截图，无法获取时使用文字目标说明缺口。

候选已接受后，在评审顶层增加：

~~~json
"delivery_review": {
  "requirements_check": "已逐项从完整原始需求核对实际实现、覆盖结果和缺口。",
  "implementation_check": "已从相关实际实现反查条件、操作、反馈、结果和遗漏说明。",
  "evidence": [{"path": "docs/validation/reward.md", "locator": "整体核对及未验证边界"}]
}
~~~

证据文件必须真实存在，定位须可复核。工具自动绑定当前已接受候选，以及归档内完整需求与设计正文。任务交互材料由候选入口交接，正式生成自动汇总；材料返修使用下述 `material_updates`，不重复抄写完整 `outcomes`，不能只改 HTML 或复用旧验收。机器检查验证结构、引用和是否过期，语义完整性由实施者、独立评审及最终双向核对负责。

## 任务材料交接

`prepare-action-input --input-kind task-package --package-id <任务标识>` 为 UI 任务增加 `delivery_requirements`。每个已登记验收场景至少有一项要求；任务包所属切片就是材料负责方。例：

~~~json
"delivery_requirements": [{
  "key": "panel.result",
  "scenario": "与本任务验收场景原文一致",
  "materials": ["interaction", "ui-location", "screenshot", "verification"],
  "required": true
}]
~~~

材料类型依次表示交互说明、真实元素定位、原始截图和验证依据。纯后端任务可只声明验证依据；必要性由实际验收范围决定。`required: false` 必须另填非空 `reason` 说明范围或延期依据，不能为过门禁降低要求。

`prepare-action-input --input-kind candidate --execution-id <执行身份>` 自动列出必要材料引用。完成执行时，原候选字段之外增加：

~~~json
"delivery_materials": [
  {"requirement": "panel.result", "kind": "interaction", "path": "05-implementation/panel-interactions.json", "locator": "界面操作与结果"},
  {"requirement": "panel.result", "kind": "ui-location", "path": "05-implementation/panel-interactions.json", "locator": "真实节点关联"},
  {"requirement": "panel.result", "kind": "screenshot", "path": "02-investigation/ui-captures/<实际原图文件>", "locator": "当前界面"},
  {"requirement": "panel.result", "kind": "verification", "path": "06-validation/panel-test-result.txt", "locator": "操作结果验证", "status": "passed"}
],
"additional_delivery_requirements": []
~~~

- 相对文件路径先按当前交付档案解析，再按任务工作区解析；也可提供绝对路径。`locator` 是材料内的具体位置，不能空白。同一要求的同一类型只登记一份文件，多项内容放入该文件；不同类型可引用同一文件。
- `interaction` 和 `ui-location` 文件使用既有评审格式：`input_version`、当前 `investigation_token`、本任务非空 `outcomes`，不含 `delivery_review`。交互必须有完整 `interaction`；定位必须引用真实采集节点，或明确 `unavailable_reason`，不能以随意矩形代替元素关联。此时只检验本任务文件，不要求其覆盖其他任务。
- `screenshot` 必须是当前调研登记且内容一致的真实截图。不能用任意图片或另制预览冒充。无法采集时在已批准范围内明确材料缺口与是否必要，不能悄悄漏交。
- `verification.status` 为 `passed`、`failed` 或 `unverified`。候选可如实交回未验证材料；最终交付前必要验证必须通过，且交互本身的必要验证状态也必须通过。
- 同范围新增材料要求放入 `additional_delivery_requirements`，结构同任务包要求，必须引用原任务已登记场景且使用新 key；不能覆盖原要求。业务范围变化仍走既有切片流程。

候选保存实际文件摘要及相关 UI 的资产、截图和节点关联，独立评审上下文自动包含材料路径。最终使用 `prepare-action-input --input-kind ui-delivery` 生成如下输入，再由 `ui-publish` 汇总已接受任务：

~~~json
{
  "input_version": 1,
  "delivery_review": {
    "requirements_check": "完整需求到实际实现的核对结论",
    "implementation_check": "实际实现到交互说明的核对结论",
    "evidence": [{"path": "06-validation/delivery-check.md", "locator": "整体核对结果"}]
  },
  "material_updates": []
}
~~~

生成器返回已交材料清单及填写说明，不覆盖已编辑的输入。正式输入不再抄写已交接的 `outcomes`；工具按操作业务 key 合并，同 key 内容冲突会报出具体交互。没有任务交互材料时仍可提交整体 `outcomes`，但不能因此省略已声明的任何任务材料。

只修材料时，在 `material_updates` 填原候选引用字段并增加 `package_id`，如：

~~~json
{"package_id": "panel-slice", "requirement": "panel.result", "kind": "interaction", "path": "05-implementation/panel-interactions-revised.json", "locator": "补全失败反馈"}
~~~

未指定的材料继续复用，更新引用写入现有 `ui-model.json` 的 `delivery_materials` 投影并绑定候选摘要，不另建管理状态或审批流程。重新生成仍使用这些引用；产品候选变化后改用新候选交接。文件已变化或关联 UI 已变化时明确报告需要更新的材料，新交互或定位文件须携带当前调研 token。最终依据绑定全部交接文件；它们再次变化会使旧交付核对失效。未受影响 UI 可沿用原绑定，不因其他页面调研变化强制重写整份材料。

`ui-publish` 固定使用载荷内 `runtime/templates/ui-delivery.html` 生成独立 HTML，并从同一数据生成仅含编号的 SVG 与 Markdown 表格。编号固定放在截图外侧，用引线指向目标，不盖住控件。页面直接展示左侧 UI 截图及右侧常驻详情抽屉，每次只展示选中功能点的说明，来源与验证细节在“核对依据”中默认展开，可手动收起；不显示通用页头、草稿横幅或重复免责声明。不得临时另写模板或把文字拼进图片。HTML 必须在真实浏览器验证桌面和窄窗口的可读性、缩放拖动及双向选择。

## 元素定位展示

HTML 自动从真实预制体路径显示 UI 文件名，从 `target.region` 对应的节点显示元素名与完整层级路径；资产和节点路径均可复制。仅指定矩形、没有实际节点关联时，不猜测元素名。

一个功能点涉及其他元素时，在对应 `change` 或 `reuse` 条目中增加可选字段：

~~~json
"related_elements": [
  {"region": "当前采集中的结果文本区域 object_id", "role": "显示结果"}
]
~~~

关联元素必须属于同一预制体的当前采集记录，名称和路径由工具读取。相同节点合并展示用途，仍使用原功能点编号。未定位的动态目标保留原有缺口说明。

## 结果和错误

调研成功返回：

- `ready_for_review`：至少有一张真实截图，需要形成视觉判断；
- `ready_to_publish`：全部需求已经形成前置跳过，可以提交空评审。

发布成功返回：

- `complete`；
- `completed_with_skips`；
- `no_applicable_prefab`。

这些状态表示采集与标注覆盖，不代表业务完成。返回 `delivery_ready: false` 时是内部草稿；`delivery_ready: true` 表示正式核对材料通过结构与当前绑定检查，仍需实际展示、用户验收。HTML 位于 `06-validation/ui-delivery.html`。

Prefab 不存在、歧义、单项截图失败和目标不可见是正常结果，最终仍需文字覆盖。来源未登记、输入字段错误、路径越界、Unity 整体不可用、截图协议损坏或 token 过期会拒绝操作；正式交付还检查完整交互、必要验证及已接受实现，不提交半份模型或页面。


## 开工清单

调查后用 `prepare-action-input --input-kind ui-baseline --feature-id <id>` 取得清单；提交使用 `ui-baseline --feature-id <id> --input <文件>`。该命令保存不完整清单和缺项报告，不因保存成功就授予实施资格。每次补充修改同一输入后重交，不直接编辑工具生成的正式清单。

顶层为 `input_version: 1` 和 `items`。每项用 `requirement_key` 对应当前完整调研中的需求键，且包含 `prefabs`、`protocols`、`configurations`、`requirement_sources` 四个材料数组。全部需求均须覆盖；遗漏或空数组形成缺项。

每份材料包含：

- `status`：`verified`（已核实）、`missing`（缺失）、`ambiguous`（待选择）或 `not_applicable`（不涉及）。不支持计划新增或延期占位。
- `reference`：已核实项填写真实本地文件路径，可为项目相对路径；网页资料先摘录为本地文件。缺项或歧义记录已经搜索的位置及候选。不涉及可留空。
- `purpose`：文件内具体节点、接口、字段或文档章节，业务用途和关键含义；缺项写明影响以及需用户补充什么。不涉及必须有理由，且只用于协议和配置，不能与同类其他条目混用。

预制体必须与当前需求调查匹配，且文件真实存在。需求描述必须具备触发条件、操作、预期结果及必要异常，不能仅凭存在一个文件就登记核实。AI 应核对字段和节点是否足以支撑所需行为。尚未明确的内容登记为缺失或歧义。

工具返回 `blockers` 集中说明缺项；为 `ready` 时同时返回确认材料路径与 `reviewed_digest`。`approval_preserved: true` 表示当前确认仍有效，按返回的下一动作继续；否则按业务场景展示完整清单，取得实际回复后使用现有阶段批准入口，传入 `--stage ui-baseline --decision approve --reviewed-digest <摘要> --user-confirmation <回复定位与原文>`。拒绝时使用 `reject`，修改方案后重新展示，不能复用旧回复。

提交默认按业务变更处理。仅修正 `purpose` 的错别字、标点或措辞且业务含义不变时，由 AI 明确传入 `--semantic-change false`，保留业务依据摘要和原有用户确认；工具不自行判断自然语言是否同义。此方式要求已有清单，且需求事实、材料引用、数量及状态不变。更换节点、接口、字段、章节或用途含义仍属于业务变更，不得声明为非语义修订。未确认、已拒绝或已失效的确认不会因此获准；同一输入原样重交仍保留当前状态。

确认绑定需求语义版本、调查中的需求及预制体匹配，以及四类材料引用和用途。业务材料或含义变化时重新提交；只调整内部实现、标注或截图不更新开工清单。更换依据后暂停实施，状态、开工、返修与实施交接检查阻止继续使用失效确认。
