"""DLoop 项目、规范档案与分享快照的唯一路径规则。"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Mapping


ARCHIVE_ROOT_RELATIVE = Path(".scratch") / "dloop-v3" / "v3.8.1" / "outputs"
SHARE_ROOT_RELATIVE = Path(".scratch") / "dloop-v3" / "v3.8.1" / "shares"
SHARE_MANIFEST_NAME = ".dloop-share.json"

WORKFLOW_ARTIFACT_DIRECTORIES: Mapping[str, Path] = {
    "plan_file": Path("04-plan"),
    "package_file": Path("05-implementation"),
    "checkpoint_file": Path("05-implementation"),
    "candidate_file": Path("05-implementation"),
    "issues_file": Path("05-implementation"),
    "verification_file": Path("06-validation"),
}

ACTION_STAGE_DIRECTORIES: Mapping[str, Path] = {
    "requirements": Path("01-requirements"),
    "investigation": Path("02-investigation"),
    "design": Path("03-design"),
    "plan": Path("04-plan"),
    "implementation": Path("05-implementation"),
    "validation": Path("06-validation"),
}


class ArchivePathError(Exception):
    """表示调用者试图绕过当前项目的规范路径。"""

    def __init__(
        self,
        code: str,
        message: str,
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})


@dataclass(frozen=True)
class ShareSnapshotLocation:
    path: Path
    feature_id: str | None = None
    exported_at: str | None = None
    source_state_digest: str | None = None

    def as_dict(self) -> Mapping[str, object]:
        return {
            "path": self.path.as_posix(),
            "manifest": (self.path / SHARE_MANIFEST_NAME).as_posix(),
            "feature_id": self.feature_id,
            "exported_at": self.exported_at,
            "source_state_digest": self.source_state_digest,
        }


@dataclass(frozen=True)
class ArchiveLocation:
    project_root: Path
    archive_root: Path
    feature_root: Path | None

    def as_dict(self) -> Mapping[str, object]:
        return {
            "kind": "canonical",
            "project_root": self.project_root.as_posix(),
            "archive_root": self.archive_root.as_posix(),
            "feature_root": (
                self.feature_root.as_posix()
                if self.feature_root is not None
                else None
            ),
        }


def project_root_from_entrypoint(entrypoint: Path) -> Path:
    """从项目内固定安装入口取得项目根，不依赖当前工作目录。"""

    normalized = entrypoint.expanduser().resolve()
    tool_root = normalized.parent
    container = tool_root.parent
    if (
        tool_root.name.casefold() != "featurearchive"
        or container.name.casefold() != "tools"
    ):
        raise ArchivePathError(
            "PROJECT_ROOT_UNAVAILABLE",
            f"DLoop 命令入口不在受支持的项目内位置：{normalized}",
        )
    try:
        project_root = normalized.parents[2]
    except IndexError as exception:
        raise ArchivePathError(
            "PROJECT_ROOT_UNAVAILABLE",
            f"DLoop 命令入口不在受支持的项目内位置：{normalized}",
        ) from exception
    return project_root


def canonical_archive_root(project_root: Path) -> Path:
    return project_root.expanduser().resolve() / ARCHIVE_ROOT_RELATIVE


def canonical_share_root(project_root: Path) -> Path:
    return project_root.expanduser().resolve() / SHARE_ROOT_RELATIVE


def project_root_from_archive_root(archive_root: Path) -> Path:
    """从唯一规范档案根反向取得项目根。"""

    current = archive_root.expanduser().resolve()
    for expected_name in reversed(ARCHIVE_ROOT_RELATIVE.parts):
        if current.name.casefold() != expected_name.casefold():
            raise ArchivePathError(
                "PROJECT_ROOT_UNAVAILABLE",
                f"DLoop 规范档案根不在受支持的项目内位置：{archive_root}",
            )
        current = current.parent
    return current


def archive_location(
    project_root: Path,
    feature_id: str | None = None,
    *,
    archive_root: Path | None = None,
) -> ArchiveLocation:
    normalized_project = project_root.expanduser().resolve()
    normalized_archive = (
        archive_root.expanduser().resolve()
        if archive_root is not None
        else canonical_archive_root(normalized_project)
    )
    feature_root = normalized_archive / feature_id if feature_id is not None else None
    return ArchiveLocation(normalized_project, normalized_archive, feature_root)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def share_snapshot_ancestor(path: Path) -> ShareSnapshotLocation | None:
    current = path if path.is_dir() else path.parent
    for directory in (current, *current.parents):
        manifest = directory / SHARE_MANIFEST_NAME
        if not manifest.is_file():
            continue
        try:
            value = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return ShareSnapshotLocation(directory)
        if not isinstance(value, dict) or value.get("kind") != "dloop-share-snapshot":
            return ShareSnapshotLocation(directory)
        return ShareSnapshotLocation(
            directory,
            feature_id=(
                value.get("feature_id")
                if isinstance(value.get("feature_id"), str)
                else None
            ),
            exported_at=(
                value.get("exported_at")
                if isinstance(value.get("exported_at"), str)
                else None
            ),
            source_state_digest=(
                value.get("source_state_digest")
                if isinstance(value.get("source_state_digest"), str)
                else None
            ),
        )
    return None


def share_recovery_details(
    archive_root: Path,
    feature_id: str,
    share: ShareSnapshotLocation,
    supplied_path: Path,
    *,
    expected_directory: Path | None = None,
    target_kind: str = "workflow-artifact",
    field: str | None = None,
) -> Mapping[str, object]:
    """返回不信任分享清单路径的规范恢复目标。"""

    normalized = supplied_path.expanduser().resolve()
    try:
        relative = normalized.relative_to(share.path)
    except ValueError:
        relative = Path(normalized.name)
    if expected_directory is not None:
        try:
            relative.relative_to(expected_directory)
        except ValueError:
            relative = expected_directory / normalized.name
    target = archive_root.expanduser().resolve() / feature_id / relative
    canonical_target = {
        "kind": target_kind,
        "field": field,
        "relative_path": relative.as_posix(),
        "path": target.as_posix(),
    }
    return {
        "share_snapshot": share.as_dict(),
        "canonical_target": canonical_target,
        "recovery": {
            "action": "use-canonical-artifact",
            "write_target": canonical_target,
        },
    }


def require_canonical_workflow_artifact(
    archive_root: Path,
    feature_id: str,
    field: str,
    value: Path,
) -> Path:
    """只允许正式工作流输入来自当前交付项的固定阶段目录。"""

    expected_directory = WORKFLOW_ARTIFACT_DIRECTORIES.get(field)
    if expected_directory is None:
        return value.expanduser().resolve()
    normalized = value.expanduser().resolve()
    share = share_snapshot_ancestor(normalized)
    if share is not None:
        expected = archive_root.expanduser().resolve() / feature_id / expected_directory
        raise ArchivePathError(
            "SHARE_SNAPSHOT_NOT_EXECUTABLE",
            f"分享快照“{share.path}”只用于读取，不能作为正式工作流输入；"
            f"请在规范目录“{expected}”中形成当前材料。",
            share_recovery_details(
                archive_root,
                feature_id,
                share,
                normalized,
                expected_directory=expected_directory,
                field=field,
            ),
        )
    expected = archive_root.expanduser().resolve() / feature_id / expected_directory
    if not _is_within(normalized, expected):
        raise ArchivePathError(
            "NON_CANONICAL_WORKFLOW_ARTIFACT",
            f"工作流输入“{normalized}”不在当前交付项的规范目录“{expected}”中。",
        )
    return normalized


def stage_overview_target(
    archive_root: Path,
    feature_id: str,
    action: str | None,
) -> Mapping[str, str] | None:
    directory = ACTION_STAGE_DIRECTORIES.get(action or "")
    if directory is None:
        return None
    feature_root = archive_root.expanduser().resolve() / feature_id
    relative = directory / "README.md"
    return {
        "kind": f"{action}-overview",
        "relative_path": relative.as_posix(),
        "path": (feature_root / relative).as_posix(),
    }
