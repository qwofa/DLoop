"""DLoop 面向协调者的最小业务入口。"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from archive_approvals import (
    _load_state,
    _require_complex_feature,
    approve_stage,
)
from archive_candidates import (
    release_blocked_slice,
    release_orphaned_lease,
    retry_slice,
    review_candidate,
    submit_candidate,
)
from archive_execution import TaskPackageTarget, start_execution
from archive_slice_flow import (
    record_checkpoint,
    record_contract,
)
from archive_slice_plan_flow import (
    approve_slice_plan as approve_versioned_slice_plan,
    check_slice_plan as check_versioned_slice_plan,
)
from archive_validation import validate_feature_archive
from archive_workspace import (
    active_modification_lease,
    final_execution_blockers,
    modification_lease_summary,
)
from archive_context import (
    ContextRequest,
    context_summary as build_context_summary,
    _delivery_view as build_delivery_view,
)


class ArchiveTransactionError(Exception):
    """表示业务入口参数不完整。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def workflow_status(
    root: Path, feature_id: str | None = None, *, include_details: bool = False,
) -> Mapping[str, object]:
    lease = active_modification_lease(root)
    if feature_id is None:
        return {
            "status": "ready",
            "modification_lease": modification_lease_summary(lease),
            **({"details": {"modification_lease": lease}} if include_details else {}),
        }
    graph = validate_feature_archive(root, feature_id)
    feature = _require_complex_feature(graph, feature_id, require_writable=False)
    state = _load_state(feature.path, feature_id)
    from archive_snapshots import snapshot_status
    return {
        "status": "ready",
        "feature_id": feature_id,
        "delivery_view": build_delivery_view(graph, feature_id, state),
        "snapshots": snapshot_status(root, feature_id),
        **({"details": {
            "workflow_state": state,
            "modification_lease": lease,
            "final_blockers": (
                [] if feature.lifecycle in {"frozen", "pending_cleanup"}
                else list(final_execution_blockers(root, state))
            ),
        }} if include_details else {}),
    }


def context_summary(
    root: Path,
    request: ContextRequest,
) -> Mapping[str, object]:
    return build_context_summary(root, request)


def stage_action(
    root: Path,
    feature_id: str,
    stage: str,
    decision: str,
    integration_confirmation: Path | None = None,
    *,
    reviewed_digest: str | None = None,
    user_confirmation: str | None = None,
) -> Mapping[str, object]:
    return approve_stage(
        root,
        feature_id,
        stage,
        decision,
        integration_confirmation,
        reviewed_digest=reviewed_digest,
        user_confirmation=user_confirmation,
    )


def check_slice_plan(
    root: Path,
    feature_id: str,
    plan_file: Path,
) -> Mapping[str, object]:
    return check_versioned_slice_plan(root, feature_id, plan_file)


def approve_slice_plan(
    root: Path,
    feature_id: str,
    plan_file: Path,
    expected_digest: str,
) -> Mapping[str, object]:
    return approve_versioned_slice_plan(root, feature_id, plan_file, expected_digest)


def start_slice(
    root: Path,
    feature_id: str,
    execution_id: str,
    package_file: Path | None,
    workspace_root: Path,
    package_id: str | None = None,
) -> Mapping[str, object]:
    return start_execution(
        root,
        feature_id,
        execution_id,
        TaskPackageTarget(package_file=package_file, package_id=package_id),
        workspace_root,
    )


def prepare_slice_contract(
    root: Path,
    feature_id: str,
    package_file: Path,
) -> Mapping[str, object]:
    return record_contract(
        root,
        feature_id,
        TaskPackageTarget(package_file=package_file),
        False,
    )


def check_slice_contract(
    root: Path,
    feature_id: str,
    package_file: Path | None,
    package_id: str | None = None,
) -> Mapping[str, object]:
    return record_contract(
        root,
        feature_id,
        TaskPackageTarget(package_file=package_file, package_id=package_id),
        True,
    )


def checkpoint_slice(
    root: Path,
    feature_id: str,
    execution_id: str,
    checkpoint_file: Path,
) -> Mapping[str, object]:
    return record_checkpoint(root, feature_id, execution_id, checkpoint_file)


def submit_slice(
    root: Path,
    feature_id: str,
    execution_id: str,
    status: str,
    candidate_file: Path | None,
) -> Mapping[str, object]:
    return submit_candidate(root, feature_id, execution_id, status, candidate_file)


def review_slice(
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
    return review_candidate(
        root,
        feature_id,
        package_id,
        candidate_id,
        review_execution_id,
        result,
        issues_file,
        verification_file,
        verification_not_applicable_reason,
    )


def resolve_slice(
    root: Path,
    feature_id: str,
    action: str,
    package_id: str,
    execution_id: str | None,
    workspace_decision: str | None,
    amendment_file: Path | None = None,
) -> Mapping[str, object]:
    if action == "amend-scope":
        if amendment_file is None:
            raise ArchiveTransactionError("SCOPE_AMENDMENT_REQUIRED", "补登记范围须提供 --amendment-file，列明具体文件、漏登原因及已有业务授权。")
        from archive_slice_flow import amend_slice_scope
        return amend_slice_scope(root, feature_id, package_id, amendment_file)
    if action == "retry":
        if execution_id is None:
            raise ArchiveTransactionError("EXECUTION_ID_REQUIRED", "返修必须提供新的执行标识。")
        return retry_slice(root, feature_id, package_id, execution_id)
    if action == "release":
        if workspace_decision is None:
            raise ArchiveTransactionError("WORKSPACE_DECISION_REQUIRED", "释放阻塞切片必须声明工作区处理结果。")
        return release_blocked_slice(root, feature_id, package_id, workspace_decision)
    if action == "release-orphan":
        if workspace_decision is None:
            raise ArchiveTransactionError(
                "WORKSPACE_DECISION_REQUIRED",
                "释放孤立占用必须声明工作区已经恢复。",
            )
        return release_orphaned_lease(root, feature_id, package_id, workspace_decision)
    raise ArchiveTransactionError(
        "INVALID_RESOLUTION_ACTION",
        "收敛动作必须为 retry、amend-scope、release 或 release-orphan。",
    )
