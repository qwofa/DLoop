"""从唯一规范档案形成不可执行、可校验的只读分享快照。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from typing import Mapping

from archive_failure_attribution import read_feature_friction
from archive_paths import SHARE_MANIFEST_NAME, canonical_share_root
from archive_root import require_root_contract
from archive_context import delivery_view
from archive_validation import validate_feature_archive
from archive_workspace import workspace_state_lock


SHARE_SCHEMA_VERSION = 1
DELIVERY_STATUS_SCHEMA_VERSION = 1
TOOL_VERSION = Path(__file__).with_name("VERSION").read_text(encoding="utf-8").strip()
SHAREABLE_MEDIA_SUFFIXES = {
    ".html",
    ".gif",
    ".jpeg",
    ".jpg",
    ".pdf",
    ".png",
    ".svg",
    ".webp",
}


class ArchiveShareError(Exception):
    """表示规范档案无法形成一致的分享快照。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ShareCapture:
    source_files: Mapping[Path, bytes]
    delivery_payload: bytes
    friction_payload: bytes
    friction_warning: str | None
    source_state_digest: str


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _timestamp() -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    recorded = now.isoformat(timespec="microseconds").replace("+00:00", "Z")
    path_value = now.strftime("%Y%m%dT%H%M%S%fZ")
    return recorded, path_value


def _shareable_paths(feature_root: Path) -> tuple[Path, ...]:
    selected = []
    for path in sorted(feature_root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(feature_root)
        if relative == Path("feature.json") or relative == Path("ui-model.json"):
            selected.append(path)
            continue
        if path.suffix.casefold() == ".md" or path.suffix.casefold() in SHAREABLE_MEDIA_SUFFIXES:
            selected.append(path)
    return tuple(selected)


def _capture_sources(feature_root: Path) -> Mapping[Path, bytes]:
    captured = {}
    for path in _shareable_paths(feature_root):
        if path.is_symlink():
            raise ArchiveShareError(
                "SHARE_SOURCE_UNSAFE",
                f"分享来源不能包含符号链接：{path}",
            )
        try:
            captured[path.relative_to(feature_root)] = path.read_bytes()
        except OSError as exception:
            raise ArchiveShareError(
                "SHARE_SOURCE_UNREADABLE",
                f"无法读取分享来源“{path}”：{exception}",
            ) from exception
    return captured


def _friction_payload(
    archive_root: Path,
    feature_id: str,
) -> tuple[bytes, str | None]:
    entries, warning = read_feature_friction(archive_root, feature_id)
    payload = b"".join(
        (
            json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        for entry in entries
    )
    return payload, warning


def _share_delivery_status(
    archive_root: Path,
    feature_id: str,
) -> Mapping[str, object]:
    """只导出面向阅读的交付结论，不暴露执行控制状态。"""

    view = delivery_view(archive_root, feature_id)
    conclusions = view.get("conclusions")
    normalized_conclusions = conclusions if isinstance(conclusions, Mapping) else {}
    approvals = normalized_conclusions.get("approvals")
    slices = normalized_conclusions.get("slices")
    candidate_summary = normalized_conclusions.get("accepted_candidate_summary")
    candidate_items = (
        candidate_summary.get("items")
        if isinstance(candidate_summary, Mapping)
        else None
    )
    return {
        "current_stage": view.get("current_stage"),
        "delivery_summary": {**view["delivery_summary"], "details": "06-validation/README.md",
                             "followups": "06-validation/README.md#后续接入清单"},
        "can_advance": view.get("can_advance"),
        "blockers": list(view.get("blockers", [])),
        "requires_human": view.get("requires_human"),
        "approval_statuses": {
            str(stage): record.get("status")
            for stage, record in approvals.items()
            if isinstance(record, Mapping)
        } if isinstance(approvals, Mapping) else {},
        "slice_statuses": {
            str(package_id): record.get("status")
            for package_id, record in slices.items()
            if isinstance(record, Mapping)
        } if isinstance(slices, Mapping) else {},
        "accepted_candidate_count": (
            len(candidate_items) if isinstance(candidate_items, list) else 0
        ),
    }


def _read_source_state(state_path: Path) -> bytes:
    try:
        return state_path.read_bytes()
    except OSError as exception:
        raise ArchiveShareError(
            "SHARE_STATE_UNREADABLE",
            f"无法读取当前工作流状态“{state_path}”：{exception}",
        ) from exception


def _capture_share_source(
    archive_root: Path,
    feature_id: str,
) -> ShareCapture:
    graph = validate_feature_archive(archive_root, feature_id)
    feature_root = graph.features[feature_id].path.resolve()
    state_payload = _read_source_state(feature_root / "workflow-state.json")
    source_files = dict(_capture_sources(feature_root))
    delivery_payload = _canonical_json({
        "schema_version": DELIVERY_STATUS_SCHEMA_VERSION,
        "source_kind": "canonical",
        "feature_id": feature_id,
        "delivery_status": _share_delivery_status(archive_root, feature_id),
    })
    friction_payload, friction_warning = _friction_payload(archive_root, feature_id)
    source_state_digest = _digest(_canonical_json({
        "workflow_state": _digest(state_payload),
        "source_files": [
            {"path": relative.as_posix(), "sha256": _digest(payload)}
            for relative, payload in sorted(
                source_files.items(),
                key=lambda item: item[0].as_posix(),
            )
        ],
        "delivery_status": _digest(delivery_payload),
        "friction": _digest(friction_payload),
        "friction_warning": friction_warning,
    }))
    return ShareCapture(
        source_files=source_files,
        delivery_payload=delivery_payload,
        friction_payload=friction_payload,
        friction_warning=friction_warning,
        source_state_digest=source_state_digest,
    )


def _write_staging_snapshot(
    staging: Path,
    output_files: Mapping[Path, bytes],
    manifest: Mapping[str, object],
) -> None:
    for relative, payload in output_files.items():
        target = staging / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    (staging / SHARE_MANIFEST_NAME).write_bytes(_canonical_json(manifest))


def export_share_snapshot(
    project_root: Path,
    archive_root: Path,
    feature_id: str,
) -> Mapping[str, object]:
    """一次性捕获规范材料并原子发布新的只读分享快照。"""

    normalized_archive_root = archive_root.expanduser().resolve()
    require_root_contract(normalized_archive_root)

    with workspace_state_lock(normalized_archive_root):
        captured = _capture_share_source(normalized_archive_root, feature_id)

    exported_at, timestamp = _timestamp()
    snapshot_id = (
        f"{timestamp}-{captured.source_state_digest.removeprefix('sha256:')[:8]}"
    )
    share_root = canonical_share_root(project_root)
    feature_share_root = share_root / feature_id
    destination = feature_share_root / snapshot_id
    feature_share_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".dloop-share-", dir=str(feature_share_root)))
    try:
        output_files = dict(captured.source_files)
        output_files[Path("delivery-status.json")] = captured.delivery_payload
        output_files[Path("friction.jsonl")] = captured.friction_payload
        manifest = {
            "kind": "dloop-share-snapshot",
            "schema_version": SHARE_SCHEMA_VERSION,
            "workflow_version": TOOL_VERSION,
            "feature_id": feature_id,
            "snapshot_id": snapshot_id,
            "source_archive": (
                Path(".scratch") / "dloop-v3" / "outputs" / feature_id
            ).as_posix(),
            "exported_at": exported_at,
            "source_state_digest": captured.source_state_digest,
            "read_only": True,
            "executable": False,
            "warnings": (
                [captured.friction_warning]
                if captured.friction_warning is not None
                else []
            ),
            "included_files": [
                {"path": relative.as_posix(), "sha256": _digest(payload)}
                for relative, payload in sorted(
                    output_files.items(),
                    key=lambda item: item[0].as_posix(),
                )
            ],
        }
        _write_staging_snapshot(staging, output_files, manifest)
        with workspace_state_lock(normalized_archive_root):
            current = _capture_share_source(normalized_archive_root, feature_id)
            if current.source_state_digest != captured.source_state_digest:
                raise ArchiveShareError(
                    "SHARE_SOURCE_CHANGED",
                    "分享快照生成期间正式来源已经变化；本次临时结果已放弃，"
                    "请根据最新状态重新显式导出。",
                )
            if destination.exists():
                raise ArchiveShareError(
                    "SHARE_DESTINATION_EXISTS",
                    f"分享快照目标已经存在：{destination}",
                )
            staging.rename(destination)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise

    return {
        "status": "exported",
        "feature_id": feature_id,
        "snapshot_id": snapshot_id,
        "share_location": {
            "kind": "share",
            "path": destination.as_posix(),
            "manifest": (destination / SHARE_MANIFEST_NAME).as_posix(),
            "source_state_digest": captured.source_state_digest,
            "read_only": True,
            "executable": False,
        },
    }
