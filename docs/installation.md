# 安装与卸载

## 源码安装

当前源码为 v3.9.4 冻结版。源码安装时，在 Git/SVN 项目中准备 `.scratch` 忽略规则，再从本版源码目录运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Target "D:\projects\test-project" -Version v3.9.4
```

同一命令追加 `-Verify` 校验，追加 `-Uninstall` 卸载。需要在线安装或 ZIP 安装时使用下文对应入口。

## 一行安装

当前使用环境为 Windows、Python 3.10+、Codex 或 Cursor、Unity MCP、Git 客户端和 Git/SVN 项目。Unity MCP 不限实现或版本；首次使用先按[依赖与连接说明](dependencies.md)准备环境，确认所选宿主能访问当前项目的编辑器。SVN 项目也需要 Git 来保存本地阶段快照。

在目标项目根目录打开 PowerShell：

```powershell
irm https://raw.githubusercontent.com/qwofa/DLoop/main/install.py -ErrorAction Stop | python -
```

一行入口在公开主线合并并发布 `v3.9.4` 标签后安装本版；此前使用上面的本地源码入口。

## 本地或离线安装

在本版源码目录使用当前源码 ZIP 安装：

```powershell
python install.py --target "D:\projects\my-project" --archive "D:\downloads\DLoop-3.9.4.zip"
```

ZIP 使用带顶层目录的源码包布局。本版修改提交后，可用 `git archive --format=zip --prefix=DLoop-3.9.4/ --output=DLoop-3.9.4.zip HEAD` 生成；未提交修改不会包含在 ZIP 中，开发期间使用源码安装。

安装完成会显示 `DLoop installed and verified.`，接着在目标项目的 Codex 中使用 `$dloop` 或 `$dloop-ui`，或在 Cursor 中使用 `/dloop` 或 `/dloop-ui`。

## 安装会做什么

- 升级到较新版本时，若旧安装中已登记的文件存在本地修改，列出全部变化路径，先完整备份旧载荷和原安装锁并核对摘要，再继续安装；无需先恢复原文件或修改安装锁。备份位于 `.scratch/dloop-install-backups/`，成功或失败均保留。
- 备份未完成不会替换旧安装；后续安装失败会恢复原文件、原安装记录及旧作业。失败原因显示在控制台，完整诊断日志另存系统临时目录，具体路径随错误返回。
- 升级时受管目录内新增文件和目录也会完整备份：不冲突的保留原位；与新版文件同名、或挡住新版目录的内容移入备份，再安装新版。备份清单记录新增与移出路径，包含空目录；新增内容不登记为新版载荷，失败恢复原状。
- 缺失的已登记文件、其文件类型变化、链接或安装锁路径归属问题仍停止处理；同版本重装、校验和卸载不会自动接受本地修改，卸载仍拒绝含未登记文件的受管目录。维护者应在源码中修复工具，再通过新版安装交付，不给已安装文件打补丁或重写安装锁。
- 在项目内安装两项 Skill、共享工具和 Unity 编辑器截图包。
- Git 项目已有有效规则时保持原样，否则在目标项目的 `.gitignore` 末尾补入 `/.scratch/`。
- SVN 项目保留原有 `svn:ignore` 内容，必要时追加 `.scratch`。
- 若 SVN 已纳管 `.scratch` 父目录，根目录的忽略规则不再覆盖其子项；临时分析输出应在 `.scratch` 自身的 `svn:ignore` 中登记 `outputs`，保留已有规则。v3.9.4 的工作区扫描也会排除未纳管的 `.scratch/outputs/`；已纳管文件继续接受变化检查。
- 安装失败时恢复本次准备修改的规则；下载失败时不改项目。
- 安装成功后执行发行版的只读校验，最后清理临时下载文件。

Git 忽略规则可能成为一项本地改动；入口不暂存或提交它。安装不会替你配置 Codex、Cursor 或 Unity MCP。

入口支持全新安装、同版本重装，以及从较旧安装升级；拒绝降级。升级前先停止旧版命令和 Agent，无需先完成旧作业。安装器验证旧载荷归属后整体归档旧活动材料并释放占用，保留项目代码改动；失败恢复原现场。同版本重装保留现有作业。重复安装保留已有忽略内容，不重复添加规则。

新材料位于 `.scratch/dloop-v3/v3.9.4/`；旧材料位于 `.scratch/dloop-history/<旧版本>-<时间>-<唯一标识>/runtime/`，外层说明标记整批作业已失效。历史不再参与活动索引、恢复、批准或清理；可直接打开文档和附件查阅。已卸载遗留材料的来源版本标为 `uninstalled`，新装时同样隔离。详见[版本交替说明](v3.7.6-version-handoff.md)。

## 常见问题

**下载返回 404 或访问失败**

确认网络能够访问 GitHub 和其源码下载服务；受限环境可以使用已取得的本版源码和本地 ZIP。

**找不到 Python**

确认在同一个 PowerShell 中可以运行 `python --version`，并返回 3.10 或更新版本。本入口不自动安装 Python，下载来源见[依赖说明](dependencies.md#需要准备什么)。

**安装成功，但编辑器没有显示入口**

确认打开的是安装目标项目，且项目内存在 `.agents/skills/dloop/SKILL.md` 和 `.agents/skills/dloop-ui/SKILL.md`。Cursor 在 Agent 会话中通过 `/dloop` 或 `/dloop-ui` 显式选择；Codex 使用 `$dloop` 或 `$dloop-ui`。再按[首次使用检查](dependencies.md#安装后的第一次使用)重新加载项目。

**能开始任务，但无法访问 Unity**

检查 Unity 项目是否已打开，Unity MCP 是否已经连接到所选宿主，以及连接目标是否为当前项目。按照[只读连接检查](dependencies.md#安装前的只读检查)确认项目身份；DLoopUI 还需要 MCP 能在编辑器进程内执行 C#、调用已安装截图器。安装校验只覆盖 DLoop 载荷，不代表编辑器连接也已验证。

## 手工验证和卸载

取得并解压对应版本的 DLoop 源码，在该源码目录运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Target "D:\projects\my-project" -Version v3.9.4 -Verify
```

卸载：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Target "D:\projects\my-project" -Version v3.9.4 -Uninstall
```

卸载移除本版管理的 Skill、工具和截图包，保留交付档案与项目忽略规则。受管文件被修改或无法确认归属时会停止并说明原因。
