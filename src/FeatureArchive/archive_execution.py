"""DLoop 严格任务包与当前切片状态。"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
import re
from typing import Dict, Mapping, Sequence

from archive_changes import ArchiveChangeError, _body_fingerprint, stale_reasons
from archive_handoff import slice_handoff_materials
from archive_delivery_materials import normalize_requirements, require_task_delivery
from archive_profiles import CONTEXT_CONTRACT_VERSION
from archive_approvals import (
    _load_state,
    _require_complex_feature,
    _state_path,
    _utc_now,
    _write_state,
    approval_status,
    invalidate_final_approval,
    last_accepted_candidate,
    matching_accepted_workspace_snapshot,
)
from archive_validation import validate_feature_archive
from archive_slice_contract import (
    CONTRACT_CHECK_CRITERIA,
    CONTRACT_SCHEMA_VERSION,
    SliceContractError,
    UNFILLED_INPUT_PREFIXES,
    normalize_task_contract_fields,
    slice_contract_input_guidance,
    transition_slice_status,
    upsert_contract_record,
)
from archive_slice_plan import (
    SlicePlanError,
    require_slice_eligible,
)
from archive_workspace import (
    active_modification_lease,
    acquire_modification_lease,
    assert_safe_write_scopes,
    serialized_workflow_state,
)
from archive_configuration import (
    require_configuration_task_materials,
)


PACKAGE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
EXECUTION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
CONTEXT_MATERIAL_MODES = {"sections", "full"}
PROJECTED_TASK_FIELDS = {
    "goal",
    "non_goals",
    "acceptance_conditions",
    "validations",
    "rollback",
}
TASK_PACKAGE_FIELDS = {
    "package_id",
    "write_scope",
    "context_contract_version",
    "context_materials",
    "slice_contract",
    "contract_check",
}


class ArchiveExecutionError(Exception):
    """表示任务包或当前切片不满足最小合同。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _active_execution_result(
    feature_id: str,
    record: Mapping[str, object],
    *,
    materials: Sequence[Mapping[str, object]],
    idempotent: bool = False,
) -> Mapping[str, object]:
    package = record["package"]
    package_id = package["package_id"]
    execution_id = record["execution_id"]
    plan_binding = record.get("slice_plan_binding")
    result = {
        "status": "active",
        "package_id": package_id,
        "execution_id": execution_id,
        "handoff_ready": True,
        "required_materials": list(materials),
        "task": package,
        "contract_check": record.get("contract_check"),
        "slice_plan_binding": plan_binding,
        "execution_handoff": {
            "feature_id": feature_id,
            "package_id": package_id,
            "execution_id": execution_id,
            "workspace_root": record["workspace_root"],
            "task_package": package,
            "allowed_write_scope": list(package["write_scope"]),
            "slice_plan_binding": plan_binding,
        },
    }
    if idempotent:
        result["idempotent"] = True
    return result


def _execution_state(state: Dict[str, object]) -> Dict[str, object]:
    execution = state.get("execution")
    if (
        not isinstance(execution, dict)
        or not isinstance(execution.get("slices"), dict)
        or not isinstance(execution.get("used_execution_ids"), list)
        or not isinstance(execution.get("slice_plan"), dict)
    ):
        raise ArchiveExecutionError("INVALID_EXECUTION_STATE", "执行状态结构不合法。")
    return execution


def ensure_execution_id_unused(execution: Mapping[str, object], execution_id: str) -> None:
    used = execution.get("used_execution_ids")
    if not isinstance(used, list) or any(not isinstance(item, str) for item in used):
        raise ArchiveExecutionError("INVALID_EXECUTION_STATE", "执行状态结构不合法。")
    if execution_id in used:
        raise ArchiveExecutionError(
            "EXECUTION_ID_CONFLICT",
            f"执行标识“{execution_id}”已经被其他执行单元使用。",
        )


def mark_execution_id_used(execution: Dict[str, object], execution_id: str) -> None:
    ensure_execution_id_unused(execution, execution_id)
    execution["used_execution_ids"].append(execution_id)


def _required_string(value: Mapping[str, object], field: str) -> str:
    result = value.get(field)
    if not isinstance(result, str) or not result.strip():
        raise ArchiveExecutionError("INVALID_TASK_PACKAGE", f"任务包字段 {field} 必须是非空字符串。")
    normalized = result.strip()
    if any(normalized.startswith(prefix) for prefix in UNFILLED_INPUT_PREFIXES):
        raise ArchiveExecutionError("INVALID_TASK_PACKAGE", f"任务包字段 {field} 仍为待填写占位内容。")
    return normalized


def _string_list(
    value: Mapping[str, object],
    field: str,
    *,
    allow_empty: bool = False,
) -> Sequence[str]:
    result = value.get(field)
    if not isinstance(result, list) or (not result and not allow_empty):
        raise ArchiveExecutionError("INVALID_TASK_PACKAGE", f"任务包字段 {field} 必须是字符串列表。")
    normalized = []
    for item in result:
        if not isinstance(item, str) or not item.strip():
            raise ArchiveExecutionError("INVALID_TASK_PACKAGE", f"任务包字段 {field} 包含空值。")
        normalized_item = item.strip()
        if any(normalized_item.startswith(prefix) for prefix in UNFILLED_INPUT_PREFIXES):
            raise ArchiveExecutionError("INVALID_TASK_PACKAGE", f"任务包字段 {field} 仍包含待填写占位内容。")
        normalized.append(normalized_item)
    return tuple(normalized)


def _safe_scope(scope: str) -> str:
    if any(character in scope for character in "*?[]"):
        raise ArchiveExecutionError(
            "INVALID_TASK_SCOPE",
            f"写入范围“{scope}”不支持通配符；请填写具体文件或目录路径。",
        )
    path = PurePosixPath(scope.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ArchiveExecutionError("INVALID_TASK_SCOPE", f"写入范围“{scope}”不安全。")
    return path.as_posix()


def _context_materials(value: Mapping[str, object]) -> Sequence[Mapping[str, object]]:
    materials = value.get("context_materials")
    if not isinstance(materials, list):
        raise ArchiveExecutionError("INVALID_TASK_PACKAGE", "任务包字段 context_materials 必须是列表。")
    version = value.get("context_contract_version")
    if version != CONTEXT_CONTRACT_VERSION:
        raise ArchiveExecutionError("INVALID_TASK_PACKAGE", "任务包上下文合同版本不受支持。")
    normalized = []
    for item in materials:
        if not isinstance(item, dict):
            raise ArchiveExecutionError("INVALID_TASK_PACKAGE", "任务包材料必须是结构化对象。")
        source = _required_string(item, "source")
        purpose = _required_string(item, "purpose")
        mode = _required_string(item, "mode")
        if mode not in CONTEXT_MATERIAL_MODES:
            raise ArchiveExecutionError("INVALID_TASK_PACKAGE", f"任务包材料读取模式“{mode}”不受支持。")
        unknown = set(item) - {"source", "purpose", "mode", "sections"}
        if unknown:
            raise ArchiveExecutionError(
                "INVALID_TASK_PACKAGE",
                "任务包材料包含未知字段：" + "、".join(sorted(unknown)),
            )
        record = {"source": source, "purpose": purpose, "mode": mode}
        if mode == "sections":
            sections = list(_string_list(item, "sections"))
            if len(sections) != len(set(sections)):
                raise ArchiveExecutionError("INVALID_TASK_PACKAGE", "任务包材料章节标题不能重复。")
            record["sections"] = sections
        elif "sections" in item:
            raise ArchiveExecutionError("INVALID_TASK_PACKAGE", "整份材料模式不能声明章节标题。")
        normalized.append(record)
    return tuple(normalized)


def _read_package(path: Path) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exception:
        raise ArchiveExecutionError("INVALID_TASK_PACKAGE", f"无法读取任务包“{path}”：{exception}") from exception
    if not isinstance(value, dict):
        raise ArchiveExecutionError("INVALID_TASK_PACKAGE", "任务包必须是 JSON 对象。")
    duplicated_fields = sorted(PROJECTED_TASK_FIELDS.intersection(value))
    if duplicated_fields:
        raise ArchiveExecutionError(
            "DUPLICATED_TASK_CONTRACT_FIELDS",
            "任务包不能重复填写由切片契约生成的字段："
            + "、".join(duplicated_fields),
        )
    allowed_fields = TASK_PACKAGE_FIELDS | ({"delivery_requirements"} if "delivery_requirements" in value else set())
    if set(value) != allowed_fields:
        missing = "、".join(sorted(TASK_PACKAGE_FIELDS - set(value)))
        unknown = "、".join(sorted(set(value) - allowed_fields))
        detail = "；".join(
            item
            for item in (
                f"缺少：{missing}" if missing else "",
                f"未知：{unknown}" if unknown else "",
            )
            if item
        )
        raise ArchiveExecutionError(
            "INVALID_TASK_PACKAGE",
            f"任务包字段不完整或包含未知字段（{detail}）。",
        )
    package_id = _required_string(value, "package_id")
    if not PACKAGE_ID_PATTERN.fullmatch(package_id):
        raise ArchiveExecutionError("INVALID_TASK_PACKAGE", "任务包标识不合法。")
    write_scope = tuple(_safe_scope(item) for item in _string_list(value, "write_scope", allow_empty=True))
    if not write_scope:
        raise ArchiveExecutionError("INVALID_TASK_PACKAGE", "修改切片必须声明写入范围。")
    context_materials = _context_materials(value)
    try:
        contract, contract_check = normalize_task_contract_fields(value)
    except SliceContractError as exception:
        raise ArchiveExecutionError(exception.code, exception.message) from exception
    package = {
        "package_id": package_id,
        "goal": contract["business_outcome"],
        "write_scope": list(write_scope),
        "non_goals": list(contract["out_of_scope"]),
        "acceptance_conditions": list(contract["acceptance_scenarios"]),
        "validations": list(contract["validation_methods"]),
        "rollback": contract["rollback_point"],
        "context_contract_version": CONTEXT_CONTRACT_VERSION,
        "context_materials": list(context_materials),
        "slice_contract": contract,
        "contract_check": contract_check,
    }
    if "delivery_requirements" in value:
        package["delivery_requirements"] = normalize_requirements(
            value["delivery_requirements"], contract["acceptance_scenarios"], complete=True,
        )
    return package


@dataclass(frozen=True)
class TaskPackageTarget:
    """统一表示任务包文件或已登记任务包标识。"""

    package_file: Path | None = None
    package_id: str | None = None

    def resolve(self, execution: Mapping[str, object]) -> Mapping[str, object]:
        if (self.package_file is None) == (self.package_id is None):
            raise ArchiveExecutionError(
                "INVALID_TASK_PACKAGE",
                "必须且只能指定任务包文件或已登记任务包标识。",
            )
        if self.package_file is not None:
            return _read_package(self.package_file)
        slices = execution.get("slices")
        registered = (
            slices.get(str(self.package_id)) if isinstance(slices, dict) else None
        )
        package = registered.get("package") if isinstance(registered, dict) else None
        if not isinstance(package, dict):
            raise ArchiveExecutionError(
                "TASK_PACKAGE_NOT_REGISTERED",
                f"任务包“{self.package_id}”尚未通过工具登记。",
            )
        return package


def task_package_input_guidance(feature_id: str) -> Mapping[str, object]:
    """返回任务包中需要明确结构的填写说明，示例不代表业务决定。"""

    return {
        **slice_contract_input_guidance(),
        "write_scope": {
            "type": "string-list",
            "description": "项目相对的具体文件或目录；目录包含子项，不支持通配符。生成文件及元文件也需覆盖。",
            "excluded": "DLoop 自有档案作为上下文引用，不作为产品写入范围。",
        },
        "context_materials": {
            "type": "object-list",
            "description": "引用相关批准场景和原始来源，核对操作去向、显示位置、触发及不处理条件；同一行为的多个位置逐一承接。交接补入未引用的需求和设计入口，保留已有章节选择。",
            "fields": {
                "source": "正式文档标识，或相对交付项/工作区的材料路径",
                "purpose": "本角色阅读这份材料的具体目的",
                "mode": "full 整份读取且省略 sections；sections 只读精确且唯一的章节标题",
                "sections": "仅 sections 模式填写非空、不重复的标题列表",
            },
            "allowed_values": {"mode": sorted(CONTEXT_MATERIAL_MODES)},
            "examples": [
                {"source": f"{feature_id}.design.overview", "purpose": "核对已确认设计", "mode": "full"},
                {"source": f"{feature_id}.requirements.overview", "purpose": "核对验收范围",
                 "mode": "sections", "sections": ["当前摘要"]},
            ],
        },
    }


def task_package_input_template(package_id: str) -> Mapping[str, object]:
    """生成严格任务包的当前结构，业务事实由使用者按切片规模填写。"""

    if not PACKAGE_ID_PATTERN.fullmatch(package_id):
        raise ArchiveExecutionError("INVALID_TASK_PACKAGE", "任务包标识不合法。")
    return {
        "package_id": package_id,
        "write_scope": ["待填写：授权写入范围"],
        "context_contract_version": CONTEXT_CONTRACT_VERSION,
        "context_materials": [],
        "slice_contract": {
            "schema_version": CONTRACT_SCHEMA_VERSION,
            "version": 1,
            "revision_summary": "待填写：本版契约变化",
            "business_outcome": "待填写：可独立验收的业务结果",
            "acceptance_scenarios": ["待填写：可观察验收场景"],
            "in_scope": {
                "business_behaviors": ["待填写：范围内业务行为"],
                "data_responsibilities": ["待填写：数据责任"],
                "system_boundaries": ["待填写：系统边界"],
                "lifecycle": ["待填写：生命周期范围"],
            },
            "out_of_scope": [],
            "dependency_assumptions": [],
            "expected_impact_areas": {
                "modules": ["待填写：受影响模块；如无模块则填写资源或数据边界"],
                "resources": [],
                "data_boundaries": [],
            },
            "invariants": ["待填写：必须保持的既有行为"],
            "validation_level": "待填写：targeted、module_full 或 project_full",
            "validation_rationale": "待填写：验证强度与风险的对应理由",
            "validation_methods": ["待填写：验证方法"],
            "unknowns": [],
            "rollback_point": "待填写：安全回退点",
            "circuit_breaker_conditions": ["待填写：切片特有熔断条件"],
            "decision_owner": "待填写：业务决策负责人",
            "approval_status": "pending",
        },
        "contract_check": {
            criterion: {
                "passed": False,
                "evidence": f"待补充：{criterion} 的事实证据",
            }
            for criterion in CONTRACT_CHECK_CRITERIA
        },
    }


def _require_ready_execution_inputs(graph, feature_id: str) -> None:
    approvals = approval_status(graph.root, feature_id)
    if approvals["requirements"]["status"] not in {"approve", "ready"}:
        raise ArchiveExecutionError("REQUIREMENTS_APPROVAL_REQUIRED", "需求共识尚未确认或已经失效。")
    if approvals["architecture"]["status"] in {"reject", "stale"}:
        raise ArchiveExecutionError(
            "ARCHITECTURE_APPROVAL_BLOCKED",
            "架构决定已拒绝或失效，不能启动新的实施切片。",
        )
    blockers = []
    for document in graph.documents.values():
        if (
            document.feature_id != feature_id
            or document.category not in {"requirements", "design", "plan"}
            or document.content_status == "superseded"
        ):
            continue
        if document.content_status not in {"confirmed", "completed"}:
            blockers.append(f"{document.document_id}: content_status={document.content_status}")
            continue
        try:
            actual = _body_fingerprint(
                document.path.read_text(encoding="utf-8"),
                document.path,
            )
        except (OSError, UnicodeError, ArchiveChangeError) as exception:
            blockers.append(f"{document.document_id}: {exception}")
            continue
        if actual != document.content_fingerprint:
            blockers.append(f"{document.document_id}: UNCONFIRMED_EDIT")
            continue
        reasons = stale_reasons(graph, document)
        if reasons:
            blockers.append(f"{document.document_id}: {'；'.join(reasons)}")
    if blockers:
        raise ArchiveExecutionError(
            "EXECUTION_INPUT_NOT_READY",
            "实施输入尚未确认或已经过期：" + "；".join(sorted(blockers)),
        )


@serialized_workflow_state
def start_execution(
    root: Path,
    feature_id: str,
    execution_id: str,
    package_target: TaskPackageTarget,
    workspace_root: Path,
) -> Mapping[str, object]:
    if not EXECUTION_ID_PATTERN.fullmatch(execution_id):
        raise ArchiveExecutionError("INVALID_EXECUTION_ID", "执行标识不合法。")
    graph = validate_feature_archive(root, feature_id)
    feature = _require_complex_feature(graph, feature_id)
    state = _load_state(feature.path, feature_id)
    _require_ready_execution_inputs(graph, feature_id)
    execution = _execution_state(state)
    package = package_target.resolve(execution)
    require_configuration_task_materials(
        state, feature_id, package["context_materials"]
    )
    require_task_delivery(state, package)
    assert_safe_write_scopes(package["write_scope"], feature_id)
    package_id = str(package["package_id"])
    existing = find_slice_by_execution(execution, execution_id)
    used_execution_ids = execution.get("used_execution_ids")
    if isinstance(used_execution_ids, list) and execution_id in used_execution_ids:
        normalized_workspace = str(workspace_root.expanduser().resolve())
        lease = active_modification_lease(root, feature_id, str(package_id))
        if (
            isinstance(existing, dict)
            and existing.get("status") == "active"
            and existing.get("package") == package
            and existing.get("workspace_root") == normalized_workspace
            and isinstance(lease, dict)
            and lease.get("package_id") == package_id
            and lease.get("workspace_root") == normalized_workspace
            and lease.get("holder_execution_id") == execution_id
        ):
            materials = slice_handoff_materials(graph, feature, workspace_root, package)
            return _active_execution_result(feature_id, existing, materials=materials, idempotent=True)
        ensure_execution_id_unused(execution, execution_id)
    materials = slice_handoff_materials(graph, feature, workspace_root, package)
    slices = execution["slices"]
    try:
        plan_binding = require_slice_eligible(
            execution["slice_plan"], slices, package_id
        )
    except SlicePlanError as exception:
        raise ArchiveExecutionError(exception.code, exception.message) from exception
    try:
        record, contract_check, _ = upsert_contract_record(
            slices, package, _utc_now(), evaluate=True
        )
    except SliceContractError as exception:
        raise ArchiveExecutionError(exception.code, exception.message) from exception
    if contract_check is None or contract_check.get("status") != "passed":
        _write_state(_state_path(feature.path), state)
        return {
            "status": "contract_failed",
            "package_id": package_id,
            "contract_check": contract_check,
        }
    last_candidate = last_accepted_candidate(execution)
    restored_binding = record.get("restore_binding")
    if restored_binding:
        from archive_workspace import snapshot_workspace, workspace_guard_snapshot
        if (
            snapshot_workspace(workspace_root, restored_binding["scopes"])["digest"] != restored_binding["workspace_digest"]
            or workspace_guard_snapshot(workspace_root)["digest"] != restored_binding["guard_digest"]
        ):
            raise ArchiveExecutionError("RESTORED_WORKSPACE_CHANGED", "工作区已偏离恢复结果，请重新保存并选择恢复起点。")
    elif last_candidate is not None:
        last_package_id, _ = last_candidate
        current_workspace = matching_accepted_workspace_snapshot(workspace_root, execution)
        if current_workspace is None:
            raise ArchiveExecutionError(
                "LAST_ACCEPTED_CANDIDATE_CHANGED",
                f"工作区已偏离最后接受的切片“{last_package_id}”，不能启动新切片。",
            )
    acquire_modification_lease(
        root,
        feature_id,
        package_id,
        workspace_root,
        package["write_scope"],
        _utc_now(),
        holder_execution_id=execution_id,
        restored_baseline=record.get("restored_baseline"),
    )
    try:
        transition_slice_status(record, "active")
    except SliceContractError as exception:
        raise ArchiveExecutionError(exception.code, exception.message) from exception
    record.update({
        "execution_id": execution_id,
        "workspace_root": str(workspace_root.expanduser().resolve()),
        "candidate": None,
        "started_at": _utc_now(),
        "updated_at": _utc_now(),
        "slice_plan_binding": plan_binding,
    })
    mark_execution_id_used(execution, execution_id)
    invalidate_final_approval(state)
    _write_state(_state_path(feature.path), state)
    return _active_execution_result(feature_id, record, materials=materials)


def find_slice_by_execution(execution: Mapping[str, object], execution_id: str):
    slices = execution.get("slices", {})
    if not isinstance(slices, dict):
        return None
    for record in slices.values():
        if isinstance(record, dict) and record.get("execution_id") == execution_id:
            return record
    return None


def require_slice(state: Dict[str, object], package_id: str) -> Dict[str, object]:
    execution = _execution_state(state)
    record = execution["slices"].get(package_id)
    if not isinstance(record, dict):
        raise ArchiveExecutionError("SLICE_NOT_FOUND", f"切片“{package_id}”不存在。")
    return record
