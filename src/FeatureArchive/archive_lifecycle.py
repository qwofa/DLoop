"""功能交付档案的生命周期转换和阶段门禁。"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import List, Mapping, Sequence

from archive_changes import (
    ArchiveChangeError,
    _body_fingerprint,
    _normalized_content,
    _replace_files_atomically,
    _rewrite_front_matter,
    stale_reasons,
)
from archive_validation import (
    CATEGORY_RANK,
    LIFECYCLE_LABELS,
    VALIDATION_STATUS_VALUES,
    ArchiveGraph,
    ArchiveValidationError,
    DocumentRecord,
    rebuild_indexes,
    validate_archive_root,
    validate_feature_archive,
)
from archive_approvals import (
    ArchiveApprovalError,
    _final_candidate_binding,
    _load_state,
    approval_status_from_state,
)
from archive_workspace import ArchiveWorkspaceError, final_execution_blockers, serialized_workflow_state


LEGAL_TRANSITIONS: Mapping[str, Sequence[str]] = {
    "draft": ("active",),
    "active": ("validating",),
    "validating": ("active", "frozen"),
    "frozen": (),
    "pending_cleanup": (),
}
VALIDATING_GATE_CATEGORIES = {"requirements", "design", "plan"}
ARCHIVE_READ_ONLY_LIFECYCLES = {"frozen", "pending_cleanup"}
ENTRY_LIFECYCLE_PATTERN = re.compile(r"(?m)^- 当前生命周期：.+$")


class ArchiveLifecycleError(Exception):
    """表示生命周期转换被确定性规则拒绝。"""

    def __init__(
        self,
        code: str,
        message: str,
        blockers: Sequence[Mapping[str, str]] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.blockers = tuple(blockers)


def _document_blocker(
    graph: ArchiveGraph,
    document: DocumentRecord,
    reason: str,
) -> Mapping[str, str]:
    return {
        "document_id": document.document_id,
        "path": document.path.relative_to(graph.root).as_posix(),
        "reason": reason,
    }


def _document_gate_blockers(
    graph: ArchiveGraph,
    feature_id: str,
    target_lifecycle: str,
) -> List[Mapping[str, str]]:
    blockers: List[Mapping[str, str]] = []
    documents = sorted(
        (
            document
            for document in graph.documents.values()
            if document.feature_id == feature_id and document.category != "archive"
        ),
        key=lambda document: (
            CATEGORY_RANK[document.category],
            document.document_id,
        ),
    )
    for document in documents:
        if (
            target_lifecycle == "validating"
            and document.category not in VALIDATING_GATE_CATEGORIES
        ):
            continue

        if document.content_status == "blocked":
            blockers.append(
                _document_blocker(graph, document, "content_status=blocked")
            )
            continue
        freshness_reasons = stale_reasons(graph, document)
        if freshness_reasons:
            blockers.append(
                _document_blocker(
                    graph,
                    document,
                    "文档过期：" + "；".join(freshness_reasons),
                )
            )
            continue
        if target_lifecycle == "frozen" and document.content_status == "draft":
            blockers.append(
                _document_blocker(graph, document, "content_status=draft")
            )
    return blockers


def _validation_gate_blockers(
    graph: ArchiveGraph,
    feature_id: str,
    validation_statuses: Mapping[str, str],
) -> List[Mapping[str, str]]:
    validation_document = graph.documents[f"{feature_id}.validation.overview"]
    blockers: List[Mapping[str, str]] = []

    conclusion = validation_statuses["conclusion"]
    if conclusion != "passed":
        blockers.append(
            _document_blocker(
                graph,
                validation_document,
                f"validation.conclusion={conclusion}，冻结要求 passed",
            )
        )
    for field in ("unverified_boundaries", "residual_risks"):
        status = validation_statuses[field]
        if status == "pending":
            blockers.append(
                _document_blocker(
                    graph,
                    validation_document,
                    f"validation.{field}=pending，必须明确为 none 或 documented",
                )
            )
    return blockers


def _validate_validation_updates(
    target_lifecycle: str,
    updates: Mapping[str, str],
) -> None:
    if updates and target_lifecycle != "frozen":
        raise ArchiveLifecycleError(
            "INVALID_VALIDATION_UPDATE",
            "验证状态只能在进入 frozen 时随转换命令一并提交。",
        )
    for field, value in updates.items():
        allowed_values = VALIDATION_STATUS_VALUES.get(field)
        if allowed_values is None or value not in allowed_values:
            raise ArchiveLifecycleError(
                "INVALID_VALIDATION_UPDATE",
                f"验证状态 {field} 的值“{value}”不合法。",
            )


def _updated_entry(
    graph: ArchiveGraph,
    feature_id: str,
    target_lifecycle: str,
) -> tuple[Path, str]:
    entry = next(
        document
        for document in graph.documents.values()
        if document.feature_id == feature_id and document.category == "archive"
    )
    content = _normalized_content(entry.path)
    updated, count = ENTRY_LIFECYCLE_PATTERN.subn(
        f"- 当前生命周期：{LIFECYCLE_LABELS[target_lifecycle]}",
        content,
    )
    if count != 1:
        raise ArchiveLifecycleError(
            "INVALID_ARCHIVE_ENTRY",
            f"功能入口“{entry.path}”必须包含唯一的“当前生命周期”摘要。",
        )
    return entry.path, _rewrite_front_matter(
        updated,
        entry.path,
        {"content_fingerprint": _body_fingerprint(updated, entry.path)},
    )


def _updated_manifest(
    graph: ArchiveGraph,
    feature_id: str,
    target_lifecycle: str,
    updated_at: str,
    validation_statuses: Mapping[str, str],
) -> tuple[Path, str]:
    path = graph.features[feature_id].path / "feature.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exception:
        raise ArchiveLifecycleError(
            "INVALID_MANIFEST",
            f"无法更新生命周期清单“{path}”：{exception}",
        ) from exception
    manifest["lifecycle"] = target_lifecycle
    manifest["updated_at"] = updated_at
    manifest["frozen_at"] = updated_at if target_lifecycle == "frozen" else None
    manifest["validation"] = dict(validation_statuses)
    return path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"


@serialized_workflow_state
def transition_lifecycle(
    root: Path,
    feature_id: str,
    target_lifecycle: str,
    updated_at: str,
    validation_updates: Mapping[str, str] | None = None,
    integration_confirmation: Path | None = None,
) -> Mapping[str, object]:
    """检查阶段门禁并原子提交功能生命周期转换。"""

    graph = validate_feature_archive(root, feature_id)
    feature = graph.features.get(feature_id)
    if feature is None:
        raise ArchiveLifecycleError(
            "UNKNOWN_FEATURE",
            f"功能档案“{feature_id}”不存在。",
        )
    updates = dict(validation_updates or {})
    _validate_validation_updates(target_lifecycle, updates)
    if integration_confirmation is not None and target_lifecycle != "frozen":
        raise ArchiveLifecycleError(
            "INVALID_INTEGRATION_CONFIRMATION", "集成确认只能用于交付冻结。",
        )

    if feature.lifecycle == target_lifecycle:
        if updates:
            raise ArchiveLifecycleError(
                "READ_ONLY_ARCHIVE"
                if feature.lifecycle in ARCHIVE_READ_ONLY_LIFECYCLES
                else "INVALID_TRANSITION",
                f"功能档案“{feature_id}”已经处于 {target_lifecycle}，"
                "不能借重复转换改写验证状态。",
            )
        return {
            "status": "unchanged",
            "feature_id": feature_id,
            "lifecycle": feature.lifecycle,
            "path": str(feature.path),
        }

    if feature.lifecycle in ARCHIVE_READ_ONLY_LIFECYCLES:
        raise ArchiveLifecycleError(
            "READ_ONLY_ARCHIVE",
            f"功能档案“{feature_id}”处于 {feature.lifecycle}，不能重新激活或"
            "改写冻结快照；新需求必须创建新的功能交付档案。",
        )

    if target_lifecycle not in LEGAL_TRANSITIONS[feature.lifecycle]:
        raise ArchiveLifecycleError(
            "INVALID_LIFECYCLE_TRANSITION",
            f"功能档案“{feature_id}”不能从 {feature.lifecycle} 转换为"
            f" {target_lifecycle}。合法目标："
            + ("、".join(LEGAL_TRANSITIONS[feature.lifecycle]) or "无"),
        )

    validation_statuses = dict(feature.validation_statuses)
    validation_statuses.update(updates)
    if feature.lifecycle == "validating" and target_lifecycle == "active":
        validation_statuses = {
            field: "pending" for field in VALIDATION_STATUS_VALUES
        }
    blockers: List[Mapping[str, str]] = []
    if target_lifecycle in {"validating", "frozen"}:
        blockers.extend(
            _document_gate_blockers(graph, feature_id, target_lifecycle)
        )
    if target_lifecycle == "frozen":
        blockers.extend(
            _validation_gate_blockers(
                graph,
                feature_id,
                validation_statuses,
            )
        )
        workflow_state = _load_state(feature.path, feature_id)
        try:
            approvals = approval_status_from_state(graph, feature_id, workflow_state)
            if approvals["requirements"]["status"] not in {"approve", "ready"}:
                raise ArchiveApprovalError("REQUIREMENTS_APPROVAL_REQUIRED", "需求共识尚未确认或已失效。")
            if approvals["architecture"]["status"] in {"reject", "stale"}:
                raise ArchiveApprovalError("ARCHITECTURE_APPROVAL_BLOCKED", "架构决定已拒绝或失效。")
            if "ui-delivery" in approvals and approvals["final"]["status"] != "approve":
                raise ArchiveApprovalError("UI_FINAL_APPROVAL_REQUIRED", "最终交互展示必须经用户验收后才能封存。")
            if approvals["final"]["status"] == "reject":
                raise ArchiveApprovalError("FINAL_APPROVAL_REJECTED", "用户已退回当前交付，须先处理退回事项。")
            confirmation = integration_confirmation
            if confirmation is None and approvals["final"]["status"] == "approve":
                saved = workflow_state["approvals"]["final"]["integration_confirmation"]
                confirmation = feature.path / saved["confirmation_path"]
            _final_candidate_binding(feature.path, workflow_state["execution"], confirmation)
        except ArchiveApprovalError as exception:
            blockers.append({
                "document_id": f"{feature_id}.workflow-state",
                "path": f"{feature_id}/workflow-state.json",
                "reason": exception.message,
            })
        try:
            execution_blockers = final_execution_blockers(
                graph.root,
                workflow_state,
            )
        except ArchiveWorkspaceError as exception:
            raise ArchiveLifecycleError(exception.code, exception.message) from exception
        blockers.extend(
            {
                "document_id": f"{feature_id}.workflow-state",
                "path": f"{feature_id}/workflow-state.json",
                "reason": item["message"],
            }
            for item in execution_blockers
        )
    if blockers:
        details = "；".join(
            f"{blocker['document_id']}（{blocker['reason']}）"
            for blocker in blockers
        )
        raise ArchiveLifecycleError(
            "LIFECYCLE_GATE_BLOCKED",
            f"功能档案“{feature_id}”不能进入 {target_lifecycle}：{details}",
            blockers,
        )

    entry_path, entry_content = _updated_entry(
        graph,
        feature_id,
        target_lifecycle,
    )
    manifest_path, manifest_content = _updated_manifest(
        graph,
        feature_id,
        target_lifecycle,
        updated_at,
        validation_statuses,
    )
    original_contents = {
        entry_path: _normalized_content(entry_path),
        manifest_path: manifest_path.read_text(encoding="utf-8"),
    }
    _replace_files_atomically(
        graph.root,
        {
            entry_path: entry_content,
            manifest_path: manifest_content,
        },
    )

    try:
        updated_graph = validate_feature_archive(graph.root, feature_id)
    except Exception as exception:
        try:
            _replace_files_atomically(graph.root, original_contents)
        except ArchiveChangeError as rollback_exception:
            raise ArchiveLifecycleError(
                "LIFECYCLE_ROLLBACK_FAILED",
                f"生命周期转换后的索引重建失败，且无法恢复原状态："
                f"{rollback_exception.message}",
            ) from exception
        raise
    try:
        index_result = rebuild_indexes(validate_archive_root(graph.root))
    except ArchiveValidationError:
        # 单功能事务不因无关档案错误回滚；显式全局索引命令仍负责严格报告。
        index_result = {"status": "deferred"}
    updated_feature = updated_graph.features[feature_id]
    return {
        "status": "transitioned",
        "feature_id": feature_id,
        "from": feature.lifecycle,
        "lifecycle": updated_feature.lifecycle,
        "path": str(updated_feature.path),
        "updated_at": updated_feature.updated_at,
        "frozen_at": updated_feature.frozen_at,
        "validation": dict(updated_feature.validation_statuses),
        "indexes": index_result["status"],
    }
