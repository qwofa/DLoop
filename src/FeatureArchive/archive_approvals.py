"""DLoop 3.0 最小人工确认状态。"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Dict, Mapping, Tuple

from archive_changes import _body_fingerprint
from archive_profiles import (
    APPROVAL_STAGES,
    STRICT_PROFILE,
    WORKFLOW_STATE_NAME,
    WORKFLOW_STATE_SCHEMA_VERSION,
)
from archive_validation import ArchiveGraph, validate_feature_archive
from archive_slice_contract import latest_review, latest_review_snapshot, reviewed_candidate
from archive_slice_plan import empty_plan_state
from archive_workspace import (
    _canonical_digest,
    matching_workspace_guard_snapshot,
    serialized_workflow_state,
    snapshot_workspace,
)
from archive_configuration import (
    ArchiveConfigurationError,
    DLOOP_UI_CONFIGURATION,
    require_configuration_checkpoint,
)


STAGE_DOCUMENT_ROLES = {
    "requirements": "requirements.overview",
    "architecture": "design.overview",
    "final": "validation.overview",
}


class ArchiveApprovalError(Exception):
    """表示人工确认状态不满足最小合同。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def initial_workflow_state(
    feature_id: str,
    configuration: Mapping[str, object] | None = None,
) -> str:
    value = {
        "schema_version": WORKFLOW_STATE_SCHEMA_VERSION,
        "workflow_profile": STRICT_PROFILE,
        "feature_id": feature_id,
        "approvals": {stage: None for stage in APPROVAL_STAGES},
        "execution": {
            "slices": {},
            "used_execution_ids": [],
            "slice_plan": empty_plan_state(),
        },
    }
    if configuration is not None:
        value["configuration"] = dict(configuration)
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _state_path(feature_path: Path) -> Path:
    return feature_path / WORKFLOW_STATE_NAME


def _require_complex_feature(
    graph: ArchiveGraph,
    feature_id: str,
    *,
    require_writable: bool = True,
):
    feature = graph.features.get(feature_id)
    if feature is None:
        raise ArchiveApprovalError("UNKNOWN_FEATURE", f"交付项“{feature_id}”不存在。")
    try:
        manifest = json.loads((feature.path / "feature.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exception:
        raise ArchiveApprovalError(
            "INVALID_MANIFEST", f"无法读取交付项“{feature_id}”的生命周期清单。"
        ) from exception
    if manifest.get("workflow_profile") != STRICT_PROFILE:
        raise ArchiveApprovalError(
            "WORKFLOW_NOT_ENABLED", f"交付项“{feature_id}”不属于 DLoop 3.0 严格合同。"
        )
    if require_writable and feature.lifecycle in {"frozen", "pending_cleanup"}:
        raise ArchiveApprovalError(
            "READ_ONLY_ARCHIVE", f"交付项“{feature_id}”已只读。"
        )
    return feature


def _load_state(feature_path: Path, feature_id: str) -> Dict[str, object]:
    path = _state_path(feature_path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exception:
        raise ArchiveApprovalError(
            "INVALID_WORKFLOW_STATE", f"无法读取工作流状态“{path}”：{exception}"
        ) from exception
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != WORKFLOW_STATE_SCHEMA_VERSION
        or value.get("workflow_profile") != STRICT_PROFILE
        or value.get("feature_id") != feature_id
        or not isinstance(value.get("approvals"), dict)
        or not isinstance(value.get("execution"), dict)
    ):
        raise ArchiveApprovalError(
            "INVALID_WORKFLOW_STATE", f"工作流状态“{path}”结构或归属不合法。"
        )
    approvals = value["approvals"]
    execution = value["execution"]
    if any(stage not in approvals for stage in APPROVAL_STAGES) or not isinstance(
        execution.get("slices"), dict
    ) or not isinstance(execution.get("used_execution_ids"), list) or not isinstance(
        execution.get("slice_plan"), dict
    ):
        raise ArchiveApprovalError(
            "INVALID_WORKFLOW_STATE", f"工作流状态“{path}”缺少必要字段。"
        )
    return value


def _write_state(path: Path, value: Mapping[str, object]) -> None:
    staging = Path(tempfile.mkdtemp(prefix=".workflow-state-", dir=path.parent))
    temporary = staging / path.name
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temporary, path)
    except Exception as exception:
        raise ArchiveApprovalError(
            "WORKFLOW_STATE_COMMIT_FAILED", f"工作流状态提交失败：{exception}"
        ) from exception
    finally:
        try:
            staging.rmdir()
        except OSError:
            pass


def invalidate_final_approval(state: Mapping[str, object]) -> None:
    """新的修改执行开始时，使旧最终验收结论失效。"""

    approvals = state.get("approvals")
    if isinstance(approvals, dict):
        approvals["final"] = None


def accepted_candidate_summary(
    execution: Mapping[str, object],
) -> Mapping[str, object]:
    """派生稳定排序的已接受候选与独立评审集合。"""

    slices = execution.get("slices")
    if not isinstance(slices, dict):
        raise ArchiveApprovalError("INVALID_ACCEPTED_CANDIDATE", "执行状态缺少切片集合。")
    items = []
    for package_id in sorted(slices):
        record = slices[package_id]
        if not isinstance(record, dict) or record.get("status") != "accepted":
            continue
        snapshot = latest_review_snapshot(record)
        candidate = reviewed_candidate(record)
        review = latest_review(record)
        implementation_id = record.get("execution_id")
        if (
            not isinstance(snapshot, dict)
            or not isinstance(candidate, dict)
            or not isinstance(review, dict)
        ):
            raise ArchiveApprovalError(
                "INVALID_ACCEPTED_CANDIDATE",
                f"已接受切片“{package_id}”缺少不可变评审快照。",
            )
        candidate_id = candidate.get("candidate_id")
        candidate_digest = candidate.get("candidate_digest")
        review_execution_id = review.get("review_execution_id")
        if (
            not isinstance(package_id, str)
            or not isinstance(candidate_id, str)
            or not isinstance(candidate_digest, str)
            or not isinstance(implementation_id, str)
            or not isinstance(review_execution_id, str)
            or review.get("candidate_id") != candidate_id
            or review.get("candidate_digest") != candidate_digest
            or review.get("result") != "passed"
            or review_execution_id == implementation_id
            or record.get("candidate") is not None
            or not isinstance(snapshot.get("snapshot_id"), str)
            or not isinstance(snapshot.get("snapshot_digest"), str)
        ):
            raise ArchiveApprovalError(
                "INVALID_ACCEPTED_CANDIDATE",
                f"已接受切片“{package_id}”的候选与独立评审绑定不合法。",
            )
        items.append({
            "package_id": package_id,
            "candidate_id": candidate_id,
            "candidate_digest": candidate_digest,
            "review_execution_id": review_execution_id,
            "review_result": "passed",
        })
    return {"items": items, "digest": _canonical_digest(items)}


def require_accepted_implementation(
    execution: Mapping[str, object],
) -> Mapping[str, object]:
    """要求最终验收至少绑定一个已经独立评审通过的实现候选。"""

    summary = accepted_candidate_summary(execution)
    if not summary["items"]:
        raise ArchiveApprovalError(
            "ACCEPTED_IMPLEMENTATION_REQUIRED",
            "DLoop 交付必须至少包含一个已经独立评审通过的实现切片；没有实现切片的事项应退出 DLoop，按普通任务处理。",
        )
    return summary


def _integration_confirmation_binding(
    feature_path: Path,
    confirmation_path: Path,
    candidate_summary: Mapping[str, object],
    workspace_snapshot: Mapping[str, object],
) -> Mapping[str, object]:
    feature_root = feature_path.resolve()
    validation_root = (feature_root / "06-validation").resolve()
    supplied_path = confirmation_path.expanduser()
    if supplied_path.is_symlink():
        raise ArchiveApprovalError(
            "INVALID_INTEGRATION_CONFIRMATION",
            "集成确认必须是当前交付项 06-validation 目录中的普通文件。",
        )
    try:
        normalized_path = supplied_path.resolve(strict=True)
        relative_path = normalized_path.relative_to(feature_root)
        normalized_path.relative_to(validation_root)
    except (OSError, ValueError) as exception:
        raise ArchiveApprovalError(
            "INVALID_INTEGRATION_CONFIRMATION",
            "集成确认必须是当前交付项 06-validation 目录中的普通文件。",
        ) from exception
    if not normalized_path.is_file() or normalized_path.is_symlink():
        raise ArchiveApprovalError(
            "INVALID_INTEGRATION_CONFIRMATION",
            "集成确认必须是当前交付项 06-validation 目录中的普通文件。",
        )
    try:
        payload = normalized_path.read_bytes()
        report = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exception:
        raise ArchiveApprovalError(
            "INVALID_INTEGRATION_CONFIRMATION",
            f"无法读取集成确认：{exception}",
        ) from exception
    required_fields = {"result", "candidate_summary_digest", "workspace_guard_digest"}
    if not isinstance(report, dict) or set(report) != required_fields:
        raise ArchiveApprovalError(
            "INVALID_INTEGRATION_CONFIRMATION",
            "集成确认必须且只能包含结果、候选集合摘要和工作区摘要。",
        )
    if report.get("result") != "passed":
        raise ArchiveApprovalError(
            "INTEGRATION_CONFIRMATION_NOT_PASSED",
            "集成确认尚未通过。",
        )
    if report.get("candidate_summary_digest") != candidate_summary.get("digest"):
        raise ArchiveApprovalError(
            "INTEGRATION_CONFIRMATION_CANDIDATE_MISMATCH",
            "集成确认未绑定当前全部已接受候选。",
        )
    if report.get("workspace_guard_digest") != workspace_snapshot.get("digest"):
        raise ArchiveApprovalError(
            "INTEGRATION_CONFIRMATION_WORKSPACE_MISMATCH",
            "集成确认未绑定当前最终工作区。",
        )
    return {
        "confirmation_path": relative_path.as_posix(),
        "confirmation_digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
    }


def _integration_confirmation_is_current(
    feature_path: Path,
    binding: object,
) -> bool:
    if not isinstance(binding, dict) or set(binding) != {"confirmation_path", "confirmation_digest"}:
        return False
    confirmation_path = binding.get("confirmation_path")
    confirmation_digest = binding.get("confirmation_digest")
    if not isinstance(confirmation_path, str) or not isinstance(confirmation_digest, str):
        return False
    feature_root = feature_path.resolve()
    validation_root = (feature_root / "06-validation").resolve()
    supplied_path = feature_root / confirmation_path
    if supplied_path.is_symlink():
        return False
    try:
        path = supplied_path.resolve(strict=True)
        path.relative_to(validation_root)
        if not path.is_file() or path.is_symlink():
            return False
        payload = path.read_bytes()
    except (OSError, ValueError):
        return False
    return "sha256:" + hashlib.sha256(payload).hexdigest() == confirmation_digest


def last_accepted_candidate(
    execution: Mapping[str, object],
) -> Tuple[str, Mapping[str, object]] | None:
    """按执行身份的实际使用顺序定位最后一个已接受候选。"""

    used_execution_ids = execution.get("used_execution_ids")
    slices = execution.get("slices")
    if (
        not isinstance(used_execution_ids, list)
        or any(not isinstance(item, str) for item in used_execution_ids)
        or not isinstance(slices, dict)
    ):
        raise ArchiveApprovalError("INVALID_ACCEPTED_CANDIDATE", "执行身份顺序或切片集合不合法。")
    positions = {execution_id: index for index, execution_id in enumerate(used_execution_ids)}
    accepted = []
    for package_id, record in slices.items():
        if not isinstance(record, dict) or record.get("status") != "accepted":
            continue
        execution_id = record.get("execution_id")
        candidate = reviewed_candidate(record)
        if not isinstance(execution_id, str) or execution_id not in positions or not isinstance(candidate, dict):
            raise ArchiveApprovalError(
                "INVALID_ACCEPTED_CANDIDATE",
                f"已接受切片“{package_id}”缺少可排序的执行身份或候选。",
            )
        accepted.append((positions[execution_id], str(package_id), candidate))
    if not accepted:
        return None
    _, package_id, candidate = max(accepted, key=lambda item: item[0])
    return package_id, candidate


def _require_single_accepted_workspace(
    execution: Mapping[str, object],
) -> None:
    """拒绝无法形成单一最终工作区的已接受候选集合。"""

    slices = execution.get("slices")
    if not isinstance(slices, dict):
        raise ArchiveApprovalError("INVALID_ACCEPTED_CANDIDATE", "执行状态缺少切片集合。")
    roots = {
        candidate.get("workspace_root")
        for item in slices.values()
        if isinstance(item, dict)
        and item.get("status") == "accepted"
        for candidate in (reviewed_candidate(item),)
        if isinstance(candidate, dict)
        if isinstance(candidate.get("workspace_root"), str)
    }
    if len(roots) > 1:
        raise ArchiveApprovalError(
            "WORKSPACE_ROOT_CONFLICT",
            "已验收切片来自多个工作区，不能形成单一最终工作区摘要。",
        )


def matching_accepted_workspace_snapshot(
    workspace_root: Path,
    execution: Mapping[str, object],
) -> Mapping[str, object] | None:
    """核对版本管理状态及全部已接受产物，以最后一次接受的正文为准。"""

    accepted = last_accepted_candidate(execution)
    if accepted is None:
        return None
    _, candidate = accepted
    current = matching_workspace_guard_snapshot(
        workspace_root, candidate["workspace_root"], candidate["workspace_guard_digest"],
    )
    if current is None:
        return None
    records = {
        record["execution_id"]: record
        for record in execution["slices"].values()
        if record["status"] == "accepted"
    }
    expected = {}
    for execution_id in execution["used_execution_ids"]:
        if execution_id not in records:
            continue
        for change in reviewed_candidate(records[execution_id])["changes"]:
            expected[change["path"]] = (
                None if change["change"] == "deleted" else change["result_digest"]
            )
    # 只复核明确交付的产物，不将 Git 忽略的其他缓存纳入产品检查。
    contents = snapshot_workspace(workspace_root, tuple(expected))["entries"]
    if contents != {path: digest for path, digest in expected.items() if digest is not None}:
        return None
    return current


def _final_candidate_binding(
    feature_path: Path,
    execution: Mapping[str, object],
    integration_confirmation: Path | None,
) -> Mapping[str, object]:
    """一次核对最终候选集合、工作区和集成确认。"""

    candidate_summary = require_accepted_implementation(execution)
    _require_single_accepted_workspace(execution)
    last_candidate = last_accepted_candidate(execution)
    if last_candidate is None:
        raise ArchiveApprovalError(
            "FINAL_REVIEW_INPUT_INCOMPLETE",
            "最终验收缺少最后一个已接受候选。",
        )
    package_id, candidate = last_candidate
    workspace_root = candidate.get("workspace_root")
    workspace_snapshot = None
    if isinstance(workspace_root, str):
        try:
            workspace_snapshot = matching_accepted_workspace_snapshot(
                Path(workspace_root), execution,
            )
        except (OSError, ValueError):
            workspace_snapshot = None
    if workspace_snapshot is None:
        raise ArchiveApprovalError(
            "FINAL_CANDIDATE_CHANGED",
            f"最终工作区已偏离最后接受的切片“{package_id}”。",
        )
    if integration_confirmation is None:
        raise ArchiveApprovalError(
            "INTEGRATION_CONFIRMATION_REQUIRED",
            "存在已接受实现候选时必须提交当前集成确认文件。",
        )
    return {
        "candidate_summary": candidate_summary,
        "workspace_snapshot": workspace_snapshot,
        "integration_confirmation": _integration_confirmation_binding(
            feature_path,
            integration_confirmation,
            candidate_summary,
            workspace_snapshot,
        ),
    }


def final_review_bundle(
    feature_path: Path,
    execution: Mapping[str, object],
    integration_confirmation: Path | None,
) -> Mapping[str, object]:
    """返回已经完成候选、独立评审、工作区和集成确认核对的最终验收事实。"""

    accepted = []
    slices = execution.get("slices")
    if not isinstance(slices, dict):
        raise ArchiveApprovalError("INVALID_ACCEPTED_CANDIDATE", "执行状态缺少切片集合。")
    for package_id in sorted(slices):
        record = slices[package_id]
        if not isinstance(record, dict) or record.get("status") != "accepted":
            continue
        snapshot = latest_review_snapshot(record)
        candidate = reviewed_candidate(record)
        review = latest_review(record)
        package = record.get("package")
        if not all(isinstance(value, dict) for value in (snapshot, candidate, review, package)):
            raise ArchiveApprovalError(
                "FINAL_REVIEW_INPUT_INCOMPLETE",
                f"已接受切片“{package_id}”缺少完整任务包、候选或独立评审。",
            )
        accepted.append({
            "package_id": package_id,
            "task_package": package,
            "candidate": candidate,
            "review": review,
            "review_snapshot_id": snapshot.get("snapshot_id"),
            "review_snapshot_digest": snapshot.get("snapshot_digest"),
        })

    binding = _final_candidate_binding(
        feature_path,
        execution,
        integration_confirmation,
    )
    return {
        "accepted_candidate_summary": binding["candidate_summary"],
        "accepted_candidates": accepted,
        "integration_confirmation": binding["integration_confirmation"],
    }


def _document_snapshot(graph: ArchiveGraph, document_id: str) -> Mapping[str, str]:
    document = graph.documents.get(document_id)
    if document is None:
        raise ArchiveApprovalError(
            "APPROVAL_DOCUMENT_MISSING", f"确认输入“{document_id}”不存在。"
        )
    actual = _body_fingerprint(document.path.read_text(encoding="utf-8"), document.path)
    if actual != document.content_fingerprint:
        raise ArchiveApprovalError("UNCONFIRMED_EDIT", f"文档“{document.document_id}”正文尚未确认。")
    if document.content_status not in {"confirmed", "completed"}:
        raise ArchiveApprovalError(
            "APPROVAL_DOCUMENT_NOT_READY",
            f"文档“{document.document_id}”状态为 {document.content_status}。",
        )
    versions = {item.document_id: item.semantic_version for item in graph.documents.values()}
    stale = [
        dependency
        for dependency, consumed in document.dependency_versions.items()
        if versions.get(dependency) != consumed
    ]
    if stale:
        raise ArchiveApprovalError(
            "STALE_APPROVAL_INPUT", f"文档“{document.document_id}”使用了过期依赖。"
        )
    return {
        "document_id": document.document_id,
        "semantic_version": document.semantic_version,
        "content_fingerprint": actual,
    }


def _stage_snapshot(graph: ArchiveGraph, feature_id: str, stage: str) -> Mapping[str, str]:
    role = STAGE_DOCUMENT_ROLES.get(stage)
    if role is None:
        raise ArchiveApprovalError("INVALID_APPROVAL_STAGE", f"未知确认阶段：{stage}")
    return _document_snapshot(graph, f"{feature_id}.{role}")


def _approval_record_status(
    graph: ArchiveGraph,
    feature_id: str,
    stage: str,
    record: object,
    state: Mapping[str, object],
) -> str:
    if is_ui_delivery(state) and stage in {"requirements", "architecture"}:
        try:
            _stage_snapshot(graph, feature_id, stage)
        except ArchiveApprovalError:
            return "pending"
        return "ready"
    if not isinstance(record, dict):
        return "pending"
    current = _stage_snapshot(graph, feature_id, stage)
    saved = record.get("snapshot")
    if saved != current:
        return "stale"
    if stage == "requirements" and record.get("decision") == "approve":
        try:
            feature = graph.features[feature_id]
            require_configuration_checkpoint(
                feature.path,
                state,
                "requirements",
            )
        except ArchiveConfigurationError:
            return "stale"
    if stage == "final" and record.get("decision") == "approve":
        try:
            current_summary = accepted_candidate_summary(
                state["execution"]
            )
        except (ArchiveApprovalError, KeyError, TypeError):
            return "stale"
        if not current_summary["items"]:
            return "stale"
        if record.get("candidate_summary") != current_summary:
            return "stale"
        if not isinstance(record.get("workspace_snapshot"), dict):
            return "stale"
    if (
        stage == "final"
        and graph.features[feature_id].lifecycle not in {"frozen", "pending_cleanup"}
        and isinstance(record.get("workspace_snapshot"), dict)
    ):
        saved_workspace = record["workspace_snapshot"]
        workspace_root = saved_workspace.get("workspace_root")
        saved_digest = saved_workspace.get("digest")
        if not isinstance(workspace_root, str) or not isinstance(saved_digest, str):
            return "stale"
        try:
            current_workspace = matching_accepted_workspace_snapshot(Path(workspace_root), state["execution"])
        except Exception:
            return "stale"
        if current_workspace is None or current_workspace.get("digest") != saved_digest:
            return "stale"
    if (
        stage == "final"
        and record.get("decision") == "approve"
        and isinstance(record.get("integration_confirmation"), dict)
        and not _integration_confirmation_is_current(
            graph.features[feature_id].path,
            record.get("integration_confirmation"),
        )
    ):
        return "stale"
    decision = record.get("decision")
    return decision if isinstance(decision, str) else "pending"


def is_ui_delivery(state: Mapping[str, object]) -> bool:
    configuration = state.get("configuration")
    return isinstance(configuration, dict) and configuration.get("id") == DLOOP_UI_CONFIGURATION


def ui_interaction_review(graph: ArchiveGraph, feature_id: str, state: Mapping[str, object]) -> Mapping[str, object] | None:
    """最终展示绑定已核对的交互和当前已接受实现，不产生前置批准。"""
    if not is_ui_delivery(state):
        return None
    from archive_ui import interaction_plan_snapshot, UI_DELIVERY_PATH

    feature = graph.features[feature_id]
    if feature.lifecycle not in {"frozen", "pending_cleanup"}:
        require_configuration_checkpoint(feature.path, state, "final")
    interaction = interaction_plan_snapshot(feature.path, state["configuration"])
    candidates = accepted_candidate_summary(state["execution"])
    if not candidates["items"] or interaction["candidate_summary"] != candidates:
        raise ArchiveApprovalError("UI_DELIVERY_OUTDATED", "交互展示尚未与当前已接受实现核对，请补全后重新发布。")
    return {
        "reviewed_digest": interaction["plan_digest"],
        "materials": [str(feature.path / UI_DELIVERY_PATH),
                      str(feature.path / "04-plan/ui-annotation-plan.md")],
    }


@serialized_workflow_state
def approve_stage(
    root: Path,
    feature_id: str,
    stage: str,
    decision: str,
    integration_confirmation: Path | None = None,
    *,
    reviewed_digest: str | None = None,
    user_confirmation: str | None = None,
) -> Mapping[str, object]:
    if decision not in {"approve", "reject"}:
        raise ArchiveApprovalError("INVALID_APPROVAL_DECISION", "确认结论必须为 approve 或 reject。")
    if integration_confirmation is not None and (stage != "final" or decision != "approve"):
        raise ArchiveApprovalError(
            "INVALID_INTEGRATION_CONFIRMATION",
            "集成确认只能随最终批准提交。",
        )
    graph = validate_feature_archive(root, feature_id)
    feature = _require_complex_feature(graph, feature_id)
    state = _load_state(feature.path, feature_id)
    if is_ui_delivery(state) and stage in {"requirements", "architecture"}:
        raise ArchiveApprovalError("UI_INTERNAL_STAGE", "DloopUI 需求与设计是内部材料，请更新文档，不登记人工批准。")
    if stage not in STAGE_DOCUMENT_ROLES:
        raise ArchiveApprovalError("INVALID_APPROVAL_STAGE", f"未知确认阶段：{stage}")
    interaction = None
    if is_ui_delivery(state) and stage == "final":
        interaction = ui_interaction_review(graph, feature_id, state)
        if not isinstance(user_confirmation, str) or not user_confirmation.strip():
            raise ArchiveApprovalError("USER_CONFIRMATION_REQUIRED", "最终验收必须引用用户对当前展示的回复，不能用 AI 评审代替。")
        if reviewed_digest != interaction["reviewed_digest"]:
            raise ArchiveApprovalError("UI_DELIVERY_OUTDATED", "最终验收与展示版本不一致，请展示当前交付页。")
    elif reviewed_digest is not None or user_confirmation is not None:
        raise ArchiveApprovalError("INVALID_UI_DELIVERY_APPROVAL", "展示确认依据只适用于 DloopUI 最终验收。")
    if stage == "requirements" and decision == "approve":
        require_configuration_checkpoint(feature.path, state, "requirements")
    approvals = state["approvals"]
    requirements_record = approvals.get("requirements")
    requirements_status = _approval_record_status(
        graph,
        feature_id,
        "requirements",
        requirements_record,
        state,
    )
    if stage != "requirements" and requirements_status not in {"approve", "ready"}:
        raise ArchiveApprovalError("REQUIREMENTS_APPROVAL_REQUIRED", "需求共识尚未确认。")
    if stage == "final" and decision == "approve":
        architecture_status = _approval_record_status(
            graph,
            feature_id,
            "architecture",
            approvals.get("architecture"),
            state,
        )
        if architecture_status in {"reject", "stale"}:
            raise ArchiveApprovalError(
                "ARCHITECTURE_APPROVAL_BLOCKED",
                "架构决定已拒绝或失效，不能进行最终验收。",
            )
        execution = state["execution"]
        require_accepted_implementation(execution)

        from archive_workspace import final_execution_blockers

        blockers = final_execution_blockers(root, feature_id, state)
        if blockers:
            raise ArchiveApprovalError(
                "FINAL_EXECUTION_BLOCKED",
                "最终验收前仍有执行问题：" + "；".join(item["message"] for item in blockers),
            )
    snapshot = _stage_snapshot(graph, feature_id, stage)
    record = {"decision": decision, "snapshot": snapshot, "decided_at": _utc_now()}
    if is_ui_delivery(state) and stage == "final":
        record.update({"reviewed_digest": reviewed_digest, "user_confirmation": user_confirmation.strip()})
    if stage == "final" and decision == "approve":
        binding = _final_candidate_binding(
            feature.path,
            execution,
            integration_confirmation,
        )
        record["candidate_summary"] = binding["candidate_summary"]
        record["workspace_snapshot"] = binding["workspace_snapshot"]
        record["integration_confirmation"] = binding["integration_confirmation"]
        if interaction is not None:
            record["ui_interaction_digest"] = interaction["reviewed_digest"]
    approvals[stage] = record
    _write_state(_state_path(feature.path), state)
    return {"status": "approved" if decision == "approve" else "rejected", "stage": stage, **record}


def approval_status(root: Path, feature_id: str) -> Mapping[str, object]:
    graph = validate_feature_archive(root, feature_id)
    feature = _require_complex_feature(graph, feature_id, require_writable=False)
    state = _load_state(feature.path, feature_id)
    return approval_status_from_state(graph, feature_id, state)


def approval_status_from_state(
    graph: ArchiveGraph,
    feature_id: str,
    state: Mapping[str, object],
) -> Mapping[str, object]:
    """复用本次已校验的档案和状态，仍核对批准材料与工作区。"""

    result: Dict[str, object] = {}
    for stage in APPROVAL_STAGES:
        record = state["approvals"].get(stage)
        result[stage] = {
            "status": _approval_record_status(graph, feature_id, stage, record, state),
        }
        if isinstance(record, dict):
            result[stage]["decided_at"] = record.get("decided_at")
    requirements_status = result["requirements"]["status"]
    architecture_status = result["architecture"]["status"]
    if is_ui_delivery(state):
        try:
            interaction = ui_interaction_review(graph, feature_id, state)
            result["ui-delivery"] = {"status": "ready", "review": interaction}
        except (ArchiveApprovalError, ArchiveConfigurationError) as exception:
            interaction = None
            result["ui-delivery"] = {"status": "pending", "review_blocker": {
                "code": exception.code, "message": exception.message,
            }}
        if result["final"]["status"] in {"approve", "reject"} and (
            interaction is None
            or state["approvals"]["final"].get("reviewed_digest") != interaction["reviewed_digest"]
        ):
            result["final"]["status"] = "stale"
    if result["architecture"]["status"] != "pending" and requirements_status not in {"approve", "ready"}:
        result["architecture"]["status"] = "stale"
        architecture_status = "stale"
    if result["final"]["status"] != "pending" and (
        requirements_status not in {"approve", "ready"} or architecture_status in {"reject", "stale"}
    ):
        result["final"]["status"] = "stale"
    return result
