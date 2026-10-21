# DLoop

**让 AI 持续推进 Unity 长任务，你只需关注关键决定与交付结果。**

从需求梳理、项目调查到实施、独立评审和验证，减少反复催促、手动交接和需求遗漏。适合需要分步实现与验收的功能开发；局部小改直接使用编辑器 Agent 即可。

## 选哪个入口

| 你的任务 | Codex | Cursor |
|---|---|---|
| 功能开发：整理需求，分步实施与验证 | `$dloop` | `/dloop` |
| 界面开发：在上述流程中增加真实截图、交互说明与集中验收 | `$dloop-ui` | `/dloop-ui` |

## 安装

**需要：** Windows、Python 3.10+、Git 客户端、Codex 或 Cursor，以及已连接 Unity MCP 的 Git / SVN 项目。首次使用先看[依赖与连接检查](docs/dependencies.md)。

在 Unity 中打开目标项目，然后在**项目根目录**打开 PowerShell，运行：

```powershell
irm https://raw.githubusercontent.com/qwofa/DLoop/main/install.py -ErrorAction Stop | python -
```

看到 `DLoop installed and verified.` 即安装成功，再用 Codex 或 Cursor 打开该项目。安装不会自动配置 Unity MCP。[离线安装、升级与卸载 →](docs/installation.md)

## 开始使用

选择入口，附上需求文档或直接描述目标：

```text
$dloop
根据附件需求完成功能，先整理业务场景供我确认，再推进开发与验证。
```

涉及界面改动时：

```text
$dloop-ui
根据附件需求修改界面，结合现有实现补齐交互，完成后展示实际结果供我验收。
```

**Cursor 用户：** 将示例首行换成 `/dloop` 或 `/dloop-ui`。

**你会看到：** 需要确认的需求与关键取舍，以及交付时的实际结果、需求差异和未验证内容。已确认范围内持续推进，出现影响预期的变化或真实阻塞时再交给你决定。

<details>
<summary><strong>界面任务怎样审核与验收？</strong></summary>

- **开工前：** AI 查找已有 Prefab，核实需求、协议和配置，列出缺项与可独立开工的部分供你确认。不清楚资产位置时，提供界面名称和项目位置即可。
- **交付时：** 用统一 HTML 展示真实截图与交互说明，点击标注可定位对应说明；列清操作、反馈、生效条件及未完成、未验证内容，并提供游戏内验收入口。
- **验收后：** DLoopUI 取得你对当前交付的验收后封存；返修后重新验收。普通 DLoop 完成交付验证后自动封存，人工验收按需进行。

</details>

<details>
<summary><strong>怎样继续、调整或暂停任务？</strong></summary>

回到项目后，使用原入口，说明功能名称和意图：

```text
$dloop
继续“<功能名称>”任务，从当前进度往下推进。
```

也可以直接说“先暂停，保留当前成果”，或指出需要调整的行为。工作流会读取已保存的进度，整理调整影响，并重新呈现需要确认的部分。界面任务继续使用 `$dloop-ui`；Cursor 使用对应的 `/` 入口。

</details>

## 按需查阅

[依赖与连接](docs/dependencies.md) · [安装与卸载](docs/installation.md) · [工作流与命令](src/FeatureArchive/README.md) · [结构化工具连接](docs/structured-tools.md) · [版本记录](CHANGELOG.md) · [测试说明](docs/ci.md)

问题与反馈：[GitHub Issues](https://github.com/qwofa/DLoop/issues)。采用 [MIT 许可证](LICENSE)，外部依赖见[许可范围](docs/dependencies.md#许可范围)。
