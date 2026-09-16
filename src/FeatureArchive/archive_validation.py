"""功能交付档案的结构校验、依赖图构建和派生索引生成。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Dict, Iterable, List, Mapping, Tuple

from archive_terminology import (
    ArchiveTerminologyError,
    TERMINOLOGY_RELATIVE_PATH,
    TERMINOLOGY_SCHEMA_VERSION,
    validate_archive_markdown,
)
from archive_profiles import (
    ARCHIVE_SCHEMA_VERSION,
    APPROVAL_STAGES,
    CONTEXT_CONTRACT_VERSION,
    STRICT_PROFILE,
    WORKFLOW_STATE_NAME,
    WORKFLOW_STATE_SCHEMA_VERSION,
)
from archive_slice_contract import (
    BREAKER_ALLOWED_ACTIONS,
    SliceContractError,
    digest as slice_contract_digest,
    normalize_task_contract_fields,
)
from archive_slice_plan import (
    SlicePlanError,
    digest as slice_plan_digest,
    evaluate_plan,
    normalize_plan,
)
from archive_root import ArchiveRootError, require_root_contract
from archive_configuration import ArchiveConfigurationError, validate_configuration


SCHEMA_VERSION = ARCHIVE_SCHEMA_VERSION
MACHINE_INDEX_NAME = "feature-archive-dependencies.json"
GLOBAL_INDEX_NAME = "feature-archives.md"

CATEGORY_DIRECTORIES: Mapping[str, str] = {
    "01-requirements": "requirements",
    "02-investigation": "investigation",
    "03-design": "design",
    "04-plan": "plan",
    "05-implementation": "implementation",
    "06-validation": "validation",
}
CATEGORY_RANK: Mapping[str, int] = {
    category: rank
    for rank, category in enumerate(CATEGORY_DIRECTORIES.values(), start=1)
}
CONTENT_STATUSES = {
    "draft",
    "confirmed",
    "stale",
    "completed",
    "blocked",
    "superseded",
}
LIFECYCLE_STATUSES = {
    "draft",
    "active",
    "validating",
    "frozen",
    "pending_cleanup",
}
LIFECYCLE_LABELS: Mapping[str, str] = {
    "draft": "草稿",
    "active": "活跃",
    "validating": "验证中",
    "frozen": "冻结",
    "pending_cleanup": "待清理",
}
VALIDATION_STATUS_VALUES: Mapping[str, Tuple[str, ...]] = {
    "conclusion": ("pending", "passed", "failed"),
    "unverified_boundaries": ("pending", "none", "documented"),
    "residual_risks": ("pending", "none", "documented"),
}
DOCUMENT_ID_PATTERN = re.compile(
    r"^[a-z0-9]+(?:-[a-z0-9]+)*(?:\.[a-z0-9]+(?:-[a-z0-9]+)*)+$"
)
FEATURE_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SEMANTIC_VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")
FINGERPRINT_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
FIELD_PATTERN = re.compile(r"^([a-z_]+):(?:\s*(.*))?$")


class ArchiveValidationError(Exception):
    """表示校验或索引重建过程中可操作的确定性错误。"""

    def __init__(
        self,
        code: str,
        message: str,
        location: Path | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.location = location


@dataclass(frozen=True)
class FeatureRecord:
    """校验通过的功能生命周期清单。"""

    feature_id: str
    title: str
    path: Path
    lifecycle: str
    created_at: str
    updated_at: str
    frozen_at: str | None
    retention_days: int
    retain_reason: str | None
    validation_statuses: Mapping[str, str]
    terminology_schema_version: int


@dataclass(frozen=True)
class DocumentRecord:
    """校验通过的文档元数据。"""

    document_id: str
    feature_id: str
    path: Path
    category: str
    content_status: str
    semantic_version: str
    content_fingerprint: str
    dependencies: Tuple[str, ...]
    dependency_versions: Mapping[str, str]
    related_documents: Tuple[str, ...]
    stale_from_status: str | None


@dataclass(frozen=True)
class ArchiveGraph:
    """从功能档案事实源重建出的完整依赖图。"""

    root: Path
    features: Mapping[str, FeatureRecord]
    documents: Mapping[str, DocumentRecord]
    dependents: Mapping[str, Tuple[str, ...]]


def _read_json_object(path: Path) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exception:
        raise ArchiveValidationError(
            "INVALID_MANIFEST",
            f"生命周期清单“{path}”不是可读取的 UTF-8 JSON：{exception}",
        ) from exception
    if not isinstance(value, dict):
        raise ArchiveValidationError(
            "INVALID_MANIFEST",
            f"生命周期清单“{path}”的根节点必须是 JSON 对象。",
        )
    return value


def _require_manifest_string(
    manifest: Mapping[str, object],
    path: Path,
    field: str,
    allow_empty: bool = False,
) -> str:
    value = manifest.get(field)
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ArchiveValidationError(
            "INVALID_MANIFEST",
            f"生命周期清单“{path}”的 {field} 必须是"
            f"{'字符串' if allow_empty else '非空字符串'}。",
        )
    return value


def _validate_timestamp(value: str, path: Path, field: str) -> None:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exception:
        raise ArchiveValidationError(
            "INVALID_MANIFEST",
            f"生命周期清单“{path}”的 {field} 不是合法 ISO 8601 时间：{value}",
        ) from exception


def _validate_categories(manifest: Mapping[str, object], path: Path) -> None:
    categories = manifest.get("categories")
    if not isinstance(categories, list) or len(categories) != len(CATEGORY_DIRECTORIES):
        raise ArchiveValidationError(
            "INVALID_MANIFEST",
            f"生命周期清单“{path}”必须声明完整的六个固定类别。",
        )

    expected = list(CATEGORY_DIRECTORIES.items())
    for index, category in enumerate(categories):
        if not isinstance(category, dict):
            raise ArchiveValidationError(
                "INVALID_MANIFEST",
                f"生命周期清单“{path}”的 categories[{index}] 必须是对象。",
            )
        expected_directory, expected_id = expected[index]
        if (
            category.get("order") != index + 1
            or category.get("id") != expected_id
            or category.get("directory") != expected_directory
        ):
            raise ArchiveValidationError(
                "INVALID_MANIFEST",
                f"生命周期清单“{path}”的第 {index + 1} 个类别必须是"
                f"“{expected_directory} / {expected_id}”。",
            )


def _validation_statuses(
    manifest: Mapping[str, object],
    path: Path,
) -> Mapping[str, str]:
    value = manifest.get("validation")
    if not isinstance(value, dict):
        raise ArchiveValidationError(
            "INVALID_MANIFEST",
            f"生命周期清单“{path}”的 validation 必须是对象。",
        )
    if set(value) != set(VALIDATION_STATUS_VALUES):
        raise ArchiveValidationError(
            "INVALID_MANIFEST",
            f"生命周期清单“{path}”的 validation 必须完整声明全部验证状态。",
        )

    statuses: Dict[str, str] = {}
    for field, allowed_values in VALIDATION_STATUS_VALUES.items():
        status = value[field]
        if not isinstance(status, str) or status not in allowed_values:
            raise ArchiveValidationError(
                "INVALID_MANIFEST",
                f"生命周期清单“{path}”的 validation.{field} 必须是："
                + "、".join(allowed_values),
            )
        statuses[field] = status
    return statuses


def _validate_complex_workflow_state(path: Path, feature_id: str) -> None:
    state_path = path / WORKFLOW_STATE_NAME
    try:
        value = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exception:
        raise ArchiveValidationError(
            "INVALID_WORKFLOW_STATE",
            f"复杂交付项“{feature_id}”缺少可读取的工作流状态：{exception}",
            state_path,
        ) from exception
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != WORKFLOW_STATE_SCHEMA_VERSION
        or value.get("workflow_profile") != STRICT_PROFILE
        or value.get("feature_id") != feature_id
        or not isinstance(value.get("approvals"), dict)
        or not isinstance(value.get("execution"), dict)
    ):
        raise ArchiveValidationError(
            "INVALID_WORKFLOW_STATE",
            f"复杂交付项“{feature_id}”的工作流状态结构或归属不合法。",
            state_path,
        )
    approvals = value["approvals"]
    execution = value["execution"]
    configuration = value.get("configuration")
    if configuration is not None:
        try:
            validate_configuration(configuration, path)
        except ArchiveConfigurationError as exception:
            raise ArchiveValidationError(
                "INVALID_WORKFLOW_STATE",
                f"复杂交付项“{feature_id}”的可选配置不合法：{exception}",
                state_path,
            ) from exception
    if (
        set(approvals) != set(APPROVAL_STAGES)
        or not isinstance(execution.get("slices"), dict)
        or not isinstance(execution.get("used_execution_ids"), list)
        or not isinstance(execution.get("slice_plan"), dict)
        or set(execution) != {"slices", "used_execution_ids", "slice_plan"}
    ):
        raise ArchiveValidationError(
            "INVALID_WORKFLOW_STATE",
            f"复杂交付项“{feature_id}”的工作流状态缺少必要字段。",
            state_path,
        )

    def invalid(message: str) -> None:
        raise ArchiveValidationError("INVALID_WORKFLOW_STATE", message, state_path)

    def nonempty_string(value: object) -> bool:
        return isinstance(value, str) and bool(value.strip())

    plan_state = execution["slice_plan"]
    if set(plan_state) != {"current_version", "history"} or not isinstance(
        plan_state.get("history"), list
    ):
        invalid(f"复杂交付项“{feature_id}”的切片方案状态不合法。")
    plan_history = plan_state["history"]
    plan_versions = []
    for item in plan_history:
        if (
            not isinstance(item, dict)
            or set(item) != {"version", "plan_digest", "approved_at", "plan"}
            or not isinstance(item.get("version"), int)
            or isinstance(item.get("version"), bool)
            or item.get("version", 0) <= 0
            or not nonempty_string(item.get("approved_at"))
            or not isinstance(item.get("plan"), dict)
        ):
            invalid(f"复杂交付项“{feature_id}”的切片方案历史不完整。")
        try:
            normalized_plan = normalize_plan(item["plan"], feature_id)
            evaluate_plan(normalized_plan, {})
        except SlicePlanError:
            invalid(f"复杂交付项“{feature_id}”的切片方案声明不合法。")
        if normalized_plan != item["plan"] or slice_plan_digest(normalized_plan) != item.get("plan_digest"):
            invalid(f"复杂交付项“{feature_id}”的切片方案摘要不一致。")
        plan_versions.append(item["version"])
    if plan_versions != sorted(set(plan_versions)):
        invalid(f"复杂交付项“{feature_id}”的切片方案版本历史不合法。")
    if plan_state.get("current_version") is None:
        if plan_history:
            invalid(f"复杂交付项“{feature_id}”的当前切片方案指针缺失。")
    elif (
        not plan_versions
        or plan_state.get("current_version") != plan_versions[-1]
    ):
        invalid(f"复杂交付项“{feature_id}”的当前切片方案指针不合法。")
    plan_history_by_version = {
        item["version"]: item for item in plan_history if isinstance(item, dict)
    }

    def valid_plan_binding(
        binding: object,
        package_id: str,
        *,
        history_reference: bool = False,
    ) -> bool:
        expected_fields = (
            {"plan_version", "plan_digest", "history_ref"}
            if history_reference
            else {"plan_version", "plan_digest", "approved_at", "plan"}
        )
        if not isinstance(binding, dict) or set(binding) != expected_fields:
            return False
        version = binding.get("plan_version")
        history_item = plan_history_by_version.get(version)
        if (
            not isinstance(history_item, dict)
            or binding.get("plan_digest") != history_item.get("plan_digest")
        ):
            return False
        if history_reference:
            if binding.get("history_ref") != "slice_plan.history":
                return False
        elif (
            binding.get("approved_at") != history_item.get("approved_at")
            or binding.get("plan") != history_item.get("plan")
        ):
            return False
        plan = history_item.get("plan")
        return isinstance(plan, dict) and package_id in {
            item.get("slice_id") for item in plan.get("slices", []) if isinstance(item, dict)
        }

    for stage in APPROVAL_STAGES:
        approval = approvals.get(stage)
        if approval is None:
            continue
        if (
            not isinstance(approval, dict)
            or approval.get("decision") not in {"approve", "reject"}
            or not isinstance(approval.get("snapshot"), dict)
            or not nonempty_string(approval.get("decided_at"))
        ):
            invalid(f"严格交付项“{feature_id}”的 {stage} 批准记录不完整。")
        if stage == "ui-baseline" and (
            not isinstance(configuration, dict) or configuration.get("id") != "dloop-ui-v1"
            or not nonempty_string(approval.get("reviewed_digest"))
            or not nonempty_string(approval.get("user_confirmation"))
        ):
            invalid(f"交付项“{feature_id}”的开工确认缺少展示版本或用户回复依据。")
        if stage == "final" and isinstance(configuration, dict) and configuration.get("id") == "dloop-ui-v1":
            if not nonempty_string(approval.get("reviewed_digest")) or not nonempty_string(approval.get("user_confirmation")):
                invalid(f"严格交付项“{feature_id}”的 UI 最终验收缺少展示版本或用户回复依据。")
        if stage == "final" and approval.get("decision") == "approve":
            if (
                isinstance(configuration, dict)
                and configuration.get("id") == "dloop-ui-v1"
                and not nonempty_string(approval.get("ui_interaction_digest"))
            ):
                invalid(f"严格交付项“{feature_id}”的最终批准缺少 UI 交互方案绑定。")
            candidate_summary = approval.get("candidate_summary")
            if (
                not isinstance(candidate_summary, dict)
                or set(candidate_summary) != {"items", "digest"}
                or not isinstance(candidate_summary.get("items"), list)
                or not candidate_summary.get("items")
                or not nonempty_string(candidate_summary.get("digest"))
            ):
                invalid(f"严格交付项“{feature_id}”的最终批准必须绑定至少一个已接受实现候选。")
            if not isinstance(approval.get("workspace_snapshot"), dict):
                invalid(f"严格交付项“{feature_id}”的最终批准缺少工作区绑定。")
            integration_confirmation = approval.get("integration_confirmation")
            if (
                not isinstance(integration_confirmation, dict)
                or set(integration_confirmation) != {"confirmation_path", "confirmation_digest"}
                or any(
                    not nonempty_string(integration_confirmation.get(field))
                    for field in ("confirmation_path", "confirmation_digest")
                )
            ):
                invalid(f"严格交付项“{feature_id}”的集成确认绑定不合法。")

    for stage in APPROVAL_STAGES:
        record = approvals[stage]
        if record is None:
            continue
        snapshot = record.get("snapshot")
        if stage == "ui-baseline":
            if set(snapshot) != {"baseline_digest"} or snapshot["baseline_digest"] != record.get("reviewed_digest"):
                invalid(f"交付项“{feature_id}”的开工确认快照不完整。")
        elif any(
            not nonempty_string(snapshot.get(field))
            for field in ("document_id", "semantic_version", "content_fingerprint")
        ):
            invalid(f"复杂交付项“{feature_id}”的确认快照不完整。")
        workspace_snapshot = record.get("workspace_snapshot")
        if workspace_snapshot is not None and (
            stage != "final"
            or not isinstance(workspace_snapshot, dict)
            or any(
                not nonempty_string(workspace_snapshot.get(field))
                for field in ("workspace_root", "source", "digest")
            )
            or not isinstance(workspace_snapshot.get("entries"), dict)
        ):
            invalid(f"复杂交付项“{feature_id}”的最终工作区摘要不合法。")

    slices = execution["slices"]
    used_execution_ids = execution["used_execution_ids"]
    if (
        any(not nonempty_string(item) for item in used_execution_ids)
        or len(used_execution_ids) != len(set(used_execution_ids))
    ):
        invalid(f"复杂交付项“{feature_id}”的历史执行标识不合法。")
    known_execution_ids = set(used_execution_ids)
    current_execution_ids = set()
    for package_id, record in slices.items():
        if not nonempty_string(package_id) or not isinstance(record, dict):
            invalid(f"复杂交付项“{feature_id}”包含不合法的切片记录。")
        status = record.get("status")
        if status not in {
            "active", "candidate", "accepted", "review_failed",
            "failed", "interrupted", "abandoned",
            "contract_draft", "contract_failed", "ready", "circuit_open",
        }:
            invalid(f"复杂交付项“{feature_id}”包含未知的切片状态。")
        execution_id = record.get("execution_id")
        abandoned_before_execution = status == "abandoned" and execution_id is None
        if status in {"contract_draft", "contract_failed", "ready"} or abandoned_before_execution:
            if execution_id is not None:
                invalid(f"复杂交付项“{feature_id}”的实施前切片不能持有执行标识。")
        else:
            if not nonempty_string(execution_id) or execution_id in current_execution_ids:
                invalid(f"复杂交付项“{feature_id}”包含缺失或重复的执行标识。")
            if execution_id not in known_execution_ids:
                invalid(f"复杂交付项“{feature_id}”的当前执行标识没有历史登记。")
            current_execution_ids.add(execution_id)
            if not valid_plan_binding(record.get("slice_plan_binding"), package_id):
                invalid(f"严格交付项“{feature_id}”的切片启动未绑定可复核方案历史。")
        package = record.get("package")
        required_package_fields = {
            "package_id",
            "goal",
            "write_scope",
            "non_goals",
            "acceptance_conditions",
            "validations",
            "rollback",
            "context_contract_version",
            "context_materials",
        }
        contract_package_fields = required_package_fields | {"slice_contract", "contract_check"}
        if isinstance(package, dict) and "delivery_requirements" in package:
            contract_package_fields.add("delivery_requirements")
        if (
            not isinstance(package, dict)
            or set(package) != contract_package_fields
            or package.get("package_id") != package_id
            or not nonempty_string(package.get("goal"))
            or not nonempty_string(package.get("rollback"))
            or not isinstance(package.get("write_scope"), list)
            or not package.get("write_scope")
            or any(not nonempty_string(item) for item in package.get("write_scope", []))
            or not isinstance(package.get("non_goals"), list)
            or any(not nonempty_string(item) for item in package.get("non_goals", []))
            or not isinstance(package.get("acceptance_conditions"), list)
            or not package.get("acceptance_conditions")
            or any(
                not nonempty_string(item)
                for item in package.get("acceptance_conditions", [])
            )
            or not isinstance(package.get("validations"), list)
            or not package.get("validations")
            or any(not nonempty_string(item) for item in package.get("validations", []))
            or package.get("context_contract_version") != CONTEXT_CONTRACT_VERSION
        ):
            invalid(f"严格交付项“{feature_id}”的切片任务包不完整或版本不受支持。")
        if "delivery_requirements" in package:
            from archive_delivery_materials import normalize_requirements
            from archive_handoff import ArchiveHandoffError
            try:
                normalize_requirements(package["delivery_requirements"], package["acceptance_conditions"], complete=True)
            except ArchiveHandoffError as exception:
                invalid(f"交付材料要求不完整：{exception.message}")
        try:
            normalize_task_contract_fields(package)
        except SliceContractError as exception:
            invalid(f"严格交付项“{feature_id}”的切片契约不合法：{exception.message}")
        history = record.get("contract_history")
        checkpoints = record.get("checkpoints")
        reports = record.get("breaker_reports")
        review_snapshots = record.get("review_snapshots")
        if (
            not isinstance(history, list) or not history
            or not isinstance(checkpoints, list)
            or not isinstance(reports, list)
            or not isinstance(review_snapshots, list)
        ):
            invalid(f"严格交付项“{feature_id}”的契约、检查点或熔断历史不完整。")
        versions = [item.get("version") for item in history if isinstance(item, dict)]
        if (
            len(versions) != len(history)
            or any(not isinstance(item, int) or isinstance(item, bool) or item <= 0 for item in versions)
            or versions != sorted(set(versions))
            or versions[-1] != package["slice_contract"]["version"]
        ):
            invalid(f"严格交付项“{feature_id}”的契约版本历史不合法。")
        latest_history = history[-1]
        current_contract_digest = slice_contract_digest(package["slice_contract"])
        if (
            set(latest_history) != {
                "version", "revision_summary", "contract_digest", "package_digest",
                "recorded_at", "contract",
            }
            or latest_history.get("contract") != package["slice_contract"]
            or latest_history.get("contract_digest") != current_contract_digest
        ):
            invalid(f"严格交付项“{feature_id}”的当前契约与版本历史不一致。")
        check = record.get("contract_check")
        if status == "contract_draft" and check is not None:
            invalid(f"严格交付项“{feature_id}”的草拟契约不应伪造检查结论。")
        if status != "contract_draft" and not (abandoned_before_execution and check is None) and (
            not isinstance(check, dict) or check.get("status") not in {"passed", "failed"}
        ):
            invalid(f"严格交付项“{feature_id}”缺少可审查的契约检查结果。")
        if status == "contract_failed" and isinstance(check, dict) and check.get("status") != "failed":
            invalid(f"严格交付项“{feature_id}”的状态与契约检查结论不一致。")
        if status not in {"contract_draft", "contract_failed"} and not abandoned_before_execution and isinstance(check, dict) and check.get("status") != "passed":
            invalid(f"严格交付项“{feature_id}”未通过契约检查却进入后续流程。")
        if isinstance(check, dict) and (
            check.get("contract_version") != package["slice_contract"]["version"]
            or check.get("contract_digest") != current_contract_digest
            or check.get("check_digest")
            != slice_contract_digest({key: value for key, value in check.items() if key != "check_digest"})
        ):
            invalid(f"严格交付项“{feature_id}”的契约检查未绑定当前契约版本。")
        history_by_version = {
            item["version"]: item for item in history if isinstance(item, dict)
        }
        checkpoint_counts: Dict[str, int] = {}
        for checkpoint in checkpoints:
            if not isinstance(checkpoint, dict):
                invalid(f"严格交付项“{feature_id}”包含不合法的实施检查点。")
            checkpoint_execution_id = checkpoint.get("execution_id")
            if (
                not nonempty_string(checkpoint_execution_id)
                or checkpoint_execution_id not in known_execution_ids
            ):
                invalid(f"严格交付项“{feature_id}”的实施检查点未绑定有效执行身份。")
            checkpoint_ordinal = checkpoint_counts.get(checkpoint_execution_id, 0) + 1
            checkpoint_counts[checkpoint_execution_id] = checkpoint_ordinal
            if checkpoint.get("checkpoint_id") != (
                f"{checkpoint_execution_id}-checkpoint-{checkpoint_ordinal}"
            ):
                invalid(f"严格交付项“{feature_id}”的实施检查点标识顺序不合法。")
            checkpoint_digest = checkpoint.get("checkpoint_digest")
            if checkpoint_digest != slice_contract_digest({
                key: item for key, item in checkpoint.items() if key != "checkpoint_digest"
            }):
                invalid(f"严格交付项“{feature_id}”的实施检查点摘要不一致。")
            checkpoint_contract = history_by_version.get(checkpoint.get("contract_version"))
            validations = checkpoint.get("validation_results")
            triggered = checkpoint.get("triggered_rules")
            if (
                not isinstance(checkpoint_contract, dict)
                or checkpoint.get("contract_digest") != checkpoint_contract.get("contract_digest")
                or not isinstance(validations, list)
                or any(
                    not isinstance(item, dict)
                    or set(item) != {"method", "status", "evidence"}
                    or item.get("status") not in {"passed", "failed", "unverified"}
                    or not nonempty_string(item.get("method"))
                    or not isinstance(item.get("evidence"), list)
                    or not item.get("evidence")
                    or any(not nonempty_string(evidence) for evidence in item.get("evidence", []))
                    for item in validations
                )
                or not isinstance(triggered, list)
                or checkpoint.get("boundary_crossed") is not bool(triggered)
                or checkpoint.get("boundary_rules")
                != [item.get("rule") for item in triggered if isinstance(item, dict)]
                or checkpoint.get("result") != ("breaker" if triggered else "continue")
            ):
                invalid(f"严格交付项“{feature_id}”的实施检查点事实不一致。")
            contract_value = checkpoint_contract.get("contract")
            expected_methods = (
                contract_value.get("validation_methods") if isinstance(contract_value, dict) else None
            )
            if (
                not isinstance(expected_methods, list)
                or any(item["method"] not in expected_methods for item in validations)
                or [item["method"] for item in validations]
                != [method for method in expected_methods if method in {
                    item["method"] for item in validations
                }]
                or checkpoint.get("validation_set_digest")
                != slice_contract_digest([item["method"] for item in validations])
            ):
                invalid(f"严格交付项“{feature_id}”的检查点包含未登记或乱序验证。")
        reports_by_id = {}
        for report in reports:
            if (
                not isinstance(report, dict)
                or not nonempty_string(report.get("report_id"))
                or report.get("allowed_actions") != [
                    dict(item) for item in BREAKER_ALLOWED_ACTIONS
                ]
                or report.get("report_digest")
                != slice_contract_digest({
                    key: item for key, item in report.items() if key != "report_digest"
                })
                or report["report_id"] in reports_by_id
            ):
                invalid(f"严格交付项“{feature_id}”的熔断报告摘要或标识不合法。")
            reports_by_id[report["report_id"]] = report
        if status == "circuit_open" and not reports:
            invalid(f"严格交付项“{feature_id}”的熔断状态缺少异常报告。")
        materials = package.get("context_materials")
        if not isinstance(materials, list):
            invalid(f"严格交付项“{feature_id}”的切片材料合同不合法。")
        for material in materials:
            if not isinstance(material, dict) or any(
                not nonempty_string(material.get(field))
                for field in ("source", "purpose", "mode")
            ):
                invalid(f"严格交付项“{feature_id}”的结构化切片材料不完整。")
            mode = material.get("mode")
            sections = material.get("sections")
            if mode == "sections":
                if set(material) != {"source", "purpose", "mode", "sections"}:
                    invalid(f"严格交付项“{feature_id}”的章节材料字段不合法。")
                if (
                    not isinstance(sections, list)
                    or not sections
                    or any(not nonempty_string(item) for item in sections)
                    or len(sections) != len(set(sections))
                ):
                    invalid(f"严格交付项“{feature_id}”的章节材料不合法。")
            elif mode == "full":
                if set(material) != {"source", "purpose", "mode"}:
                    invalid(f"严格交付项“{feature_id}”的整份材料字段不合法。")
            else:
                invalid(f"严格交付项“{feature_id}”的材料读取模式不合法。")
        snapshot_ids = set()
        for index, snapshot in enumerate(review_snapshots, start=1):
            if (
                not isinstance(snapshot, dict)
                or set(snapshot) != {
                    "snapshot_id", "candidate", "review", "recorded_at", "snapshot_digest"
                }
                or snapshot.get("snapshot_id") != f"{package_id}-review-{index}"
                or snapshot.get("snapshot_id") in snapshot_ids
                or not nonempty_string(snapshot.get("recorded_at"))
                or snapshot.get("snapshot_digest")
                != slice_plan_digest({
                    key: item for key, item in snapshot.items() if key != "snapshot_digest"
                })
            ):
                invalid(f"复杂交付项“{feature_id}”的切片“{package_id}”评审快照不合法。")
            snapshot_ids.add(snapshot["snapshot_id"])
            snapshot_candidate = snapshot.get("candidate")
            snapshot_review = snapshot.get("review")
            if not isinstance(snapshot_candidate, dict) or any(
                not nonempty_string(snapshot_candidate.get(field))
                for field in (
                    "candidate_id",
                    "candidate_digest",
                    "workspace_digest",
                    "workspace_guard_digest",
                    "workspace_root",
                    "implementation_execution_id",
                )
            ):
                invalid(f"复杂交付项“{feature_id}”的评审快照候选不完整。")
            if (
                snapshot_candidate.get("candidate_digest")
                != slice_plan_digest({
                    key: item
                    for key, item in snapshot_candidate.items()
                    if key != "candidate_digest"
                })
                or not valid_plan_binding(
                    snapshot_candidate.get("slice_plan_binding"),
                    package_id,
                    history_reference=True,
                )
                or snapshot_candidate.get("implementation_execution_id")
                not in known_execution_ids
            ):
                invalid(f"复杂交付项“{feature_id}”的评审快照候选绑定不合法。")
            if (
                not isinstance(snapshot_review, dict)
                or set(snapshot_review) != {
                    "candidate_id",
                    "candidate_digest",
                    "review_execution_id",
                    "result",
                    "issues",
                    "verification",
                    "verification_not_applicable_reason",
                    "reviewed_at",
                    "acceptance_scope_digest",
                }
                or snapshot_review.get("candidate_id")
                != snapshot_candidate.get("candidate_id")
                or snapshot_review.get("candidate_digest")
                != snapshot_candidate.get("candidate_digest")
                or snapshot_review.get("result") not in {"passed", "failed", "unverified"}
                or not nonempty_string(snapshot_review.get("review_execution_id"))
                or not isinstance(snapshot_review.get("issues"), list)
                or any(not nonempty_string(item) for item in snapshot_review.get("issues", []))
                or not isinstance(snapshot_review.get("verification"), list)
                or any(
                    not nonempty_string(item)
                    for item in snapshot_review.get("verification", [])
                )
                or (
                    snapshot_review.get("verification_not_applicable_reason") is not None
                    and not nonempty_string(
                        snapshot_review.get("verification_not_applicable_reason")
                    )
                )
                or (
                    bool(snapshot_review.get("verification"))
                    and snapshot_review.get("verification_not_applicable_reason") is not None
                )
                or (
                    snapshot_review.get("result") != "passed"
                    and snapshot_review.get("verification_not_applicable_reason") is not None
                )
                or (
                    snapshot_review.get("result") != "passed"
                    and not snapshot_review.get("issues")
                )
            ):
                invalid(f"复杂交付项“{feature_id}”的评审快照结论不完整。")
            review_execution_id = snapshot_review["review_execution_id"]
            if (
                review_execution_id in current_execution_ids
                or review_execution_id not in known_execution_ids
                or review_execution_id
                == snapshot_candidate.get("implementation_execution_id")
            ):
                invalid(f"复杂交付项“{feature_id}”包含不合法的评审执行标识。")
            current_execution_ids.add(review_execution_id)

        if "review" in record:
            invalid(f"复杂交付项“{feature_id}”不能在评审快照之外保存当前评审副本。")
        candidate = record.get("candidate")
        if status == "candidate":
            if not isinstance(candidate, dict) or any(
                not nonempty_string(candidate.get(field))
                for field in (
                    "candidate_id",
                    "candidate_digest",
                    "workspace_digest",
                    "workspace_guard_digest",
                    "workspace_root",
                )
            ):
                invalid(f"复杂交付项“{feature_id}”的候选记录不完整。")
            if not valid_plan_binding(
                candidate.get("slice_plan_binding"),
                package_id,
                history_reference=True,
            ):
                invalid(f"复杂交付项“{feature_id}”的候选未绑定可复核方案历史。")
            if candidate.get("candidate_digest") != slice_plan_digest({
                key: item for key, item in candidate.items() if key != "candidate_digest"
            }):
                invalid(f"复杂交付项“{feature_id}”的固定候选摘要不一致。")
        if status in {"accepted", "review_failed"}:
            latest_snapshot = review_snapshots[-1] if review_snapshots else None
            if (
                not isinstance(latest_snapshot, dict)
                or candidate is not None
            ):
                invalid(f"复杂交付项“{feature_id}”的已评审状态必须只绑定最新评审快照。")
            review = latest_snapshot.get("review")
            result = review.get("result")
            if (status == "accepted" and result != "passed") or (
                status == "review_failed" and result not in {"failed", "unverified"}
            ):
                invalid(f"复杂交付项“{feature_id}”的切片状态与验收结论不一致。")


def _load_feature(path: Path) -> FeatureRecord:
    manifest_path = path / "feature.json"
    manifest = _read_json_object(manifest_path)
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ArchiveValidationError(
            "INVALID_MANIFEST",
            f"生命周期清单“{manifest_path}”的 schema_version 必须为"
            f" {SCHEMA_VERSION}。",
        )
    terminology_schema_version = manifest.get("terminology_schema_version")
    if (
        not isinstance(terminology_schema_version, int)
        or isinstance(terminology_schema_version, bool)
        or terminology_schema_version != TERMINOLOGY_SCHEMA_VERSION
    ):
        raise ArchiveValidationError(
            "INVALID_MANIFEST",
            f"生命周期清单“{manifest_path}”的 terminology_schema_version "
            f"必须为 {TERMINOLOGY_SCHEMA_VERSION}。",
        )

    workflow_profile = manifest.get("workflow_profile")
    if workflow_profile != STRICT_PROFILE:
        raise ArchiveValidationError(
            "INVALID_MANIFEST",
            f"生命周期清单“{manifest_path}”的 workflow_profile 必须为"
            f" {STRICT_PROFILE}。",
        )

    feature_id = _require_manifest_string(manifest, manifest_path, "feature_id")
    if not FEATURE_ID_PATTERN.fullmatch(feature_id):
        raise ArchiveValidationError(
            "INVALID_MANIFEST",
            f"生命周期清单“{manifest_path}”的 feature_id 不合法：{feature_id}",
        )
    if path.name != feature_id:
        raise ArchiveValidationError(
            "FEATURE_ID_MISMATCH",
            f"功能目录“{path.name}”与清单 feature_id“{feature_id}”不一致。",
        )

    title = _require_manifest_string(manifest, manifest_path, "title")
    lifecycle = _require_manifest_string(manifest, manifest_path, "lifecycle")
    if lifecycle not in LIFECYCLE_STATUSES:
        raise ArchiveValidationError(
            "INVALID_MANIFEST",
            f"生命周期清单“{manifest_path}”包含未知 lifecycle：{lifecycle}",
        )

    created_at = _require_manifest_string(manifest, manifest_path, "created_at")
    updated_at = _require_manifest_string(manifest, manifest_path, "updated_at")
    _validate_timestamp(created_at, manifest_path, "created_at")
    _validate_timestamp(updated_at, manifest_path, "updated_at")

    frozen_value = manifest.get("frozen_at")
    if frozen_value is not None and not isinstance(frozen_value, str):
        raise ArchiveValidationError(
            "INVALID_MANIFEST",
            f"生命周期清单“{manifest_path}”的 frozen_at 必须是字符串或 null。",
        )
    if isinstance(frozen_value, str):
        _validate_timestamp(frozen_value, manifest_path, "frozen_at")
    if lifecycle in {"frozen", "pending_cleanup"} and frozen_value is None:
        raise ArchiveValidationError(
            "INVALID_MANIFEST",
            f"生命周期清单“{manifest_path}”处于 {lifecycle} 时必须记录 frozen_at。",
        )
    if lifecycle not in {"frozen", "pending_cleanup"} and frozen_value is not None:
        raise ArchiveValidationError(
            "INVALID_MANIFEST",
            f"生命周期清单“{manifest_path}”处于 {lifecycle} 时 frozen_at 必须为 null。",
        )

    retention_days = manifest.get("retention_days")
    if (
        not isinstance(retention_days, int)
        or isinstance(retention_days, bool)
        or retention_days <= 0
    ):
        raise ArchiveValidationError(
            "INVALID_MANIFEST",
            f"生命周期清单“{manifest_path}”的 retention_days 必须是正整数。",
        )
    retain_reason = manifest.get("retain_reason")
    if retain_reason is not None and (
        not isinstance(retain_reason, str) or not retain_reason.strip()
    ):
        raise ArchiveValidationError(
            "INVALID_MANIFEST",
            f"生命周期清单“{manifest_path}”的 retain_reason "
            "必须是非空字符串或 null。",
        )

    _validate_categories(manifest, manifest_path)
    required_paths = [path / "README.md"]
    for directory in CATEGORY_DIRECTORIES:
        category_path = path / directory
        if not category_path.is_dir():
            raise ArchiveValidationError(
                "INVALID_ARCHIVE_STRUCTURE",
                f"功能档案“{path}”缺少固定类别目录“{directory}”。",
                category_path,
            )
        required_paths.append(category_path / "README.md")
    required_paths.append(path / TERMINOLOGY_RELATIVE_PATH)
    for required_path in required_paths:
        if not required_path.is_file():
            raise ArchiveValidationError(
                "INVALID_ARCHIVE_STRUCTURE",
                f"功能档案“{path}”缺少必需文档“"
                f"{required_path.relative_to(path).as_posix()}”。",
                required_path,
            )
    _validate_complex_workflow_state(path, feature_id)
    return FeatureRecord(
        feature_id=feature_id,
        title=title,
        path=path,
        lifecycle=lifecycle,
        created_at=created_at,
        updated_at=updated_at,
        frozen_at=frozen_value,
        retention_days=retention_days,
        retain_reason=retain_reason,
        validation_statuses=_validation_statuses(manifest, manifest_path),
        terminology_schema_version=terminology_schema_version,
    )


def _strip_yaml_scalar(value: str, path: Path, field: str) -> str:
    normalized = value.strip()
    if len(normalized) >= 2 and normalized[0] == normalized[-1] == '"':
        try:
            decoded = json.loads(normalized)
        except json.JSONDecodeError as exception:
            raise ArchiveValidationError(
                "INVALID_DOCUMENT",
                f"文档“{path}”的 {field} 包含无效双引号字符串。",
            ) from exception
        if not isinstance(decoded, str):
            raise ArchiveValidationError(
                "INVALID_DOCUMENT",
                f"文档“{path}”的 {field} 必须是字符串。",
            )
        return decoded
    if len(normalized) >= 2 and normalized[0] == normalized[-1] == "'":
        return normalized[1:-1].replace("''", "'")
    return normalized


def _parse_inline_list(value: str, path: Path, field: str) -> List[str]:
    normalized = value.strip()
    if not (normalized.startswith("[") and normalized.endswith("]")):
        raise ArchiveValidationError(
            "INVALID_DOCUMENT",
            f"文档“{path}”的 {field} 必须使用 YAML 列表。",
        )
    inner = normalized[1:-1].strip()
    if not inner:
        return []

    items = []
    current = []
    quote = ""
    escaped = False
    for character in inner:
        if escaped:
            current.append(character)
            escaped = False
            continue
        if character == "\\" and quote == '"':
            current.append(character)
            escaped = True
            continue
        if quote:
            current.append(character)
            if character == quote:
                quote = ""
            continue
        if character in ("'", '"'):
            quote = character
            current.append(character)
            continue
        if character == ",":
            items.append(_strip_yaml_scalar("".join(current), path, field))
            current = []
            continue
        current.append(character)
    if quote:
        raise ArchiveValidationError(
            "INVALID_DOCUMENT",
            f"文档“{path}”的 {field} 包含未闭合引号。",
        )
    items.append(_strip_yaml_scalar("".join(current), path, field))
    if any(not item for item in items):
        raise ArchiveValidationError(
            "INVALID_DOCUMENT",
            f"文档“{path}”的 {field} 包含空列表项。",
        )
    return items


def _parse_front_matter(path: Path) -> Mapping[str, object]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exception:
        raise ArchiveValidationError(
            "UNREADABLE_DOCUMENT",
            f"无法读取文档“{path}”：{exception}",
            path,
        ) from exception
    if not lines or lines[0] != "---":
        raise ArchiveValidationError(
            "INVALID_DOCUMENT",
            f"功能档案文档“{path}”缺少 YAML 元数据起始标记“---”。",
        )

    fields: Dict[str, object] = {}
    index = 1
    while index < len(lines) and lines[index] != "---":
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        match = FIELD_PATTERN.fullmatch(line)
        if match is None:
            raise ArchiveValidationError(
                "INVALID_DOCUMENT",
                f"文档“{path}”的元数据行格式不合法：{line}",
            )
        field = match.group(1)
        if field in fields:
            raise ArchiveValidationError(
                "INVALID_DOCUMENT",
                f"文档“{path}”重复声明元数据字段 {field}。",
            )
        raw_value = (match.group(2) or "").strip()
        if field in ("dependencies", "related_documents"):
            if raw_value:
                fields[field] = _parse_inline_list(raw_value, path, field)
            else:
                values = []
                index += 1
                while index < len(lines) and lines[index].startswith(("  - ", "- ")):
                    item_line = lines[index].lstrip()
                    values.append(
                        _strip_yaml_scalar(item_line[2:], path, field)
                    )
                    index += 1
                if any(not item for item in values):
                    raise ArchiveValidationError(
                        "INVALID_DOCUMENT",
                        f"文档“{path}”的 {field} 包含空列表项。",
                    )
                fields[field] = values
                continue
        elif field == "dependency_versions":
            if not raw_value:
                raise ArchiveValidationError(
                    "INVALID_DOCUMENT",
                    f"文档“{path}”的 dependency_versions 必须是行内 JSON 对象。",
                )
            try:
                value = json.loads(raw_value)
            except json.JSONDecodeError as exception:
                raise ArchiveValidationError(
                    "INVALID_DOCUMENT",
                    f"文档“{path}”的 dependency_versions 不是合法行内 JSON 对象。",
                ) from exception
            if not isinstance(value, dict):
                raise ArchiveValidationError(
                    "INVALID_DOCUMENT",
                    f"文档“{path}”的 dependency_versions 必须是对象。",
                )
            fields[field] = value
        else:
            if not raw_value:
                raise ArchiveValidationError(
                    "INVALID_DOCUMENT",
                    f"文档“{path}”的元数据字段 {field} 不能为空。",
                )
            fields[field] = _strip_yaml_scalar(raw_value, path, field)
        index += 1

    if index >= len(lines):
        raise ArchiveValidationError(
            "INVALID_DOCUMENT",
            f"文档“{path}”缺少 YAML 元数据结束标记“---”。",
        )
    return fields


def _require_document_string(
    fields: Mapping[str, object],
    path: Path,
    field: str,
) -> str:
    value = fields.get(field)
    if not isinstance(value, str) or not value:
        raise ArchiveValidationError(
            "INVALID_DOCUMENT",
            f"文档“{path}”的 {field} 必须是非空字符串。",
        )
    return value


def _require_document_list(
    fields: Mapping[str, object],
    path: Path,
    field: str,
) -> Tuple[str, ...]:
    value = fields.get(field)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ArchiveValidationError(
            "INVALID_DOCUMENT",
            f"文档“{path}”的 {field} 必须是字符串列表。",
        )
    normalized = tuple(value)
    if len(normalized) != len(set(normalized)):
        raise ArchiveValidationError(
            "INVALID_DOCUMENT",
            f"文档“{path}”的 {field} 包含重复项。",
        )
    for document_id in normalized:
        if not DOCUMENT_ID_PATTERN.fullmatch(document_id):
            raise ArchiveValidationError(
                "INVALID_DOCUMENT",
                f"文档“{path}”的 {field} 包含非法文档标识：{document_id}",
            )
    return normalized


def _dependency_versions(
    fields: Mapping[str, object],
    path: Path,
    dependencies: Tuple[str, ...],
) -> Mapping[str, str]:
    value = fields.get("dependency_versions")
    if not isinstance(value, dict):
        raise ArchiveValidationError(
            "INVALID_DOCUMENT",
            f"文档“{path}”的 dependency_versions 必须是对象。",
        )

    normalized: Dict[str, str] = {}
    for document_id, version in value.items():
        if not isinstance(document_id, str) or not DOCUMENT_ID_PATTERN.fullmatch(
            document_id
        ):
            raise ArchiveValidationError(
                "INVALID_DOCUMENT",
                f"文档“{path}”的 dependency_versions 包含非法文档标识："
                f"{document_id}",
            )
        if document_id not in dependencies:
            raise ArchiveValidationError(
                "INVALID_DOCUMENT",
                f"文档“{path}”记录了未声明依赖“{document_id}”的消费版本。",
            )
        if not isinstance(version, str) or not SEMANTIC_VERSION_PATTERN.fullmatch(
            version
        ):
            raise ArchiveValidationError(
                "INVALID_DOCUMENT",
                f"文档“{path}”记录的依赖“{document_id}”版本必须是 x.y.z。",
            )
        normalized[document_id] = version
    return normalized


def _stale_from_status(
    fields: Mapping[str, object],
    path: Path,
    content_status: str,
) -> str | None:
    value = fields.get("stale_from_status")
    if value is None:
        if content_status == "stale":
            raise ArchiveValidationError(
                "INVALID_DOCUMENT",
                f"文档“{path}”处于 stale 时必须声明 stale_from_status。",
            )
        return None
    if (
        not isinstance(value, str)
        or value not in CONTENT_STATUSES
        or value == "stale"
    ):
        raise ArchiveValidationError(
            "INVALID_DOCUMENT",
            f"文档“{path}”的 stale_from_status 必须是非过期内容状态。",
        )
    if content_status != "stale":
        raise ArchiveValidationError(
            "INVALID_DOCUMENT",
            f"文档“{path}”仅在 content_status 为 stale 时才能声明"
            " stale_from_status。",
        )
    return value


def _category_from_path(feature: FeatureRecord, path: Path, declared: str) -> str:
    relative_path = path.relative_to(feature.path)
    if relative_path == Path("README.md"):
        if declared != "archive":
            raise ArchiveValidationError(
                "INVALID_DOCUMENT_CATEGORY",
                f"功能入口“{path}”的 category 必须是 archive。",
            )
        return declared

    if len(relative_path.parts) < 2:
        raise ArchiveValidationError(
            "INVALID_DOCUMENT_CATEGORY",
            f"文档“{path}”必须位于一个固定类别目录中。",
        )
    expected = CATEGORY_DIRECTORIES.get(relative_path.parts[0])
    if expected is None:
        raise ArchiveValidationError(
            "INVALID_DOCUMENT_CATEGORY",
            f"文档“{path}”位于未知类别目录“{relative_path.parts[0]}”。",
        )
    if declared != expected:
        raise ArchiveValidationError(
            "INVALID_DOCUMENT_CATEGORY",
            f"文档“{path}”声明 category“{declared}”，但所在目录要求"
            f"“{expected}”。",
        )
    return declared


def _load_document(feature: FeatureRecord, path: Path) -> DocumentRecord:
    fields = _parse_front_matter(path)
    document_id = _require_document_string(fields, path, "document_id")
    if not DOCUMENT_ID_PATTERN.fullmatch(document_id):
        raise ArchiveValidationError(
            "INVALID_DOCUMENT",
            f"文档“{path}”的 document_id 不合法：{document_id}",
        )
    if document_id.split(".", 1)[0] != feature.feature_id:
        raise ArchiveValidationError(
            "DOCUMENT_FEATURE_MISMATCH",
            f"文档“{path}”的 document_id 不属于功能“{feature.feature_id}”。",
        )

    category = _category_from_path(
        feature,
        path,
        _require_document_string(fields, path, "category"),
    )
    content_status = _require_document_string(fields, path, "content_status")
    if content_status not in CONTENT_STATUSES:
        raise ArchiveValidationError(
            "INVALID_DOCUMENT",
            f"文档“{path}”包含未知 content_status：{content_status}",
        )
    semantic_version = _require_document_string(fields, path, "semantic_version")
    if not SEMANTIC_VERSION_PATTERN.fullmatch(semantic_version):
        raise ArchiveValidationError(
            "INVALID_DOCUMENT",
            f"文档“{path}”的 semantic_version 必须是 x.y.z："
            f"{semantic_version}",
        )
    content_fingerprint = _require_document_string(
        fields, path, "content_fingerprint"
    )
    if not FINGERPRINT_PATTERN.fullmatch(content_fingerprint):
        raise ArchiveValidationError(
            "INVALID_DOCUMENT",
            f"文档“{path}”的 content_fingerprint 必须是 sha256 加 64 位"
            "小写十六进制摘要。",
        )

    dependencies = _require_document_list(fields, path, "dependencies")
    return DocumentRecord(
        document_id=document_id,
        feature_id=feature.feature_id,
        path=path,
        category=category,
        content_status=content_status,
        semantic_version=semantic_version,
        content_fingerprint=content_fingerprint,
        dependencies=dependencies,
        dependency_versions=_dependency_versions(fields, path, dependencies),
        related_documents=_require_document_list(fields, path, "related_documents"),
        stale_from_status=_stale_from_status(fields, path, content_status),
    )


def _discover_features(root: Path) -> Mapping[str, FeatureRecord]:
    features: Dict[str, FeatureRecord] = {}
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        if not path.is_dir():
            continue
        manifest_path = path / "feature.json"
        looks_like_archive = any(
            (path / directory).exists() for directory in CATEGORY_DIRECTORIES
        )
        if not manifest_path.exists():
            if looks_like_archive:
                raise ArchiveValidationError(
                    "MISSING_MANIFEST",
                    f"疑似功能档案“{path}”缺少 feature.json。",
                )
            continue
        if not manifest_path.is_file():
            raise ArchiveValidationError(
                "INVALID_MANIFEST",
                f"生命周期清单路径“{manifest_path}”不是文件。",
            )

        feature = _load_feature(path)
        if feature.feature_id in features:
            previous = features[feature.feature_id]
            raise ArchiveValidationError(
                "DUPLICATE_FEATURE_ID",
                f"功能标识“{feature.feature_id}”同时出现在“{previous.path}”和"
                f"“{feature.path}”。",
            )
        features[feature.feature_id] = feature
    return features


def _load_documents(
    features: Mapping[str, FeatureRecord],
) -> Mapping[str, DocumentRecord]:
    documents: Dict[str, DocumentRecord] = {}
    for feature in features.values():
        for path in sorted(feature.path.rglob("*.md")):
            if not path.is_file():
                continue
            document = _load_document(feature, path)
            previous = documents.get(document.document_id)
            if previous is not None:
                raise ArchiveValidationError(
                    "DUPLICATE_DOCUMENT_ID",
                    f"文档标识“{document.document_id}”同时出现在"
                    f"“{previous.path}”和“{document.path}”。",
                )
            documents[document.document_id] = document
    return documents


def _validate_dependencies(
    documents: Mapping[str, DocumentRecord],
) -> Mapping[str, Tuple[str, ...]]:
    dependents: Dict[str, List[str]] = {
        document_id: [] for document_id in documents
    }
    for document_id in sorted(documents):
        document = documents[document_id]
        for dependency_id in document.dependencies:
            if dependency_id == document.document_id:
                raise ArchiveValidationError(
                    "SELF_DEPENDENCY",
                    f"文档“{document.document_id}”不能依赖自身。",
                )
            dependency_feature_id = dependency_id.split(".", 1)[0]
            if dependency_feature_id != document.feature_id:
                raise ArchiveValidationError(
                    "CROSS_FEATURE_DEPENDENCY",
                    f"文档“{document.document_id}”不能硬依赖其他功能的文档"
                    f"“{dependency_id}”；请改用 related_documents。",
                )
            dependency = documents.get(dependency_id)
            if dependency is None:
                raise ArchiveValidationError(
                    "MISSING_DEPENDENCY",
                    f"文档“{document.document_id}”声明的依赖“{dependency_id}”不存在。",
                    document.path,
                )
            dependents[dependency_id].append(document.document_id)

    normalized_dependents = {
        document_id: tuple(sorted(values))
        for document_id, values in dependents.items()
    }
    _validate_acyclic(documents, normalized_dependents)

    for document_id in sorted(documents):
        document = documents[document_id]
        for dependency_id in document.dependencies:
            dependency = documents[dependency_id]
            if document.category == "archive" or dependency.category == "archive":
                raise ArchiveValidationError(
                    "INVALID_DEPENDENCY_DIRECTION",
                    f"人工导航入口不能参与硬依赖：{dependency_id} -> "
                    f"{document.document_id}",
                )
            dependency_rank = CATEGORY_RANK[dependency.category]
            document_rank = CATEGORY_RANK[document.category]
            terminology_dependency = (
                dependency.document_id
                == f"{document.feature_id}.requirements.terminology"
                and document.document_id
                == f"{document.feature_id}.requirements.overview"
                and dependency.category == "requirements"
                and document.category == "requirements"
            )
            if dependency_rank >= document_rank and not terminology_dependency:
                raise ArchiveValidationError(
                    "INVALID_DEPENDENCY_DIRECTION",
                    f"依赖“{dependency_id} -> {document.document_id}”违反六层正向"
                    "职责顺序；依赖源必须位于更上游类别。",
                )
    return normalized_dependents


def _validate_acyclic(
    documents: Mapping[str, DocumentRecord],
    dependents: Mapping[str, Tuple[str, ...]],
) -> None:
    indegrees = {
        document_id: len(document.dependencies)
        for document_id, document in documents.items()
    }
    ready = sorted(
        document_id for document_id, degree in indegrees.items() if degree == 0
    )
    processed = 0
    while ready:
        current = ready.pop(0)
        processed += 1
        for dependent_id in dependents[current]:
            indegrees[dependent_id] -= 1
            if indegrees[dependent_id] == 0:
                ready.append(dependent_id)
                ready.sort()
    if processed == len(documents):
        return

    remaining = sorted(
        document_id for document_id, degree in indegrees.items() if degree > 0
    )
    raise ArchiveValidationError(
        "CYCLIC_DEPENDENCY",
        "检测到多文档循环依赖，涉及文档：" + "、".join(remaining),
    )


def validate_archive_root(root: Path) -> ArchiveGraph:
    """校验共同父目录并仅从清单和文档元数据构建依赖图。"""

    normalized_root = root.expanduser().resolve()
    if not normalized_root.exists() or not normalized_root.is_dir():
        raise ArchiveValidationError(
            "INVALID_ROOT",
            f"档案父目录“{normalized_root}”不存在或不是目录。",
        )
    try:
        require_root_contract(normalized_root)
    except ArchiveRootError as exception:
        raise ArchiveValidationError(
            exception.code, exception.message, normalized_root
        ) from exception
    features = _discover_features(normalized_root)
    documents = _load_documents(features)
    try:
        validate_archive_markdown(features, documents)
    except ArchiveTerminologyError as exception:
        raise ArchiveValidationError(exception.code, exception.message) from exception
    dependents = _validate_dependencies(documents)
    return ArchiveGraph(
        root=normalized_root,
        features=features,
        documents=documents,
        dependents=dependents,
    )


def validate_feature_archive(root: Path, feature_id: str) -> ArchiveGraph:
    """只校验指定交付项及其文档依赖，不读取其他交付项。"""

    normalized_root = root.expanduser().resolve()
    if not normalized_root.exists() or not normalized_root.is_dir():
        raise ArchiveValidationError(
            "INVALID_ROOT",
            f"档案父目录“{normalized_root}”不存在或不是目录。",
            normalized_root,
        )
    try:
        require_root_contract(normalized_root)
    except ArchiveRootError as exception:
        raise ArchiveValidationError(
            exception.code, exception.message, normalized_root
        ) from exception
    if not FEATURE_ID_PATTERN.fullmatch(feature_id):
        raise ArchiveValidationError(
            "INVALID_FEATURE_ID",
            f"功能标识不合法：{feature_id}",
            normalized_root,
        )

    feature_path = normalized_root / feature_id
    if not feature_path.exists() or not feature_path.is_dir():
        raise ArchiveValidationError(
            "UNKNOWN_FEATURE",
            f"功能档案“{feature_id}”不存在。",
            feature_path,
        )
    manifest_path = feature_path / "feature.json"
    if not manifest_path.exists():
        raise ArchiveValidationError(
            "MISSING_MANIFEST",
            f"功能档案“{feature_path}”缺少 feature.json。",
            manifest_path,
        )
    if not manifest_path.is_file():
        raise ArchiveValidationError(
            "INVALID_MANIFEST",
            f"生命周期清单路径“{manifest_path}”不是文件。",
            manifest_path,
        )

    feature = _load_feature(feature_path)
    features = {feature.feature_id: feature}
    documents = _load_documents(features)
    try:
        validate_archive_markdown(features, documents)
    except ArchiveTerminologyError as exception:
        raise ArchiveValidationError(exception.code, exception.message) from exception
    dependents = _validate_dependencies(documents)
    return ArchiveGraph(
        root=normalized_root,
        features=features,
        documents=documents,
        dependents=dependents,
    )


def _relative_path(graph: ArchiveGraph, path: Path) -> str:
    return path.relative_to(graph.root).as_posix()


def render_machine_index(graph: ArchiveGraph) -> str:
    """生成不包含运行时间、可稳定比较的机器依赖索引。"""

    features = {
        feature_id: {
            "title": feature.title,
            "path": _relative_path(graph, feature.path),
            "lifecycle": feature.lifecycle,
            "created_at": feature.created_at,
            "updated_at": feature.updated_at,
            "frozen_at": feature.frozen_at,
            "retention_days": feature.retention_days,
            "retain_reason": feature.retain_reason,
            "validation": dict(sorted(feature.validation_statuses.items())),
            "terminology_schema_version": feature.terminology_schema_version,
        }
        for feature_id, feature in sorted(graph.features.items())
    }
    documents = {}
    dependency_edges = []
    related_edges = []
    for document_id, document in sorted(graph.documents.items()):
        documents[document_id] = {
            "feature_id": document.feature_id,
            "path": _relative_path(graph, document.path),
            "category": document.category,
            "content_status": document.content_status,
            "semantic_version": document.semantic_version,
            "content_fingerprint": document.content_fingerprint,
            "dependencies": list(document.dependencies),
            "dependency_versions": dict(sorted(document.dependency_versions.items())),
            "dependents": list(graph.dependents[document_id]),
            "related_documents": list(document.related_documents),
        }
        for dependency_id in document.dependencies:
            dependency_edges.append(
                {"from": dependency_id, "to": document.document_id}
            )
        for related_id in document.related_documents:
            related_edges.append(
                {
                    "from": document.document_id,
                    "to": related_id,
                    "resolved": related_id in graph.documents,
                }
            )

    value = {
        "schema_version": SCHEMA_VERSION,
        "generated_from": "feature.json and document front matter",
        "features": features,
        "documents": documents,
        "dependency_edges": sorted(
            dependency_edges, key=lambda edge: (edge["from"], edge["to"])
        ),
        "related_edges": sorted(
            related_edges, key=lambda edge: (edge["from"], edge["to"])
        ),
    }
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _document_list(document_ids: Iterable[str]) -> str:
    values = tuple(sorted(document_ids))
    return "、".join(f"`{value}`" for value in values) if values else "无"


def render_global_index(graph: ArchiveGraph) -> str:
    """生成供人工阅读的全局状态索引。"""

    lines = [
        "# 功能交付档案全局索引",
        "",
        "> 本文件由工具根据 `feature.json` 和文档元数据自动生成，请勿手工维护。",
        "",
        "## 功能状态",
        "",
        "| 功能 | 阶段 | 最近变化 | 过期项 | 阻塞项 | 冻结时间 | 待清理 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for feature_id, feature in sorted(graph.features.items()):
        feature_documents = tuple(
            document
            for document in graph.documents.values()
            if document.feature_id == feature_id
        )
        stale = _document_list(
            document.document_id
            for document in feature_documents
            if document.content_status == "stale"
        )
        blocked = _document_list(
            document.document_id
            for document in feature_documents
            if document.content_status == "blocked"
        )
        lines.append(
            "| "
            + " | ".join(
                (
                    _markdown_cell(f"{feature.title} (`{feature_id}`)"),
                    LIFECYCLE_LABELS[feature.lifecycle],
                    feature.updated_at,
                    stale,
                    blocked,
                    feature.frozen_at or "—",
                    "是" if feature.lifecycle == "pending_cleanup" else "否",
                )
            )
            + " |"
        )
    if not graph.features:
        lines.append("| 无 | — | — | 无 | 无 | — | 否 |")

    stale_documents = [
        document.document_id
        for document in graph.documents.values()
        if document.content_status == "stale"
    ]
    blocked_documents = [
        document.document_id
        for document in graph.documents.values()
        if document.content_status == "blocked"
    ]
    cleanup_features = [
        feature.feature_id
        for feature in graph.features.values()
        if feature.lifecycle == "pending_cleanup"
    ]
    lines.extend(
        [
            "",
            "## 汇总",
            "",
            f"- 过期文档：{_document_list(stale_documents)}",
            f"- 阻塞文档：{_document_list(blocked_documents)}",
            f"- 待清理档案：{_document_list(cleanup_features)}",
            "",
            "## 非阻塞关联",
            "",
            "| 来源文档 | 关联目标 | 目标状态 |",
            "| --- | --- | --- |",
        ]
    )
    related_rows = []
    for document in graph.documents.values():
        for related_id in document.related_documents:
            related_rows.append(
                (
                    document.document_id,
                    related_id,
                    "已解析" if related_id in graph.documents else "未解析（非阻塞）",
                )
            )
    for source, target, status in sorted(related_rows):
        lines.append(f"| `{source}` | `{target}` | {status} |")
    if not related_rows:
        lines.append("| 无 | 无 | — |")
    lines.append("")
    return "\n".join(lines)


def _replace_derived_files(
    root: Path,
    contents: Mapping[str, str],
) -> Tuple[str, ...]:
    changed_names_list = []
    for name, content in contents.items():
        destination = root / name
        if destination.exists() and not destination.is_file():
            raise ArchiveValidationError(
                "INDEX_PATH_CONFLICT",
                f"派生索引目标“{destination}”已经存在但不是文件。",
            )
        try:
            unchanged = destination.is_file() and (
                destination.read_text(encoding="utf-8") == content
            )
        except (OSError, UnicodeError) as exception:
            raise ArchiveValidationError(
                "INDEX_REBUILD_FAILED",
                f"无法读取已有派生索引“{destination}”：{exception}",
            ) from exception
        if not unchanged:
            changed_names_list.append(name)
    changed_names = tuple(changed_names_list)
    if not changed_names:
        return ()

    staging = Path(tempfile.mkdtemp(prefix=".feature-archive-index-", dir=root))
    replaced = []
    backups: Dict[str, Path] = {}
    replacements: Dict[str, Path] = {}
    try:
        for name in changed_names:
            if os.name == "nt":
                descriptor, replacement_name = tempfile.mkstemp(
                    prefix=f".{name}.",
                    suffix=".tmp",
                    dir=root,
                )
                os.close(descriptor)
                replacement = Path(replacement_name)
            else:
                replacement = staging / name
            replacement.write_text(
                contents[name], encoding="utf-8", newline="\n"
            )
            replacements[name] = replacement
        for name in changed_names:
            destination = root / name
            if destination.exists() and not destination.is_file():
                raise ArchiveValidationError(
                    "INDEX_PATH_CONFLICT",
                    f"派生索引目标“{destination}”已经存在但不是文件。",
                )
            if destination.exists():
                backup = staging / f"{name}.backup"
                os.replace(destination, backup)
                backups[name] = backup
            os.replace(replacements[name], destination)
            replaced.append(name)
    except Exception as exception:
        for name in reversed(replaced):
            destination = root / name
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
        for name, backup in backups.items():
            if backup.exists():
                try:
                    os.replace(backup, root / name)
                except OSError:
                    pass
        if isinstance(exception, ArchiveValidationError):
            raise
        raise ArchiveValidationError(
            "INDEX_REBUILD_FAILED",
            f"重建派生索引失败，已尝试恢复原索引：{exception}",
        ) from exception
    finally:
        for replacement in replacements.values():
            try:
                replacement.unlink(missing_ok=True)
            except OSError:
                pass
        shutil.rmtree(staging, ignore_errors=True)
    return changed_names


def rebuild_indexes(graph: ArchiveGraph) -> Mapping[str, object]:
    """幂等重建机器索引和人工索引，不修改任何功能档案。"""

    contents = {
        MACHINE_INDEX_NAME: render_machine_index(graph),
        GLOBAL_INDEX_NAME: render_global_index(graph),
    }
    changed_names = _replace_derived_files(graph.root, contents)
    return {
        "status": "rebuilt" if changed_names else "unchanged",
        "root": str(graph.root),
        "feature_count": len(graph.features),
        "document_count": len(graph.documents),
        "machine_index": str(graph.root / MACHINE_INDEX_NAME),
        "global_index": str(graph.root / GLOBAL_INDEX_NAME),
        "changed_indexes": list(changed_names),
    }
