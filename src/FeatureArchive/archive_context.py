"""面向 Agent 的紧凑上下文摘要。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Dict, Mapping, Sequence, Tuple

from archive_approvals import (
    ArchiveApprovalError,
    _document_snapshot,
    _integration_confirmation_is_current,
    _load_state,
    _require_complex_feature,
    accepted_candidate_summary,
    approval_status_from_state,
    final_review_bundle,
    last_accepted_candidate,
    matching_accepted_workspace_snapshot,
    requirements_blocker,
    ui_interaction_review,
)
from archive_candidates import (
    ArchiveCandidateError,
    candidate_matches_workspace,
    require_candidate_submission_ready,
)
from archive_changes import _body_fingerprint, refresh_queue
from archive_execution import EXECUTION_ID_PATTERN, _execution_state
from archive_handoff import ArchiveHandoffError, _repair_role_projection, slice_handoff_materials
from archive_slice_contract import latest_review, reviewed_candidate
from archive_slice_plan import (
    current_plan,
    evaluate_plan,
    pending_plan_slices,
)
from archive_validation import ArchiveGraph, DocumentRecord, validate_feature_archive
from archive_workspace import (
    active_modification_lease,
    final_execution_blockers,
    matching_workspace_guard_snapshot,
    modification_lease_summary,
)
from archive_paths import (
    project_root_from_archive_root,
    share_recovery_details,
    share_snapshot_ancestor,
    stage_overview_target,
)
from archive_configuration import ArchiveConfigurationError
from archive_action_inputs import action_input_preparation
from archive_failure_attribution import friction_note_contract


CONTEXT_SUMMARY_VERSION = 2
CONTEXT_ACTIONS = (
    "requirements",
    "investigation",
    "design",
    "plan",
    "implementation",
    "validation",
    "freeze",
    "cleanup",
)
WRITABLE_ACTION_ORDER = {
    "requirements": 0,
    "investigation": 1,
    "design": 2,
    "plan": 3,
    "implementation": 4,
    "validation": 5,
}
DELIVERY_STAGE_WRITE_BOUNDARIES = {
    "requirements": "requirements",
    "activation": "requirements",
    "design": "design",
    "plan": "plan",
    "ui-inputs": "investigation",
    "ui-inputs-blocked": "investigation",
    "ui-baseline-review": "plan",
    "ui-interaction-review": "validation",
    "contract-check": "implementation",
    "contract-revision": "implementation",
    "implementation-ready": "implementation",
    "implementation-required": "implementation",
    "implementation-recovery": "implementation",
    "implementation": "implementation",
    "candidate-review": "implementation",
    "implementation-complete": "implementation",
    "validation": "validation",
    "validation-recovery": "validation",
    "final-review": "validation",
    "freeze-ready": "validation",
}
ACTION_CONTRACT_SPECS: Mapping[str, Mapping[str, object]] = {
    "transition-lifecycle": {
        "accepted_arguments": ("to",),
        "required_inputs": (),
    },
    "stage-action": {
        "accepted_arguments": ("stage", "reviewed_digest"),
        "required_inputs": ("decision",),
    },
    "checkpoint-slice": {
        "accepted_arguments": ("execution_id",),
        "required_inputs": ("checkpoint_file",),
        "input_kind": "checkpoint",
    },
    "submit-slice": {
        "accepted_arguments": ("execution_id",),
        "required_inputs": ("status", "candidate_file（status=completed 时）"),
        "input_kind": "candidate",
    },
    "review-slice": {
        "accepted_arguments": ("package_id", "candidate_id"),
        "required_inputs": ("result", "issues_file（result!=passed 时）"),
        "identity": ("review_execution_id", "review"),
    },
    "resolve-slice": {
        "accepted_arguments": ("package_id", "action"),
        "required_inputs": ("action", "workspace_decision（释放时）", "amendment_file（补登记范围时）"),
    },
    "start-slice": {
        "accepted_arguments": ("package_id",),
        "required_inputs": (),
        "identity": ("execution_id", "implementation"),
        "include_workspace_root": True,
    },
    "check-slice-contract": {
        "accepted_arguments": ("package_id",),
        "required_inputs": (),
    },
    "ui-investigate": {
        "accepted_arguments": (),
        "required_inputs": ("input",),
    },
    "ui-baseline": {
        "accepted_arguments": ("semantic_change",),
        "required_inputs": ("input",),
        "input_kind": "ui-baseline",
    },
    "ui-publish": {
        "accepted_arguments": (),
        "required_inputs": ("input",),
    },
}
DESIGN_REVIEW_MATERIAL_SUFFIXES = (
    "requirements.overview",
    "design.overview",
)
DESIGN_REVIEW_SCAFFOLD_LINES = frozenset({
    "记录方案、权衡、接口边界和设计决策。",
    "尚待补充。",
    "当前没有专题文档。仅在内容复杂度确有需要时新增。",
})
DESIGN_REVIEW_PLACEHOLDER_LINES = frozenset({
    "todo",
    "tbd",
    "待补充",
    "尚待补充",
    "待定",
})


class ArchiveContextError(Exception):
    """表示上下文摘要请求不满足稳定合同。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ContextRequest:
    feature_id: str
    action: str
    role: str | None
    execution_id: str | None = None
    entry_question: str | None = None
    allowed_materials: Tuple[str, ...] = ()
    integration_confirmation: Path | None = None


@dataclass(frozen=True)
class RoleProjectionContext:
    graph: ArchiveGraph
    state: Mapping[str, object]
    request: ContextRequest
    materials: Sequence[Mapping[str, object]]
    delivery: Mapping[str, object]
    requirements_status: str
    stale_documents: Sequence[str]


RoleProjectionResult = Tuple[
    Mapping[str, object] | None,
    Sequence[Mapping[str, object]],
]


@dataclass(frozen=True)
class RoleContract:
    builder: Callable[[RoleProjectionContext], RoleProjectionResult]
    task_message_fields: Tuple[str, ...]
    allowed_action: str | None = None
    blocking_stale_suffixes: Tuple[str, ...] | None = None
    required_stage: str | Tuple[str, ...] | None = None
    requires_clear_architecture: bool = False
    all_or_nothing: bool = True
    action_mismatch_message: str = "当前角色不能执行请求的上下文动作。"
    stage_mismatch_message: str = "统一交付投影当前不允许进入该角色合同。"


def _feature_document(graph: ArchiveGraph, feature_id: str, suffix: str) -> DocumentRecord | None:
    return graph.documents.get(f"{feature_id}.{suffix}")


def _ready(document: DocumentRecord | None) -> bool:
    return document is not None and document.content_status in {"confirmed", "completed"}


def _suggest_execution_id(
    execution: Mapping[str, object],
    package_id: str,
    purpose: str,
) -> str:
    """依据已使用身份生成可读建议；实际执行仍由现有冲突检查最终确认。"""

    used = {
        str(item)
        for item in execution.get("used_execution_ids", [])
        if isinstance(item, str)
    }
    base = f"{package_id}-{purpose}"
    candidate = base
    ordinal = 2
    while candidate in used:
        candidate = f"{base}-{ordinal}"
        ordinal += 1
    return candidate


def _document_write_action(
    document: DocumentRecord | None,
    *,
    expected_status: str,
    reason: str,
) -> Mapping[str, object]:
    return {
        "kind": "write",
        "target": str(document.path) if document is not None else None,
        "expected_status": expected_status,
        "reason": reason,
    }


def _document_readiness_issue(
    document: DocumentRecord | None,
    *,
    allowed_statuses: Sequence[str],
    stale_documents: Sequence[str],
    label: str,
) -> Tuple[str, str] | None:
    if document is None or document.content_status not in allowed_statuses:
        return "APPROVAL_DOCUMENT_NOT_READY", f"{label}尚未达到可交付状态。"
    if document.document_id in stale_documents:
        return "STALE_DOCUMENTS", f"{label}依赖已经过期。"
    actual = _body_fingerprint(
        document.path.read_text(encoding="utf-8"),
        document.path,
    )
    if actual != document.content_fingerprint:
        return "UNCONFIRMED_EDIT", f"{label}正文修改尚未确认。"
    return None


def _action_is_open_for_writing(action: str, delivery_stage: object) -> bool:
    boundary = DELIVERY_STAGE_WRITE_BOUNDARIES.get(str(delivery_stage))
    action_order = WRITABLE_ACTION_ORDER.get(action)
    boundary_order = WRITABLE_ACTION_ORDER.get(boundary or "")
    return (
        action_order is not None
        and boundary_order is not None
        and action_order <= boundary_order
    )


def _allowed_actions(graph: ArchiveGraph, feature_id: str) -> Sequence[str]:
    feature = graph.features[feature_id]
    if feature.lifecycle == "draft":
        return ("requirements",)
    if feature.lifecycle == "active":
        actions = ["investigation", "design", "plan"]
        if _ready(_feature_document(graph, feature_id, "design.overview")) and _ready(
            _feature_document(graph, feature_id, "plan.overview")
        ):
            actions.append("implementation")
        actions.append("validation")
        return tuple(actions)
    if feature.lifecycle == "validating":
        return ("validation", "freeze")
    return ("cleanup",)


def _material(document: DocumentRecord | None, purpose: str) -> Mapping[str, object] | None:
    if document is None:
        return None
    try:
        text = document.path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exception:
        raise ArchiveContextError(
            "CONTEXT_MATERIAL_UNREADABLE",
            f"无法读取上下文材料“{document.document_id}”：{exception}",
        ) from exception
    if "## 当前摘要" in text:
        return {
            "source": document.document_id,
            "purpose": purpose,
            "mode": "sections",
            "sections": ["当前摘要"],
        }
    return {"source": document.document_id, "purpose": purpose, "mode": "full"}


def _canonical_material_path(graph: ArchiveGraph, document: DocumentRecord) -> str:
    """返回不依赖当前工作目录的档案根相对路径。"""

    return document.path.resolve().relative_to(graph.root.resolve()).as_posix()


def _material_issue(
    original: str,
    code: str,
    reason: str,
    recovery: str,
    details: Mapping[str, object] | None = None,
) -> Mapping[str, object]:
    issue = {
        "code": code,
        "input": original,
        "message": reason,
        "recovery": recovery,
    }
    if details is not None:
        issue.update(details)
    return issue


def _resolve_material_reference(
    graph: ArchiveGraph,
    feature_id: str,
    original: str,
) -> Tuple[DocumentRecord | None, Mapping[str, object] | None]:
    """把正式标识或当前交付项内路径解析为正式文档。"""

    value = original.strip()
    if not value:
        return None, _material_issue(
            original,
            "COLD_READ_MATERIAL_INVALID",
            "冷读材料输入不能为空。",
            "改用正式文档标识，或当前交付项内的正式文档路径。",
        )
    direct = graph.documents.get(value)
    if direct is not None:
        if direct.feature_id == feature_id:
            return direct, None
        return None, _material_issue(
            original,
            "COLD_READ_MATERIAL_OUT_OF_SCOPE",
            "冷读材料属于其他交付项。",
            f"改用交付项“{feature_id}”中的正式文档。",
        )
    if "/" not in value and "\\" not in value and not value.lower().endswith(".md"):
        return None, _material_issue(
            original,
            (
                "COLD_READ_MATERIAL_UNKNOWN"
                if value.startswith(feature_id + ".")
                else "COLD_READ_MATERIAL_OUT_OF_SCOPE"
            ),
            "当前交付项不存在该正式文档标识。",
            "从上下文摘要建议材料中复制正式文档标识。",
        )
    feature_root = graph.features[feature_id].path.resolve()
    archive_root = graph.root.resolve()
    project_root = project_root_from_archive_root(archive_root)
    try:
        path = Path(value.replace("\\", "/")).expanduser()
        candidates = (
            (path.resolve(),)
            if path.is_absolute()
            else (
                (project_root / path).resolve(),
                (archive_root / path).resolve(),
                (feature_root / path).resolve(),
            )
        )
    except (OSError, ValueError):
        return None, _material_issue(
            original,
            "COLD_READ_MATERIAL_INVALID",
            "材料路径格式不合法。",
            "改用正式文档标识，或当前交付项内的规范路径。",
        )
    shared_candidate = next(
        (
            (candidate, snapshot)
            for candidate in candidates
            if (snapshot := share_snapshot_ancestor(candidate)) is not None
        ),
        None,
    )
    if shared_candidate is not None:
        candidate, share = shared_candidate
        return None, _material_issue(
            original,
            "SHARE_SNAPSHOT_NOT_EXECUTABLE",
            f"分享快照“{share.path}”只用于读取，不能作为正式工作流材料。",
            f"改用“{feature_id}”档案目录内的正式文档路径。",
            share_recovery_details(
                archive_root,
                feature_id,
                share,
                candidate,
                target_kind="formal-document",
            ),
        )
    documents_by_path = {
        document.path.resolve(): document
        for document in graph.documents.values()
        if document.feature_id == feature_id
    }
    for candidate in candidates:
        document = documents_by_path.get(candidate)
        if document is not None:
            return document, None
    inside_feature = []
    inside_archive = []
    for candidate in candidates:
        try:
            candidate.relative_to(feature_root)
            inside_feature.append(candidate)
        except ValueError:
            pass
        try:
            candidate.relative_to(archive_root)
            inside_archive.append(candidate)
        except ValueError:
            pass
    if not inside_archive or not inside_feature:
        return None, _material_issue(
            original,
            "COLD_READ_MATERIAL_OUT_OF_SCOPE",
            "材料路径越出当前交付项。",
            f"改用“{feature_id}”档案目录内的正式文档路径。",
        )
    if any(candidate.exists() for candidate in inside_feature):
        return None, _material_issue(
            original,
            "COLD_READ_MATERIAL_NOT_FORMAL",
            "材料路径存在，但不是正式档案文档。",
            "改用上下文摘要建议材料中的正式文档。",
        )
    return None, _material_issue(
        original,
        "COLD_READ_MATERIAL_UNKNOWN",
        "当前交付项不存在该材料路径。",
        "检查路径拼写，或从上下文摘要建议材料中复制正式来源。",
    )


def _material_closure(
    graph: ArchiveGraph,
    document_ids: Sequence[str],
) -> Sequence[str]:
    """按正式依赖顺序生成最小材料闭包。"""

    ordered = []
    visited = set()

    def visit(document_id: str) -> None:
        if document_id in visited:
            return
        document = graph.documents[document_id]
        for dependency_id in sorted(document.dependencies):
            visit(dependency_id)
        visited.add(document_id)
        ordered.append(document_id)

    for document_id in sorted(set(document_ids)):
        visit(document_id)
    return tuple(ordered)


def _resolve_cold_read_materials(
    graph: ArchiveGraph,
    feature_id: str,
    requested: Sequence[str],
    allowed: Sequence[Mapping[str, object]],
) -> Tuple[Sequence[Mapping[str, object]], Sequence[Mapping[str, object]]]:
    """归一化调用者材料，并自动加入正式依赖闭包。"""

    allowed_by_source = {
        str(material.get("source")): material
        for material in allowed
        if isinstance(material.get("source"), str)
        and str(material.get("source")) in graph.documents
    }
    allowed_closure = set(_material_closure(graph, tuple(allowed_by_source)))
    selected = []
    selected_inputs: Dict[str, str] = {}
    issues = []
    for original in requested:
        document, issue = _resolve_material_reference(graph, feature_id, original)
        if issue is not None:
            issues.append(issue)
            continue
        assert document is not None
        if document.document_id in selected_inputs:
            issues.append(_material_issue(
                original,
                "COLD_READ_MATERIAL_DUPLICATE",
                f"该输入与“{selected_inputs[document.document_id]}”指向同一正式文档。",
                "删除重复输入，每份正式材料只声明一次。",
            ))
            continue
        selected_inputs[document.document_id] = original
        if document.document_id not in allowed_closure:
            issues.append(_material_issue(
                original,
                "COLD_READ_MATERIAL_NOT_ALLOWED",
                "该正式材料不属于当前动作的授权闭包。",
                "只使用上下文摘要建议材料及其正式依赖。",
            ))
            continue
        selected.append(document.document_id)
    if issues:
        return (), tuple(issues)
    selected_closure = tuple(_material_closure(graph, selected))
    missing = sorted(allowed_closure - set(selected_closure))
    if missing:
        return (), ({
            "code": "COLD_READ_MATERIALS_INCOMPLETE",
            "message": "冷读材料未覆盖当前动作所需的全部正式来源。",
            "missing": missing,
            "recovery": "补充缺失的正式材料；其依赖会由工具自动加入。",
        },)
    normalized = []
    for document_id in selected_closure:
        document = graph.documents[document_id]
        material = allowed_by_source.get(document_id) or _material(
            document, f"读取“{document_id}”的正式依赖"
        )
        assert material is not None
        record = dict(material)
        record["source"] = document_id
        record["path"] = _canonical_material_path(graph, document)
        normalized.append(record)
    return tuple(normalized), ()


def _suggested_materials(
    graph: ArchiveGraph,
    feature_id: str,
    action: str,
    execution: Mapping[str, object],
) -> Sequence[Mapping[str, object]]:
    if action == "implementation":
        records = [
            record for record in execution["slices"].values()
            if isinstance(record, dict) and record.get("status") in {
                "active", "candidate", "review_failed", "failed", "interrupted",
            }
        ]
        if records:
            record = records[-1]
            package = record.get("package")
            if isinstance(package, dict):
                result = [{
                    "source": f"task-package:{package.get('package_id')}",
                    "purpose": "读取当前执行授权、范围和验收条件",
                    "mode": "full",
                }]
                materials = package.get("context_materials")
                if isinstance(materials, list):
                    result.extend(item for item in materials if isinstance(item, dict))
                return tuple(result)
    roles = {
        "requirements": (("requirements.terminology", "统一业务含义"), ("requirements.overview", "确认目标、范围和验收")),
        "investigation": (("requirements.overview", "限定调查问题和范围"), ("investigation.overview", "读取已确认事实和未知")),
        "design": (("requirements.overview", "读取已确认目标和验收"), ("investigation.overview", "读取选定证据"), ("design.overview", "评审职责、行为和取舍")),
        "plan": (("requirements.overview", "读取验收条件"), ("design.overview", "读取已选方案"), ("plan.overview", "执行任务与回滚")),
        "implementation": (("plan.overview", "读取执行边界"), ("implementation.overview", "记录实际变化")),
        "validation": (("requirements.overview", "读取验收条件"), ("implementation.overview", "读取实际变化"), ("validation.overview", "判断证据、边界和风险")),
        "freeze": (("validation.overview", "确认最终验证结论"),),
        "cleanup": (),
    }
    result = []
    for suffix, purpose in roles[action]:
        material = _material(_feature_document(graph, feature_id, suffix), purpose)
        if material is not None:
            result.append(material)
    return tuple(result)


def _slice_conclusions(execution: Mapping[str, object]) -> Mapping[str, Mapping[str, object]]:
    """生成稳定且不暴露执行账本内部结构的切片结论。"""

    slices = execution.get("slices")
    if not isinstance(slices, dict):
        return {}
    result: Dict[str, Mapping[str, object]] = {}
    for package_id in sorted(slices):
        record = slices[package_id]
        if not isinstance(record, dict):
            continue
        candidate = (
            record.get("candidate")
            if record.get("status") == "candidate"
            else reviewed_candidate(record)
        )
        review = latest_review(record)
        result[str(package_id)] = {
            "status": record.get("status"),
            "execution_id": record.get("execution_id"),
            "contract_status": (
                record.get("contract_check", {}).get("status")
                if isinstance(record.get("contract_check"), dict)
                else "pending" if record.get("status") == "contract_draft" else None
            ),
            "contract_version": (
                record.get("package", {}).get("slice_contract", {}).get("version")
                if isinstance(record.get("package"), dict)
                and isinstance(record.get("package", {}).get("slice_contract"), dict)
                else None
            ),
            "checkpoint_count": (
                len(record.get("checkpoints", []))
                if isinstance(record.get("checkpoints"), list)
                else 0
            ),
            "breaker_report": (
                record.get("breaker_reports", [])[-1]
                if isinstance(record.get("breaker_reports"), list) and record.get("breaker_reports")
                else None
            ),
            "candidate_id": candidate.get("candidate_id") if isinstance(candidate, dict) else None,
            "candidate_digest": (
                candidate.get("candidate_digest") if isinstance(candidate, dict) else None
            ),
            "review": {
                "result": review.get("result"),
                "review_execution_id": review.get("review_execution_id"),
                "candidate_id": review.get("candidate_id"),
                "candidate_digest": review.get("candidate_digest"),
            } if isinstance(review, dict) else None,
        }
    return result


def _current_candidate(
    slices: Mapping[str, Mapping[str, object]],
) -> Mapping[str, object] | None:
    unresolved = [
        (package_id, record)
        for package_id, record in slices.items()
        if record.get("status") in {
            "active", "candidate", "review_failed", "failed", "interrupted",
            "contract_draft", "contract_failed", "ready", "circuit_open",
        }
    ]
    if len(unresolved) != 1:
        return None
    package_id, record = unresolved[0]
    return {
        "package_id": package_id,
        "status": record.get("status"),
        "execution_id": record.get("execution_id"),
        "candidate_id": record.get("candidate_id"),
        "candidate_digest": record.get("candidate_digest"),
        "review": record.get("review"),
    }


def _candidate_rechecks(
    feature_path: Path,
    state: Mapping[str, object],
    execution: Mapping[str, object],
    *,
    archive_read_only: bool = False,
) -> Tuple[Mapping[str, object], Sequence[Mapping[str, object]]]:
    """复用候选派生能力，返回当前候选事实和固定三项复核。"""

    summary_error = None
    try:
        summary = accepted_candidate_summary(execution)
    except ArchiveApprovalError as exception:
        summary = {"items": [], "digest": None}
        summary_error = exception.code

    last_candidate = None
    if summary_error is None:
        try:
            last_candidate = last_accepted_candidate(execution)
        except ArchiveApprovalError as exception:
            summary_error = exception.code

    drift: Dict[str, object] = {
        "code": "candidate_workspace_drift",
        "status": "not_applicable",
        "package_id": None,
        "candidate_id": None,
    }
    if last_candidate is not None and not archive_read_only:
        package_id, candidate = last_candidate
        matches = False
        workspace_root = candidate.get("workspace_root")
        if isinstance(workspace_root, str):
            try:
                matches = matching_accepted_workspace_snapshot(
                    Path(workspace_root), execution,
                ) is not None
            except (OSError, ValueError):
                matches = False
        drift = {
            "code": "candidate_workspace_drift",
            "status": "passed" if matches else "failed",
            "package_id": package_id,
            "candidate_id": candidate.get("candidate_id"),
        }
    elif summary_error is not None:
        drift["status"] = "failed"
        drift["reason"] = summary_error

    approvals = state.get("approvals")
    final_record = approvals.get("final") if isinstance(approvals, dict) else None
    saved_summary = (
        final_record.get("candidate_summary") if isinstance(final_record, dict) else None
    )
    if saved_summary is None:
        validation_binding_status = "pending"
    elif summary_error is not None or saved_summary != summary:
        validation_binding_status = "failed"
    elif (
        summary.get("items")
        and isinstance(final_record, dict)
        and isinstance(final_record.get("integration_confirmation"), dict)
        and not _integration_confirmation_is_current(
            feature_path,
            final_record.get("integration_confirmation"),
        )
    ):
        validation_binding_status = "failed"
    else:
        validation_binding_status = "passed"
    validation_binding = {
        "code": "final_validation_candidate_binding",
        "status": validation_binding_status,
        "current_candidate_digest": summary.get("digest"),
        "bound_candidate_digest": (
            saved_summary.get("digest") if isinstance(saved_summary, dict) else None
        ),
    }

    review_binding = {
        "code": "passed_review_binding",
        "status": (
            "failed" if summary_error is not None
            else "passed" if summary.get("items")
            else "not_applicable"
        ),
        "candidate_digest": summary.get("digest"),
        "accepted_slice_count": len(summary.get("items", [])),
    }
    if summary_error is not None:
        review_binding["reason"] = summary_error
    return summary, (drift, validation_binding, review_binding)


def _next_action_contract(
    graph: ArchiveGraph,
    feature_id: str,
    action: Mapping[str, object] | None,
    execution: Mapping[str, object],
) -> Mapping[str, object] | None:
    """把紧凑动作补齐为当前命令可直接消费的参数合同。"""

    if action is None:
        return None
    kind = action.get("kind")
    if kind in {"write", "human"}:
        return {
            "kind": kind,
            "target": action.get("target"),
            "expected_status": action.get("expected_status"),
            "reason": action.get("reason"),
            "preflight": {
                "status": "passed",
                "source": "delivery-view",
                "invocation": "ready",
            },
        }
    if not isinstance(action.get("command"), str):
        return None
    command = str(action["command"])
    spec = ACTION_CONTRACT_SPECS.get(command, {})
    arguments: Dict[str, object] = {
        "feature_id": feature_id,
    }
    accepted_arguments = spec.get("accepted_arguments", ())
    assert isinstance(accepted_arguments, tuple)
    arguments.update({key: action[key] for key in accepted_arguments if key in action})
    configured_inputs = spec.get("required_inputs", ())
    assert isinstance(configured_inputs, tuple)
    required_inputs = list(configured_inputs)
    package_id = action.get("package_id")
    identity = spec.get("identity")
    if isinstance(package_id, str) and isinstance(identity, tuple):
        identity_argument, purpose = identity
        arguments[str(identity_argument)] = _suggest_execution_id(
            execution,
            package_id,
            str(purpose),
        )
    if spec.get("include_workspace_root") is True:
        arguments["workspace_root"] = str(project_root_from_archive_root(graph.root))
    if command == "resolve-slice" and action.get("action") == "retry":
        required_inputs = []
        if isinstance(package_id, str):
            arguments["execution_id"] = _suggest_execution_id(
                execution, package_id, "repair"
            )
    if command == "stage-action" and action.get("stage") == "final":
        summary = accepted_candidate_summary(execution)
        if summary["items"]:
            required_inputs.append("integration_confirmation")
    if command == "stage-action" and action.get("stage") in {"ui-baseline", "final"} and action.get("reviewed_digest"):
        required_inputs.append("user_confirmation")
    if command == "transition-lifecycle" and action.get("to") == "frozen":
        validation_statuses = graph.features[feature_id].validation_statuses
        validation_arguments = {
            "validation_conclusion": validation_statuses["conclusion"],
            "unverified_boundaries": validation_statuses["unverified_boundaries"],
            "residual_risks": validation_statuses["residual_risks"],
        }
        arguments.update(validation_arguments)
        required_inputs = [
            name for name, value in validation_arguments.items() if value == "pending"
        ]
        required_inputs.append("integration_confirmation")
    contract = {
        "command": command,
        "arguments": arguments,
        "required_inputs": required_inputs,
        "preflight": {
            "status": "passed",
            "source": "delivery-view",
            "invocation": "ready" if not required_inputs else "needs-input",
        },
    }
    input_kind = spec.get("input_kind")
    if command == "ui-baseline":
        contract["input_preparation"] = action_input_preparation(feature_id, "ui-baseline")
    if command == "ui-publish" and action.get("delivery_materials"):
        contract["input_preparation"] = action_input_preparation(feature_id, "ui-delivery")
    if isinstance(input_kind, str) and isinstance(action.get("execution_id"), str):
        contract["input_preparation"] = action_input_preparation(
            feature_id, input_kind, execution_id=action["execution_id"],
        )
    if command == "review-slice":
        contract["input_preparations"] = [
            action_input_preparation(feature_id, kind, execution_id=arguments["review_execution_id"])
            for kind in ("review-issues", "review-verification")
        ]
        contract["handoff_preparation"] = {
            "command": "prepare-handoff",
            "arguments": {"feature_id": feature_id, "action": "implementation", "role": "review",
                          "execution_id": arguments["review_execution_id"]},
        }
    if (
        command == "resolve-slice"
        and isinstance(package_id, str)
        and action.get("action") != "retry"
    ):
        contract["generated_inputs"] = {
            "execution_id": _suggest_execution_id(execution, package_id, "repair")
        }
    return contract


def _stage_projection(
    graph: ArchiveGraph,
    feature_id: str,
    execution: Mapping[str, object],
    approvals: Mapping[str, object],
    slices: Mapping[str, Mapping[str, object]],
    stale_documents: Sequence[str],
    lease: Mapping[str, object] | None,
    candidate_rechecks: Sequence[Mapping[str, object]] = (),
) -> Tuple[str, Mapping[str, object] | None, Mapping[str, object], Sequence[Mapping[str, str]]]:
    """依据现有事实选择唯一动作；事实不足时保留人工判断点。"""

    feature = graph.features[feature_id]
    requirements = approvals["requirements"]["status"]
    architecture = approvals["architecture"]["status"]
    final = approvals["final"]["status"]
    blockers = []
    next_action = None
    requires_human: Mapping[str, object] = {"required": False, "decision": None}

    if feature.lifecycle in {"frozen", "pending_cleanup"}:
        return "frozen" if feature.lifecycle == "frozen" else "cleanup", None, requires_human, blockers

    if requirements not in {"approve", "ready"}:
        stage = "requirements"
        requirements_document = _feature_document(
            graph, feature_id, "requirements.overview"
        )
        document_issue = _document_readiness_issue(
            requirements_document,
            allowed_statuses=("confirmed", "completed"),
            stale_documents=stale_documents,
            label="需求总览",
        )
        if document_issue is not None:
            document_code, document_message = document_issue
            blockers.append({"code": document_code, "message": document_message})
            next_action = _document_write_action(
                requirements_document,
                expected_status="confirmed",
                reason=document_message,
            )
            return stage, next_action, requires_human, blockers
        blockers.append({"code": "REQUIREMENTS_APPROVAL_REQUIRED", "message": "需求共识尚未批准或已失效。"})
        next_action = {"command": "stage-action", "stage": "requirements"}
        requires_human = {"required": True, "decision": "确认需求结论。"}
        return stage, next_action, requires_human, blockers

    if feature.lifecycle == "draft" and not slices:
        return (
            "activation",
            {"command": "transition-lifecycle", "to": "active"},
            requires_human,
            blockers,
        )

    baseline = approvals.get("ui-baseline", {"status": "not_applicable"})
    review = baseline.get("review", {})
    if review.get("status") == "blocked":
        from archive_ui_baseline import baseline_material_action
        return ("ui-inputs-blocked", baseline_material_action(), requires_human, review["blockers"])

    design = _feature_document(graph, feature_id, "design.overview")
    plan = _feature_document(graph, feature_id, "plan.overview")
    design_issue = _document_readiness_issue(
        design,
        allowed_statuses=("confirmed", "completed"),
        stale_documents=stale_documents,
        label="设计总览",
    )
    if design_issue is not None:
        code, message = design_issue
        blockers.append({"code": code, "message": message})
        return (
            "design",
            _document_write_action(
                design,
                expected_status="confirmed",
                reason=message,
            ),
            requires_human,
            blockers,
        )
    if architecture in {"reject", "stale"}:
        blockers.append({
            "code": "ARCHITECTURE_APPROVAL_BLOCKED",
            "message": "架构结论已拒绝或失效。",
        })
        return (
            "design",
            {"command": "stage-action", "stage": "architecture"},
            {"required": True, "decision": "重新判断并记录当前架构结论。"},
            blockers,
        )
    plan_issue = _document_readiness_issue(
        plan,
        allowed_statuses=("confirmed", "completed"),
        stale_documents=stale_documents,
        label="实施计划",
    )
    if plan_issue is not None:
        code, message = plan_issue
        blockers.append({"code": code, "message": message})
        return (
            "plan",
            _document_write_action(
                plan,
                expected_status="completed",
                reason=message,
            ),
            requires_human,
            blockers,
        )
    if baseline.get("status") not in {"approve", "not_applicable"}:
        if review.get("status") != "ready":
            return "ui-inputs", {"command": "ui-baseline"}, requires_human, review.get("blockers", [])
        return ("ui-baseline-review", {"command": "stage-action", "stage": "ui-baseline", **review},
                {"required": True, "decision": "展示当前开工清单，等待用户明确确认。"},
                [{"code": "UI_BASELINE_APPROVAL_REQUIRED", "message": "当前开工清单尚未得到用户确认。"}])
    if feature.lifecycle not in {"validating", "frozen", "pending_cleanup"} and (
        feature.lifecycle == "active" or slices
    ):
        unresolved = [
            (package_id, record)
            for package_id, record in slices.items()
            if record.get("status") in {
                "active", "candidate", "review_failed", "failed", "interrupted",
                "contract_draft", "contract_failed", "ready", "circuit_open",
            }
        ]
        executing = [
            (package_id, record)
            for package_id, record in unresolved
            if record.get("status") not in {"contract_draft", "contract_failed", "ready"}
        ]
        leased = [
            (package_id, record)
            for package_id, record in executing
            if lease is not None
            and lease.get("feature_id") == feature_id
            and lease.get("package_id") == package_id
        ]
        selected = leased or executing or unresolved
        if len(selected) > 1:
            blockers.append({"code": "AMBIGUOUS_CURRENT_SLICE", "message": "存在多个未收敛切片，无法确定唯一下一动作。"})
            return (
                "implementation-recovery",
                None,
                {"required": True, "decision": "确认需要收敛的当前切片。"},
                blockers,
            )
        if len(selected) == 1:
            package_id, record = selected[0]
            status = record.get("status")
            execution_id = record.get("execution_id")
            if status == "contract_draft":
                return (
                    "contract-check",
                    {"command": "check-slice-contract", "package_id": package_id},
                    requires_human,
                    blockers,
                )
            if status == "contract_failed":
                blockers.append({"code": "CONTRACT_CHECK_FAILED", "message": "切片契约检查未通过。"})
                return (
                    "contract-revision",
                    None,
                    {"required": True, "decision": "能补全时修订当前契约；需要换新切片时先为当前交付项和切片调用 resolve-slice --action release --workspace-decision kept 终止未实施记录，再修改方案并创建新切片。"},
                    blockers,
                )
            if status == "ready":
                if lease is not None:
                    blockers.append({
                        "code": "MODIFICATION_LEASE_CONFLICT",
                        "message": "当前工作流已有未释放的全局修改租约。",
                    })
                    return (
                        "implementation-recovery",
                        None,
                        {"required": True, "decision": "先收敛并释放现有修改租约。"},
                        blockers,
                    )
                return (
                    "implementation-ready",
                    {"command": "start-slice", "package_id": package_id},
                    requires_human,
                    blockers,
                )
            if (
                status != "interrupted"
                and (
                    lease is None
                    or lease.get("feature_id") != feature_id
                    or lease.get("package_id") != package_id
                )
            ):
                blockers.append({
                    "code": "MODIFICATION_LEASE_MISMATCH",
                    "message": "当前切片与全局修改租约不一致。",
                })
                return (
                    "implementation-recovery",
                    None,
                    {"required": True, "decision": "恢复切片与工作区租约的一致状态。"},
                    blockers,
                )
            if status == "circuit_open":
                source_record = execution["slices"][package_id]
                scope_only = set(source_record["checkpoints"][-1]["boundary_rules"]) == {"write_scope_violation"}
                blockers.append({"code": "SLICE_BREAKER_RESOLUTION_REQUIRED", "message": "切片已熔断。仅文件漏登且属于原业务授权时，可保留成果补登记范围后重试；其他阻断仍须解决后重试，或恢复工作区后终止。"})
                return (
                    "implementation-recovery",
                    {"command": "resolve-slice", "package_id": package_id},
                    {"required": not scope_only, "decision": "核对阻断与原授权；仅漏登文件可补登记范围，无须重复业务批准，否则处理阻断后重试或恢复到基线后终止。"},
                    blockers,
                )
            if status == "active":
                source_record = execution.get("slices", {}).get(package_id)
                assert isinstance(source_record, dict)
                try:
                    require_candidate_submission_ready(source_record, lease)
                except ArchiveCandidateError as exception:
                    blockers.append({
                        "code": exception.code,
                        "message": exception.message,
                    })
                    if exception.code == "CANDIDATE_EMPTY":
                        return "implementation", None, requires_human, blockers
                    return (
                        "implementation",
                        {
                            "command": "checkpoint-slice",
                            "execution_id": execution_id,
                        },
                        requires_human,
                        blockers,
                    )
                return (
                    "implementation",
                    {"command": "submit-slice", "package_id": package_id, "execution_id": execution_id},
                    requires_human,
                    blockers,
                )
            if status == "candidate":
                source_record = execution.get("slices", {}).get(package_id)
                candidate = (
                    source_record.get("candidate")
                    if isinstance(source_record, dict)
                    else None
                )
                candidate_current = (
                    isinstance(candidate, dict)
                    and lease is not None
                    and candidate_matches_workspace(lease, candidate)
                )
                if not candidate_current:
                    blockers.append({
                        "code": "CANDIDATE_CHANGED",
                        "message": "工作区已偏离固定候选，旧候选不能继续评审。",
                    })
                    return (
                        "implementation-recovery",
                        None,
                        {
                            "required": True,
                            "decision": "恢复固定候选继续评审，或选择返修、恢复后放弃。",
                        },
                        blockers,
                    )
                blockers.append({"code": "CANDIDATE_REVIEW_REQUIRED", "message": "固定候选等待独立评审。"})
                return (
                    "candidate-review",
                    {"command": "review-slice", "package_id": package_id, "candidate_id": record.get("candidate_id")},
                    {"required": True, "decision": "独立评审候选并给出结论。"},
                    blockers,
                )
            blockers.append({"code": "SLICE_RECOVERY_REQUIRED", "message": "当前切片需要返修或释放。"})
            return (
                "implementation-recovery",
                {"command": "resolve-slice", "package_id": package_id},
                {"required": True, "decision": "选择返修或释放当前切片。"},
                blockers,
            )
        if lease is not None:
            if lease.get("feature_id") != feature_id:
                blockers.append({
                    "code": "GLOBAL_MODIFICATION_LEASE_HELD",
                    "message": "另一交付项正在持有全局修改租约。",
                })
                return (
                    "implementation-recovery",
                    None,
                    {"required": True, "decision": "先收敛当前持有全局修改租约的交付项。"},
                    blockers,
                )
            blockers.append({
                "code": "ORPHANED_MODIFICATION_LEASE",
                "message": "全局修改租约没有对应的当前未收敛切片。",
            })
            return (
                "implementation-recovery",
                None,
                {"required": True, "decision": "恢复当前项目的 Git 或 SVN 工作区后释放孤立租约。"},
                blockers,
            )
        if not any(record.get("status") == "accepted" for record in slices.values()):
            blockers.append({
                "code": "ACCEPTED_IMPLEMENTATION_REQUIRED",
                "message": "DLoop 交付必须至少完成并独立验收一个实现切片。",
            })
            return (
                "implementation-required",
                None,
                {"required": True, "decision": "形成并批准至少一个可独立验收的实现切片；没有实现工作的事项退出 DLoop。"},
                blockers,
            )
        pending = pending_plan_slices(execution)
        if pending:
            blockers.append({"code": "SLICE_PLAN_INCOMPLETE", "message": "批准方案仍有未收敛切片：" + "、".join(pending)})
            return "implementation-complete", None, requires_human, blockers
        return (
            "implementation-complete",
            {"command": "transition-lifecycle", "to": "active" if feature.lifecycle == "draft" else "validating"},
            requires_human,
            blockers,
        )

    if feature.lifecycle == "validating":
        if not any(record.get("status") == "accepted" for record in slices.values()):
            blockers.append({
                "code": "ACCEPTED_IMPLEMENTATION_REQUIRED",
                "message": "当前没有已独立评审通过的实现切片，不能进入最终验证和验收。",
            })
            return (
                "implementation-required",
                {"command": "transition-lifecycle", "to": "active"},
                requires_human,
                blockers,
            )
        validation = _feature_document(graph, feature_id, "validation.overview")
        validation_issue = _document_readiness_issue(
            validation,
            allowed_statuses=("completed",),
            stale_documents=stale_documents,
            label="最终验证材料",
        )
        if validation_issue is not None:
            code, message = validation_issue
            blockers.append({"code": code, "message": message})
            return (
                "validation",
                _document_write_action(
                    validation,
                    expected_status="completed",
                    reason=message,
                ),
                requires_human,
                blockers,
            )
        failed_rechecks = [
            item for item in candidate_rechecks
            if item.get("status") == "failed"
            and item.get("code") != "final_validation_candidate_binding"
        ]
        if failed_rechecks:
            blockers.extend({
                "code": str(item.get("code") or "FINAL_RECHECK_FAILED"),
                "message": "交付依赖的候选事实已经失效。",
            } for item in failed_rechecks)
            return (
                "validation-recovery",
                None,
                {"required": True, "decision": "恢复候选事实或形成新的修复切片。"},
                blockers,
            )
        ui_delivery = approvals.get("ui-delivery")
        if ui_delivery is not None and ui_delivery["status"] != "ready":
            blockers.append(ui_delivery["review_blocker"])
            return "validation", {"command": "ui-publish", "delivery_materials": True}, requires_human, blockers
        if ui_delivery is not None and final in {"pending", "stale"}:
            return (
                "final-review",
                {"command": "stage-action", "stage": "final", **ui_delivery["review"]},
                {"required": True, "decision": "查看最终交互页及验证边界，验收当前实现。"}, blockers,
            )
        if final == "reject":
            blockers.append({"code": "FINAL_APPROVAL_REJECTED", "message": "用户已退回当前交付，须先处理退回事项。"})
            return "validation-recovery", None, {"required": True, "decision": "处理用户退回事项。"}, blockers
        return (
            "freeze-ready",
            {"command": "transition-lifecycle", "to": "frozen"},
            requires_human,
            blockers,
        )

    return (
        "cleanup",
        None,
        {"required": True, "decision": "确认保留期处理或清理授权。"},
        blockers,
    )


def delivery_view(root: Path, feature_id: str) -> Mapping[str, object]:
    """从档案、执行账本和当前工作区派生统一只读交付视图。"""

    graph = validate_feature_archive(root, feature_id)
    feature = _require_complex_feature(graph, feature_id, require_writable=False)
    state = _load_state(feature.path, feature_id)
    return _delivery_view(graph, feature_id, state)


def _completion_claim(feature, stage, approvals, blockers):
    """给汇报者唯一状态；测试通过不能替代候选、评审和用户接受。"""
    final = approvals["final"]["status"]
    if feature.lifecycle in {"frozen", "pending_cleanup"} and final == "approve" and not blockers:
        return "complete"
    if stage == "freeze-ready" and final == "approve" and not blockers:
        return "accepted"
    if stage == "final-review" and not blockers:
        return "ready-for-acceptance"
    return "incomplete"


def _delivery_summary(feature, stage, execution, approvals, rechecks, blockers):
    """从已有批准和候选区分接受、验证程度与档案保留状态。"""
    candidates = []
    for package_id, record in execution["slices"].items():
        if record.get("status") == "accepted":
            candidate = reviewed_candidate(record)
        elif record.get("status") == "candidate":
            candidate = record.get("candidate")
        else:
            continue
        if isinstance(candidate, dict):
            candidates.append({
                "package_id": package_id,
                "candidate_id": candidate["candidate_id"],
                "verification": candidate.get("verification", []),
                "unverified_boundaries": candidate.get("unverified_boundaries", []),
            })
    acceptance = approvals["final"]["status"]
    needs_recheck = any(item["status"] == "failed" for item in rechecks)
    if acceptance == "approve" and (needs_recheck or blockers):
        acceptance = "stale"
    labels = {"approve": "已接受", "reject": "已退回", "stale": "原接受结论需复核", "pending": "尚未接受"}
    boundaries = list(dict.fromkeys(boundary for item in candidates for boundary in item["unverified_boundaries"]))
    evidence_status = "needs-recheck" if needs_recheck and candidates else (
        "documented-with-boundaries" if boundaries else "documented" if candidates else "not-recorded"
    )
    summary = {
        "acceptance": acceptance,
        "archive_lifecycle": feature.lifecycle,
        "verification_status": evidence_status,
        "completion_claim": _completion_claim(feature, stage, approvals, blockers),
    }
    if not candidates:
        return {"acceptance": acceptance, "completion_claim": summary["completion_claim"]}
    return {
        **summary,
        "acceptance_label": labels[acceptance],
        "archive_read_only": feature.lifecycle in {"frozen", "pending_cleanup"},
        "unverified_boundaries": boundaries,
        "details": "06-validation/README.md",
        "followups": "06-validation/README.md#后续接入清单",
        "interpretation": "冻结表示本轮开发交付已封存，不代表用户已验收；验证与接受结论仅对应当时交付，未列边界不证明验证完整。",
    }


def _delivery_view(
    graph: ArchiveGraph,
    feature_id: str,
    state: Mapping[str, object],
) -> Mapping[str, object]:
    """从同次读取的正式事实投影，结果不跨命令复用。"""

    root = graph.root
    feature = graph.features[feature_id]
    execution = _execution_state(state)
    approvals = approval_status_from_state(graph, feature_id, state)
    stale_documents = list(refresh_queue(graph, feature_id))
    slices = _slice_conclusions(execution)
    plan_record = current_plan(execution["slice_plan"])
    slice_plan = {
        "current_version": execution["slice_plan"].get("current_version"),
        "history": [
            {"version": item.get("version"), "plan_digest": item.get("plan_digest")}
            for item in execution["slice_plan"].get("history", [])
            if isinstance(item, dict)
        ],
    }
    if isinstance(plan_record, dict) and isinstance(plan_record.get("plan"), dict):
        slice_plan.update(evaluate_plan(plan_record["plan"], execution["slices"]))
    candidate_summary, required_rechecks = _candidate_rechecks(
        feature.path,
        state,
        execution,
        archive_read_only=feature.lifecycle in {"frozen", "pending_cleanup"},
    )
    lease = active_modification_lease(root)
    normalized_lease = lease if isinstance(lease, dict) else None
    stage, next_action, requires_human, blockers = _stage_projection(
        graph,
        feature_id,
        execution,
        approvals,
        slices,
        stale_documents,
        normalized_lease,
        required_rechecks,
    )
    if stage in {"final-review", "freeze-ready"}:
        execution_blockers = final_execution_blockers(root, state)
        if execution_blockers:
            stage = "validation-recovery"
            next_action = None
            requires_human = {"required": False, "decision": None}
            blockers = execution_blockers
    lease_summary = modification_lease_summary(normalized_lease)
    return {
        "current_stage": stage,
        "delivery_summary": _delivery_summary(feature, stage, execution, approvals, required_rechecks, blockers),
        "can_advance": next_action is not None and not blockers and not requires_human["required"],
        "blockers": list(blockers),
        "next_action": next_action,
        "next_action_contract": _next_action_contract(
            graph,
            feature_id,
            next_action,
            execution,
        ),
        "requires_human": requires_human,
        "conclusions": {
            "approvals": approvals,
            "slices": slices,
            "slice_plan": slice_plan,
            "current_candidate": _current_candidate(slices),
            "accepted_candidate_summary": candidate_summary,
        },
        "trusted_machine_facts": {
            "lifecycle": feature.lifecycle,
            "approval_statuses": {
                stage_name: record["status"]
                for stage_name, record in approvals.items()
            },
            "slice_statuses": {
                package_id: record.get("status")
                for package_id, record in slices.items()
            },
            "modification_lease": lease_summary,
            "stale_documents": stale_documents,
        },
        "required_rechecks": list(required_rechecks),
    }


def _coordinator_role_view(
    context: RoleProjectionContext,
) -> RoleProjectionResult:
    action = context.request.action
    delivery = context.delivery
    allowed_operations = ["选择业务动作", "执行唯一下一机械动作", "发起单线角色交接"]
    forbidden_operations = ["并行推进决策、写入、实施、评审或验证决策", "代替角色执行完整合同"]
    if action == "investigation":
        allowed_operations.insert(2, "并行发起互不依赖的事实取证")
    else:
        forbidden_operations.insert(0, "在非调查动作中发起并行分支")
    return {
        "role": "coordinator",
        "baton": action,
        "goal": "选择业务动作，执行唯一下一步。",
        "allowed_operations": allowed_operations,
        "forbidden_operations": forbidden_operations,
        "decision_rules": [
            "按批准场景核对位置、触发、证据；延期、复用、隐藏项注明承接者、缺失输入、影响及恢复条件。",
        ] if action in {"plan", "implementation", "validation"} else [],
        "preconditions": ["当前交付项已选择", "全局写入资格与当前功能审计通过"],
        "required_materials": [],
        "expected_outputs": ["业务决定或下一角色所需的最小交接标识"],
        "completion_conditions": ["完成当前决定或动作后等待下一角色"],
        "source_rechecks": list(delivery["required_rechecks"]),
    }, ()


def _cold_read_role_view(
    context: RoleProjectionContext,
) -> RoleProjectionResult:
    graph = context.graph
    feature_id = context.request.feature_id
    action = context.request.action
    materials = context.materials
    requirements_status = context.requirements_status
    stale_documents = context.stale_documents
    entry_question = context.request.entry_question
    allowed_materials = context.request.allowed_materials
    blockers = []
    normalized_question = entry_question.strip() if isinstance(entry_question, str) else ""
    if not normalized_question:
        blockers.append({
            "code": "COLD_READ_QUESTION_REQUIRED",
            "message": "冷读角色必须由主协调者提供当次入口问题。",
        })
    if not allowed_materials:
        blockers.append({
            "code": "COLD_READ_MATERIALS_REQUIRED",
            "message": "冷读角色必须由主协调者声明当次允许材料。",
        })

    execution = _execution_state(context.state)
    slices = execution.get("slices")
    unresolved = [
        record
        for record in slices.values()
        if isinstance(record, dict) and record.get("status") in {
            "active", "candidate", "review_failed", "failed", "interrupted",
        }
    ] if isinstance(slices, dict) else []
    if unresolved:
        blockers.append({
            "code": "ACTIVE_BATON_CONFLICT",
            "message": "当前存在尚未闭环的实施、评审或返修棒次。",
        })

    required_materials: Sequence[Mapping[str, object]] = ()
    if allowed_materials:
        required_materials, material_blockers = _resolve_cold_read_materials(
            graph,
            feature_id,
            allowed_materials,
            materials,
        )
        blockers.extend(material_blockers)
    if blockers:
        return None, tuple(blockers)

    normalized_materials = []
    for material in required_materials:
        normalized = dict(material)
        if action == "requirements":
            normalized["mode"] = "full"
            normalized.pop("sections", None)
        normalized_materials.append(normalized)
    return {
        "role": "cold-read",
        "baton": action,
        "goal": normalized_question,
        "allowed_operations": ["读取列出的正式材料", "标记直接说明、推断、缺失或歧义", "提交冷读结论"],
        "forbidden_operations": ["读取常驻入口", "使用名单外材料补足事实", "判断模块设计或实现质量", "修改档案或业务实现"],
        "preconditions": ["所需材料完整且新鲜", "当前棒次没有其他活动工作上下文"],
        "required_materials": normalized_materials,
        "expected_outputs": ["一句复述", "理解断点", "通过或不通过结论"],
        "completion_conditions": ["提交冷读结论后立即停止并返回主协调者"],
        "source_rechecks": {
            "requirements_approval": requirements_status,
            "stale_documents": list(stale_documents),
        },
    }, ()


def _role_materials_with_paths(
    graph: ArchiveGraph,
    materials: Sequence[Mapping[str, object]],
) -> Sequence[Mapping[str, object]]:
    normalized = []
    for material in materials:
        record = dict(material)
        source = material.get("source")
        document = graph.documents.get(source) if isinstance(source, str) else None
        if document is not None:
            record["path"] = _canonical_material_path(graph, document)
        normalized.append(record)
    return tuple(normalized)


def _design_review_content_blockers(
    design: DocumentRecord,
) -> Sequence[Mapping[str, object]]:
    try:
        text = design.path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exception:
        raise ArchiveContextError(
            "CONTEXT_MATERIAL_UNREADABLE",
            f"无法读取上下文材料“{design.document_id}”：{exception}",
        ) from exception
    parts = text.split("---", 2)
    body = parts[2].strip() if len(parts) == 3 else text.strip()
    meaningful_lines = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        while line.startswith(">"):
            line = line[1:].lstrip()
        if line.startswith("#"):
            continue
        if line[:2] in {"- ", "* ", "+ "}:
            line = line[2:].strip()
        number, separator, remainder = line.partition(". ")
        if separator and number.isdigit():
            line = remainder.strip()
        if not line or line in {"```", "~~~"}:
            continue
        if line in DESIGN_REVIEW_SCAFFOLD_LINES:
            continue
        normalized = line.casefold().strip()
        placeholder = normalized.strip(" \t。.!！?？:：-—_[]()（）【】")
        if placeholder in DESIGN_REVIEW_PLACEHOLDER_LINES or any(
            normalized.startswith(marker + separator)
            for marker in DESIGN_REVIEW_PLACEHOLDER_LINES
            for separator in (" ", "\t", ":", "：", "-", "—", "–", "(", "（", "[", "【")
        ):
            continue
        meaningful_lines.append(line)
    if meaningful_lines:
        return ()
    return ({
        "code": "DESIGN_REVIEW_INPUTS_INCOMPLETE",
        "message": "设计入口仍为空或保留待补充内容，无法形成完整交接。",
        "missing": ["设计内容"],
    },)


def _design_review_role_view(
    context: RoleProjectionContext,
) -> RoleProjectionResult:
    graph = context.graph
    feature_id = context.request.feature_id
    materials = context.materials
    delivery = context.delivery
    design = _feature_document(graph, feature_id, "design.overview")
    architecture_status = delivery["trusted_machine_facts"]["approval_statuses"]["architecture"]
    if not _ready(design):
        return None, ({"code": "DESIGN_INPUT_REQUIRED", "message": "独立设计评审需要已确认的设计入口。"},)
    try:
        _document_snapshot(graph, design.document_id)
    except ArchiveApprovalError as exception:
        return None, ({"code": exception.code, "message": exception.message},)
    content_blockers = _design_review_content_blockers(design)
    if content_blockers:
        return None, content_blockers
    if architecture_status == "approve":
        return None, ({"code": "DESIGN_REVIEW_ALREADY_APPROVED", "message": "当前设计已经完成架构确认。"},)
    if architecture_status == "reject":
        return None, ({"code": "DESIGN_REVISION_REQUIRED", "message": "当前设计已经被退回，必须先修改正式设计。"},)

    execution = _execution_state(context.state)
    slices = execution.get("slices")
    unresolved = [
        record
        for record in slices.values()
        if isinstance(record, dict) and record.get("status") in {
            "active", "candidate", "review_failed", "failed", "interrupted",
            "contract_draft", "contract_failed", "ready", "circuit_open",
        }
    ] if isinstance(slices, dict) else []
    if unresolved:
        return None, ({
            "code": "ACTIVE_BATON_CONFLICT",
            "message": "当前存在尚未闭环的实施、评审或返修棒次，不能开始独立设计评审。",
        },)
    return {
        "role": "design-review",
        "baton": "architecture-review",
        "goal": "独立判断当前设计是否满足已批准需求，并把复杂度隐藏在清晰的模块边界内。",
        "decision_rules": [
            "对照已批准场景及其原始来源定位核对操作去向、显示对象、结果范围；复用页面与原意有差异时明确指出，不能以派生文档一致证明需求完整",
            "涉及状态变化时核对触发者、获取新事实的动作、返回后的更新和条件仍不满足时的表现；不把收到更新后会切页等同于到期会触发刷新，不要求另建业务状态或重试机制",
            "按公共接口、跨模块职责或状态归属、外部可观察行为与安全约束判断设计边界；授权范围内保持这些合同的内部整理不要求单独架构批准，仍检查正确性和维护成本",
        ],
        "allowed_operations": [
            "读取列出的正式材料",
            "从设计材料出发，只读核对能改变当前判断的调用链、已有实现和项目惯例；引用清单不是只读调查白名单",
            "存在明显简化空间时比较更简单替代方案",
            "提交通过、退回或未验证结论",
        ],
        "forbidden_operations": [
            "读取常驻入口",
            "读取与当前设计判断无关的项目内容",
            "修改正式材料或项目实现",
            "替主协调者或用户记录架构决定",
        ],
        "preconditions": [
            "需求批准有效且材料新鲜",
            "设计入口已确认且包含可评审的设计内容",
            "当前没有未闭环执行棒次",
        ],
        "required_materials": list(_role_materials_with_paths(
            graph,
            [
                material
                for material in materials
                if material.get("source") in {
                    f"{feature_id}.{suffix}"
                    for suffix in DESIGN_REVIEW_MATERIAL_SUFFIXES
                }
            ],
        )),
        "expected_outputs": ["设计偏差", "残留风险", "通过、退回或未验证结论"],
        "completion_conditions": ["提交单一设计评审结论后停止并返回主协调者"],
        "source_rechecks": {
            "requirements_approval": delivery["trusted_machine_facts"]["approval_statuses"]["requirements"],
            "architecture_approval": architecture_status,
            "stale_documents": list(delivery["trusted_machine_facts"]["stale_documents"]),
        },
    }, ()


def _implementation_role_view(
    context: RoleProjectionContext,
) -> RoleProjectionResult:
    graph = context.graph
    feature_id = context.request.feature_id
    execution_id = context.request.execution_id
    delivery = context.delivery
    feature = graph.features[feature_id]
    execution = _execution_state(context.state)
    slices = execution.get("slices")
    active = [
        (package_id, record)
        for package_id, record in slices.items()
        if isinstance(record, dict) and record.get("status") == "active"
    ] if isinstance(slices, dict) else []
    if execution_id is None:
        return None, ({"code": "EXECUTION_ID_REQUIRED", "message": "实施角色必须提供执行身份。"},)
    if len(active) != 1:
        return None, ({"code": "ACTIVE_SLICE_REQUIRED", "message": "当前没有唯一活动实施切片。"},)
    package_id, record = active[0]
    if record.get("execution_id") != execution_id:
        return None, ({"code": "EXECUTION_ID_MISMATCH", "message": "执行身份与当前实施切片不匹配。"},)
    try:
        task_materials = slice_handoff_materials(
            graph, feature, Path(record["workspace_root"]), record["package"]
        )
    except ArchiveHandoffError as exception:
        return None, exception.blockers
    repair_handoff, repair_blockers = _repair_role_projection(record)
    if repair_blockers:
        return None, repair_blockers
    result = {
        "role": "implementation",
        "baton": package_id,
        "execution_id": execution_id,
        "goal": "仅在固定任务包授权范围内完成修改和聚焦验证。",
        "allowed_operations": [
            "从任务包精确引用开始，只读调查当前调用链、相关实现和项目惯例；只读范围不等于写入范围",
            "修改授权范围文件", "运行任务包验证与范围内必要的诊断检查",
        ],
        "forbidden_operations": ["读取常驻入口", "修改范围外文件", "自行评审候选", "并行推进其他棒次"],
        "preconditions": ["需求批准有效且架构未拒绝或失效", "任务包输入新鲜", "执行身份匹配唯一活动切片"],
        "required_materials": [{
            "source": f"task-package:{package_id}",
            "purpose": "读取当前执行授权、范围和验收条件",
            "mode": "full",
        }, *task_materials],
        "expected_outputs": [
            "授权范围内的实际变化", "验证结果", "候选输入",
            "沿用任务包中已批准需求的场景名称和材料定位记录结果，不按控件或 Prefab 内部状态另拆场景",
            "保留各场景的真实结果证据与已有体验入口，按需说明初始配置和操作；缺少环境、入口或证据时明确未验证部分，不编造入口或新建无必要的演示场景",
            "沿用 Prefab 时验证当前场景相关的接线、遮挡、输入与状态清理，不把沿用视为集成已通过",
        ],
        "completion_conditions": [
            f"使用 submit-slice 成功提交 {package_id} 后停止并交还协调者",
            f"SVN 项目的检查点自动将本次文件及 Unity .meta 纳入同一提交组；Git 项目跳过归组且不改写暂存区。SVN 补齐产物可运行 sync-svn-changelist --feature-id {feature_id}，失败先修复，不能声称已归组。此操作不提交服务器",
            "提交被材料缺项阻断时保留当前上下文和执行身份，在授权范围内补齐后重新提交；输入未变化时不重试",
            "补齐改变产品文件时按既有验证策略更新检查点；涉及范围、业务决定或批准变化时交还协调者",
        ],
        "source_rechecks": list(delivery["required_rechecks"]),
        "decision_rules": [
            "在授权写入范围内，保持已批准外部可观察行为、公共接口、跨模块状态归属和安全约束的内部整理由实施者决定，包括私有辅助逻辑、等价校验顺序与错误处理收拢；不因此增加架构评审或人工确认，也不擅自扩大范围或重命名",
            "先核对相关需求场景与原始来源定位，再核对设计；不能按复用组件改变操作去向，显示信息须对应具体对象、位置与已核实数据，状态变化须覆盖触发、获取事实和最终更新",
            "暂未实现的业务结果交还协调者明确承接切片或依赖输入、影响和恢复条件，不因本切片非目标而取消整体承诺；依赖补齐后核对受影响的全部结果，缺证据时如实记录未验证",
            "范围内普通测试失败时保留当前身份，继续诊断、修复和运行受影响验证，不因失败次数或耗时自动熔断",
            "完成验收场景、改变关键数据流、引入依赖或首次验证失败等有意义节点记录检查点；中间可只登记实际运行的验证子集",
            "检查点自动保存正文；覆盖已有成果前用 snapshot-save --reason before-overwrite 并附交付项，保存失败先恢复再继续",
            "候选提交前最后检查点必须绑定当前契约、覆盖完整登记验证并全部通过；优先引用已有测试运行器原生结果，不能用文字确认代替执行，也不为另存日志重跑已有可复核结果的测试",
            "先用 prepare-action-input 生成检查点或候选输入，按返回合同填写；确定性失败只补报告的缺项，输入未变不重试",
            "发现新增风险时停止沿用旧验证结论并交还协调者；只需追加验证或提高强度时使用 supplement-slice-validation，保留成果并重新形成当前契约的最终检查点",
            "目标、验收、强依赖、公共或存量数据、不变量、安全回退或授权范围确实失效时记录熔断并停止生产修改；验证补充不能改变这些边界",
        ],
        "validation_supplement": {
            "command": "supplement-slice-validation",
            "arguments": {"feature_id": feature_id, "execution_id": execution_id},
            "owner": "coordinator",
            "inputs": ["validation_level", "validation_method（可重复追加）", "rationale（新增风险事实）"],
        },
        "execution_handoff": {
            "feature_id": feature_id,
            "package_id": package_id,
            "execution_id": execution_id,
            "workspace_root": record.get("workspace_root"),
            "task_package": record.get("package"),
        },
    }
    if "ui-delivery" in delivery["trusted_machine_facts"]["approval_statuses"]:
        from archive_ui_baseline import BASELINE_PATH
        result["preconditions"].append("当前开工清单已得到用户确认")
        result["required_materials"].append({"source": "ui-baseline", "path": str(feature.path / BASELINE_PATH),
                                            "purpose": "使用用户已确认的预制体、协议、配置及需求依据", "mode": "full"})
        result["decision_rules"].append("发现必需材料缺失、歧义或需要更换业务依据时，停止整个需求的实施，交还协调者更新开工清单并重新确认；不得自行用占位、延期或拆分绕过。")
        from archive_candidates import candidate_delivery_package
        result["delivery_requirements"] = candidate_delivery_package(record).get("delivery_requirements", [])
        result["expected_outputs"].append(
            "按 delivery_requirements 交回实际文件；通过候选输入 delivery_materials 登记交互、元素定位、截图及验证依据。实施中新发现的同范围材料要求填入 additional_delivery_requirements，不能削减既定要求。"
        )
        result["decision_rules"].append(
            "每完成一个功能单元，结合本次后端、既有逻辑和界面核对遗漏交互；原范围内有依据的补全实现及交互数据，缺业务依据或超范围时交还协调者提出具体问题。候选评审须从需求查实现、从实现查交互说明，不以静态截图代替运行验证。"
        )
    if isinstance(repair_handoff, dict):
        result["repair_handoff"] = repair_handoff
    return result, ()


def _review_role_view(
    context: RoleProjectionContext,
) -> RoleProjectionResult:
    graph = context.graph
    feature_id = context.request.feature_id
    execution_id = context.request.execution_id
    feature = graph.features[feature_id]
    execution = _execution_state(context.state)
    slices = execution.get("slices")
    candidates = [
        (package_id, record)
        for package_id, record in slices.items()
        if isinstance(record, dict) and record.get("status") == "candidate"
    ] if isinstance(slices, dict) else []
    if execution_id is None:
        return None, ({"code": "EXECUTION_ID_REQUIRED", "message": "评审角色必须提供执行身份。"},)
    if not EXECUTION_ID_PATTERN.fullmatch(execution_id):
        return None, ({"code": "INVALID_EXECUTION_ID", "message": "评审执行身份格式不合法。"},)
    if len(candidates) != 1:
        return None, ({"code": "FIXED_CANDIDATE_REQUIRED", "message": "当前没有唯一待评审固定候选。"},)
    package_id, record = candidates[0]
    if record.get("execution_id") == execution_id:
        return None, ({"code": "SELF_REVIEW_FORBIDDEN", "message": "评审身份不能与实施身份相同。"},)
    used_execution_ids = execution.get("used_execution_ids")
    if isinstance(used_execution_ids, list) and execution_id in used_execution_ids:
        return None, ({"code": "EXECUTION_ID_ALREADY_USED", "message": "评审执行身份已经使用。"},)
    candidate = record.get("candidate")
    if not isinstance(candidate, dict):
        return None, ({"code": "FIXED_CANDIDATE_REQUIRED", "message": "固定候选事实缺失。"},)
    workspace_root = candidate.get("workspace_root")
    workspace_matches = False
    if isinstance(workspace_root, str):
        try:
            workspace_matches = matching_workspace_guard_snapshot(
                Path(workspace_root),
                workspace_root,
                candidate.get("workspace_guard_digest"),
            ) is not None
        except (OSError, ValueError):
            workspace_matches = False
    if not workspace_matches:
        return None, ({"code": "CANDIDATE_WORKSPACE_DRIFT", "message": "工作区已经偏离固定候选。"},)
    candidate_id = candidate.get("candidate_id")
    package = record.get("package")
    if not isinstance(package, dict):
        return None, ({"code": "TASK_PACKAGE_REQUIRED", "message": "候选评审缺少完整任务包。"},)
    try:
        task_materials = slice_handoff_materials(
            graph, feature, Path(record["workspace_root"]), record["package"]
        )
    except ArchiveHandoffError as exception:
        return None, exception.blockers
    repair_handoff, repair_blockers = _repair_role_projection(record)
    if repair_blockers:
        return None, repair_blockers
    materials = [
        {
            "source": f"task-package:{package_id}",
            "purpose": "读取目标、授权边界和验收条件",
            "mode": "full",
        },
        {
            "source": f"candidate:{candidate_id}",
            "purpose": "读取固定候选、实际变化与验证证据",
            "mode": "full",
        },
    ]
    materials.extend(task_materials)
    from archive_delivery_materials import delivery_paths
    try:
        materials.extend(delivery_paths(candidate))
    except ArchiveHandoffError as exception:
        return None, exception.blockers
    result = {
        "role": "review",
        "baton": package_id,
        "execution_id": execution_id,
        "goal": "独立判断固定候选在真实使用流程中是否正确、满足需求和批准设计，且新增复杂度具有当前用途。",
        "allowed_operations": [
            "读取任务包引用的批准材料", "读取固定候选引用",
            "沿实际变化只读核对相关调用链、现有实现惯例与验证证据，按风险在隔离位置补充负向或边界检查",
            "提交通过、失败或未验证结论",
        ],
        "decision_rules": [
            "结合本次变化检查输入、状态更新、失败后果和受影响调用方；重复触发、资源释放、状态清理等只检查真实相关的风险，不机械填写全量清单",
            "回到需求场景及原始来源核对操作去向、显示位置、触发及不处理条件；同一行为的多个位置分别核对，标注 PASS 不证明覆盖完整；指出来源缺项或设计遗漏",
            "核对展示数据到目标区域、触发到新事实获取及界面更新的实际证据；暂时隐藏或交给后续的结果须有明确承接与影响，局部候选不替代整体完成，也不要求当前切片越界补做",
            "测试通过和符合批准设计不能代替实际正确性；发现设计遗漏或错误时明确返回设计偏差及影响，交还协调者处理，不能为符合设计而接受错误实现",
            "拒绝未经批准的公共接口、跨模块职责或状态归属、外部配置合同和恢复协议变化；授权范围内保持已批准外部行为与安全约束的内部整理不因缺少单独架构批准而退回，仍检查正确性与维护成本；有明显更简单实现时说明具体维护成本，不为理论完整性要求新增机制",
            "补充检查不改变候选或工作区，不要求固定测试数量；记录实际验证与未验证边界后提交单一结论",
        ],
        "forbidden_operations": ["读取常驻入口", "不得使用实施者对话", "修改候选或工作区", "使用实施执行身份自评"],
        "preconditions": ["候选唯一且未漂移", "任务包批准材料完整且新鲜", "评审身份未使用且不同于实施身份"],
        "required_materials": materials,
        "expected_outputs": [
            "通过、失败或未验证结论",
            "失败或未验证时的问题列表",
            "可选的独立负向或边界测试证据或不适用理由",
        ],
        "input_preparations": [
            action_input_preparation(feature_id, kind, execution_id=execution_id)
            for kind in ("review-issues", "review-verification")
        ],
        "completion_conditions": [f"使用 review-slice 提交 {candidate_id} 的结论后停止"],
        "review_handoff": {
            "feature_id": feature_id,
            "package_id": package_id,
            "review_execution_id": execution_id,
            "task_package": package,
            "candidate": candidate,
        },
        "source_rechecks": {
            "candidate_id": candidate_id,
            "candidate_digest": candidate.get("candidate_digest"),
            "slice_plan_binding": candidate.get("slice_plan_binding"),
            "workspace": "passed",
        },
    }
    if "ui-delivery" in context.delivery["trusted_machine_facts"]["approval_statuses"]:
        result["decision_rules"].append(
            "从完整原始需求查实际实现，再从本次实际实现反查业务场景验收记录；按场景检查相关后端状态、入口条件、操作、反馈和结果，不要求逐控件重复说明。原范围内且不更换已确认开工依据的补充不重复请求批准，但须同步设计、场景记录及验证；遗漏必要场景、无依据扩展或未验证冒充完成时退回具体问题。"
        )
    if isinstance(repair_handoff, dict):
        result["repair_handoff"] = repair_handoff
    return result, ()


def _final_review_role_view(
    context: RoleProjectionContext,
) -> RoleProjectionResult:
    graph = context.graph
    feature_id = context.request.feature_id
    materials = context.materials
    delivery = context.delivery
    integration_confirmation = context.request.integration_confirmation
    feature = graph.features[feature_id]
    state = context.state
    execution = _execution_state(state)
    try:
        bundle = final_review_bundle(
            feature.path,
            execution,
            integration_confirmation,
        )
        ui_review = ui_interaction_review(graph, feature_id, state)
    except (ArchiveApprovalError, ArchiveConfigurationError) as exception:
        return None, ({"code": exception.code, "message": exception.message},)
    candidate_summary = bundle["accepted_candidate_summary"]
    confirmation_binding = bundle["integration_confirmation"]
    accepted_candidates = bundle["accepted_candidates"]
    final_material_sources = {
        f"{feature_id}.requirements.overview",
        f"{feature_id}.validation.overview",
    }

    final_materials = [
        material
        for material in materials
        if material.get("source") in final_material_sources
    ]
    required_materials = list(_role_materials_with_paths(graph, final_materials))
    required_materials.append({
        "source": "integration-confirmation",
        "path": f"{feature_id}/{confirmation_binding['confirmation_path']}",
        "purpose": "核对当前候选集合与最终工作区的集成确认",
        "mode": "full",
    })

    if ui_review:
        required_materials.extend({"source": "ui-delivery", "path": path,
                                   "purpose": "核对最终交互、截图定位、验证状态与实际实现", "mode": "full"}
                                  for path in ui_review["materials"])
    result = {
        "role": "final-review",
        "baton": "final-review",
        "goal": "独立判断当前验证结论和固定交付结果是否满足已批准需求，并提交最终验收建议；业务决定由用户确认。",
        "decision_rules": [
            "从已批准场景反查操作去向、显示对象、结果集合与状态触发链；每项对应实际结果或明确缺项，不能从已接受切片集合正向推断全部需求完成",
            "缺页签、缺信息或缺到期刷新等已承诺结果仍未完成时指出影响与恢复条件；证据不足返回未验证，需要范围调整交还协调者，不改写需求或凭编译与规划审计通过批准",
        ],
        "allowed_operations": [
            "读取列出的正式材料",
            "核对全部已接受候选与独立评审",
            "核对集成确认、未验证边界和残留风险",
            "沿用已批准需求的场景名称和正文定位，逐个核对实际结果与证据，不按控件或 Prefab 内部状态重新拆分",
            "按场景需要核对真实体验入口、初始配置和操作；区分现状、设计示意、原型与最终运行证据，不强制截图、录像或试玩数量",
            "提交批准、退回或未验证建议",
        ],
        "forbidden_operations": [
            "读取实施对话或早期过程材料",
            "修改候选、工作区、验证材料或集成确认",
            "自行补造缺失验证或批准事实",
            "替用户记录最终业务验收决定",
            "用示意、规划截图或原型冒充最终运行结果，或用测试通过代替人的体验判断和业务批准",
        ],
        "preconditions": [
            "需求批准有效且最终验证入口已完成",
            "全部实施切片已经收敛且候选评审绑定有效",
            "最终工作区和集成确认仍绑定当前候选集合",
        ],
        "required_materials": required_materials,
        "expected_outputs": [
            "批准、退回或未验证建议", "未验证边界与残留风险判断", "缺项时的精确缺项列表",
            "按已批准场景指出实际偏差及影响、缺少入口或证据的未验证部分，以及仍需人判断的布局、手感和业务取舍；不复制或改写需求来掩盖偏差",
        ],
        "completion_conditions": ["提交单一最终验收建议后停止并返回主协调者"],
        "final_review_handoff": {
            "feature_id": feature_id,
            "accepted_candidate_summary": {
                **candidate_summary,
                "total_items": len(accepted_candidates),
            },
            "accepted_candidates": accepted_candidates,
            "integration_confirmation": confirmation_binding,
        },
        "source_rechecks": list(delivery["required_rechecks"]),
    }
    if ui_review:
        result["preconditions"][0] = "内部需求就绪，最终交互材料与验证入口已完成"
        result["decision_rules"].append(
            "从实际实现反查统一交互页：独立操作、出现条件、即时反馈和结果须完整；隐藏与无法截图定位的交互仍有文字，状态与证据一致。机器就绪不代表行为已验证，不以静态截图代替运行验证或用户验收。"
        )
    return result, ()


ROLE_CONTRACTS: Mapping[str, RoleContract] = {
    "coordinator": RoleContract(
        builder=_coordinator_role_view,
        task_message_fields=("feature_id", "role"),
        all_or_nothing=False,
    ),
    "cold-read": RoleContract(
        builder=_cold_read_role_view,
        task_message_fields=("feature_id", "role", "entry_question", "allowed_materials"),
    ),
    "design-review": RoleContract(
        builder=_design_review_role_view,
        task_message_fields=("feature_id", "role"),
        allowed_action="design",
        blocking_stale_suffixes=DESIGN_REVIEW_MATERIAL_SUFFIXES,
        action_mismatch_message="设计评审角色只能执行设计上下文动作。",
    ),
    "implementation": RoleContract(
        builder=_implementation_role_view,
        task_message_fields=("feature_id", "role", "execution_id"),
        allowed_action="implementation",
        required_stage="implementation",
        requires_clear_architecture=True,
        action_mismatch_message="实施角色不能执行当前上下文动作。",
        stage_mismatch_message="统一交付投影当前不允许进入实施合同。",
    ),
    "review": RoleContract(
        builder=_review_role_view,
        task_message_fields=("feature_id", "role", "execution_id"),
        allowed_action="implementation",
        required_stage="candidate-review",
        requires_clear_architecture=True,
        action_mismatch_message="候选评审角色不能执行当前上下文动作。",
        stage_mismatch_message="统一交付投影当前不允许进入候选评审合同。",
    ),
    "final-review": RoleContract(
        builder=_final_review_role_view,
        task_message_fields=("feature_id", "role", "integration_confirmation"),
        allowed_action="validation",
        required_stage=("final-review", "freeze-ready"),
        action_mismatch_message="最终验收角色只能执行验证上下文动作。",
        stage_mismatch_message="统一交付投影当前不允许进入最终验收合同。",
    ),
}
CONTEXT_ROLES = tuple(ROLE_CONTRACTS)
ROLE_HANDOFF_ROLES = tuple(
    role for role, contract in ROLE_CONTRACTS.items()
    if contract.all_or_nothing
)


def _delivery_view_for_role(
    delivery: Mapping[str, object],
    contract: RoleContract,
) -> Mapping[str, object]:
    if not contract.all_or_nothing:
        return delivery
    return {
        "current_stage": delivery["current_stage"],
        "blockers": delivery["blockers"],
    }


def context_summary(
    root: Path,
    request: ContextRequest,
) -> Mapping[str, object]:
    feature_id = request.feature_id
    from archive_snapshots import require_snapshot_recovered
    require_snapshot_recovered(root, feature_id)
    action = request.action
    role = request.role
    if action not in CONTEXT_ACTIONS:
        raise ArchiveContextError("INVALID_CONTEXT_ACTION", f"上下文动作“{action}”不受支持。")
    if role is None:
        raise ArchiveContextError("CONTEXT_ROLE_REQUIRED", "DLoop 上下文摘要必须显式声明角色。")
    if role not in CONTEXT_ROLES:
        raise ArchiveContextError("INVALID_CONTEXT_ROLE", f"上下文角色“{role}”不受支持。")
    contract = ROLE_CONTRACTS[role]
    graph = validate_feature_archive(root, feature_id)
    feature = _require_complex_feature(graph, feature_id, require_writable=False)
    state = _load_state(feature.path, feature_id)
    delivery = _delivery_view(graph, feature_id, state)
    allowed = _allowed_actions(graph, feature_id)
    stale = list(refresh_queue(graph, feature_id))
    blockers = []
    requirements = delivery["trusted_machine_facts"]["approval_statuses"]["requirements"]
    if action != "requirements" and requirements not in {"approve", "ready"}:
        blockers.append(requirements_blocker(graph, feature_id, state))
    if action not in allowed:
        blockers.append({"code": "ACTION_NOT_AVAILABLE", "message": f"当前交付状态不允许动作“{action}”。"})
    blocking_stale = stale
    if contract.blocking_stale_suffixes is not None:
        blocking_sources = {
            f"{feature_id}.{suffix}"
            for suffix in contract.blocking_stale_suffixes
        }
        blocking_stale = [
            document_id for document_id in stale
            if document_id in blocking_sources
        ]
    if blocking_stale:
        blockers.append({"code": "STALE_DOCUMENTS", "message": "存在待刷新的上游输入。"})
    materials = list(_suggested_materials(graph, feature_id, action, _execution_state(state)))
    role_view = None
    role_blockers: Sequence[Mapping[str, object]] = ()
    approval_statuses = delivery["trusted_machine_facts"]["approval_statuses"]
    projection = RoleProjectionContext(
        graph=graph,
        state=state,
        request=request,
        materials=materials,
        delivery=delivery,
        requirements_status=requirements,
        stale_documents=stale,
    )
    if request.role == "implementation" and approval_statuses.get("ui-baseline", "not_applicable") not in {"approve", "not_applicable"}:
        role_blockers = ({"code": "UI_BASELINE_APPROVAL_REQUIRED", "message": "开工依据未确认或已变化，停止实施并交还协调者。"},)
    elif contract.requires_clear_architecture and approval_statuses["architecture"] in {
        "reject", "stale",
    }:
        role_blockers = ({
            "code": "ARCHITECTURE_APPROVAL_BLOCKED",
            "message": "架构批准已拒绝或失效。",
        },)
    elif contract.allowed_action is not None and action != contract.allowed_action:
        role_blockers = ({
            "code": "ROLE_ACTION_MISMATCH",
            "message": contract.action_mismatch_message,
        },)
    elif (
        contract.required_stage is not None
        and delivery["current_stage"] not in (
            (contract.required_stage,) if isinstance(contract.required_stage, str) else contract.required_stage
        )
    ):
        role_blockers = ({
            "code": "DELIVERY_STAGE_MISMATCH",
            "message": contract.stage_mismatch_message,
        },)
    else:
        role_view, role_blockers = contract.builder(projection)
        if role_view is not None:
            write_target = (
                stage_overview_target(graph.root, feature_id, action)
                if (
                    role == "coordinator"
                    and not blockers
                    and not role_blockers
                    and _action_is_open_for_writing(
                        action,
                        delivery.get("current_stage"),
                    )
                )
                else None
            )
            input_preparations = []
            if role == "coordinator" and write_target is not None and action in {"plan", "implementation"}:
                input_preparations = [action_input_preparation(feature_id, "slice-plan")]
            role_view = {
                **role_view,
                "write_targets": [write_target] if write_target is not None else [],
                "friction_note": friction_note_contract(
                    feature_id, context={"role": role, "stage": delivery["current_stage"],
                                         "execution_id": request.execution_id},
                ),
            }
            if input_preparations:
                role_view["input_preparations"] = input_preparations
    all_blockers = [*blockers, *role_blockers]
    result = {
        "schema_version": CONTEXT_SUMMARY_VERSION,
        "feature_id": feature_id,
        "lifecycle": feature.lifecycle,
        "requested_action": action,
        **({"allowed_actions": list(allowed)} if role == "coordinator" else {}),
        "blockers": all_blockers,
        "stale_documents": stale,
        "suggested_materials": (
            []
            if contract.all_or_nothing and role_view is not None
            else materials
        ),
        "delivery_view": _delivery_view_for_role(delivery, contract),
    }
    if contract.all_or_nothing and all_blockers:
        result["suggested_materials"] = []
        result["role_blocker"] = {
            "code": "ROLE_VIEW_BLOCKED",
            "role": role,
            "reasons": all_blockers,
            "actual_stage": delivery.get("current_stage"),
            "required_stage": contract.required_stage,
            "next_action": delivery.get("next_action_contract"),
        }
        return result
    if role_view is not None:
        result["role_view"] = role_view
    return result


def prepare_handoff(
    root: Path, request: ContextRequest, *, include_role_view: bool = False,
) -> Mapping[str, object]:
    """上游检查与装配；不启动角色、不使用身份、不保存第二份事实。"""

    if request.role not in ROLE_HANDOFF_ROLES:
        raise ArchiveContextError("INVALID_HANDOFF_ROLE", "交接准备必须指定下游角色。")
    if request.execution_id is None and request.role in {"implementation", "review"}:
        graph = validate_feature_archive(root, request.feature_id)
        state = _load_state(graph.features[request.feature_id].path, request.feature_id)
        execution = _execution_state(state)
        status = "active" if request.role == "implementation" else "candidate"
        records = [item for item in execution["slices"].values() if item.get("status") == status]
        if len(records) == 1:
            record = records[0]
            execution_id = (
                record["execution_id"] if request.role == "implementation"
                else _suggest_execution_id(execution, record["package"]["package_id"], "review")
            )
            request = replace(request, execution_id=execution_id)
    summary = context_summary(root, request)
    blockers = summary["blockers"]
    if blockers:
        return {
            "status": "blocked",
            "feature_id": request.feature_id,
            "role": request.role,
            "handoff_ready": False,
            "blockers": [
                {
                    "owner": "upstream",
                    "recovery": "在当前上游上下文补齐；涉及范围或批准变化时交还主协调者。",
                    **item,
                }
                for item in blockers
            ],
            "next_action": {"action": "complete-upstream-input", "start_downstream": False},
        }
    role_view = summary["role_view"]
    values = {
        "feature_id": request.feature_id,
        "role": request.role,
        "execution_id": request.execution_id,
        "entry_question": request.entry_question,
        "allowed_materials": list(request.allowed_materials),
        "integration_confirmation": (
            str(request.integration_confirmation.resolve())
            if request.integration_confirmation is not None else None
        ),
    }
    task_message = {
        field: values[field] for field in ROLE_CONTRACTS[request.role].task_message_fields
    }
    arguments = ["--action", request.action]
    for field, value in task_message.items():
        if field == "allowed_materials":
            for material in value:
                arguments.extend(["--allowed-material", material])
        elif value is not None:
            arguments.extend(["--" + field.replace("_", "-"), str(value)])
    return {
        "status": "ready",
        "feature_id": request.feature_id,
        "role": request.role,
        "handoff_ready": True,
        "task_message": task_message,
        "context_command": {
            "command": "context-summary",
            "arguments": arguments,
        },
        **({"role_view": role_view} if include_role_view else {}),
    }
