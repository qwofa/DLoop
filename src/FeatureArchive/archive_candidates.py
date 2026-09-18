"""DLoop 当前候选、独立验收与人工收敛。"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Mapping, Sequence, Tuple

from archive_approvals import (
    _load_state,
    _require_complex_feature,
    _state_path,
    _utc_now,
    _write_state,
    invalidate_final_approval,
)
from archive_execution import (
    EXECUTION_ID_PATTERN,
    _execution_state,
    _require_ready_execution_inputs,
    ensure_execution_id_unused,
    find_slice_by_execution,
    mark_execution_id_used,
    require_slice,
)
from archive_slice_contract import (
    SliceContractError,
    digest as contract_digest,
    latest_review,
    latest_review_snapshot,
    reviewed_candidate,
    transition_slice_status,
)
from archive_slice_plan import SlicePlanError, require_slice_eligible
from archive_handoff import ArchiveHandoffError, _repair_role_projection, slice_handoff_materials
from archive_delivery_materials import delivery_paths, material_input_template, submitted_delivery
from archive_validation import validate_feature_archive
from archive_workspace_close import close_execution_atomically, commit_workflow_and_lease_atomically
from archive_workspace import (
    ArchiveWorkspaceError,
    acquire_modification_lease,
    current_workspace_guard_snapshot,
    current_workspace_snapshot,
    outside_scope_guard_changes,
    release_modification_lease,
    require_modification_lease,
    serialized_workflow_state,
    snapshot_changes,
)


class ArchiveCandidateError(Exception):
    """表示当前候选或独立验收不满足最小合同。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _transition(record: dict, target: str) -> None:
    try:
        transition_slice_status(record, target)
    except SliceContractError as exception:
        raise ArchiveCandidateError(exception.code, exception.message) from exception


def _digest(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _read_json(path: Path, label: str) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exception:
        raise ArchiveCandidateError("INVALID_CANDIDATE_INPUT", f"无法读取{label}“{path}”：{exception}") from exception
    if not isinstance(value, dict):
        raise ArchiveCandidateError("INVALID_CANDIDATE_INPUT", f"{label}必须是 JSON 对象。")
    return value


def _string_list(value: Mapping[str, object], field: str, allow_empty: bool = False) -> Sequence[str]:
    raw = value.get(field)
    if not isinstance(raw, list) or (not raw and not allow_empty):
        raise ArchiveCandidateError("INVALID_CANDIDATE_INPUT", f"字段 {field} 必须是字符串列表。")
    result = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ArchiveCandidateError("INVALID_CANDIDATE_INPUT", f"字段 {field} 包含空值。")
        result.append(item.strip())
    return tuple(result)


def _acceptance_scope(package: Mapping[str, object]) -> Mapping[str, object]:
    """固定返修需要复核的最小任务包事实。"""

    scope = {
        "goal": package.get("goal"),
        "write_scope": list(package.get("write_scope", [])),
        "non_goals": list(package.get("non_goals", [])),
        "acceptance_conditions": list(package.get("acceptance_conditions", [])),
        "validations": list(package.get("validations", [])),
        "slice_contract": package.get("slice_contract"),
    }
    if "delivery_requirements" in package:
        scope["delivery_requirements"] = package["delivery_requirements"]
    return {**scope, "digest": _digest(scope)}


def candidate_delivery_package(record: Mapping[str, object]) -> dict:
    """同一验收范围内返修时保留上一候选补充的材料要求。"""
    package = deepcopy(record["package"])
    previous = reviewed_candidate(record)
    if previous and previous.get("acceptance_scope_digest") == _acceptance_scope(package)["digest"]:
        keys = {item["key"] for item in package.get("delivery_requirements", [])}
        additional = [item for item in previous.get("delivery", {}).get("requirements", []) if item["key"] not in keys]
        if additional:
            package["delivery_requirements"].extend(deepcopy(additional))
    return package


def candidate_input_template(record: Mapping[str, object]) -> Mapping[str, object]:
    """依据当前实施切片生成候选结果输入。"""

    package = record.get("package")
    package_id = package.get("package_id") if isinstance(package, dict) else None
    execution_id = record.get("execution_id")
    if not isinstance(package_id, str) or not isinstance(execution_id, str):
        raise ArchiveCandidateError("EXECUTION_NOT_ACTIVE", "当前执行切片不完整。")
    snapshots = record.get("review_snapshots")
    ordinal = len(snapshots) + 1 if isinstance(snapshots, list) else 1
    template = {
        "candidate_id": f"{package_id}-candidate-{ordinal}",
        "verification": ["待填写：候选已通过的验证证据"],
        "unverified_boundaries": [],
    }
    if "delivery_requirements" in package:
        template["delivery_materials"] = material_input_template(candidate_delivery_package(record)["delivery_requirements"])
        template["additional_delivery_requirements"] = []
    return template


def candidate_matches_workspace(
    lease: Mapping[str, object],
    candidate: Mapping[str, object],
) -> bool:
    """判断当前工作区是否仍与固定候选完全一致。"""

    current = current_workspace_snapshot(lease)
    guard = current_workspace_guard_snapshot(lease)
    return (
        current.get("digest") == candidate.get("workspace_digest")
        and guard.get("digest") == candidate.get("workspace_guard_digest")
    )


def require_candidate_submission_ready(
    record: Mapping[str, object],
    lease: Mapping[str, object],
) -> Tuple[Mapping[str, object], Mapping[str, object], Sequence[Mapping[str, object]]]:
    """核对当前实施事实是否已达到候选提交门禁。"""

    package = record.get("package")
    contract = package.get("slice_contract") if isinstance(package, dict) else None
    checkpoints = record.get("checkpoints")
    if not isinstance(checkpoints, list) or not checkpoints:
        raise ArchiveCandidateError("FINAL_CHECKPOINT_REQUIRED", "候选提交前必须记录最终实施检查点。")
    checkpoint = checkpoints[-1]
    if not isinstance(checkpoint, dict) or not isinstance(contract, dict):
        raise ArchiveCandidateError("FINAL_CHECKPOINT_REQUIRED", "最终检查点或当前契约不完整。")
    if checkpoint.get("execution_id") != record.get("execution_id"):
        raise ArchiveCandidateError(
            "FINAL_CHECKPOINT_STALE",
            "最终检查点不属于当前执行，必须重新验证。",
        )
    validations = checkpoint.get("validation_results")
    if (
        checkpoint.get("result") != "continue"
        or not isinstance(validations, list)
        or not validations
        or any(not isinstance(item, dict) or item.get("status") != "passed" for item in validations)
    ):
        raise ArchiveCandidateError("FINAL_CHECKPOINT_NOT_PASSED", "最终检查点必须逐项通过当前契约全部验证。")
    if (
        checkpoint.get("contract_version") != contract.get("version")
        or checkpoint.get("contract_digest") != contract_digest(contract)
        or checkpoint.get("validation_set_digest") != contract_digest(contract.get("validation_methods"))
    ):
        raise ArchiveCandidateError("FINAL_CHECKPOINT_STALE", "最终检查点未绑定当前契约版本和验证集合。")
    current = current_workspace_snapshot(lease)
    guard = current_workspace_guard_snapshot(lease)
    if (
        checkpoint.get("workspace_digest") != current.get("digest")
        or checkpoint.get("workspace_guard_digest") != guard.get("digest")
    ):
        raise ArchiveCandidateError("CHECKPOINT_WORKSPACE_DRIFT", "工作区在最终检查点之后发生变化，必须重新验证。")
    outside_changes = outside_scope_guard_changes(
        lease["baseline_guard_snapshot"],
        guard,
        lease["write_scopes"],
    )
    if outside_changes:
        raise ArchiveCandidateError(
            "WORKSPACE_SCOPE_VIOLATION",
            "授权范围外存在执行期间变化：" + "，".join(outside_changes),
        )
    changes = tuple(snapshot_changes(lease["baseline_snapshot"], current))
    retained_paths = {path for amendment in record.get("scope_amendments", []) for path in amendment["paths"]}
    changed_paths = {item["path"] for item in changes}
    changes += tuple({"path": path, "change": "retained", "result_digest": current["entries"].get(path, "missing")}
                     for path in sorted(retained_paths - changed_paths))
    if not changes:
        raise ArchiveCandidateError("CANDIDATE_EMPTY", "修改切片没有产生授权范围内的实际变化。")
    return current, guard, changes


@serialized_workflow_state
def submit_candidate(
    root: Path,
    feature_id: str,
    execution_id: str,
    status: str,
    candidate_file: Path | None,
) -> Mapping[str, object]:
    if status not in {"completed", "failed", "interrupted"}:
        raise ArchiveCandidateError("INVALID_EXECUTION_STATUS", "执行结果不合法。")
    graph = validate_feature_archive(root, feature_id)
    feature = _require_complex_feature(graph, feature_id)
    state = _load_state(feature.path, feature_id)
    execution = _execution_state(state)
    record = find_slice_by_execution(execution, execution_id)
    if (
        status == "interrupted"
        and isinstance(record, dict)
        and record.get("status") == "interrupted"
    ):
        return {
            "status": "interrupted",
            "package_id": record["package"]["package_id"],
            "execution_id": execution_id,
            "workspace_restored": True,
            "idempotent": True,
        }
    if not isinstance(record, dict) or record.get("status") != "active":
        raise ArchiveCandidateError("EXECUTION_NOT_ACTIVE", "绑定的当前切片不存在或没有处于执行中。")
    package = record["package"]
    package_id = package["package_id"]
    if status == "interrupted":
        _, lease = require_modification_lease(root, feature_id, package_id)
        state_after = deepcopy(state)
        closed = find_slice_by_execution(_execution_state(state_after), execution_id)
        if not isinstance(closed, dict):
            raise ArchiveCandidateError("EXECUTION_NOT_ACTIVE", "绑定的当前切片不存在。")
        _transition(closed, "interrupted")
        closed["updated_at"] = _utc_now()
        closed["workspace_restored"] = True
        close_execution_atomically(
            root,
            feature.path,
            feature_id,
            package_id,
            state_after,
            lease,
        )
        return {
            "status": "interrupted",
            "package_id": package_id,
            "execution_id": execution_id,
            "workspace_restored": True,
            "idempotent": False,
        }
    if status == "failed":
        _transition(record, status)
        record["updated_at"] = _utc_now()
        _write_state(_state_path(feature.path), state)
        return {"status": status, "package_id": package_id, "execution_id": execution_id}
    if candidate_file is None:
        raise ArchiveCandidateError("CANDIDATE_REQUIRED", "完成执行必须提交候选结果。")
    value = _read_json(candidate_file, "候选结果")
    fields = {"candidate_id", "verification", "unverified_boundaries"}
    if "delivery_requirements" in package:
        fields.add("delivery_materials")
        if "additional_delivery_requirements" in value:
            fields.add("additional_delivery_requirements")
    if set(value) != fields:
        raise ArchiveCandidateError(
            "INVALID_CANDIDATE_INPUT",
            "候选结果字段不完整或包含未知字段。",
        )
    candidate_id = value.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id.strip():
        raise ArchiveCandidateError("INVALID_CANDIDATE_INPUT", "候选标识不能为空。")
    candidate = {
        "candidate_id": candidate_id.strip(),
        "verification": list(_string_list(value, "verification")),
        "unverified_boundaries": list(_string_list(value, "unverified_boundaries", True)),
        "submitted_at": _utc_now(),
        "implementation_execution_id": execution_id,
        "acceptance_scope_digest": _acceptance_scope(package)["digest"],
    }
    try:
        candidate_plan_binding = require_slice_eligible(
            execution["slice_plan"], execution["slices"], package_id
        )
    except SlicePlanError as exception:
        raise ArchiveCandidateError(exception.code, exception.message) from exception
    repair_handoff = record.get("repair_handoff")
    if isinstance(repair_handoff, dict):
        candidate["repair_source"] = {
            "original_candidate_id": repair_handoff.get("original_candidate_id"),
            "original_candidate_digest": repair_handoff.get("original_candidate_digest"),
            "review_execution_id": repair_handoff.get("review_execution_id"),
            "issues": list(repair_handoff.get("issues", [])),
            "acceptance_scope_digest": repair_handoff.get("acceptance_scope_digest"),
            "review_snapshot_id": repair_handoff.get("review_snapshot_id"),
            "review_snapshot_digest": repair_handoff.get("review_snapshot_digest"),
        }
    _, lease = require_modification_lease(root, feature_id, package_id)
    _require_ready_execution_inputs(graph, feature_id)
    materials = slice_handoff_materials(graph, feature, Path(lease["workspace_root"]), package)
    if "delivery_requirements" in package:
        candidate["delivery"] = submitted_delivery(candidate_delivery_package(record), value, feature.path, Path(lease["workspace_root"]))
        materials.extend(delivery_paths(candidate))
    _, repair_blockers = _repair_role_projection(record)
    if repair_blockers:
        raise ArchiveHandoffError(repair_blockers)
    current, guard, changes = require_candidate_submission_ready(record, lease)
    candidate["workspace_digest"] = current["digest"]
    candidate["workspace_root"] = lease["workspace_root"]
    candidate["workspace_guard_digest"] = guard["digest"]
    candidate["changes"] = [dict(item) for item in changes]
    candidate["scope_amendments"] = [{k: v for k, v in item.items() if k != "retained_snapshot"}
                                     for item in record.get("scope_amendments", [])]
    candidate["slice_plan_binding"] = {
        "plan_version": candidate_plan_binding["plan_version"],
        "plan_digest": candidate_plan_binding["plan_digest"],
        "history_ref": "slice_plan.history",
    }
    candidate["candidate_digest"] = _digest(candidate)
    record["candidate"] = candidate
    _transition(record, "candidate")
    record["updated_at"] = _utc_now()
    commit_workflow_and_lease_atomically(
        root,
        feature.path,
        feature_id,
        package_id,
        state,
        {
            "status": "candidate",
            "holder_execution_id": execution_id,
            "current_candidate_id": candidate_id.strip(),
        },
    )
    return {
        "status": "candidate", "package_id": package_id, **candidate,
        "handoff_ready": True, "required_materials": materials,
    }


def _review_issues(path: Path | None, required: bool) -> Sequence[str]:
    if path is None:
        if required:
            raise ArchiveCandidateError("REVIEW_ISSUES_REQUIRED", "未通过或未验证必须提交问题。")
        return ()
    value = _read_json(path, "验收问题")
    return _string_list(value, "issues", allow_empty=not required)


def _review_verification(path: Path | None) -> Sequence[str]:
    if path is None:
        return ()
    value = _read_json(path, "评审验证证据")
    if set(value) != {"verification"}:
        raise ArchiveCandidateError(
            "INVALID_REVIEW_VERIFICATION",
            "评审验证证据必须且只能包含 verification。",
        )
    return _string_list(value, "verification")


@serialized_workflow_state
def review_candidate(
    root: Path,
    feature_id: str,
    package_id: str,
    candidate_id: str,
    review_execution_id: str,
    result: str,
    issues_file: Path | None,
    verification_file: Path | None = None,
    verification_not_applicable_reason: str | None = None,
) -> Mapping[str, object]:
    if not EXECUTION_ID_PATTERN.fullmatch(review_execution_id):
        raise ArchiveCandidateError("INVALID_EXECUTION_ID", "验收执行标识不合法。")
    if result not in {"passed", "failed", "unverified"}:
        raise ArchiveCandidateError("INVALID_REVIEW_RESULT", "验收结论不合法。")
    graph = validate_feature_archive(root, feature_id)
    feature = _require_complex_feature(graph, feature_id)
    state = _load_state(feature.path, feature_id)
    record = require_slice(state, package_id)
    candidate = record.get("candidate")
    if record.get("status") != "candidate" or not isinstance(candidate, dict):
        raise ArchiveCandidateError("CANDIDATE_NOT_READY", "当前切片没有待验收候选。")
    if candidate.get("candidate_id") != candidate_id:
        raise ArchiveCandidateError("CANDIDATE_MISMATCH", "验收目标不是当前候选。")
    if record.get("execution_id") == review_execution_id:
        raise ArchiveCandidateError("REVIEW_NOT_INDEPENDENT", "实现执行不能验收自己的候选。")
    ensure_execution_id_unused(_execution_state(state), review_execution_id)
    _, lease = require_modification_lease(root, feature_id, package_id)
    if not candidate_matches_workspace(lease, candidate):
        raise ArchiveCandidateError("CANDIDATE_CHANGED", "工作区已偏离当前候选，旧候选不能验收。")
    if result == "passed":
        delivery_paths(candidate)
    issues = list(_review_issues(issues_file, result != "passed"))
    verification = list(_review_verification(verification_file))
    not_applicable_reason = (
        verification_not_applicable_reason.strip()
        if isinstance(verification_not_applicable_reason, str)
        and verification_not_applicable_reason.strip()
        else None
    )
    if verification and not_applicable_reason is not None:
        raise ArchiveCandidateError(
            "INVALID_REVIEW_VERIFICATION",
            "评审验证证据与不适用理由不能同时提交。",
        )
    if result != "passed" and not_applicable_reason is not None:
        raise ArchiveCandidateError(
            "INVALID_REVIEW_VERIFICATION",
            "未通过或未验证结论不能使用验证不适用理由。",
        )
    review = {
        "candidate_id": candidate_id,
        "candidate_digest": candidate["candidate_digest"],
        "review_execution_id": review_execution_id,
        "result": result,
        "issues": issues,
        "verification": verification,
        "verification_not_applicable_reason": not_applicable_reason,
        "reviewed_at": _utc_now(),
        "acceptance_scope_digest": _acceptance_scope(record["package"])["digest"],
    }
    snapshots = record.get("review_snapshots")
    if not isinstance(snapshots, list):
        raise ArchiveCandidateError(
            "INVALID_EXECUTION_STATE",
            "当前切片缺少不可变评审快照集合。",
        )
    snapshot = {
        "snapshot_id": f"{package_id}-review-{len(snapshots) + 1}",
        "candidate": deepcopy(candidate),
        "review": deepcopy(review),
        "recorded_at": review["reviewed_at"],
    }
    snapshot["snapshot_digest"] = _digest(snapshot)
    snapshots.append(snapshot)
    record["candidate"] = None
    _transition(record, "accepted" if result == "passed" else "review_failed")
    record["updated_at"] = _utc_now()
    mark_execution_id_used(_execution_state(state), review_execution_id)
    if result == "passed":
        commit_workflow_and_lease_atomically(
            root,
            feature.path,
            feature_id,
            package_id,
            state,
            {},
            release_lease=True,
        )
    else:
        _write_state(_state_path(feature.path), state)
    return {"status": record["status"], "package_id": package_id, **review}


@serialized_workflow_state
def retry_slice(
    root: Path,
    feature_id: str,
    package_id: str,
    execution_id: str,
) -> Mapping[str, object]:
    if not EXECUTION_ID_PATTERN.fullmatch(execution_id):
        raise ArchiveCandidateError("INVALID_EXECUTION_ID", "执行标识不合法。")
    graph = validate_feature_archive(root, feature_id)
    feature = _require_complex_feature(graph, feature_id)
    state = _load_state(feature.path, feature_id)
    ensure_execution_id_unused(_execution_state(state), execution_id)
    record = require_slice(state, package_id)
    slice_status = record.get("status")
    lease = None
    if slice_status != "interrupted":
        _, lease = require_modification_lease(root, feature_id, package_id)
    candidate = record.get("candidate")
    candidate_drifted = (
        slice_status == "candidate"
        and isinstance(candidate, dict)
        and isinstance(lease, dict)
        and not candidate_matches_workspace(lease, candidate)
    )
    if slice_status not in {"review_failed", "failed", "interrupted", "circuit_open"} and not candidate_drifted:
        raise ArchiveCandidateError("SLICE_NOT_RETRYABLE", "当前切片不需要返修。")
    if lease is not None:
        outside_changes = outside_scope_guard_changes(
            lease["baseline_guard_snapshot"], current_workspace_guard_snapshot(lease),
            tuple(lease["write_scopes"]),
        )
        if outside_changes:
            version = record["package"]["slice_contract"]["version"]
            raise ArchiveCandidateError(
                "RETRY_SCOPE_UNRESOLVED",
                f"原范围外变化尚未处理，当前生效契约为第 {version} 版："
                + "、".join(outside_changes)
                + "。重试沿用原授权范围和基线，更换执行身份或修改任务文件不会使新范围生效。"
                "先保留现场；恢复这些范围外变化后可原范围重试。确需修订范围时，"
                "若仅漏登原业务范围内的具体文件，使用 resolve-slice --action amend-scope --amendment-file 补登记；业务扩展仍须重新确认范围。"
                "新增范围的既有修改须保留完整差异供评审，不能直接当作干净基线。",
            )
    reviewed = reviewed_candidate(record)
    review = latest_review(record)
    if slice_status == "review_failed" and (
        not isinstance(reviewed, dict) or not isinstance(review, dict)
    ):
        raise ArchiveCandidateError(
            "REPAIR_HANDOFF_MISSING",
            "评审失败切片缺少固定候选或问题，不能开始返修。",
        )
    if isinstance(reviewed, dict) and isinstance(review, dict):
        scope = _acceptance_scope(record["package"])
        latest_snapshot = latest_review_snapshot(record)
        record["repair_handoff"] = {
            "original_candidate_id": reviewed.get("candidate_id"),
            "original_candidate_digest": reviewed.get("candidate_digest"),
            "review_execution_id": review.get("review_execution_id"),
            "issues": list(review.get("issues", [])),
            "acceptance_scope": scope,
            "acceptance_scope_digest": scope["digest"],
            "review_snapshot_id": (
                latest_snapshot.get("snapshot_id")
                if isinstance(latest_snapshot, dict)
                else None
            ),
            "review_snapshot_digest": (
                latest_snapshot.get("snapshot_digest")
                if isinstance(latest_snapshot, dict)
                else None
            ),
        }
    _require_ready_execution_inputs(graph, feature_id)
    materials = slice_handoff_materials(
        graph, feature, Path(record["workspace_root"]), record["package"]
    )
    repair_handoff, repair_blockers = _repair_role_projection(record)
    if repair_blockers:
        raise ArchiveHandoffError(repair_blockers)
    record["execution_id"] = execution_id
    record["candidate"] = None
    _transition(record, "active")
    record["updated_at"] = _utc_now()
    mark_execution_id_used(_execution_state(state), execution_id)
    invalidate_final_approval(state)
    acquired_for_retry = False
    if slice_status == "interrupted":
        workspace_root = record.get("workspace_root")
        package = record.get("package")
        write_scope = package.get("write_scope") if isinstance(package, dict) else None
        if not isinstance(workspace_root, str) or not isinstance(write_scope, list):
            raise ArchiveCandidateError(
                "INVALID_EXECUTION_STATE",
                "中断切片缺少重新取得修改租约所需的工作区事实。",
            )
        lease = acquire_modification_lease(
            root,
            feature_id,
            package_id,
            Path(workspace_root),
            tuple(str(item) for item in write_scope),
            _utc_now(),
            holder_execution_id=execution_id,
        )
        acquired_for_retry = True
    assert isinstance(lease, dict)
    try:
        commit_workflow_and_lease_atomically(
            root,
            feature.path,
            feature_id,
            package_id,
            state,
            {
                "status": "active",
                "holder_execution_id": execution_id,
                "current_candidate_id": None,
            },
        )
    except Exception as exception:
        rollback_failed = (
            isinstance(exception, ArchiveWorkspaceError)
            and exception.code == "ATOMIC_STATE_ROLLBACK_FAILED"
        )
        if acquired_for_retry and not rollback_failed:
            try:
                release_modification_lease(root, feature_id, package_id)
            except Exception as cleanup_exception:
                raise ArchiveWorkspaceError(
                    "ATOMIC_STATE_ROLLBACK_FAILED",
                    "返修状态提交失败且无法释放新取得的修改租约。",
                ) from cleanup_exception
        raise
    result = {
        "status": "active", "package_id": package_id, "execution_id": execution_id,
        "handoff_ready": True, "required_materials": materials,
    }
    if repair_handoff is not None:
        result["repair_handoff"] = repair_handoff
    return result


@serialized_workflow_state
def release_blocked_slice(
    root: Path,
    feature_id: str,
    package_id: str,
    workspace_decision: str,
) -> Mapping[str, object]:
    if workspace_decision not in {"kept", "restored"}:
        raise ArchiveCandidateError(
            "WORKSPACE_DECISION_REQUIRED", "必须明确工作区变化是保留还是已恢复。"
        )
    graph = validate_feature_archive(root, feature_id)
    feature = _require_complex_feature(graph, feature_id)
    state = _load_state(feature.path, feature_id)
    record = require_slice(state, package_id)
    slice_status = record.get("status")
    if slice_status == "abandoned":
        return {
            "status": "abandoned",
            "package_id": package_id,
            "workspace_decision": workspace_decision,
            "idempotent": True,
        }
    if slice_status in {"contract_draft", "contract_failed", "ready"}:
        _transition(record, "abandoned")
        record["updated_at"] = _utc_now()
        invalidate_final_approval(state)
        _write_state(_state_path(feature.path), state)
        return {
            "status": "abandoned",
            "package_id": package_id,
            "workspace_decision": workspace_decision,
            "idempotent": False,
        }
    if slice_status == "accepted":
        if workspace_decision != "kept":
            raise ArchiveCandidateError(
                "SLICE_ALREADY_ACCEPTED",
                "已验收切片只允许保留候选后释放残留修改占用。",
            )
        _, lease = require_modification_lease(root, feature_id, package_id)
        candidate = reviewed_candidate(record)
        if (
            not isinstance(candidate, dict)
            or not candidate_matches_workspace(lease, candidate)
        ):
            raise ArchiveCandidateError("CANDIDATE_CHANGED", "工作区已偏离已验收候选，不能释放修改占用。")
        release_modification_lease(root, feature_id, package_id)
        return {"status": "accepted", "package_id": package_id, "workspace_decision": workspace_decision}

    candidate = record.get("candidate")
    candidate_drifted = False
    if slice_status == "candidate" and isinstance(candidate, dict):
        _, lease = require_modification_lease(root, feature_id, package_id)
        candidate_drifted = not candidate_matches_workspace(lease, candidate)
    if slice_status not in {"failed", "interrupted", "review_failed", "circuit_open"} and not candidate_drifted:
        raise ArchiveCandidateError(
            "SLICE_NOT_RELEASABLE",
            "只有失败、中断、熔断或验收未通过的切片才能在人工处理后放弃。",
        )

    if workspace_decision != "restored":
        raise ArchiveCandidateError(
            "UNREVIEWED_WORKSPACE_CHANGES",
            "未验收的工作区变化必须恢复，保留现场时继续阻塞。",
        )
    if slice_status == "interrupted":
        try:
            _, lease = require_modification_lease(root, feature_id, package_id)
        except ArchiveWorkspaceError as exception:
            if exception.code != "MODIFICATION_LEASE_CONFLICT":
                raise
            _transition(record, "abandoned")
            record["updated_at"] = _utc_now()
            _write_state(_state_path(feature.path), state)
            return {
                "status": "abandoned",
                "package_id": package_id,
                "workspace_decision": workspace_decision,
                "idempotent": False,
            }
    else:
        _, lease = require_modification_lease(root, feature_id, package_id)

    if slice_status == "circuit_open":
        current = current_workspace_snapshot(lease)
        guard = current_workspace_guard_snapshot(lease)
        if (
            current.get("digest") != lease.get("baseline_digest")
            or guard.get("digest") != lease.get("baseline_guard_digest")
        ):
            raise ArchiveCandidateError(
                "WORKSPACE_NOT_RESTORED",
                "熔断切片只能在工作区已由人工恢复到执行基线后终止。",
            )
        state_after = deepcopy(state)
        abandoned = require_slice(state_after, package_id)
        _transition(abandoned, "abandoned")
        abandoned["updated_at"] = _utc_now()
        abandoned["workspace_restored"] = True
        commit_workflow_and_lease_atomically(
            root,
            feature.path,
            feature_id,
            package_id,
            state_after,
            {},
            release_lease=True,
        )
        return {
            "status": "abandoned",
            "package_id": package_id,
            "workspace_decision": workspace_decision,
            "idempotent": False,
        }

    state_after = deepcopy(state)
    abandoned = require_slice(state_after, package_id)
    _transition(abandoned, "abandoned")
    abandoned["updated_at"] = _utc_now()
    abandoned["workspace_restored"] = True
    close_execution_atomically(
        root,
        feature.path,
        feature_id,
        package_id,
        state_after,
        lease,
    )
    return {
        "status": "abandoned",
        "package_id": package_id,
        "workspace_decision": workspace_decision,
        "idempotent": False,
    }


@serialized_workflow_state
def release_orphaned_lease(
    root: Path,
    feature_id: str,
    package_id: str,
    workspace_decision: str,
) -> Mapping[str, object]:
    if workspace_decision != "restored":
        raise ArchiveCandidateError(
            "WORKSPACE_DECISION_REQUIRED",
            "孤立修改占用只允许在工作区已恢复时释放。",
        )
    graph = validate_feature_archive(root, feature_id)
    feature = _require_complex_feature(graph, feature_id)
    state = _load_state(feature.path, feature_id)
    execution = _execution_state(state)
    if package_id in execution["slices"]:
        raise ArchiveCandidateError(
            "LEASE_NOT_ORPHANED",
            "修改占用存在对应切片，必须使用正常返修或释放路径。",
        )
    _, lease = require_modification_lease(root, feature_id, package_id)
    current = current_workspace_snapshot(lease)
    guard = current_workspace_guard_snapshot(lease)
    if (
        current["digest"] != lease.get("baseline_digest")
        or guard["digest"] != lease.get("baseline_guard_digest")
    ):
        raise ArchiveCandidateError(
            "WORKSPACE_NOT_RESTORED",
            "孤立修改占用对应的工作区尚未恢复到执行前状态。",
        )
    release_modification_lease(root, feature_id, package_id)
    return {
        "status": "released",
        "package_id": package_id,
        "workspace_decision": workspace_decision,
        "recovery": "orphaned-lease",
    }
