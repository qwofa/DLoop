# 依赖、连接与首次检查

DLoop v3.7.6 面向 Windows 上的 Unity 项目，通过 Codex 组织开发工作。Unity MCP 按能力接入，不绑定供应商、包名、工具名称或某个版本。

## 需要准备什么

| 依赖 | 要求与用途 | 安装来源 |
|---|---|---|
| Windows 与 PowerShell | 当前安装入口只支持 Windows；安装、校验和卸载使用 PowerShell | Windows 自带的 Windows PowerShell；已有安装回归使用 5.1 |
| Python | 3.10 或更新版本，能在项目终端直接运行 `python`；执行安装入口和工作流工具 | [Python Windows 下载](https://www.python.org/downloads/windows/) |
| Git | 所有项目都需要命令行 Git；用于 Git 项目操作，也用于 SVN 项目的本地阶段快照 | [Git for Windows](https://git-scm.com/install/windows) |
| SVN | 仅 SVN 项目需要命令行 `svn`；仅有图形客户端且未安装命令行工具不够 | [Apache Subversion 客户端列表](https://subversion.apache.org/packages.html) |
| Codex | 能读取项目内 Skill、运行本地命令、使用 MCP，并启动独立评审上下文；已完成登录或其他可用的身份配置 | [Codex 官方文档](https://developers.openai.com/codex/) |
| Unity Editor | 使用目标项目要求的版本；随 DLoop 提供的截图包声明最低 Unity 2021.3，需要项目能正常编译 | 通过 Unity Hub 安装项目所需编辑器 |
| Unity UI 依赖 | DLoopUI 的 Prefab 截图使用 uGUI（`com.unity.ugui`）和图像编码模块（`com.unity.modules.imageconversion`） | Unity Package Manager；按当前项目的 Unity 版本解析 |
| Unity MCP | 满足下节能力要求，连接到当前项目已打开的 Unity Editor | 自行选择实现并遵循其安装说明 |

DLoop 的 Python 运行代码使用标准库，不需要额外执行 `pip install`。Unity MCP 自己可能需要 `uv`、额外 Python 包或其他服务，这些由选用的实现决定，不能视为所有 DLoop 用户都必须安装的依赖。DLoop 安装器只部署自己的 Skill、共享工具和截图包，不代装上述外部环境。

项目本身的渲染管线、字体、业务包和编译依赖仍由项目管理；安装 DLoop 不会替项目补齐这些内容。当前 UI 截图基于已有 uGUI Prefab，不据此承诺所有 UI 技术栈都有相同采集效果。

SVN 客户端还须能无损接收当前项目的文件名和提交组名称。英文 Windows 上采用旧式字符编码的命令行客户端可能把中文参数转换成问号；仅设置 Python 为 UTF-8 或能显示中文，不代表 SVN 已满足要求。请先在临时 SVN 项目核对中文文件纳管与分组；CI 的客户端编码设置及往返检查见 [CI 说明](ci.md)，本项目安装器不代为改变系统区域设置。

## Unity MCP 需要具备哪些能力

普通开发所需的场景读取、资产操作、代码修改和测试能力，取决于当前任务。DLoopUI 在采集真实 Prefab 材料时还需要以下能力：

1. 能识别连接的项目；多个编辑器同时打开时，能选择本次目标实例。
2. 能在该编辑器进程中执行 C#，调用随 DLoop 安装的截图器。仅能读取层级、获取编辑器窗口截图或运行系统终端，不足以替代这项能力。
3. 编辑器与 DLoop 工作流能访问同一份项目、采集请求和输出文件。远程 MCP 需要自行满足路径与文件访问条件；本版不提供远程文件同步。
4. 能返回真实的执行错误，并允许工作流检查生成的截图和清单。某个工具显示“调用成功”不等于已得到可用的截图材料。

具体调用合同见[当前编辑器采集](../plugin/dloop/skills/dloop-ui/references/model-contract.md#当前编辑器采集)。截图器负责临时采集环境及恢复；工作流按当前 MCP 提供的工具完成调用，不要求固定的 MCP 工具名称。

满足这些能力的其他实现可以接入，但仍应先完成下方检查；这里没有宣称所有实现或版本均已验证兼容。缺少必要能力时应明确报告，不以虚构截图、手工拼造清单或要求关闭正在使用的项目来绕过。

## 连接到 Codex

先按照所选 Unity MCP 的上游说明，在目标 Unity 项目内安装并启动它。采用本地 HTTP 的实现时，记录其实际显示的 MCP 地址；下面的 `8080` 只是示例端口。

已经安装 Codex CLI 的用户，可在 PowerShell 运行：

```powershell
codex mcp add unityMCP --url http://localhost:8080/mcp
codex mcp list
```

没有使用 CLI 时，也可在 Codex 的 MCP 配置中添加同一连接。使用 `config.toml` 的配置方式如下；已有同名配置时修改原项，不重复添加：

```toml
[mcp_servers.unityMCP]
url = "http://localhost:8080/mcp"
```

`unityMCP` 是此示例的连接名称，不是工作流限制。实际地址、认证和传输方式以所选 MCP 实现为准。采用标准输入输出连接时，按照上游提供的启动命令配置；不要把 HTTP 示例套用到其他传输方式。配置位置和可用选项见 [Codex MCP 官方说明](https://developers.openai.com/codex/mcp/)。

连接建立后重新打开目标项目任务；如工具仍未加载，重新启动 Codex，并确认 Unity MCP 服务和目标编辑器仍在运行。

### 可选示例：CoplayDev MCP for Unity

如果还没有选用实现，可以参考 [CoplayDev/unity-mcp](https://github.com/CoplayDev/unity-mcp)。这是连接示例，不是 DLoop 的唯一支持项，也没有要求安装某个固定版本。

1. 根据其[安装文档](https://github.com/CoplayDev/unity-mcp#quickstart)准备 Python 和 `uv` 等上游依赖；`uv` 的 Windows 安装方式见[官方说明](https://docs.astral.sh/uv/getting-started/installation/)。
2. 在 Unity 的 `Window > Package Manager` 中选择从 Git URL 添加包，使用上游文档给出的地址。当前示例为：

   ```text
   https://github.com/CoplayDev/unity-mcp.git?path=/MCPForUnity#main
   ```

3. 打开 `Window > MCP for Unity`，按该版本界面完成环境准备，启动服务并连接当前编辑器。使用 HTTP 时，将界面显示的实际 MCP 地址填入前面的 Codex 配置。
4. 执行下方只读检查，确认当前工具包含编辑器内执行 C# 的能力；部分版本可能需要启用对应工具组。

该地址跟随上游主分支。需要固定环境时，可自行锁定已经验证过的上游标签或提交，并保持服务端与编辑器端版本配套；DLoop 不规定该版本号，也不把升级后的兼容性视为已经验证。

## 安装前的只读检查

在目标项目根目录打开 PowerShell：

```powershell
python --version
git --version
```

Python 应为 3.10 或更新版本，两条命令都应正常返回。SVN 项目再运行 `svn --version --quiet`；使用需要 `uv` 的 MCP 时再运行 `uv --version`。安装依赖后若终端仍找不到命令，关闭并重新打开终端。

在 Codex 中打开目标项目，发送：

```text
请只读检查 Unity MCP 连接：读取当前连接的 Unity 项目路径、编辑器版本和活动场景，
确认它们与本次项目一致；再确认当前工具是否支持在该编辑器进程内执行 C#。
不创建或修改对象、资源、场景，不进入运行模式。
```

检查结果应能指出当前项目和场景。出现多个实例时先选择正确项目；连接不存在、编译未完成或能力不足时先修复环境，再按[安装说明](installation.md)部署 DLoop。

## 安装后的第一次使用

1. 安装程序显示 `DLoop installed and verified.` 后，确认项目内存在 `.agents/skills/dloop/SKILL.md` 和 `.agents/skills/dloop-ui/SKILL.md`。
2. 在 Codex 的同一个项目内确认能够选择 `$dloop` 和 `$dloop-ui`。Skill 的发现方式见[官方说明](https://developers.openai.com/codex/skills/)；没有显示时重新启动 Codex，并检查项目目录是否选对。
3. Unity 完成资源刷新和编译后，确认没有由 `Packages/com.dloop.ui-capture` 引起的编译错误。
4. 先在可丢弃的示例项目中，用 `$dloop-ui` 完成一个范围清楚、包含已有 uGUI Prefab 的小任务。检查真实截图、操作说明与最终 HTML 是否对应实际结果；必要实现或验证未完成时，材料应如实标明。

文件部署成功只证明安装校验通过，不证明 MCP 连接、编辑器编译或业务交互验证通过。

## 已有验证的范围

- 3.7.3 在隔离的 Windows 项目中完成实际安装、Codex Skill 识别、Unity 编译、MCP 采集、运行模式按钮交互、HTML 展示和卸载；完整范围见[发行验证记录](../CHANGELOG.md#发行验证)。
- 本次实测使用 Unity 2022.3.62f2c1、CoplayDev MCP for Unity 10.0.0；这是已验证的环境记录，不是 MCP 版本约束，也不代表所有实现或版本均兼容。
- 截图包声明最低 Unity 2021.3，真实采集测试夹具配置使用 Unity 2022.3.62f1，本次以 2022.3.62f2c1 执行 2 项真实编辑器测试并通过；声明、夹具配置和实际执行版本分别记录。
- 云端安装与发行检查在 Windows Server 2022、Python 3.10 执行；真实编辑器检查单独在本地运行。CI 的覆盖与允许跳过项见 [CI 说明](ci.md)。
- 当前不承诺 macOS、Linux、WSL 或远程编辑器组合的安装与采集兼容性；工作流版本固定在单次作业内，不支持跨版本继续旧作业。

## 许可范围

DLoop 的代码、Skill、模板和随仓库提供的说明采用根目录 [MIT 许可证](../LICENSE)。复制或再分发全部或实质部分内容，包括安装到项目中的副本时，需要保留版权和许可声明。

Python、Git、SVN、Unity、Codex 和所选 MCP 由用户另行取得，仍适用各自的许可与服务条款；DLoop 的 MIT 许可不替代它们的授权。本仓库不随包分发这些外部产品。
