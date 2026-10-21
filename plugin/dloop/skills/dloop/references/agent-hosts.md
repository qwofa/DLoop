# 宿主使用约定

DLoop v4.0.3 共用一份工作流、项目工具、档案和安装锁。宿主负责模型连接、本地命令、文件访问与原生子 Agent；DLoop 不启动外部 agent 进程，不管理模型密钥或会话数据库。

## 显式入口

| 宿主 | 普通任务 | 界面任务 | 项目 Skill 位置 |
|---|---|---|---|
| Codex | `$dloop` | `$dloop-ui` | `.agents/skills/` |
| Cursor | `/dloop` | `/dloop-ui` | `.agents/skills/` |
| Claude Code | `/dloop` | `/dloop-ui` | `.claude/skills/` |
| OpenCode | `/dloop` | `/dloop-ui` | `.agents/skills/`（也可发现 `.claude/skills/`） |

只在用户显式选择后启动。保留 Skill 的 `disable-model-invocation`，普通需求或 UI 关键词不代表启动授权。

OpenCode 复用现有目录，不再复制第三份 Skill。两处同名内容由同一安装器保持一致；使用支持 `opencode/slash` 和 `opencode/autoinvoke` 元数据的版本，分别显示显式入口并关闭自动选择。首次使用核对 `/dloop` 和 `/dloop-ui` 可选；不显示时先升级宿主，不用普通自然语言隐式启动。

Claude Code 的两个 Skill 由安装器从共享源码复制，和 `.agents/skills/` 内正文保持一致；不手改其中一份。规则读取仍使用项目的 `.agents/skills/dloop/`，不要删除该目录，也不要把 Skill 单独升级到与项目工具不同的版本。

## 必需能力

协调者须能读取项目文件、运行 Python/Git 及项目所需的 SVN、创建新的原生子 Agent 上下文并等待其最终结果。DLoop MCP 可选：已连接时先核对返回项目根，未覆盖动作与未连接时均沿用现有 CLI 合同。Unity 操作按当前任务核对 Unity MCP；DloopUI 还须能读取实际截图并展示生成的验收材料。

缺少哪项就报告具体能力和受影响操作。独立评审不可用时保持未验证，不以协调者自评、改执行身份或外部命令行代理代替；不为通用接入增加新的审批阶段。

## 原生交接

1. 主协调者在自己的上下文准备交接，材料完整后只传递工具返回的任务消息、读取命令及当前角色必要身份。业务依据通过正式文件读取，不复制实施对话。
2. Claude Code 使用不继承父对话的新子 Agent，不能用继承整段对话的 fork 作为独立评审，也不能复用实施会话作为评审；Codex、Cursor 继续使用原有原生交接。
   OpenCode 使用原生通用子 Agent 创建新任务上下文；实施与独立评审各自新建，后续补材料只恢复对应的原任务。不要用只读探索角色承担必须登记工作流结论的评审。
3. 实施者补材料或重试时在原上下文继续，沿用原执行身份；确需返修时按现有返修流程取得新身份。宿主的会话标识只用于继续该上下文，不替换 DLoop 返回的业务执行身份。
4. 评审和冷读只读产品文件；允许通过当前角色合同运行工作流工具登记结论。不要选用禁止一切命令、因而无法读取角色材料或登记结果的受限角色。子 Agent 必需的命令及 MCP 权限应在正常宿主权限配置中可用，不关闭全局权限检查。
5. 主协调者等待真实最终结果后按工作流状态继续。执行中断、工具失败或只返回文字摘要不等于候选或评审已提交；响应不确定先查状态。保留现有单线实施与修改租约。

下游角色不再读取常驻 Skill，也不派生下一位实施者或评审者；交接由主协调者完成。宿主无法恢复原上下文时按现有中断处置交还协调者，不在新上下文冒用原身份。

## 模型配置与验收

DeepSeek、Claude 等模型由所选宿主配置。DLoop 不增加模型名称分支、密钥文件、模型网关或自动切换。使用 DeepSeek 时也须核对实际工具调用、原生子 Agent 和图片输入能力；成功登录或普通文字回复不证明完整工作流可用。

新宿主首次验收应在临时项目完成显式入口、状态查询、实施、独立评审及最终验证；DloopUI 另查真实截图与交互。没有实际执行的部分保留未验证，不宣称所有版本或模型都已兼容。

依据：[Claude Code Skills](https://code.claude.com/docs/en/skills)、[Claude Code 子 Agent](https://code.claude.com/docs/en/sub-agents)、[OpenCode Skills](https://opencode.ai/docs/skills/)、[OpenCode Agents](https://opencode.ai/docs/agents/)、[DeepSeek 宿主配置](https://api-docs.deepseek.com/guides/coding_agents/)。
