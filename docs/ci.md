# 持续集成与仓库迁移

CI 配置在 `.github/workflows/ci.yml`。每个 PR、默认分支的推送及 Actions 页手动触发时运行，普通开发分支推送不重复执行。PR 检查检出 GitHub 为该 PR 生成的合并结果；安装包由本次源码生成，不从固定仓库或旧版本标签下载。

| 检查名称 | 环境 | 覆盖 |
|---|---|---|
| `core-and-snapshots` | Windows Server 2022、Python 3.10、Git、SVN、PowerShell | 全量核心工作流，含批准、候选、快照和恢复、UI 材料与模板、Windows 权限；随后检查评测工具自身及 CI 结果处理 |
| `installation-and-release` | 同上 | Git/SVN 安装、校验、重装、卸载、失败恢复、一行入口、发行一致性及 Unity 截图静态合同 |
| `credentials` | Ubuntu 24.04、Gitleaks 8.30.1 | 已检出历史的凭据扫描，以及当前提交全部源码扫描；输出隐藏命中的凭据值 |

Git、SVN、svnadmin 和 Windows PowerShell 使用 Windows 镜像提供的客户端，启动前检查是否存在且能运行。缺失或无法运行时直接失败，不用跳过代替验证。第三方 Actions 固定到提交，凭据扫描工具固定版本并核验下载校验值。CI 仅申请代码读取权限，不需要个人令牌、模型密钥或 Unity 许可证。

英文 Windows 镜像中的 SVN 客户端会把部分中文命令参数转换成问号。CI 将镜像提供的 SVN 复制到运行器临时目录，通过 Windows SDK 的清单工具为该副本声明 UTF-8 进程编码，保留原有清单内容；不修改系统区域设置或原客户端。启动测试前用真实临时 SVN 仓库核对中文文件名和提交组名称往返一致，失败时立即停止。此配置采用 [Microsoft 的进程编码机制](https://learn.microsoft.com/en-us/windows/apps/design/globalizing/use-utf8-code-page)，不表示未经配置的所有 Windows SVN 客户端都支持中文参数。

## 结果与跳过规则

测试明细保留在 Actions 日志；各测试集合的数量、失败和跳过明细同时写入运行摘要。没有发现任何测试、测试失败或发生未登记的跳过都会使检查失败。

仅允许以下两项按环境跳过：

- 源码目录中的已安装归属检查：源码由发行清单检查，实际安装归属另由安装测试覆盖。
- 真实 Unity 编辑器编译和截图：默认运行环境没有配置编辑器，不计为已验证。发布前或截图器变化时需要在有编辑器的环境另行运行。

现有 UI 数据和模板测试包含在核心回归中；云端 CI 不覆盖真实浏览器展示、真实模型任务、Unity 业务运行或公开下载链路。发布时另行执行的隔离实测见 [3.7.3 实测记录](v3.7.3-open-source-ci.md#发布与隔离验证)。评测工具自身通过也不代表模型行为通过。

## 本地复现

在具备相同依赖的 Windows 源码目录运行，三个命令各自使用独立 Python 进程：

```powershell
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONDONTWRITEBYTECODE = '1'
python .github/scripts/run_tests.py core
python .github/scripts/run_tests.py distribution
python .github/scripts/run_tests.py evals
```

核心和安装测试创建隔离的临时项目，不在使用者的 Unity 工程中运行。全量历史凭据扫描与内容脱敏是不同检查；提交身份、业务资料及版权授权仍须在公开前单独核对。

## 迁移到新仓库

将 `.github/` 连同源码、测试和评测工具一起导入新仓库即可。配置自动使用所在仓库及其默认分支，无须修改仓库名，也不需要迁移个人密钥。历史是否一并导入由发布范围决定；只导入当前源码时，扫描范围就是新仓库实际拥有的历史。

在新仓库中允许 GitHub Actions 运行；首次运行后，可在分支规则中将上表三个名称设为合并前必须通过的检查。Actions 开关、首次贡献者运行审批和分支规则属于仓库设置，不随 Git 文件迁移。

产品安装入口中的下载地址须与公开仓库及发行标签一致；本次公开仓库沿用 `qwofa/DLoop`。CI 使用本地源码包，不能替代迁移后的匿名下载与安装验收。
