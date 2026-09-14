"""冻结档案保留期检测、清理预览和独立清理。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
from typing import Dict, List, Mapping, Sequence

from archive_changes import (
    ArchiveChangeError,
    _normalized_content,
    _replace_files_atomically,
)
from archive_lifecycle import _updated_entry
from archive_validation import (
    ArchiveGraph,
    ArchiveValidationError,
    FeatureRecord,
    rebuild_indexes,
    validate_archive_root,
)
from archive_workspace import serialized_workflow_state


class ArchiveCleanupError(Exception):
    """表示保留期检测或清理被确定性规则拒绝。"""

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


def normalize_utc_timestamp(value: str | None) -> str:
    """把可控时间输入规范化为秒级 UTC 时间。"""

    if value is None:
        parsed = datetime.now(timezone.utc)
    else:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exception:
            raise ArchiveCleanupError(
                "INVALID_TIME",
                f"时间“{value}”不是合法 ISO 8601 时间。",
            ) from exception
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ArchiveCleanupError(
                "INVALID_TIME",
                "时间输入必须包含时区，例如 2026-07-29T00:00:00Z。",
            )
    return (
        parsed.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _parse_recorded_timestamp(value: str, field: str, feature_id: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exception:
        raise ArchiveCleanupError(
            "INVALID_MANIFEST_TIME",
            f"功能档案“{feature_id}”的 {field} 不是合法 ISO 8601 时间。",
        ) from exception
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ArchiveCleanupError(
            "INVALID_MANIFEST_TIME",
            f"功能档案“{feature_id}”的 {field} 必须包含时区。",
        )
    return parsed.astimezone(timezone.utc)


def _retention_result(
    feature: FeatureRecord,
    now: datetime,
) -> Mapping[str, object]:
    if feature.frozen_at is None:
        return {
            "feature_id": feature.feature_id,
            "lifecycle": feature.lifecycle,
            "frozen_at": None,
            "retention_days": feature.retention_days,
            "eligible_at": None,
            "eligible": False,
        }
    frozen_at = _parse_recorded_timestamp(
        feature.frozen_at,
        "frozen_at",
        feature.feature_id,
    )
    eligible_at = frozen_at + timedelta(days=feature.retention_days)
    return {
        "feature_id": feature.feature_id,
        "lifecycle": feature.lifecycle,
        "frozen_at": feature.frozen_at,
        "retention_days": feature.retention_days,
        "eligible_at": eligible_at.isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        ),
        "eligible": now >= eligible_at,
    }


def _updated_cleanup_manifest(
    feature: FeatureRecord,
    updated_at: str,
) -> tuple[Path, str]:
    path = feature.path / "feature.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exception:
        raise ArchiveCleanupError(
            "INVALID_MANIFEST",
            f"无法更新待清理状态“{path}”：{exception}",
        ) from exception
    manifest["lifecycle"] = "pending_cleanup"
    manifest["updated_at"] = updated_at
    return path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"


def detect_cleanup_candidates(
    root: Path,
    now_value: str | None,
    feature_ids: Sequence[str] = (),
) -> Mapping[str, object]:
    """检测满保留期档案，只标记待清理状态并重建派生索引。"""

    now_text = normalize_utc_timestamp(now_value)
    now = _parse_recorded_timestamp(now_text, "now", "cleanup-detection")
    graph = validate_archive_root(root)
    selected_ids = tuple(dict.fromkeys(feature_ids))
    if selected_ids:
        unknown = tuple(
            feature_id
            for feature_id in selected_ids
            if feature_id not in graph.features
        )
        if unknown:
            raise ArchiveCleanupError(
                "UNKNOWN_FEATURE",
                "以下功能档案不存在：" + "、".join(unknown),
            )
        features = tuple(graph.features[feature_id] for feature_id in selected_ids)
    else:
        features = tuple(
            feature
            for _, feature in sorted(graph.features.items())
        )

    evaluations = tuple(_retention_result(feature, now) for feature in features)
    candidates = tuple(
        feature
        for feature, evaluation in zip(features, evaluations)
        if feature.lifecycle == "frozen" and evaluation["eligible"]
    )
    if not candidates:
        index_result = rebuild_indexes(graph)
        return {
            "status": "unchanged",
            "root": str(graph.root),
            "now": now_text,
            "marked": [],
            "evaluations": list(evaluations),
            "indexes": index_result["status"],
        }

    changes: Dict[Path, str] = {}
    originals: Dict[Path, str] = {}
    for feature in candidates:
        entry_path, entry_content = _updated_entry(
            graph,
            feature.feature_id,
            "pending_cleanup",
        )
        manifest_path, manifest_content = _updated_cleanup_manifest(
            feature,
            now_text,
        )
        changes[entry_path] = entry_content
        changes[manifest_path] = manifest_content
        originals[entry_path] = _normalized_content(entry_path)
        originals[manifest_path] = manifest_path.read_text(encoding="utf-8")

    _replace_files_atomically(graph.root, changes)
    try:
        updated_graph = validate_archive_root(graph.root)
        index_result = rebuild_indexes(updated_graph)
    except Exception as exception:
        try:
            _replace_files_atomically(graph.root, originals)
        except ArchiveChangeError as rollback_exception:
            raise ArchiveCleanupError(
                "CLEANUP_DETECTION_ROLLBACK_FAILED",
                "待清理状态标记失败，且无法恢复原状态："
                f"{rollback_exception.message}",
            ) from exception
        raise

    return {
        "status": "marked",
        "root": str(updated_graph.root),
        "now": now_text,
        "marked": [feature.feature_id for feature in candidates],
        "evaluations": list(evaluations),
        "indexes": index_result["status"],
    }


def _feature_purge_blockers(
    graph: ArchiveGraph,
    feature: FeatureRecord,
    now: datetime,
) -> List[Mapping[str, str]]:
    blockers: List[Mapping[str, str]] = []

    def add(reason: str, code: str) -> None:
        blockers.append(
            {
                "feature_id": feature.feature_id,
                "code": code,
                "reason": reason,
            }
        )

    if feature.path.is_symlink():
        add("功能档案目录是符号链接，拒绝执行删除。", "SYMLINK_ARCHIVE")
    if feature.lifecycle != "pending_cleanup":
        add(
            f"lifecycle={feature.lifecycle}，清理要求 pending_cleanup。",
            "INELIGIBLE_LIFECYCLE",
        )
    retention = _retention_result(feature, now)
    if not retention["eligible"]:
        add(
            f"保留期尚未结束，最早清理时间为 {retention['eligible_at']}。",
            "RETENTION_NOT_ELAPSED",
        )
    if feature.retain_reason is not None:
        add(
            f"存在明确保留原因：{feature.retain_reason}",
            "RETAIN_REASON",
        )

    for document in sorted(
        (
            document
            for document in graph.documents.values()
            if document.feature_id == feature.feature_id
            and document.category != "archive"
            and document.content_status in {"draft", "stale", "blocked"}
        ),
        key=lambda document: document.document_id,
    ):
        add(
            f"{document.document_id} 的 content_status="
            f"{document.content_status}。",
            "DOCUMENT_BLOCKER",
        )

    validation = feature.validation_statuses
    if validation["conclusion"] != "passed":
        add(
            f"validation.conclusion={validation['conclusion']}，清理要求 passed。",
            "INVALID_VALIDATION_STATE",
        )
    for field in ("unverified_boundaries", "residual_risks"):
        if validation[field] == "pending":
            add(
                f"validation.{field}=pending，冻结验证状态不完整。",
                "INVALID_VALIDATION_STATE",
            )
    return blockers


def preview_purge(
    root: Path,
    feature_ids: Sequence[str],
    now_value: str | None,
) -> Mapping[str, object]:
    """预览明确选定档案的清理目标和全部拒绝原因。"""
    from archive_snapshots import snapshot_repository

    selected_ids = tuple(dict.fromkeys(feature_ids))
    now_text = normalize_utc_timestamp(now_value)
    if not selected_ids:
        raise ArchiveCleanupError(
            "MISSING_FEATURE_SELECTION",
            "清理必须明确指定至少一个 --feature-id。",
        )

    try:
        graph = validate_archive_root(root)
    except ArchiveValidationError as exception:
        targets = [
            {
                "feature_id": feature_id,
                "path": str(root.expanduser().resolve() / feature_id),
                "action": "refuse",
                "blockers": [
                    {
                        "feature_id": feature_id,
                        "code": "INVALID_ARCHIVE_REFERENCE",
                        "reason": f"{exception.code}: {exception.message}",
                    }
                ],
            }
            for feature_id in selected_ids
        ]
        return {
            "status": "preview",
            "root": str(root.expanduser().resolve()),
            "now": now_text,
            "can_execute": False,
            "targets": targets,
        }

    now = _parse_recorded_timestamp(now_text, "now", "purge-preview")
    targets = []
    can_execute = True
    for feature_id in selected_ids:
        feature = graph.features.get(feature_id)
        if feature is None:
            blockers = [
                {
                    "feature_id": feature_id,
                    "code": "UNKNOWN_FEATURE",
                    "reason": "明确选定的功能档案不存在。",
                }
            ]
            path = graph.root / feature_id
        else:
            blockers = _feature_purge_blockers(graph, feature, now)
            path = feature.path
        if blockers:
            can_execute = False
        targets.append(
            {
                "feature_id": feature_id,
                "path": str(path),
                "snapshot_repository": str(snapshot_repository(graph.root, feature_id)),
                "snapshot_history_will_be_deleted": snapshot_repository(graph.root, feature_id).exists(),
                "action": "delete" if not blockers else "refuse",
                "blockers": blockers,
            }
        )
    return {
        "status": "preview",
        "root": str(graph.root),
        "now": now_text,
        "can_execute": can_execute,
        "targets": targets,
    }


@serialized_workflow_state
def purge_archives(
    root: Path,
    feature_ids: Sequence[str],
    now_value: str | None,
) -> Mapping[str, object]:
    """重新检查全部安全条件后，原子移走并删除明确选定的档案。"""

    preview = preview_purge(root, feature_ids, now_value)
    if not preview["can_execute"]:
        blockers = [
            blocker
            for target in preview["targets"]
            for blocker in target["blockers"]
        ]
        raise ArchiveCleanupError(
            "PURGE_BLOCKED",
            "清理被安全条件拒绝；请先查看 blockers 或执行不带 --execute 的预览。",
            blockers,
        )

    graph = validate_archive_root(root)
    selected_ids = tuple(dict.fromkeys(feature_ids))
    staging = Path(
        tempfile.mkdtemp(
            prefix=".feature-archive-purge-",
            dir=graph.root.parent,
        )
    )
    moved: List[tuple[Path, Path]] = []
    try:
        for feature_id in selected_ids:
            source = graph.features[feature_id].path
            destination = staging / feature_id
            os.replace(source, destination)
            moved.append((source, destination))
            from archive_snapshots import snapshot_repository
            history = snapshot_repository(graph.root, feature_id)
            if history.exists():
                history_destination = staging / (feature_id + ".git")
                os.replace(history, history_destination)
                moved.append((history, history_destination))

        updated_graph = validate_archive_root(graph.root)
        index_result = rebuild_indexes(updated_graph)
    except Exception as exception:
        rollback_errors = []
        for source, destination in reversed(moved):
            try:
                if destination.exists():
                    os.replace(destination, source)
            except OSError as rollback_exception:
                rollback_errors.append(f"{source}：{rollback_exception}")
        try:
            rebuild_indexes(validate_archive_root(graph.root))
        except Exception as rollback_exception:
            rollback_errors.append(f"派生索引：{rollback_exception}")
        if rollback_errors:
            raise ArchiveCleanupError(
                "PURGE_ROLLBACK_FAILED",
                "清理失败且未能完整恢复：" + "；".join(rollback_errors),
            ) from exception
        raise ArchiveCleanupError(
            "PURGE_FAILED",
            f"清理失败，已恢复全部档案：{exception}",
        ) from exception

    def remove_readonly(function, path, error):
        target = Path(path).resolve()
        if os.name != "nt" or not target.is_relative_to(staging.resolve()) or not isinstance(error[1], PermissionError):
            raise error[1]
        target.chmod(target.stat().st_mode | stat.S_IWRITE)
        function(path)

    try:
        shutil.rmtree(staging, onerror=remove_readonly)
    except OSError as exception:
        raise ArchiveCleanupError(
            "PURGE_FINALIZE_FAILED",
            f"档案已从活跃目录移除，但无法删除隔离目录“{staging}”：{exception}",
        ) from exception

    return {
        "status": "purged",
        "root": str(updated_graph.root),
        "now": preview["now"],
        "purged": list(selected_ids),
        "indexes": index_result["status"],
    }
