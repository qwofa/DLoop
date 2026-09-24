# 结构化工具连接

v4.0.0 提供项目内 MCP stdio 服务。模型直接传递结构化内容，工具负责序列化、规范路径及保存；业务状态、范围校验、批准、独立评审和快照仍由现有核心处理。

| 工具 | 用途 |
|---|---|
| `dloop_status` | 当前状态、阻断和下一动作 |
| `dloop_prepare_input` | 当前模板及填写要求，不写模板文件 |
| `dloop_submit_task_package` | 保存任务包并登记契约草稿 |
| `dloop_submit_checkpoint` | 保存检查点，相同内容和工作区重复提交复用记录 |
| `dloop_submit_candidate` | 固定候选并进入独立评审 |
| `dloop_submit_ui_baseline` | 保存开工清单并核对材料，用户确认另行完成 |

先查询状态，再按当前模板填写真实业务事实。提交返回已保存材料的路径，不需要调用者指定 JSON 路径、编码或转义。字段结构不合法时返回字段定位；业务内容不满足要求时沿用原错误及摩擦记录。响应不确定时先查询状态；候选成功后已退出实施，不能再次提交。其他动作使用返回的 CLI 合同。

## 运行依赖

安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)，并确认 `uv --version` 正常。入口声明 Python 3.10+ 与官方 [MCP Python SDK 2.2.0](https://github.com/modelcontextprotocol/python-sdk/tree/v2.2.0)。首次启动需要下载依赖，后续复用项目外缓存。此服务不调用模型 API，不需要模型密钥。CLI 核心仍只依赖标准库。

安装 DLoop 后连接项目内 `Tools/FeatureArchive/dloop_mcp.py`。服务从自己的安装位置确定唯一项目，不按宿主当前工作目录选择项目。首次调用核对结果中的 `tool_context.project_root`。一个配置只绑定一个项目。

## Codex

在已信任项目的 `.codex/config.toml` 中合并以下条目，替换示例绝对路径。保留其他配置；已有同名服务时先核对其用途。

```toml
[mcp_servers.dloop]
command = "uv"
args = ["run", "--script", "D:/projects/my-project/Tools/FeatureArchive/dloop_mcp.py"]
startup_timeout_sec = 60
tool_timeout_sec = 120
```

重新加载 MCP 连接后，应能发现上表六个工具。配置格式见 [Codex MCP 文档](https://developers.openai.com/codex/mcp/)。

## Cursor

在项目 `.cursor/mcp.json` 的 `mcpServers` 中合并：

```json
{
  "mcpServers": {
    "dloop": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--script", "${workspaceFolder}/Tools/FeatureArchive/dloop_mcp.py"]
    }
  }
}
```

在宿主 MCP 配置中启用服务并检查工具列表。配置位置与变量格式见 [Cursor MCP 文档](https://cursor.com/docs/mcp)。

## 升级与卸载

安装器部署服务脚本，不覆盖 Codex 或 Cursor 的配置。升级前停止正在执行的工作及 DLoop MCP 服务，完成安装后重新连接。旧进程发现版本变化会返回 `DLOOP_RESTART_REQUIRED`，不会继续执行旧代码。升级仍整体归档旧作业，不跨版本续跑。

卸载 DLoop 后移除对应 MCP 条目。启动失败时检查 `uv` 是否可从宿主访问，以及依赖下载是否成功；宿主找不到 `uv` 时可把 `command` 改为其绝对路径。
