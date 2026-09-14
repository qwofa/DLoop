#!/usr/bin/env python3
"""功能交付档案的确定性命令行入口。"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sys
from typing import Mapping, Sequence

if __name__ == "__main__":
    sys.dont_write_bytecode = True

from archive_validation import (
    ArchiveValidationError,
    rebuild_indexes,
    validate_archive_root,
    validate_feature_archive,
)
from archive_changes import (
    ArchiveChangeError,
    assert_documents_fresh,
    confirm_change_batch,
    refresh_queue,
)
from archive_audit import audit_archive
from archive_lifecycle import ArchiveLifecycleError, transition_lifecycle
from archive_approvals import (
    APPROVAL_STAGES,
    ArchiveApprovalError,
)
from archive_execution import ArchiveExecutionError
from archive_svn import sync_svn_changelist
from archive_candidates import ArchiveCandidateError
from archive_slice_flow import ArchiveSliceFlowError, supplement_slice_validation
from archive_slice_contract import SliceContractError, VALIDATION_LEVELS
from archive_slice_plan import SlicePlanError
from archive_failure_attribution import (
    ArchiveFrictionError, friction_note_contract, friction_summary,
    record_friction, record_friction_note, record_operation_success,
)
from archive_transactions import (
    ArchiveTransactionError,
    check_slice_contract,
    check_slice_plan,
    checkpoint_slice,
    approve_slice_plan,
    prepare_slice_contract,
    resolve_slice,
    review_slice,
    stage_action,
    start_slice,
    submit_slice,
    workflow_status,
    context_summary,
)
from archive_context import (
    ArchiveContextError,
    CONTEXT_ACTIONS,
    CONTEXT_ROLES,
    ContextRequest,
    ROLE_HANDOFF_ROLES,
    prepare_handoff,
)
from archive_handoff import ArchiveHandoffError
from archive_workspace import ArchiveWorkspaceError
from archive_cleanup import (
    ArchiveCleanupError,
    detect_cleanup_candidates,
    normalize_utc_timestamp,
    preview_purge,
    purge_archives,
)
from archive_terminology import (
    ArchiveTerminologyError,
    analyze_term_impact,
    rename_term,
)
from archive_initialization import ArchiveError, initialize_archive
from archive_configuration import (
    ArchiveConfigurationError,
    investigate_ui,
    publish_ui,
)
from archive_action_inputs import (
    ACTION_INPUT_KINDS,
    ArchiveActionInputError,
    prepare_action_input,
)
from archive_workflow_rules import ArchiveWorkflowRulesError, workflow_rules
from archive_share import ArchiveShareError, export_share_snapshot
from archive_snapshots import (
    STAGES, save_snapshot, list_snapshots, export_snapshot, restore_snapshot,
    recover_snapshots, fork_snapshot,
)
from archive_paths import (
    ArchivePathError,
    WORKFLOW_ARTIFACT_DIRECTORIES,
    archive_location,
    canonical_archive_root,
    project_root_from_entrypoint,
    require_canonical_workflow_artifact,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="feature_archive",
        description="初始化和维护 DLoop 3.0 功能交付档案。",
        allow_abbrev=False,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    workflow_rules_parser = subparsers.add_parser(
        "workflow-rules",
        help="按固定动作一次返回完整工作流参考规则。",
    )
    workflow_rules_parser.add_argument(
        "--action",
        required=True,
        action="append",
        help="固定工作流动作；可重复传入并按顺序合并。",
    )
    for command in ("snapshot-save", "snapshot-list", "snapshot-export", "snapshot-restore", "snapshot-recover", "snapshot-fork"):
        snapshot_parser = subparsers.add_parser(command, help="保存、查看或恢复作业中间态。")
        snapshot_parser.add_argument("--feature-id", required=True)
        if command == "snapshot-save":
            snapshot_parser.add_argument("--reason", default="manual", choices=("manual", "stage-start", "stage-submitted", "stage-completed", "before-overwrite", "pause", "resume"))
            snapshot_parser.add_argument("--stage", choices=STAGES)
            snapshot_parser.add_argument("--name", default="")
            snapshot_parser.add_argument("--request-id")
        elif command in {"snapshot-export", "snapshot-fork"}:
            snapshot_parser.add_argument("--snapshot-id", required=True)
            if command == "snapshot-export":
                snapshot_parser.add_argument("--destination", required=True, type=Path)
            else:
                snapshot_parser.add_argument("--source-feature-id", required=True)
                snapshot_parser.add_argument("--title", required=True)
        elif command == "snapshot-restore":
            selection = snapshot_parser.add_mutually_exclusive_group(required=True)
            selection.add_argument("--snapshot-id")
            selection.add_argument("--stage", choices=STAGES)
            snapshot_parser.add_argument("--execute", action="store_true")
    svn_parser = subparsers.add_parser("sync-svn-changelist", help="将当前交付产物整理到同一本地 SVN 提交组。")
    svn_parser.add_argument("--feature-id", required=True)
    init_parser = subparsers.add_parser("init", help="初始化 DLoop 3.0 严格功能交付档案。")
    init_parser.add_argument(
        "--feature-id",
        required=True,
        help="稳定的小写英文功能标识，例如 building-interaction。",
    )
    ui_investigate_parser = subparsers.add_parser(
        "ui-investigate",
        help="提交需求与真实 Prefab 判断，并取得 Unity 截图调研包。",
    )
    ui_investigate_parser.add_argument("--feature-id", required=True)
    ui_investigate_parser.add_argument("--input", required=True, type=Path)
    capture_options = ui_investigate_parser.add_mutually_exclusive_group()
    capture_options.add_argument(
        "--unity-executable",
        type=Path,
        help="显式使用批处理 Unity 截图；项目已在编辑器打开时应使用当前编辑器采集。",
    )
    capture_options.add_argument(
        "--capture-manifest",
        type=Path,
        help="当前 Unity 编辑器按本次请求生成的截图清单；仍执行完整证据校验。",
    )
    ui_publish_parser = subparsers.add_parser(
        "ui-publish",
        help="提交真实截图上的修改、复用或不可见判断，并发布标注计划。",
    )
    ui_publish_parser.add_argument("--feature-id", required=True)
    ui_publish_parser.add_argument("--input", required=True, type=Path)
    init_parser.add_argument("--title", required=True, help="生成到文档中的中文功能标题。")
    init_parser.add_argument(
        "--configuration",
        help="显式选择可选规划配置；省略时保持普通 DLoop 行为。",
    )
    init_parser.add_argument(
        "--source-document",
        action="append",
        default=[],
        type=Path,
        help="已存在的本地 UTF-8 需求材料；网页先保存正文摘录后传文件路径，可重复传入。",
    )
    validate_parser = subparsers.add_parser(
        "validate",
        help="校验全部功能档案的结构和依赖图。",
    )
    workflow_status_parser = subparsers.add_parser(
        "workflow-status",
        help="读取审批、执行、工作区租约和最终门禁的聚合状态。",
    )
    workflow_status_parser.add_argument("--feature-id")
    workflow_status_parser.add_argument(
        "--include-details", action="store_true",
        help="排查时展开完整工作流状态、修改占用基线和最终门禁明细。",
    )

    export_share_parser = subparsers.add_parser(
        "export-share",
        help="从唯一规范档案形成不可执行的只读分享快照。",
    )
    export_share_parser.add_argument("--feature-id", required=True)

    friction_note_parser = subparsers.add_parser(
        "friction-note", help="就近补记额外劳动或恢复方式；不改变业务状态。",
    )
    friction_note_parser.add_argument("--feature-id", required=True)
    friction_note_parser.add_argument("--incident-id")
    friction_note_parser.add_argument("--summary", help="当时目标与卡点；新观察必填。")
    friction_note_parser.add_argument("--extra-work", help="实际多做了什么；新观察必填。")
    friction_note_parser.add_argument("--evidence", action="append", default=[], help="证据引用，可重复；文件用 path:项目相对路径。")
    friction_note_parser.add_argument("--recovery", help="关键改动和恢复结果。")
    friction_note_parser.add_argument("--cost", help="可选：有依据的次数或耗时，写明单位和来源。")
    friction_note_parser.add_argument("--source", choices=("role-report", "user-feedback"), default="role-report")
    friction_note_parser.add_argument("--role", choices=CONTEXT_ROLES)
    friction_note_parser.add_argument("--stage")
    friction_note_parser.add_argument("--execution-id")
    friction_summary_parser = subparsers.add_parser(
        "friction-summary", help="只读复盘指定交付项的异常、观察、恢复方式与证据入口。",
    )
    friction_summary_parser.add_argument("--feature-id", required=True)

    for command in ("context-summary", "prepare-handoff"):
        context_summary_parser = subparsers.add_parser(
            command,
            help="按当前动作读取角色材料或在上游准备完整交接。",
        )
        context_summary_parser.add_argument("--feature-id", required=True)
        context_summary_parser.add_argument("--action", required=True, choices=CONTEXT_ACTIONS)
        context_summary_parser.add_argument(
            "--role", required=True,
            choices=ROLE_HANDOFF_ROLES if command == "prepare-handoff" else CONTEXT_ROLES,
        )
        context_summary_parser.add_argument("--execution-id")
        context_summary_parser.add_argument("--entry-question")
        context_summary_parser.add_argument("--allowed-material", action="append", default=[])
        if command == "prepare-handoff":
            context_summary_parser.add_argument(
                "--include-role-view", action="store_true", help="排查交接时显式展开完整角色视图。",
            )
        context_summary_parser.add_argument(
            "--integration-confirmation",
            type=Path,
            help="最终验收角色必须提供的当前集成确认文件。",
        )

    supplement_parser = subparsers.add_parser(
        "supplement-slice-validation", help="保留当前实施成果，仅追加验证或提高验证强度。",
    )
    supplement_parser.add_argument("--feature-id", required=True)
    supplement_parser.add_argument("--execution-id", required=True)
    supplement_parser.add_argument("--validation-level", required=True, choices=VALIDATION_LEVELS)
    supplement_parser.add_argument("--validation-method", action="append", default=[])
    supplement_parser.add_argument("--rationale", required=True)

    stage_action_parser = subparsers.add_parser(
        "stage-action",
        help="记录用户明确作出的需求、UI 交互、条件性架构或最终验收决定。",
    )
    stage_action_parser.add_argument("--feature-id", required=True)
    stage_action_parser.add_argument("--stage", required=True, choices=APPROVAL_STAGES)
    stage_action_parser.add_argument("--decision", required=True, choices=("approve", "reject"))
    stage_action_parser.add_argument("--reviewed-digest", help="用户审核时交付视图返回的 UI 方案摘要。")
    stage_action_parser.add_argument("--user-confirmation", help="对应用户回复的定位和原文；不得填写 AI 评审结论。")
    stage_action_parser.add_argument(
        "--integration-confirmation",
        type=Path,
        help="最终批准必须提供的当前集成确认文件。",
    )

    check_plan_parser = subparsers.add_parser(
        "check-slice-plan",
        help="规范化并检查版本化切片方案；任意数量都派生关系图与资格。",
    )
    check_plan_parser.add_argument("--feature-id", required=True)
    check_plan_parser.add_argument("--plan-file", required=True, type=Path)

    approve_plan_parser = subparsers.add_parser(
        "approve-slice-plan",
        help="按检查摘要原子批准切片方案并更新唯一当前版本。",
    )
    approve_plan_parser.add_argument("--feature-id", required=True)
    approve_plan_parser.add_argument("--plan-file", required=True, type=Path)
    approve_plan_parser.add_argument("--plan-digest", required=True)

    prepare_contract_parser = subparsers.add_parser(
        "prepare-slice-contract",
        help="把切片契约草稿写入既有执行账本，等待检查。",
    )
    prepare_contract_parser.add_argument("--feature-id", required=True)
    prepare_contract_parser.add_argument("--package-file", required=True, type=Path)

    check_contract_parser = subparsers.add_parser(
        "check-slice-contract",
        help="形成可审查的实施前契约检查结果。",
    )
    check_contract_parser.add_argument("--feature-id", required=True)
    check_contract_target = check_contract_parser.add_mutually_exclusive_group(required=True)
    check_contract_target.add_argument("--package-file", type=Path)
    check_contract_target.add_argument("--package-id")

    prepare_input_parser = subparsers.add_parser(
        "prepare-action-input",
        help="按当前事实生成任务包、切片方案、检查点或候选，并给出填写说明与后续入口。",
    )
    prepare_input_parser.add_argument("--feature-id", required=True)
    prepare_input_parser.add_argument("--input-kind", required=True, choices=ACTION_INPUT_KINDS)
    prepare_input_parser.add_argument("--package-id")
    prepare_input_parser.add_argument("--execution-id")

    start_slice_parser = subparsers.add_parser(
        "start-slice",
        help="校验简明任务包并启动当前执行切片。",
    )
    start_slice_parser.add_argument("--feature-id", required=True)
    start_slice_parser.add_argument("--execution-id", required=True)
    start_slice_target = start_slice_parser.add_mutually_exclusive_group(required=True)
    start_slice_target.add_argument("--package-file", type=Path)
    start_slice_target.add_argument("--package-id")
    start_slice_parser.add_argument("--workspace-root", required=True, type=Path)

    checkpoint_parser = subparsers.add_parser(
        "checkpoint-slice",
        help="记录实施检查点，并按集中阈值判定是否熔断。",
    )
    checkpoint_parser.add_argument("--feature-id", required=True)
    checkpoint_parser.add_argument("--execution-id", required=True)
    checkpoint_parser.add_argument("--checkpoint-file", required=True, type=Path)

    submit_slice_parser = subparsers.add_parser(
        "submit-slice",
        help="关闭执行切片，并在修改成功时提交固定候选。",
    )
    submit_slice_parser.add_argument("--feature-id", required=True)
    submit_slice_parser.add_argument("--execution-id", required=True)
    submit_slice_parser.add_argument(
        "--status", required=True, choices=("completed", "failed", "interrupted")
    )
    submit_slice_parser.add_argument("--candidate-file", type=Path)

    review_slice_parser = subparsers.add_parser(
        "review-slice",
        help="在不同职责的新上下文中验收当前候选。",
    )
    review_slice_parser.add_argument("--feature-id", required=True)
    review_slice_parser.add_argument("--package-id", required=True)
    review_slice_parser.add_argument("--candidate-id", required=True)
    review_slice_parser.add_argument("--review-execution-id", required=True)
    review_slice_parser.add_argument(
        "--result", required=True, choices=("passed", "failed", "unverified")
    )
    review_slice_parser.add_argument("--issues-file", type=Path)
    review_slice_parser.add_argument(
        "--verification-file",
        type=Path,
        help="评审者自行设计并实际运行的负向或边界测试证据。",
    )
    review_slice_parser.add_argument(
        "--verification-not-applicable-reason",
        help="通过评审但负向或边界测试确实不适用时的明确理由。",
    )

    resolve_slice_parser = subparsers.add_parser(
        "resolve-slice",
        help="终止未实施切片，或在原范围重试失败或熔断切片、在人工恢复后终止并释放。",
    )
    resolve_slice_parser.add_argument("--feature-id", required=True)
    resolve_slice_parser.add_argument(
        "--action", required=True, choices=("retry", "release", "release-orphan")
    )
    resolve_slice_parser.add_argument("--package-id", required=True)
    resolve_slice_parser.add_argument("--execution-id")
    resolve_slice_parser.add_argument("--workspace-decision", choices=("kept", "restored"))

    audit_parser = subparsers.add_parser(
        "audit",
        help="只读审计指定交付项的结构与正文确认状态。",
    )
    audit_parser.add_argument(
        "--feature-id",
        required=True,
        help="需要审计的功能标识。",
    )
    rebuild_parser = subparsers.add_parser(
        "rebuild-indexes",
        help="校验后重建机器依赖索引和人工全局索引。",
    )
    confirm_parser = subparsers.add_parser(
        "confirm-change",
        help="明确确认一次文档编辑批次是否改变语义。",
    )
    confirm_parser.add_argument(
        "--document-id",
        required=True,
        action="append",
        help="本批次编辑的文档标识；同一批次可重复传入。",
    )
    confirm_parser.add_argument(
        "--semantic-change",
        required=True,
        choices=("true", "false"),
        help="由 AI 明确提交本批次是否改变语义，工具不会自行猜测。",
    )
    queue_parser = subparsers.add_parser(
        "refresh-queue",
        help="按拓扑顺序列出当前待刷新的文档。",
    )
    queue_parser.add_argument(
        "--feature-id",
        help="可选，仅查看指定功能的待刷新队列。",
    )
    fresh_parser = subparsers.add_parser(
        "assert-fresh",
        help="断言指定文档可作为可靠执行输入。",
    )
    fresh_parser.add_argument(
        "--document-id",
        required=True,
        action="append",
        help="需要检查的文档标识；可重复传入。",
    )
    rename_term_parser = subparsers.add_parser(
        "rename-term",
        help="在活动交付项内执行无语义术语重命名。",
    )
    rename_term_parser.add_argument(
        "--feature-id",
        required=True,
        help="术语所属功能标识。",
    )
    rename_term_parser.add_argument(
        "--from",
        dest="old_term",
        required=True,
        help="需要重命名的已确认首选术语。",
    )
    rename_term_parser.add_argument(
        "--to",
        dest="new_term",
        required=True,
        help="新的首选术语。",
    )
    impact_parser = subparsers.add_parser(
        "analyze-impact",
        help="组合术语引用和文档依赖图生成只读影响分析。",
    )
    impact_parser.add_argument(
        "--feature-id",
        required=True,
        help="术语所属功能标识。",
    )
    impact_parser.add_argument(
        "--term",
        required=True,
        help="需要分析的首选术语。",
    )
    lifecycle_parser = subparsers.add_parser(
        "transition-lifecycle",
        help="检查阶段门禁并转换功能生命周期。",
    )
    lifecycle_parser.add_argument(
        "--feature-id",
        required=True,
        help="需要转换的功能标识。",
    )
    lifecycle_parser.add_argument(
        "--to",
        required=True,
        choices=("draft", "active", "validating", "frozen"),
        help="目标生命周期。",
    )
    lifecycle_parser.add_argument(
        "--integration-confirmation",
        type=Path,
        help="交付冻结时核对的集成确认文件；已有有效最终批准时可复用其确认。",
    )
    lifecycle_parser.add_argument(
        "--validation-conclusion",
        choices=("pending", "passed", "failed"),
        help="进入 frozen 时提交验证结论状态。",
    )
    lifecycle_parser.add_argument(
        "--unverified-boundaries",
        choices=("pending", "none", "documented"),
        help="进入 frozen 时提交未验证边界状态。",
    )
    lifecycle_parser.add_argument(
        "--residual-risks",
        choices=("pending", "none", "documented"),
        help="进入 frozen 时提交残留风险状态。",
    )
    lifecycle_parser.add_argument(
        "--now",
        help="可控的 ISO 8601 当前时间；默认使用系统 UTC 时间。",
    )
    detect_parser = subparsers.add_parser(
        "detect-cleanup",
        help="检测达到保留期的冻结档案并标记为待清理。",
    )
    detect_parser.add_argument(
        "--feature-id",
        action="append",
        default=[],
        help="可选，仅检测明确指定的功能档案；可重复传入。",
    )
    detect_parser.add_argument(
        "--now",
        help="可控的 ISO 8601 当前时间；默认使用系统 UTC 时间。",
    )
    purge_parser = subparsers.add_parser(
        "purge",
        help="预览或执行明确选定的待清理档案删除。",
    )
    purge_parser.add_argument(
        "--feature-id",
        required=True,
        action="append",
        help="明确选定的待清理功能档案；可重复传入。",
    )
    purge_parser.add_argument(
        "--now",
        help="可控的 ISO 8601 当前时间；默认使用系统 UTC 时间。",
    )
    purge_parser.add_argument(
        "--execute",
        action="store_true",
        help="执行删除；省略时只输出安全预览，不修改任何文件。",
    )
    for command_parser in subparsers.choices.values():
        command_parser.allow_abbrev = False
    return parser


EXPECTED_GUARD_CODES = {
    "ACTION_NOT_AVAILABLE",
    "APPROVAL_DOCUMENT_NOT_READY",
    "ARCHITECTURE_APPROVAL_BLOCKED",
    "CONTEXT_ACTION_REQUIRED",
    "DELIVERY_STAGE_MISMATCH",
    "EXECUTION_INPUT_NOT_READY",
    "DLOOP_UI_PLAN_BLOCKED",
    "DLOOP_UI_REQUIREMENTS_REQUIRED",
    "REQUIREMENTS_APPROVAL_REQUIRED",
    "UI_FINAL_APPROVAL_REQUIRED",
    "UI_DELIVERY_OUTDATED",
    "UI_INTERNAL_STAGE",
    "USER_CONFIRMATION_REQUIRED",
    "STALE_DOCUMENTS",
}


@dataclass(frozen=True)
class _OperationContext:
    feature_id: str | None
    command: str
    target: Mapping[str, object]
    request_input: Mapping[str, object]
    context: Mapping[str, object]


def _operation_target(arguments: argparse.Namespace) -> Mapping[str, object]:
    fields = (
        "package_id",
        "candidate_id",
        "execution_id",
        "review_execution_id",
        "document_id",
        "stage",
        "package_file",
        "plan_file",
        "candidate_file",
        "issues_file",
        "verification_file",
        "checkpoint_file",
        "decision_file",
        "recovery_file",
        "integration_confirmation",
    )
    return {
        field: getattr(arguments, field)
        for field in fields
        if getattr(arguments, field, None) is not None
    }


def _request_input_value(value: object) -> object:
    if isinstance(value, Path):
        result = {"path": str(value)}
        try:
            if value.is_file():
                digest = hashlib.sha256()
                with value.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                result.update({"kind": "file", "sha256": digest.hexdigest()})
            elif value.is_dir():
                result["kind"] = "directory"
            elif value.exists():
                result["kind"] = "other"
            else:
                result["kind"] = "missing"
        except (OSError, ValueError):
            result["kind"] = "unreadable"
        return result
    if isinstance(value, Mapping):
        return {
            str(key): _request_input_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_request_input_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _operation_context(
    arguments: argparse.Namespace,
    *,
    capture_request_input: bool = True,
) -> _OperationContext:
    request_input = (
        {
            field: _request_input_value(value)
            for field, value in vars(arguments).items()
            if field not in {"command", "root"} and value is not None
        }
        if capture_request_input
        else {}
    )
    feature_id = getattr(arguments, "feature_id", None)
    if feature_id is None:
        document_ids = getattr(arguments, "document_id", None)
        if isinstance(document_ids, list) and document_ids:
            # 文档标识首段就是所属交付项；跨交付项批次不归给其中某一个。
            owners = {document_id.split(".", 1)[0] for document_id in document_ids}
            if len(owners) == 1 and all("." in document_id for document_id in document_ids):
                feature_id = next(iter(owners))
    return _OperationContext(
        feature_id=feature_id if isinstance(feature_id, str) else None,
        command=arguments.command,
        target=_operation_target(arguments),
        request_input=request_input,
        context={
            "role": getattr(arguments, "role", None),
            "stage": (getattr(arguments, "action", None)
                      if arguments.command in {"context-summary", "prepare-handoff"}
                      else getattr(arguments, "stage", None)),
            "execution_id": getattr(arguments, "execution_id", None) or getattr(arguments, "review_execution_id", None),
        },
    )


def _attach_friction_record(
    result: Mapping[str, object],
    root: object,
    friction: str,
    *,
    operation: _OperationContext,
    code: str,
    expected_guard: bool,
) -> Mapping[str, object]:
    if not isinstance(root, Path):
        return result
    recorded = record_friction(
        root,
        friction,
        feature_id=operation.feature_id,
        command=operation.command,
        code=code,
        target=operation.target,
        request_input=operation.request_input,
        expected_guard=expected_guard,
        context=operation.context,
    )
    enriched = dict(result)
    if recorded.get("status") in {"recorded", "unchanged"} and operation.feature_id is not None:
        enriched["friction_note"] = friction_note_contract(
            operation.feature_id, incident_id=recorded["incident_id"], context=operation.context,
        )
    if not expected_guard and recorded.get("status") in {"recorded", "unchanged"}:
        enriched["friction_id"] = recorded["incident_id"]
    elif isinstance(recorded.get("warning"), str):
        enriched["friction_record_warning"] = recorded["warning"]
    return enriched


def _expected_guard(result: Mapping[str, object]) -> Mapping[str, str] | None:
    blockers = result.get("blockers")
    if not isinstance(blockers, list):
        return None
    for blocker in blockers:
        if not isinstance(blocker, dict):
            continue
        code = blocker.get("code")
        message = blocker.get("message")
        if code in EXPECTED_GUARD_CODES and isinstance(message, str) and message:
            return {"code": str(code), "message": message}
    return None


def _configure_stdio() -> None:
    """统一命令行机器输出编码，避免 Windows 区域设置改变 JSON 字节。"""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


def _with_archive_location(
    payload: Mapping[str, object],
    arguments: argparse.Namespace,
    project_root: Path,
) -> Mapping[str, object]:
    feature_id = getattr(arguments, "feature_id", None)
    if not isinstance(feature_id, str):
        return dict(payload)
    root = getattr(arguments, "root", canonical_archive_root(project_root))
    return {
        **payload,
        "archive_location": archive_location(
            project_root,
            feature_id,
            archive_root=root,
        ).as_dict(),
    }


def _resolve_path_arguments(
    arguments: argparse.Namespace,
    project_root: Path,
) -> None:
    """让公开命令中的相对文件路径始终以当前项目为基准。"""

    def resolve(value: Path) -> Path:
        expanded = value.expanduser()
        return (
            expanded.resolve()
            if expanded.is_absolute()
            else (project_root / expanded).resolve()
        )

    for field, value in vars(arguments).items():
        if isinstance(value, Path):
            setattr(arguments, field, resolve(value))
        elif isinstance(value, list) and any(
            isinstance(item, Path) for item in value
        ):
            setattr(
                arguments,
                field,
                [resolve(item) if isinstance(item, Path) else item for item in value],
            )


def main(argv: Sequence[str] | None = None) -> int:
    _configure_stdio()
    parser = _build_parser()
    raw_arguments = list(argv) if argv is not None else list(sys.argv[1:])
    if any(
        item == option or item.startswith(option + "=")
        for item in raw_arguments
        for option in ("--root", "--project-root")
    ):
        parser.error("规范档案根由 DLoop 根据项目内安装位置确定，不能由调用者指定。")
    arguments = parser.parse_args(raw_arguments)
    resolved_project_root = project_root_from_entrypoint(Path(__file__))
    if arguments.command == "workflow-rules":
        arguments.project_root = resolved_project_root
    else:
        arguments.root = canonical_archive_root(resolved_project_root)
    _resolve_path_arguments(arguments, resolved_project_root)
    exit_code = 0

    try:
        feature_id = getattr(arguments, "feature_id", None)
        root = getattr(arguments, "root", None)
        if isinstance(feature_id, str) and isinstance(root, Path):
            for field in WORKFLOW_ARTIFACT_DIRECTORIES:
                value = getattr(arguments, field, None)
                if isinstance(value, Path):
                    setattr(
                        arguments,
                        field,
                        require_canonical_workflow_artifact(
                            root,
                            feature_id,
                            field,
                            value,
                        ),
                    )
        if arguments.command == "workflow-rules":
            result = workflow_rules(arguments.project_root, arguments.action)
        elif arguments.command == "snapshot-save":
            result = save_snapshot(arguments.root, arguments.feature_id, arguments.reason,
                                   name=arguments.name, stage=arguments.stage, request_id=arguments.request_id)
        elif arguments.command == "snapshot-list":
            result = list_snapshots(arguments.root, arguments.feature_id)
        elif arguments.command == "snapshot-export":
            result = export_snapshot(arguments.root, arguments.feature_id, arguments.snapshot_id, arguments.destination)
        elif arguments.command == "snapshot-restore":
            result = restore_snapshot(arguments.root, arguments.feature_id, arguments.snapshot_id,
                                      stage=arguments.stage, execute=arguments.execute)
        elif arguments.command == "snapshot-recover":
            result = recover_snapshots(arguments.root, arguments.feature_id)
        elif arguments.command == "snapshot-fork":
            result = fork_snapshot(arguments.root, arguments.source_feature_id, arguments.snapshot_id,
                                   arguments.feature_id, arguments.title)
        elif arguments.command == "sync-svn-changelist":
            validate_feature_archive(arguments.root, arguments.feature_id)
            result = sync_svn_changelist(arguments.root, arguments.feature_id, resolved_project_root)
        elif arguments.command == "init":
            result = initialize_archive(
                arguments.root,
                arguments.feature_id,
                arguments.title,
                arguments.configuration,
                arguments.source_document,
            )
        elif arguments.command == "validate":
            graph = validate_archive_root(arguments.root)
            result = {
                "status": "valid",
                "root": str(graph.root),
                "feature_count": len(graph.features),
                "document_count": len(graph.documents),
                "dependency_count": sum(
                    len(document.dependencies)
                    for document in graph.documents.values()
                ),
                "related_count": sum(
                    len(document.related_documents)
                    for document in graph.documents.values()
                ),
            }
        elif arguments.command == "ui-investigate":
            result = investigate_ui(
                arguments.root,
                arguments.feature_id,
                arguments.input,
                resolved_project_root,
                arguments.unity_executable,
                arguments.capture_manifest,
            )
        elif arguments.command == "ui-publish":
            result = publish_ui(
                arguments.root,
                arguments.feature_id,
                arguments.input,
                resolved_project_root,
            )
        elif arguments.command == "workflow-status":
            result = workflow_status(
                arguments.root, arguments.feature_id, include_details=arguments.include_details,
            )
        elif arguments.command == "export-share":
            result = export_share_snapshot(
                resolved_project_root,
                arguments.root,
                arguments.feature_id,
            )
        elif arguments.command == "friction-note":
            result = record_friction_note(
                arguments.root, feature_id=arguments.feature_id,
                incident_id=arguments.incident_id, summary=arguments.summary,
                extra_work=arguments.extra_work, evidence=arguments.evidence,
                recovery=arguments.recovery, cost=arguments.cost, source=arguments.source,
                context=_operation_context(arguments, capture_request_input=False).context,
            )
            if result["status"] == "not-recorded":
                exit_code = 1
        elif arguments.command == "friction-summary":
            result = friction_summary(arguments.root, arguments.feature_id)
        elif arguments.command in {"context-summary", "prepare-handoff"}:
            operation = prepare_handoff if arguments.command == "prepare-handoff" else context_summary
            options = {"include_role_view": arguments.include_role_view} if arguments.command == "prepare-handoff" else {}
            result = operation(
                arguments.root,
                ContextRequest(
                    feature_id=arguments.feature_id,
                    action=arguments.action,
                    role=arguments.role,
                    execution_id=arguments.execution_id,
                    entry_question=arguments.entry_question,
                    allowed_materials=tuple(arguments.allowed_material),
                    integration_confirmation=arguments.integration_confirmation,
                ),
                **options,
            )
        elif arguments.command == "supplement-slice-validation":
            result = supplement_slice_validation(
                arguments.root, arguments.feature_id, arguments.execution_id,
                arguments.validation_level, arguments.validation_method, arguments.rationale,
            )
        elif arguments.command == "stage-action":
            result = stage_action(
                arguments.root,
                arguments.feature_id,
                arguments.stage,
                arguments.decision,
                arguments.integration_confirmation,
                reviewed_digest=arguments.reviewed_digest,
                user_confirmation=arguments.user_confirmation,
            )
        elif arguments.command == "check-slice-plan":
            result = check_slice_plan(
                arguments.root,
                arguments.feature_id,
                arguments.plan_file,
            )
        elif arguments.command == "approve-slice-plan":
            result = approve_slice_plan(
                arguments.root,
                arguments.feature_id,
                arguments.plan_file,
                arguments.plan_digest,
            )
        elif arguments.command == "prepare-slice-contract":
            result = prepare_slice_contract(
                arguments.root,
                arguments.feature_id,
                arguments.package_file,
            )
        elif arguments.command == "check-slice-contract":
            result = check_slice_contract(
                arguments.root,
                arguments.feature_id,
                arguments.package_file,
                arguments.package_id,
            )
            if result["status"] == "contract_failed":
                exit_code = 1
        elif arguments.command == "prepare-action-input":
            result = prepare_action_input(
                arguments.root,
                arguments.feature_id,
                arguments.input_kind,
                package_id=arguments.package_id,
                execution_id=arguments.execution_id,
            )
        elif arguments.command == "start-slice":
            result = start_slice(
                arguments.root,
                arguments.feature_id,
                arguments.execution_id,
                arguments.package_file,
                arguments.workspace_root,
                arguments.package_id,
            )
            if result["status"] == "contract_failed":
                exit_code = 1
        elif arguments.command == "checkpoint-slice":
            result = checkpoint_slice(
                arguments.root,
                arguments.feature_id,
                arguments.execution_id,
                arguments.checkpoint_file,
            )
        elif arguments.command == "submit-slice":
            result = submit_slice(
                arguments.root,
                arguments.feature_id,
                arguments.execution_id,
                arguments.status,
                arguments.candidate_file,
            )
        elif arguments.command == "review-slice":
            result = review_slice(
                arguments.root,
                arguments.feature_id,
                arguments.package_id,
                arguments.candidate_id,
                arguments.review_execution_id,
                arguments.result,
                arguments.issues_file,
                arguments.verification_file,
                arguments.verification_not_applicable_reason,
            )
        elif arguments.command == "resolve-slice":
            result = resolve_slice(
                arguments.root,
                arguments.feature_id,
                arguments.action,
                arguments.package_id,
                arguments.execution_id,
                arguments.workspace_decision,
            )
        elif arguments.command == "audit":
            result = audit_archive(
                arguments.root,
                arguments.feature_id,
            )
            if result["status"] == "blocked":
                exit_code = 1
        elif arguments.command == "rebuild-indexes":
            graph = validate_archive_root(arguments.root)
            result = rebuild_indexes(graph)
        elif arguments.command == "confirm-change":
            result = confirm_change_batch(
                arguments.root,
                arguments.document_id,
                arguments.semantic_change == "true",
            )
        elif arguments.command == "refresh-queue":
            graph = (
                validate_feature_archive(arguments.root, arguments.feature_id)
                if arguments.feature_id
                else validate_archive_root(arguments.root)
            )
            result = {
                "status": "ready",
                "root": str(graph.root),
                "feature_id": arguments.feature_id,
                "refresh_queue": list(
                    refresh_queue(graph, arguments.feature_id)
                ),
            }
        elif arguments.command == "assert-fresh":
            feature_ids = {
                document_id.split(".", 1)[0]
                for document_id in arguments.document_id
            }
            graph = (
                validate_feature_archive(arguments.root, next(iter(feature_ids)))
                if len(feature_ids) == 1
                else validate_archive_root(arguments.root)
            )
            result = assert_documents_fresh(graph, arguments.document_id)
        elif arguments.command == "rename-term":
            result = rename_term(
                arguments.root,
                arguments.feature_id,
                arguments.old_term,
                arguments.new_term,
            )
        elif arguments.command == "analyze-impact":
            result = analyze_term_impact(
                arguments.root,
                arguments.feature_id,
                arguments.term,
            )
        elif arguments.command == "transition-lifecycle":
            validation_updates = {
                field: value
                for field, value in (
                    ("conclusion", arguments.validation_conclusion),
                    ("unverified_boundaries", arguments.unverified_boundaries),
                    ("residual_risks", arguments.residual_risks),
                )
                if value is not None
            }
            result = transition_lifecycle(
                arguments.root,
                arguments.feature_id,
                arguments.to,
                normalize_utc_timestamp(arguments.now),
                validation_updates,
                arguments.integration_confirmation,
            )
        elif arguments.command == "detect-cleanup":
            result = detect_cleanup_candidates(
                arguments.root,
                arguments.now,
                arguments.feature_id,
            )
        elif arguments.command == "purge":
            if arguments.execute:
                result = purge_archives(
                    arguments.root,
                    arguments.feature_id,
                    arguments.now,
                )
            else:
                result = preview_purge(
                    arguments.root,
                    arguments.feature_id,
                    arguments.now,
                )
        else:
            parser.error(f"不支持的命令：{arguments.command}")
            return 2
    except (
        ArchiveError,
        ArchiveValidationError,
        ArchiveChangeError,
        ArchiveLifecycleError,
        ArchiveCleanupError,
        ArchiveTerminologyError,
        ArchiveApprovalError,
        ArchiveExecutionError,
        ArchiveCandidateError,
        ArchiveSliceFlowError,
        SliceContractError,
        SlicePlanError,
        ArchiveTransactionError,
        ArchiveWorkspaceError,
        ArchiveContextError,
        ArchiveHandoffError,
        ArchiveWorkflowRulesError,
        ArchiveConfigurationError,
        ArchiveActionInputError,
        ArchivePathError,
        ArchiveShareError,
        ArchiveFrictionError,
    ) as exception:
        failure = {
            "status": "failed",
            "code": exception.code,
            "message": exception.message,
        }
        details = getattr(exception, "details", None)
        if isinstance(details, Mapping):
            failure.update(details)
        blockers = getattr(exception, "blockers", ())
        if blockers:
            failure["blockers"] = list(blockers)
        relation_chain = getattr(exception, "relation_chain", ())
        if relation_chain:
            failure["relation_chain"] = list(relation_chain)
        if isinstance(exception, ArchiveHandoffError) or arguments.command == "prepare-handoff":
            failure["handoff_ready"] = False
            failure["next_action"] = {"action": "complete-upstream-input", "start_downstream": False}
        role = getattr(arguments, "role", None)
        operation = _operation_context(arguments)
        if arguments.command not in {"friction-note", "friction-summary"}:
            failure = _attach_friction_record(
                failure,
                getattr(arguments, "root", None),
                f"[{exception.code}] {exception.message}",
                operation=operation,
                code=exception.code,
                expected_guard=exception.code in EXPECTED_GUARD_CODES,
            )
        if (
            arguments.command == "context-summary"
            and role in ROLE_HANDOFF_ROLES
        ):
            reason = {"code": exception.code, "message": exception.message}
            if isinstance(details, Mapping):
                reason.update(details)
            failure["role_blocker"] = {
                "code": "ROLE_VIEW_BLOCKED",
                "role": role,
                "reasons": [reason],
                "next_action": {"action": "return-to-coordinator"},
            }
        failure = _with_archive_location(
            failure,
            arguments,
            resolved_project_root,
        )
        print(
            json.dumps(failure, ensure_ascii=False),
            file=sys.stderr,
        )
        return 1

    result = _with_archive_location(
        result,
        arguments,
        resolved_project_root,
    )
    if arguments.command in {"friction-note", "friction-summary"}:
        print(json.dumps(result, ensure_ascii=False))
        return exit_code
    root = getattr(arguments, "root", None)
    guard = _expected_guard(result) if exit_code == 0 else None
    operation = _operation_context(
        arguments,
        capture_request_input=guard is not None,
    )
    if isinstance(root, Path) and guard is not None:
        result = _attach_friction_record(
            result,
            root,
            f"[{guard['code']}] {guard['message']}",
            operation=operation,
            code=guard["code"],
            expected_guard=True,
        )
    elif isinstance(root, Path) and exit_code == 0 and result.get("status") != "awaiting_capture":
        closure = record_operation_success(
            root,
            feature_id=operation.feature_id,
            command=operation.command,
            target=operation.target,
            successful_operation="相同操作和目标已经成功完成。",
            context=operation.context,
        )
        if closure.get("status") == "recorded":
            result = {
                **result,
                "resolved_friction_ids": closure["resolved_incidents"],
                "friction_notes": [
                    friction_note_contract(operation.feature_id, incident_id=incident_id, context=operation.context)
                    for incident_id in closure["resolved_incidents"]
                    if operation.feature_id is not None
                ],
            }
        elif isinstance(closure.get("warning"), str):
            result = {**result, "friction_record_warning": closure["warning"]}
    print(json.dumps(result, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
