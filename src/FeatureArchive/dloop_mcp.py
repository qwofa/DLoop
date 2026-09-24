# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp==2.2.0"]
# ///
"""DLoop 项目内 MCP stdio 入口：uv run --script Tools/FeatureArchive/dloop_mcp.py。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True

import anyio
from jsonschema import Draft202012Validator
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from archive_paths import project_root_from_entrypoint
from archive_tool_api import TOOLS, invoke_tool


def create_server(entrypoint: Path) -> Server:
    project = project_root_from_entrypoint(entrypoint)
    version_path = entrypoint.parent / "VERSION"
    version = version_path.read_text(encoding="utf-8").strip()

    async def list_tools(context, parameters):
        return types.ListToolsResult(tools=[types.Tool(
            name=name, description=spec["description"], inputSchema=spec["inputSchema"],
            annotations=types.ToolAnnotations(readOnlyHint=name in {"dloop_status", "dloop_prepare_input"},
                                             openWorldHint=False),
        ) for name, spec in TOOLS.items()])

    async def call_tool(context, parameters):
        name = parameters.name
        arguments = parameters.arguments or {}
        if not version_path.is_file() or version_path.read_text(encoding="utf-8").strip() != version:
            code, result = 1, {"status": "failed", "code": "DLOOP_RESTART_REQUIRED", "message": "项目工具版本已变化；请重启 DLoop MCP 服务后查询新版状态。"}
        elif name not in TOOLS:
            code, result = 1, {"status": "failed", "code": "UNKNOWN_TOOL", "message": "未知 DLoop 工具。"}
        else:
            errors = sorted(Draft202012Validator(TOOLS[name]["inputSchema"]).iter_errors(arguments), key=lambda error: str(error.path))
            if errors:
                code, result = 1, {"status": "failed", "code": "INVALID_TOOL_ARGUMENTS",
                                   "message": errors[0].message, "field": list(errors[0].absolute_path)}
            else:
                code, result = await anyio.to_thread.run_sync(invoke_tool, project, name, arguments)
        result = {**result, "tool_context": {"project_root": str(project), "workflow_version": version}}
        return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
                                    structuredContent=result, isError=code != 0)

    return Server("dloop", version=version, on_list_tools=list_tools, on_call_tool=call_tool,
                  instructions="当前服务绑定单个已安装项目。先查询状态，按准备工具的当前模板填写结构化提交。未接入的动作沿用 CLI；工具不代替用户批准或独立评审。")


async def main():
    server = create_server(Path(__file__).resolve())
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
