"""六个结构化工具的参数合同与共享命令适配，不拥有第二套业务状态。"""

from __future__ import annotations

import argparse
from contextlib import nullcontext
from pathlib import Path
from typing import Mapping

from archive_execution import TASK_PACKAGE_FIELDS
from archive_paths import canonical_archive_root
from archive_workspace import workspace_state_lock
from feature_archive import execute_command


def _object(properties, required=None):
    return {"type": "object", "properties": properties, "required": list(properties if required is None else required),
            "additionalProperties": False}


_TEXT = {"type": "string"}
_ID = {"type": "string", "minLength": 1}
_TEXTS = {"type": "array", "items": _TEXT}
_RECORDS = {"type": "array", "items": {"type": "object"}}
_PACKAGE = _object({
    "package_id": _ID, "write_scope": _TEXTS, "generated_write_scope": _TEXTS,
    "context_contract_version": {"type": "integer"}, "context_materials": _RECORDS,
    "slice_contract": {"type": "object", "description": "按准备工具返回的当前契约模板填写业务事实。"},
    "contract_check": {"type": "object", "description": "当前七项检查的实际结论与证据。"},
    "delivery_requirements": _RECORDS,
}, sorted(TASK_PACKAGE_FIELDS))
_CHECKPOINT = _object({
    "contract_version": {"type": "integer"}, "hypothesis": _TEXT, "change_summary": _TEXT,
    "validation_results": {"type": "array", "items": _object({
        "method": _TEXT, "status": {"enum": ["passed", "failed", "unverified"]}, "evidence": _TEXTS})},
    "discoveries": _TEXTS, "next_step": _TEXT, "observed_breakers": _RECORDS,
})
_CANDIDATE = _object({
    "candidate_id": _ID, "verification": _TEXTS, "unverified_boundaries": _TEXTS,
    "delivery_materials": _RECORDS, "additional_delivery_requirements": _RECORDS,
}, ["candidate_id", "verification", "unverified_boundaries"])
_MATERIALS = {"type": "array", "items": _object({
    "status": {"enum": ["verified", "inherited", "missing", "ambiguous", "not_applicable"]},
    "reference": _TEXT, "purpose": _TEXT,
})}
_BASELINE = _object({
    "input_version": {"const": 2},
    "scope_exclusions": {"type": "array", "items": _object({key: _TEXT for key in ("source", "locator", "outcome", "reason")})},
    "items": {"type": "array", "items": _object({"requirement_key": _ID,
        **{key: _MATERIALS for key in ("prefabs", "protocols", "configurations", "requirement_sources")}})},
})

_PREPARE = _object({"feature_id": _ID, "input_kind": {"enum": ["task-package", "checkpoint", "candidate", "ui-baseline"]},
                    "package_id": _ID, "execution_id": _ID}, ["feature_id", "input_kind"])
_PREPARE["oneOf"] = [
    {"properties": {"input_kind": {"const": "task-package"}}, "required": ["package_id"], "not": {"required": ["execution_id"]}},
    {"properties": {"input_kind": {"enum": ["checkpoint", "candidate"]}}, "required": ["execution_id"], "not": {"required": ["package_id"]}},
    {"properties": {"input_kind": {"const": "ui-baseline"}}, "not": {"anyOf": [{"required": ["package_id"]}, {"required": ["execution_id"]}]}},
]

TOOLS = {
    "dloop_status": {
        "description": "查询绑定项目的当前工作流、阻断和下一动作。交付项已知时填写 feature_id；省略时只查询全局占用。",
        "inputSchema": _object({"feature_id": _ID, "include_details": {"type": "boolean", "default": False}}, []),
        "command": "workflow-status",
    },
    "dloop_prepare_input": {
        "description": "取得当前任务包、检查点、候选或开工清单的结构化模板及填写要求；不写模板文件，不批准或提交。",
        "inputSchema": _PREPARE, "command": "prepare-action-input",
    },
    "dloop_submit_task_package": {
        "description": "提交完整任务包并登记契约草稿。工具保存输入；仍需按下一动作检查契约及开工，不自动取得实施授权。",
        "inputSchema": _object({"feature_id": _ID, "package": _PACKAGE}),
        "command": "prepare-slice-contract", "payload": "package", "file_field": "package_file",
    },
    "dloop_submit_checkpoint": {
        "description": "为当前实施身份记录检查点，保留工作区、契约和验证校验。相同内容及工作区重复提交复用检查点。",
        "inputSchema": _object({"feature_id": _ID, "execution_id": _ID, "checkpoint": _CHECKPOINT}),
        "command": "checkpoint-slice", "payload": "checkpoint", "file_field": "checkpoint_file",
    },
    "dloop_submit_candidate": {
        "description": "提交当前实施的完整候选，须先有通过的最终检查点；不代表独立评审通过。响应不确定时先查询状态，已提交的候选不会重复登记。",
        "inputSchema": _object({"feature_id": _ID, "execution_id": _ID, "candidate": _CANDIDATE}),
        "command": "submit-slice", "payload": "candidate", "file_field": "candidate_file",
    },
    "dloop_submit_ui_baseline": {
        "description": "保存开工清单并核对必要业务材料，返回缺项或确认材料；不代替用户确认。只有用途措辞且含义不变时将 semantic_change 设为 false。",
        "inputSchema": _object({"feature_id": _ID, "baseline": _BASELINE, "semantic_change": {"type": "boolean", "default": True}},
                               ["feature_id", "baseline"]),
        "command": "ui-baseline", "payload": "baseline", "file_field": "input",
    },
}


def with_tool_hints(value):
    """保留 CLI 合同，附上已接入动作的结构化调用指引。"""
    if isinstance(value, list):
        return [with_tool_hints(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: with_tool_hints(item) for key, item in value.items()}
    for name, spec in TOOLS.items():
        if value.get("command") != spec["command"]:
            continue
        arguments = value.get("arguments", {})
        if name == "dloop_prepare_input" and arguments.get("input_kind") not in {"task-package", "checkpoint", "candidate", "ui-baseline"}:
            break
        properties = spec["inputSchema"]["properties"]
        supplied = {key: item for key, item in arguments.items() if key in properties}
        if "semantic_change" in supplied and isinstance(supplied["semantic_change"], str):
            supplied["semantic_change"] = supplied["semantic_change"] == "true"
        result["tool_call"] = {"name": name, "arguments": supplied,
                               "required_inputs": [key for key in spec["inputSchema"]["required"] if key not in supplied]}
        break
    return result


def invoke_tool(project_root: Path, name: str, values: Mapping[str, object]) -> tuple[int, Mapping[str, object]]:
    """参数结构由 MCP 合同验证；所有业务状态与结果共用命令入口。"""
    spec = TOOLS[name]
    arguments = {key: item for key, item in values.items() if key != spec.get("payload")}
    arguments["command"] = spec["command"]
    payload = values.get(spec.get("payload"))
    if name == "dloop_status":
        arguments.setdefault("feature_id", None)
        arguments.setdefault("include_details", False)
    elif name == "dloop_prepare_input":
        arguments.setdefault("package_id", None)
        arguments.setdefault("execution_id", None)
        arguments["write_template"] = False
    else:
        arguments[spec["file_field"]] = None
        if name == "dloop_submit_candidate":
            arguments["status"] = "completed"
        if name == "dloop_submit_ui_baseline":
            arguments["semantic_change"] = "true" if values.get("semantic_change", True) else "false"
    namespace = argparse.Namespace(**arguments)
    lock = workspace_state_lock(canonical_archive_root(project_root)) if payload is not None else nullcontext()
    with lock:
        code, result = execute_command(namespace, project_root, structured_input=payload)
    result = with_tool_hints(dict(result))
    if name == "dloop_prepare_input" and code == 0:
        result.pop("target", None)
        result["next_action"] = {"tool_call": result["next_action"]["tool_call"],
                                 "when": result["next_action"]["when"]}
    if payload is not None and getattr(namespace, spec["file_field"], None) is not None:
        result["input_artifact"] = str(getattr(namespace, spec["file_field"]))
    return code, result
