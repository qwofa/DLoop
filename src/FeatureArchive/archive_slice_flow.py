"""在现有切片执行账本上实现契约门禁、检查点和实施熔断。"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Mapping, Sequence

from archive_approvals import (
    _load_state,
    _require_complex_feature,
    _state_path,
    _utc_now,
    _write_state,
    invalidate_final_approval,
)
from archive_configuration import require_configuration_task_materials
from archive_delivery_materials import require_task_delivery
from archive_execution import (
    ArchiveExecutionError,
    TaskPackageTarget,
    _execution_state,
    _require_ready_execution_inputs,
    find_slice_by_execution,
)
from archive_slice_contract import (
    BREAKER_ALLOWED_ACTIONS,
    HARD_BREAKER_RULES,
    SliceContractError,
    VALIDATION_LEVELS,
    digest,
    evaluate_contract,
    _history_entry,
    required_string,
    string_list,
    transition_slice_status,
    upsert_contract_record,
)
from archive_validation import validate_feature_archive
from archive_workspace_close import commit_workflow_and_lease_atomically
from archive_workspace import (
    current_workspace_guard_snapshot,
    current_workspace_snapshot,
    outside_scope_guard_changes,
    require_modification_lease,
    serialized_workflow_state,
)


class ArchiveSliceFlowError(Exception):
    """表示切片契约流程、熔断或恢复输入不合法。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _convert(exception: SliceContractError) -> ArchiveSliceFlowError:
    return ArchiveSliceFlowError(exception.code, exception.message)


@serialized_workflow_state
def amend_slice_scope(root: Path, feature_id: str, package_id: str, input_path: Path) -> Mapping[str, object]:
    """只补登记当前漏登文件，保留成果、原契约和熔断历史。"""
    from archive_execution import _safe_scope
    from archive_workspace import assert_safe_write_scopes, workspace_content_snapshot, _canonical_digest
    from archive_snapshots import register_snapshot_scopes

    graph = validate_feature_archive(root, feature_id)
    feature = _require_complex_feature(graph, feature_id)
    state = _load_state(feature.path, feature_id)
    record = _execution_state(state)["slices"].get(package_id)
    if not isinstance(record, dict) or record.get("status") != "circuit_open":
        raise ArchiveSliceFlowError("SCOPE_AMENDMENT_NOT_AVAILABLE", "仅能修订因写入范围漏登而熔断的切片。")
    latest = record["checkpoints"][-1]
    if set(latest["boundary_rules"]) != {"write_scope_violation"}:
        raise ArchiveSliceFlowError("SCOPE_AMENDMENT_NOT_AVAILABLE", "仍有业务或依赖阻断，不能仅修订文件范围后继续。")
    _, lease = require_modification_lease(root, feature_id, package_id)
    current_guard = current_workspace_guard_snapshot(lease)
    changes = outside_scope_guard_changes(lease["baseline_guard_snapshot"], current_guard, lease["write_scopes"])
    value = _read_json(input_path, "范围修订")
    if set(value) != {"paths", "reason", "authorization"}:
        raise ArchiveSliceFlowError("INVALID_SCOPE_AMENDMENT", "范围修订须包含 paths、漏登原因 reason 和已有业务授权依据 authorization。")
    try:
        paths = tuple(_safe_scope(path) for path in string_list(value["paths"], "paths"))
        reason = required_string(value["reason"], "reason")
        authorization = required_string(value["authorization"], "authorization")
    except (SliceContractError, ArchiveExecutionError) as error:
        raise ArchiveSliceFlowError(error.code, error.message) from error
    if not changes or set(paths) != set(changes):
        raise ArchiveSliceFlowError("SCOPE_AMENDMENT_PATHS_MISMATCH", "仅补登记本次实际漏登的具体文件，须逐项核对：" + "、".join(changes))
    workspace = Path(lease["workspace_root"])
    if any((workspace / path).is_dir() for path in paths):
        raise ArchiveSliceFlowError("INVALID_SCOPE_AMENDMENT", "范围修订不能扩大到整个目录。")
    scopes = sorted(set(lease["write_scopes"]) | set(paths))
    assert_safe_write_scopes(scopes, feature_id)
    # 新增范围没有受管的执行前正文；其恢复点明确设为本次保留现场，不能假称干净基线。
    retained = workspace_content_snapshot(workspace, paths)
    contents = deepcopy(lease["baseline_content_snapshot"])
    contents["scopes"] = scopes
    contents["entries"].update(retained["entries"])
    entries = {path: entry["digest"] for path, entry in contents["entries"].items()}
    contents["digest"] = _canonical_digest(entries)
    baseline = {**lease["baseline_snapshot"], "scopes": scopes, "entries": entries, "digest": contents["digest"]}
    package = deepcopy(record["package"])
    package["write_scope"] = scopes
    contract = package["slice_contract"]
    contract["version"] += 1
    contract["revision_summary"] = reason
    contract["rollback_point"] += "；本次补登记文件（" + "、".join(paths) + "）恢复至范围修订时保留的现场。"
    package["rollback"] = contract["rollback_point"]
    now = _utc_now()
    check = evaluate_contract(contract, package["contract_check"], now)
    amendment = {"paths": list(paths), "reason": reason, "authorization": authorization,
                 "retained_snapshot": retained, "guard_digest": current_guard["digest"], "recorded_at": now,
                 "rollback_boundary": "原范围恢复至实施起点；补登记文件恢复至本次保留现场。"}
    record.setdefault("scope_amendments", []).append(amendment)
    record["package"] = package
    record["contract_check"] = check
    record["contract_history"].append(_history_entry(package, check, now))
    record["updated_at"] = now
    invalidate_final_approval(state)
    register_snapshot_scopes(root, feature_id, workspace, scopes)
    commit_workflow_and_lease_atomically(root, feature.path, feature_id, package_id, state,
                                        {"write_scopes": scopes, "baseline_snapshot": baseline,
                                         "baseline_digest": baseline["digest"], "baseline_content_snapshot": contents})
    return {"status": "circuit_open", "scope_amended": True, "package_id": package_id,
            "contract_version": contract["version"], "retained_paths": list(paths),
            "rollback_boundary": amendment["rollback_boundary"],
            "next_action": "使用新的执行身份 resolve-slice --action retry，重新验证后提交独立评审。"}


def _read_json(path: Path, label: str) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exception:
        raise ArchiveSliceFlowError("INVALID_SLICE_ARTIFACT", f"无法读取{label}：{exception}") from exception
    if not isinstance(value, dict):
        raise ArchiveSliceFlowError("INVALID_SLICE_ARTIFACT", f"{label}必须是 JSON 对象。")
    return value


@serialized_workflow_state
def record_contract(
    root: Path,
    feature_id: str,
    package_target: TaskPackageTarget,
    evaluate: bool,
) -> Mapping[str, object]:
    graph = validate_feature_archive(root, feature_id)
    feature = _require_complex_feature(graph, feature_id)
    state = _load_state(feature.path, feature_id)
    try:
        package = package_target.resolve(_execution_state(state))
    except ArchiveExecutionError as exception:
        raise ArchiveSliceFlowError(exception.code, exception.message) from exception
    require_configuration_task_materials(
        state, feature_id, package["context_materials"]
    )
    require_task_delivery(state, package)
    try:
        record, check, idempotent = upsert_contract_record(
            _execution_state(state)["slices"], package, _utc_now(), evaluate=evaluate
        )
    except SliceContractError as exception:
        raise _convert(exception) from exception
    invalidate_final_approval(state)
    _write_state(_state_path(feature.path), state)
    return {
        "status": record["status"],
        "package_id": package["package_id"],
        "contract_version": package["slice_contract"]["version"],
        "contract_check": check,
        "idempotent": idempotent,
    }


def _checkpoint_strings(value: Mapping[str, object], field: str, *, allow_empty: bool = False) -> Sequence[str]:
    try:
        return list(string_list(value.get(field), field, allow_empty=allow_empty, code="INVALID_CHECKPOINT"))
    except SliceContractError as exception:
        raise _convert(exception) from exception


@serialized_workflow_state
def supplement_slice_validation(
    root: Path,
    feature_id: str,
    execution_id: str,
    validation_level: str,
    validation_methods: Sequence[str],
    rationale: str,
) -> Mapping[str, object]:
    """仅加强活动切片验证；业务契约、执行身份和恢复基线保持不变。"""

    graph = validate_feature_archive(root, feature_id)
    feature = _require_complex_feature(graph, feature_id)
    state = _load_state(feature.path, feature_id)
    record = find_slice_by_execution(_execution_state(state), execution_id)
    if not isinstance(record, dict) or record.get("status") != "active":
        raise ArchiveSliceFlowError("EXECUTION_NOT_ACTIVE", "只能补充当前实施或返修棒次的验证。")
    package_id = record["package"]["package_id"]
    _, lease = require_modification_lease(root, feature_id, package_id)
    if lease.get("holder_execution_id") != execution_id:
        raise ArchiveSliceFlowError("EXECUTION_ID_MISMATCH", "执行身份与修改租约不一致。")
    _require_ready_execution_inputs(graph, feature_id)
    package = deepcopy(record["package"])
    contract = package["slice_contract"]
    if validation_level not in VALIDATION_LEVELS or VALIDATION_LEVELS.index(validation_level) < VALIDATION_LEVELS.index(contract["validation_level"]):
        raise ArchiveSliceFlowError("VALIDATION_DOWNGRADE_FORBIDDEN", "验证补充不能降低当前验证强度。")
    try:
        reason = required_string(rationale, "rationale")
        additions = string_list(list(validation_methods), "validation_methods", allow_empty=True)
    except SliceContractError as exception:
        raise _convert(exception) from exception
    methods = list(dict.fromkeys([*contract["validation_methods"], *additions]))
    unchanged = methods == contract["validation_methods"] and validation_level == contract["validation_level"]
    if unchanged:
        return {"status": "active", "package_id": package_id, "execution_id": execution_id,
                "contract_version": contract["version"], "idempotent": True}
    now = _utc_now()
    contract.update({
        "version": contract["version"] + 1,
        "revision_summary": reason,
        "validation_level": validation_level,
        "validation_rationale": reason,
        "validation_methods": methods,
    })
    package["validations"] = list(methods)
    check = evaluate_contract(contract, package["contract_check"], now)
    if check["status"] != "passed":
        raise ArchiveSliceFlowError("CONTRACT_CHECK_FAILED", "当前契约检查未通过，不能补充验证。")
    record["package"] = package
    record["contract_check"] = check
    record["contract_history"].append(_history_entry(package, check, now))
    record["updated_at"] = now
    invalidate_final_approval(state)
    _write_state(_state_path(feature.path), state)
    return {
        "status": "active", "package_id": package_id, "execution_id": execution_id,
        "contract_version": contract["version"], "idempotent": False,
        "next_action": {"command": "prepare-action-input", "arguments": {
            "feature_id": feature_id, "execution_id": execution_id, "input_kind": "checkpoint",
        }},
    }


def _normalize_checkpoint(value: Mapping[str, object], contract: Mapping[str, object]) -> Mapping[str, object]:
    required = {
        "contract_version", "hypothesis", "change_summary", "validation_results",
        "discoveries", "next_step", "observed_breakers",
    }
    if set(value) != required:
        raise ArchiveSliceFlowError("INVALID_CHECKPOINT", "实施检查点字段不完整或包含未知字段。")
    if type(value["contract_version"]) is not int or value["contract_version"] != contract["version"]:
        raise ArchiveSliceFlowError(
            "CHECKPOINT_CONTRACT_STALE", "检查点输入未绑定当前契约，请重新准备输入并核对当前验证要求。",
        )
    validations = value.get("validation_results")
    if not isinstance(validations, list) or any(not isinstance(item, dict) for item in validations):
        raise ArchiveSliceFlowError("INVALID_CHECKPOINT", "验证结果必须是对象列表。")
    by_method = {}
    for item in validations:
        if set(item) != {"method", "status", "evidence"}:
            raise ArchiveSliceFlowError("INVALID_CHECKPOINT", "验证结果字段不完整或包含未知字段。")
        method = required_string(item.get("method"), "validation_results.method", "INVALID_CHECKPOINT")
        if method in by_method:
            raise ArchiveSliceFlowError("INVALID_CHECKPOINT", "同一验证方法不能重复提交结果。")
        status = item.get("status")
        if status not in {"passed", "failed", "unverified"}:
            raise ArchiveSliceFlowError(
                "INVALID_CHECKPOINT",
                "验证状态只能是 passed、failed 或 unverified。",
            )
        evidence = list(string_list(
            item.get("evidence"), "validation_results.evidence", code="INVALID_CHECKPOINT"
        ))
        by_method[method] = {"method": method, "status": status, "evidence": evidence}
    methods = list(contract.get("validation_methods", []))
    if set(by_method) - set(methods):
        raise ArchiveSliceFlowError(
            "CHECKPOINT_VALIDATION_SET_MISMATCH",
            "检查点不能增加当前契约未登记的验证方法。",
        )
    normalized_validations = [by_method[str(method)] for method in methods if method in by_method]
    hard = value.get("observed_breakers")
    if not isinstance(hard, list) or any(not isinstance(item, dict) for item in hard):
        raise ArchiveSliceFlowError("INVALID_CHECKPOINT", "契约边界观察必须是对象列表。")
    normalized_hard = []
    seen_rules = set()
    for item in hard:
        rule = item.get("rule")
        expected = {"rule", "failed_assumption", "evidence"}
        if rule == "contract_condition":
            expected.add("condition")
            if item.get("condition") not in contract.get("circuit_breaker_conditions", []):
                raise ArchiveSliceFlowError("INVALID_CHECKPOINT", "切片特有熔断条件不属于当前契约。")
        elif rule not in HARD_BREAKER_RULES or rule == "write_scope_violation":
            raise ArchiveSliceFlowError("INVALID_CHECKPOINT", f"未知硬熔断规则：{rule}。")
        if rule in seen_rules:
            raise ArchiveSliceFlowError("INVALID_CHECKPOINT", "同一契约边界不能在一个检查点重复声明。")
        seen_rules.add(rule)
        if set(item) != expected:
            raise ArchiveSliceFlowError("INVALID_CHECKPOINT", "硬熔断命中字段不完整。")
        normalized = {
            "rule": rule,
            "failed_assumption": required_string(item.get("failed_assumption"), "failed_assumption"),
            "evidence": required_string(item.get("evidence"), "hard_triggers.evidence"),
        }
        if rule == "contract_condition":
            normalized["condition"] = item["condition"]
        normalized_hard.append(normalized)
    try:
        return {
            "contract_version": contract["version"],
            "hypothesis": required_string(value.get("hypothesis"), "hypothesis", "INVALID_CHECKPOINT"),
            "change_summary": required_string(value.get("change_summary"), "change_summary", "INVALID_CHECKPOINT"),
            "validation_results": normalized_validations,
            "discoveries": _checkpoint_strings(value, "discoveries", allow_empty=True),
            "next_step": required_string(value.get("next_step"), "next_step", "INVALID_CHECKPOINT"),
            "observed_breakers": normalized_hard,
        }
    except SliceContractError as exception:
        raise _convert(exception) from exception


def checkpoint_input_template(record: Mapping[str, object]) -> Mapping[str, object]:
    """依据当前切片契约生成精确检查点结构。"""

    package = record.get("package")
    contract = package.get("slice_contract") if isinstance(package, dict) else None
    if not isinstance(contract, dict):
        raise ArchiveSliceFlowError("SLICE_CONTRACT_REQUIRED", "当前切片缺少契约。")
    return {
        "contract_version": contract["version"],
        "hypothesis": "待填写：本轮实现所验证的契约假设",
        "change_summary": "待填写：本轮实际变化",
        "validation_results": [
            {
                "method": str(method),
                "status": "unverified",
                "evidence": ["待填写：验证证据或未验证原因"],
            }
            for method in contract.get("validation_methods", [])
        ],
        "discoveries": [],
        "next_step": "待填写：基于当前事实的下一步",
        "observed_breakers": [],
    }


def _build_breaker_report(
    report_id: str,
    triggered: Sequence[Mapping[str, object]],
    checkpoint: Mapping[str, object],
    owner: object,
) -> Mapping[str, object]:
    validations = checkpoint["validation_results"]
    completed = [str(item["method"]) for item in validations if item.get("status") == "passed"]
    incomplete = [
        str(item["method"])
        for item in validations
        if item.get("status") in {"failed", "unverified"}
    ]
    report = {
        "report_id": report_id,
        "created_at": _utc_now(),
        "triggered_rules": list(triggered),
        "failed_contract_assumptions": sorted({str(item["failed_assumption"]) for item in triggered}),
        "current_status": {"completed": completed, "incomplete": incomplete},
        "safe_verified_results": [
            evidence
            for item in validations if item.get("status") == "passed"
            for evidence in item.get("evidence", [])
        ],
        "discoveries": list(checkpoint.get("discoveries", [])),
        "allowed_actions": [dict(item) for item in BREAKER_ALLOWED_ACTIONS],
        "decision_owner": required_string(owner, "decision_owner", "BREAKER_REPORT_REQUIRED"),
    }
    report["report_digest"] = digest(report)
    return report


@serialized_workflow_state
def record_checkpoint(root: Path, feature_id: str, execution_id: str, checkpoint_file: Path) -> Mapping[str, object]:
    graph = validate_feature_archive(root, feature_id)
    feature = _require_complex_feature(graph, feature_id)
    state = _load_state(feature.path, feature_id)
    record = next(
        (item for item in _execution_state(state)["slices"].values() if isinstance(item, dict) and item.get("execution_id") == execution_id),
        None,
    )
    if not isinstance(record, dict):
        raise ArchiveSliceFlowError("IMPLEMENTATION_HALTED", "当前执行没有可恢复的实施切片。")
    package = record.get("package")
    contract = package.get("slice_contract") if isinstance(package, dict) else None
    if not isinstance(contract, dict):
        raise ArchiveSliceFlowError("SLICE_CONTRACT_REQUIRED", "当前切片缺少契约，不能继续实施。")
    raw = _read_json(checkpoint_file, "实施检查点")
    checkpoint = dict(_normalize_checkpoint(raw, contract))
    request_digest = digest(checkpoint)
    checkpoints = record.get("checkpoints")
    if not isinstance(checkpoints, list):
        raise ArchiveSliceFlowError("INVALID_EXECUTION_STATE", "实施检查点历史不合法。")
    if record.get("status") not in {"active", "circuit_open"}:
        raise ArchiveSliceFlowError(
            "IMPLEMENTATION_HALTED",
            "只有实施中的切片能记录检查点；熔断后禁止继续追加补丁。",
        )
    _, existing_lease = require_modification_lease(
        root, feature_id, str(package["package_id"])
    )
    existing_workspace = current_workspace_snapshot(existing_lease)
    existing_guard = current_workspace_guard_snapshot(existing_lease)
    for existing_checkpoint in checkpoints:
        if isinstance(existing_checkpoint, dict) and (
            existing_checkpoint.get("execution_id") == execution_id
            and existing_checkpoint.get("request_digest") == request_digest
            and existing_checkpoint.get("contract_digest") == digest(contract)
            and existing_checkpoint.get("workspace_digest") == existing_workspace.get("digest")
            and existing_checkpoint.get("workspace_guard_digest") == existing_guard.get("digest")
        ):
            reports = record.get("breaker_reports")
            report = (
                reports[-1]
                if existing_checkpoint.get("boundary_crossed") is True
                and isinstance(reports, list)
                and reports
                and isinstance(reports[-1], dict)
                else None
            )
            return {
                "status": record.get("status"),
                "package_id": package["package_id"],
                "checkpoint": existing_checkpoint,
                "breaker_report": report,
                "idempotent": True,
            }
    if record.get("status") != "active":
        raise ArchiveSliceFlowError("IMPLEMENTATION_HALTED", "只有实施中的切片能记录检查点；熔断后禁止继续追加补丁。")
    lease = existing_lease
    current = existing_workspace
    guard = existing_guard
    outside_changes = outside_scope_guard_changes(
        lease.get("baseline_guard_snapshot", {}),
        guard,
        tuple(str(item) for item in lease.get("write_scopes", [])),
    )
    hard_triggered = [{
        "kind": "hard",
        "rule": item["rule"],
        "failed_assumption": item["failed_assumption"],
        "evidence": item["evidence"],
        "label": HARD_BREAKER_RULES.get(item["rule"], "切片特有熔断条件"),
    } for item in checkpoint["observed_breakers"]]
    if outside_changes:
        hard_triggered.append({
            "kind": "system",
            "rule": "write_scope_violation",
            "failed_assumption": "实施变化全部位于契约授权写入范围内",
            "evidence": "授权范围外变化：" + "、".join(outside_changes),
            "paths": list(outside_changes),
            "label": HARD_BREAKER_RULES["write_scope_violation"],
        })
    triggered = hard_triggered
    if not triggered:
        from archive_svn import sync_svn_changelist

        grouping = sync_svn_changelist(root, feature_id, Path(str(lease["workspace_root"])))
        # SVN add/delete 改变状态，检查点必须绑定整理后的工作区。
        if grouping["status"] == "grouped":
            guard = current_workspace_guard_snapshot(lease)
    execution_ordinal = 1 + sum(
        1
        for item in checkpoints
        if isinstance(item, dict) and item.get("execution_id") == execution_id
    )
    checkpoint["checkpoint_id"] = f"{execution_id}-checkpoint-{execution_ordinal}"
    checkpoint.update({
        "execution_id": execution_id,
        "request_digest": request_digest,
        "recorded_at": _utc_now(),
        "contract_version": contract["version"],
        "contract_digest": digest(contract),
        "validation_set_digest": digest([
            item["method"] for item in checkpoint["validation_results"]
        ]),
        "workspace_digest": current["digest"],
        "workspace_guard_digest": guard["digest"],
        "boundary_crossed": bool(triggered),
        "boundary_rules": [str(item["rule"]) for item in triggered],
        "triggered_rules": triggered,
        "result": "breaker" if triggered else "continue",
    })
    checkpoint["checkpoint_digest"] = digest(checkpoint)
    report = None
    if triggered:
        reports = record.get("breaker_reports")
        if not isinstance(reports, list):
            raise ArchiveSliceFlowError("INVALID_EXECUTION_STATE", "熔断报告历史不合法。")
        report = _build_breaker_report(
            f"{package['package_id']}-breaker-{len(reports) + 1}",
            triggered,
            checkpoint,
            contract.get("decision_owner"),
        )
        reports.append(report)
        try:
            transition_slice_status(record, "circuit_open")
        except SliceContractError as exception:
            raise _convert(exception) from exception
        invalidate_final_approval(state)
    checkpoints.append(checkpoint)
    record["updated_at"] = _utc_now()
    if triggered:
        commit_workflow_and_lease_atomically(
            root,
            feature.path,
            feature_id,
            str(package["package_id"]),
            state,
            {"status": "circuit_open", "holder_execution_id": None},
        )
    else:
        _write_state(_state_path(feature.path), state)
    return {
        "status": record["status"],
        "package_id": package["package_id"],
        "checkpoint": checkpoint,
        "breaker_report": report,
        "idempotent": False,
    }
