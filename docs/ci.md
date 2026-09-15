# 持续集成与测试

CI 配置在 `.github/workflows/ci.yml`。每个 PR、默认分支的推送及 Actions 页手动触发时运行，普通开发分支推送不重复执行。PR 检查检出 GitHub 为该 PR 生成的合并结果；安装包由本次源码生成，不从固定仓库或旧版本标签下载。

| 检查名称 | 环境 | 覆盖 |
|---|---|---|
| `core-and-snapshots` | Windows Server 2022、Python 3.10、Git、SVN、PowerShell | 全量核心工作流，含批准、候选、快照和恢复、UI 材料与模板、Windows 权限，以及 CI 结果处理 |
| `installation-and-release` | 同上 | Git/SVN 安装、校验、重装、卸载、失败恢复、一行入口、发行一致性及 Unity 截图静态合同 |
| `credentials` | Ubuntu 24.04、Gitleaks 8.30.1 | 已检出历史的凭据扫描，以及当前提交全部源码扫描；输出隐藏命中的凭据值 |

Git、SVN、svnadmin 和 Windows PowerShell 使用 Windows 镜像提供的客户端，启动前检查是否存在且能运行。缺失或无法运行时直接失败，不用跳过代替验证。第三方 Actions 固定到提交，凭据扫描工具固定版本并核验下载校验值。CI 仅申请代码读取权限，不需要个人令牌、模型密钥或 Unity 许可证。

英文 Windows 镜像中的 SVN 客户端会把部分中文命令参数转换成问号。CI 将镜像提供的 SVN 复制到运行器临时目录，通过 Windows SDK 的清单工具为该副本声明 UTF-8 进程编码，保留原有清单内容；不修改系统区域设置或原客户端。启动测试前用真实临时 SVN 仓库核对中文文件名和提交组名称往返一致，失败时立即停止。此配置采用 [Microsoft 的进程编码机制](https://learn.microsoft.com/en-us/windows/apps/design/globalizing/use-utf8-code-page)，不表示未经配置的所有 Windows SVN 客户端都支持中文参数。

## 结果与跳过规则

测试明细保留在 Actions 日志；各测试集合的数量、失败、跳过、用例总耗时和最慢十项同时写入运行摘要。每项计时包含用例自身的准备、执行和清理；另列整组耗时，包含共享的类级准备与清理。两者均不包含运行前的环境检查和测试发现，不把用例计时之和当成整次命令耗时。完整计时 JSON 在后续日志步骤展示，本地可用 `--timings-json <输出文件>` 保存。没有发现任何测试、测试失败或发生未登记的跳过都会使检查失败。

仅允许以下两项按环境跳过：

- 源码目录中的已安装归属检查：源码由发行清单检查，实际安装归属另由安装测试覆盖。
- 真实 Unity 编辑器编译和截图：默认运行环境没有配置编辑器，不计为已验证。发布前或截图器变化时需要在有编辑器的环境另行运行。

现有 UI 数据和模板测试包含在核心回归中；云端 CI 不覆盖真实浏览器展示、真实模型任务、Unity 业务运行或公开下载链路。发布时另行执行的隔离实测见[发行验证记录](../CHANGELOG.md#发行验证)。

## 按变化选择验证范围

局部改动只验证直接变化、受影响调用边界及稳定回归；共享核心、安装或测试基础设施变化运行对应责任域完整集合；正式发布、影响无法界定或出现跨模块回归时运行项目全量。同一成果、环境与风险未变化时复用有效结果，不因候选评审、最终验收或冻结重复运行。具体验证强度遵循 [唯一策略](../plugin/dloop/skills/dloop/references/validation-policy.md)。

| 本地入口 | 用途 |
|---|---|
| `core --pattern test_feature_archive_approvals.py` | 按文件定向验证；可用文件名通配符 |
| `core --match requirement --match final_approval` | 按用例名称筛选，多个条件取并集，不重复执行 |
| `core --layer business` | 核心业务规则集合，包含审批、候选、上下文、计划和模板 |
| `core --layer integration` | 真实 Git/SVN、快照、UI、命令行及 Windows 权限边界 |
| `core` | 两层完整核心集合；CI 保持此默认入口 |
| `distribution --pattern test_release.py` | 发行元数据定向检查 |
| `distribution` | 安装与发行完整集合 |

上述入口均为 `python .github/scripts/run_tests.py` 后的参数。文件、用例和分层条件同时填写时取交集；未匹配到测试会失败，不能记为通过。定向或单层通过仅代表所选范围；CI 和正式发布仍执行完整集合。

普通业务用例显式设置 `real_snapshots = False`，仅在每个用例期间隔离初始化、范围登记和事务结束后的历史快照写入；工作区变化检查、归属、审批、档案结构及回滚仍使用原实现。帮助器默认保留真实快照，真实仓库、UI 和命令行集成用例不关闭历史保存；已有完整链路验证实际接线。新增宿主或快照集成模块时应将其加入运行器的集成模块集合。两层是现有用例的运行分组，不增加产品状态或生产开关。

## 本地复现

在具备相同依赖的 Windows 源码目录运行以下完整发布前集合：

```powershell
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONDONTWRITEBYTECODE = '1'
python .github/scripts/run_tests.py core --timings-json "$env:TEMP/dloop-core-timings.json"
python .github/scripts/run_tests.py distribution --timings-json "$env:TEMP/dloop-distribution-timings.json"
python -m unittest discover -s .github/tests -v
```

核心和安装测试创建隔离的临时项目，不在使用者的 Unity 工程中运行。全量历史凭据扫描与内容脱敏是不同检查；提交身份、业务资料及版权授权仍须在公开前单独核对。

## 主分支检查

公开仓库主分支已将上表三项设为合并前检查，并禁止强制推送与删除；管理员保留管理豁免。Actions 开关和分支规则属于仓库设置，不随源码自动生效。

产品安装入口中的下载地址须与公开仓库及发行标签一致。CI 使用本地源码包，不能替代发行时的匿名下载与安装验收。
