# 独立编码对照

这是一项局部业务编码实验，模型真实修改代码、运行测试和调用对应版本的 DLoop CLI。准备器创建本地 SVN 仓库和工作副本，并把本版本测试辅助材料用作预批准夹具；准备阶段不评测需求讨论或用户批准。模型从活动实施角色开始，提交候选即结束。它不是完整 Unity 交付实验，也不替代安装回归或人工验收。

## 运行

Windows 需要 Python、svn 和 svnadmin。分别从要比较的两个源码目录准备新的运行目录，命令拒绝覆盖：

```powershell
python -B evals/dloop-code-task/prepare_run.py --source <源码目录> --run-dir <新的隔离目录>
```

使用两个独立且无前序对话的原生 Agent，保持模型和推理配置一致，只改变项目路径。交付相同任务：

> 在指定 project 目录完成已批准并启动的“批量处理条目”切片。先运行 `python -B Tools/FeatureArchive/feature_archive.py context-summary --feature-id reliable-delivery --action implementation --role implementation --execution-id implement-1`，按独立角色合同读取、实现、验证和提交候选。只访问该项目，不读评测目录、相邻运行或另一个 Agent 的产物；不得自评或批准。候选提交或真实阻塞后停止，报告实际结果和额外劳动，不估算 token 或工具次数。

根协调者事后运行隐藏业务断言和项目全部测试：

```powershell
python -B evals/dloop-code-task/grade_task.py --project <运行目录>/project
python -B -m unittest discover -s tests -v
```

第二条在被评测 project 目录中运行。两类结果都保存原生标准输出、标准错误和退出码；单独隐藏断言通过不代表完整对照通过。保存初始文件摘要、最终代码/测试、对应工具与 Skill 摘要、候选状态及额外劳动事实。运行目录含实际状态与证据，不作为项目发行载荷。

## 判断边界

- 初始产品摘要必须一致；正常读取相关共享实现不是越界。
- 检查取消、锁定、重复输入、缺失对象、发放失败、重复确认和既有引用。新增测试数量不等于代码质量。
- 耗时保留取数来源，一次样本不能证明提速；并行模型执行的资源竞争也会影响时间。
- 无宿主 trace 时，工具次数、重复读取和 token 保留为不可取得，不能用自报意图代替实际调用。
- 结果见 `reports/2026-09-07/`；后续重复时使用新目录并保留各次结果，不覆盖历史实验。
