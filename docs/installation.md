# 安装与卸载

## 源码安装

当前源码为 v3.8.1 冻结版。源码安装时，在 Git/SVN 项目中准备 `.scratch` 忽略规则，再从本版源码目录运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Target "D:\projects\test-project" -Version v3.8.1
```

同一命令追加 `-Verify` 校验，追加 `-Uninstall` 卸载。需要在线安装或 ZIP 安装时使用下文对应入口。

## 一行安装

当前使用环境为 Windows、Python 3.10+、Codex、Unity MCP、Git 客户端和 Git/SVN 项目。Unity MCP 不限实现或版本；首次使用先按[依赖与连接说明](dependencies.md)准备环境，确认 Codex 能访问当前项目的编辑器。SVN 项目也需要 Git 来保存本地阶段快照。

在目标项目根目录打开 PowerShell：

```powershell
irm https://raw.githubusercontent.com/qwofa/DLoop/main/install.py -ErrorAction Stop | python -
```

一行入口安装公开主线指定的发行版；本版入口固定下载含路径及 SVN 编码修正的安装包标签 `installer-v3.8.1-r2`，工作流载荷仍为冻结版 v3.8.1。原 `v3.8.1` 标签保持不变；离线安装请使用上述安装包标签或当前主线源码，避免重新取得未修正的安装器。模板随共享工具一起安装。

## 本地或离线安装

在本版源码目录使用当前源码 ZIP 安装：

```powershell
python install.py --target "D:\projects\my-project" --archive "D:\downloads\DLoop-3.8.1.zip"
```

ZIP 使用带顶层目录的源码包布局。将本版修改提交后，可用 `git archive --format=zip --prefix=DLoop-3.8.1/ --output=DLoop-3.8.1.zip HEAD` 生成；该命令不包含未提交改动。存在未提交修改时使用上文的源码安装。不要用旧标签或其他版本的 ZIP 替代当前源码。

安装完成会显示 `DLoop installed and verified.`，接着在目标项目的 Codex 中使用 `$dloop` 或 `$dloop-ui`。

## 安装会做什么

- 在项目内安装两项 Skill、共享工具和 Unity 编辑器截图包。
- Git 项目已有有效规则时保持原样，否则在目标项目的 `.gitignore` 末尾补入 `/.scratch/`。
- SVN 项目保留原有 `svn:ignore` 内容，必要时追加 `.scratch`。
- 安装失败时恢复本次准备修改的规则；下载失败时不改项目。
- 安装成功后执行发行版的只读校验，最后清理临时下载文件。

Git 忽略规则可能成为一项本地改动；入口不暂存或提交它。安装不会替你配置 Codex 或 Unity MCP。

入口支持全新安装、同版本重装，以及从较旧安装升级；拒绝降级。升级前先停止旧版命令和 Agent，无需先完成旧作业。安装器验证旧载荷归属后整体归档旧活动材料并释放占用，保留项目代码改动；失败恢复原现场。同版本重装保留现有作业。重复安装保留已有忽略内容，不重复添加规则。

新材料位于 `.scratch/dloop-v3/v3.8.1/`；旧材料位于 `.scratch/dloop-history/<旧版本>-<时间>-<唯一标识>/runtime/`，外层说明标记整批作业已失效。历史不再参与活动索引、恢复、批准或清理；可直接打开文档和附件查阅。已卸载遗留材料的来源版本标为 `uninstalled`，新装时同样隔离。详见[版本交替说明](v3.7.6-version-handoff.md)。

## 常见问题

**下载返回 404 或访问失败**

确认网络能够访问 GitHub 和其源码下载服务；受限环境可以使用已取得的本版源码和本地 ZIP。

**找不到 Python**

确认在同一个 PowerShell 中可以运行 `python --version`，并返回 3.10 或更新版本。本入口不自动安装 Python，下载来源见[依赖说明](dependencies.md#需要准备什么)。

**安装成功，但 Codex 没有显示入口**

确认打开的是安装目标项目，且项目内存在 `.agents/skills/dloop/SKILL.md` 和 `.agents/skills/dloop-ui/SKILL.md`。再按[首次使用检查](dependencies.md#安装后的第一次使用)重新加载项目。

**能开始任务，但无法访问 Unity**

检查 Unity 项目是否已打开，Unity MCP 是否已经连接到 Codex，以及连接目标是否为当前项目。按照[只读连接检查](dependencies.md#安装前的只读检查)确认项目身份；DLoopUI 还需要 MCP 能在编辑器进程内执行 C#、调用已安装截图器。安装校验只覆盖 DLoop 载荷，不代表编辑器连接也已验证。

## 手工验证和卸载

取得并解压对应版本的 DLoop 源码，在该源码目录运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Target "D:\projects\my-project" -Version v3.8.1 -Verify
```

卸载：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Target "D:\projects\my-project" -Version v3.8.1 -Uninstall
```

卸载移除本版管理的 Skill、工具和截图包，保留交付档案与项目忽略规则。受管文件被修改或无法确认归属时会停止并说明原因。
