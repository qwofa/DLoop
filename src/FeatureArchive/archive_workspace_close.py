"""中断或放弃执行时，原子恢复授权正文并释放租约。"""

from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import tempfile
from typing import Mapping

from archive_approvals import _state_path, _write_state
from archive_workspace import (
    ArchiveWorkspaceError,
    _safe_relative_path,
    _state_path as _workspace_state_path,
    _write_workspace_state,
    current_workspace_guard_snapshot,
    current_workspace_snapshot,
    load_workspace_state,
    outside_scope_guard_changes,
    workspace_content_snapshot,
)


def commit_workflow_and_lease_atomically(
    root: Path,
    feature_path: Path,
    feature_id: str,
    package_id: str,
    workflow_state_after: Mapping[str, object],
    lease_updates: Mapping[str, object],
    *,
    release_lease: bool = False,
) -> None:
    """同时提交执行账本和现有修改租约；任一写入失败时恢复两份状态。"""

    workflow_path = _state_path(feature_path)
    workspace_path = _workspace_state_path(root)
    workflow_bytes = workflow_path.read_bytes() if workflow_path.exists() else None
    workspace_bytes = workspace_path.read_bytes() if workspace_path.exists() else None
    workspace_state = load_workspace_state(root)
    current_lease = workspace_state.get("modification_lease")
    if (
        not isinstance(current_lease, dict)
        or current_lease.get("feature_id") != feature_id
        or current_lease.get("package_id") != package_id
    ):
        raise ArchiveWorkspaceError(
            "MODIFICATION_LEASE_CONFLICT",
            "当前执行与修改租约不匹配。",
        )
    workspace_after = deepcopy(workspace_state)
    if release_lease:
        workspace_after["modification_lease"] = None
    else:
        updated_lease = workspace_after["modification_lease"]
        updated_lease.update(lease_updates)
    try:
        _write_state(workflow_path, workflow_state_after)
        _write_workspace_state(root, workspace_after)
    except Exception as exception:
        rollback_errors = []
        for path, content in ((workflow_path, workflow_bytes), (workspace_path, workspace_bytes)):
            try:
                _restore_state_file(path, content)
            except Exception as rollback_exception:
                rollback_errors.append(str(rollback_exception))
        if rollback_errors:
            raise ArchiveWorkspaceError(
                "ATOMIC_STATE_ROLLBACK_FAILED",
                "状态提交失败且无法完整恢复调用前状态：" + "；".join(rollback_errors),
            ) from exception
        raise


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _entries(snapshot: Mapping[str, object]) -> Mapping[str, Mapping[str, str]]:
    values = snapshot.get("entries")
    if not isinstance(values, dict):
        raise ArchiveWorkspaceError(
            "WORKSPACE_BASELINE_CONTENT_INVALID",
            "授权范围正文基线缺少文件内容。",
        )
    result = {}
    for relative, value in values.items():
        if (
            not isinstance(relative, str)
            or not isinstance(value, dict)
            or not isinstance(value.get("digest"), str)
            or not isinstance(value.get("content_base64"), str)
        ):
            raise ArchiveWorkspaceError(
                "WORKSPACE_BASELINE_CONTENT_INVALID",
                "授权范围正文基线包含不合法文件记录。",
            )
        result[relative] = value
    return result


def _decode(entry: Mapping[str, str], relative: str) -> bytes:
    try:
        return base64.b64decode(entry["content_base64"], validate=True)
    except (ValueError, TypeError) as exception:
        raise ArchiveWorkspaceError(
            "WORKSPACE_BASELINE_CONTENT_INVALID",
            f"授权文件“{relative}”的正文基线无法解码。",
        ) from exception


def _file_digest(path: Path) -> str:
    try:
        return _digest(path.read_bytes())
    except OSError as exception:
        raise ArchiveWorkspaceError(
            "WORKSPACE_RESTORE_FAILED",
            f"无法读取恢复事务暂存文件“{path}”：{exception}",
        ) from exception


def _restore_backup(backup: Path, target: Path) -> None:
    """目标仍为空时才恢复暂存正文，避免覆盖并发写入。"""

    try:
        os.link(backup, target)
    except (FileExistsError, OSError):
        return


def _move_current(target: Path, backup: Path, relative: str) -> None:
    try:
        os.rename(target, backup)
    except FileNotFoundError as exception:
        raise ArchiveWorkspaceError(
            "WORKSPACE_CONCURRENT_DRIFT",
            f"授权文件“{relative}”在恢复期间发生并发变化。",
        ) from exception
    except OSError as exception:
        raise ArchiveWorkspaceError(
            "WORKSPACE_RESTORE_FAILED",
            f"无法暂存授权文件“{relative}”：{exception}",
        ) from exception


def _remove_if_unchanged(target: Path, relative: str, expected_digest: str) -> None:
    staging = Path(tempfile.mkdtemp(prefix=".workspace-restore-", dir=target.parent))
    backup = staging / target.name
    try:
        _move_current(target, backup, relative)
        if _file_digest(backup) != expected_digest:
            _restore_backup(backup, target)
            raise ArchiveWorkspaceError(
                "WORKSPACE_CONCURRENT_DRIFT",
                f"授权文件“{relative}”在恢复期间发生并发变化。",
            )
        if target.exists():
            raise ArchiveWorkspaceError(
                "WORKSPACE_CONCURRENT_DRIFT",
                f"授权文件“{relative}”在恢复期间被并发重建。",
            )
    finally:
        try:
            backup.unlink(missing_ok=True)
            staging.rmdir()
        except OSError:
            pass


def _replace_if_unchanged(
    target: Path,
    relative: str,
    expected_digest: str,
    content: bytes,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".workspace-restore-", dir=target.parent))
    replacement = staging / f"{target.name}.replacement"
    backup = staging / f"{target.name}.current"
    try:
        replacement.write_bytes(content)
        if expected_digest != "missing":
            _move_current(target, backup, relative)
            if _file_digest(backup) != expected_digest:
                _restore_backup(backup, target)
                raise ArchiveWorkspaceError(
                    "WORKSPACE_CONCURRENT_DRIFT",
                    f"授权文件“{relative}”在恢复期间发生并发变化。",
                )
        try:
            os.link(replacement, target)
        except FileExistsError as exception:
            raise ArchiveWorkspaceError(
                "WORKSPACE_CONCURRENT_DRIFT",
                f"授权文件“{relative}”在恢复期间被并发写入。",
            ) from exception
        except OSError as exception:
            _restore_backup(backup, target)
            raise ArchiveWorkspaceError(
                "WORKSPACE_RESTORE_FAILED",
                f"无法恢复授权文件“{relative}”：{exception}",
            ) from exception
    finally:
        try:
            replacement.unlink(missing_ok=True)
            backup.unlink(missing_ok=True)
            staging.rmdir()
        except OSError:
            pass


def _restore_snapshot(
    snapshot: Mapping[str, object],
    expected_current: Mapping[str, object],
) -> None:
    root_value = snapshot.get("workspace_root")
    scopes_value = snapshot.get("scopes")
    if not isinstance(root_value, str) or not isinstance(scopes_value, list):
        raise ArchiveWorkspaceError(
            "WORKSPACE_BASELINE_CONTENT_INVALID",
            "授权范围正文基线缺少工作区根或写入范围。",
        )
    workspace_root = Path(root_value).expanduser().resolve()
    scopes = tuple(str(item) for item in scopes_value)
    desired = _entries(snapshot)
    current = workspace_content_snapshot(workspace_root, scopes)
    if current.get("digest") != expected_current.get("digest"):
        raise ArchiveWorkspaceError(
            "WORKSPACE_CONCURRENT_DRIFT",
            "授权范围在恢复准备期间发生并发变化，已停止闭合。",
        )
    before = _entries(current)
    applied = []
    try:
        for relative in sorted(set(before) - set(desired), reverse=True):
            target = _safe_relative_path(workspace_root, relative)
            _remove_if_unchanged(target, relative, before[relative]["digest"])
            applied.append((relative, "missing"))
        for relative in sorted(desired):
            target = _safe_relative_path(workspace_root, relative)
            previous = before.get(relative)
            content = _decode(desired[relative], relative)
            _replace_if_unchanged(
                target,
                relative,
                previous["digest"] if previous is not None else "missing",
                content,
            )
            applied.append((relative, _digest(content)))
    except Exception as exception:
        rollback_errors = []
        for relative, expected_digest in reversed(applied):
            target = _safe_relative_path(workspace_root, relative)
            previous = before.get(relative)
            try:
                if previous is None:
                    _remove_if_unchanged(target, relative, expected_digest)
                else:
                    _replace_if_unchanged(
                        target,
                        relative,
                        expected_digest,
                        _decode(previous, relative),
                    )
            except Exception as rollback_exception:
                rollback_errors.append(str(rollback_exception))
        if rollback_errors:
            raise ArchiveWorkspaceError(
                "ATOMIC_CLOSE_ROLLBACK_FAILED",
                "工作区恢复失败且无法完整回到调用前正文：" + "；".join(rollback_errors),
            ) from exception
        raise


def _restore_state_file(path: Path, content: bytes | None) -> None:
    if content is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".atomic-close-", dir=path.parent))
    temporary = staging / path.name
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
        try:
            staging.rmdir()
        except OSError:
            pass


def close_execution_atomically(
    root: Path,
    feature_path: Path,
    feature_id: str,
    package_id: str,
    workflow_state_after: Mapping[str, object],
    lease: Mapping[str, object],
) -> None:
    """只恢复当前授权范围，并同时写入终态和释放当前租约。"""

    if lease.get("feature_id") != feature_id or lease.get("package_id") != package_id:
        raise ArchiveWorkspaceError(
            "MODIFICATION_LEASE_CONFLICT",
            "当前执行与修改租约不匹配。",
        )
    baseline = lease.get("baseline_content_snapshot")
    if not isinstance(baseline, dict):
        raise ArchiveWorkspaceError(
            "WORKSPACE_BASELINE_CONTENT_MISSING",
            "当前租约没有执行前授权正文，不能安全自动恢复。",
        )
    current = current_workspace_snapshot(lease)
    guard = current_workspace_guard_snapshot(lease)
    outside_changes = outside_scope_guard_changes(
        lease.get("baseline_guard_snapshot", {}),
        guard,
        tuple(str(item) for item in lease.get("write_scopes", [])),
    )
    if outside_changes:
        raise ArchiveWorkspaceError(
            "WORKSPACE_SCOPE_VIOLATION",
            "授权范围外存在执行期间变化：" + "，".join(outside_changes),
        )

    workspace_root = Path(str(lease["workspace_root"]))
    scopes = tuple(str(item) for item in lease["write_scopes"])
    pre_call_contents = workspace_content_snapshot(workspace_root, scopes)
    workflow_path = _state_path(feature_path)
    workspace_path = _workspace_state_path(root)
    workflow_bytes = workflow_path.read_bytes() if workflow_path.exists() else None
    workspace_bytes = workspace_path.read_bytes() if workspace_path.exists() else None
    workspace_state = load_workspace_state(root)
    restored = False
    try:
        _restore_snapshot(baseline, current)
        restored = True
        if current_workspace_snapshot(lease).get("digest") != lease.get("baseline_digest"):
            raise ArchiveWorkspaceError(
                "WORKSPACE_RESTORE_FAILED",
                "授权范围未恢复到执行前正文基线。",
            )
        _write_state(workflow_path, workflow_state_after)
        current_lease = workspace_state.get("modification_lease")
        if (
            not isinstance(current_lease, dict)
            or current_lease.get("feature_id") != feature_id
            or current_lease.get("package_id") != package_id
        ):
            raise ArchiveWorkspaceError(
                "MODIFICATION_LEASE_CONFLICT",
                "当前执行与修改租约不匹配。",
            )
        workspace_state["modification_lease"] = None
        _write_workspace_state(root, workspace_state)
    except Exception as exception:
        rollback_errors = []
        if restored:
            try:
                restored_snapshot = {"digest": lease.get("baseline_digest")}
                _restore_snapshot(pre_call_contents, restored_snapshot)
            except Exception as rollback_exception:
                rollback_errors.append(str(rollback_exception))
        for path, content in ((workflow_path, workflow_bytes), (workspace_path, workspace_bytes)):
            try:
                _restore_state_file(path, content)
            except Exception as rollback_exception:
                rollback_errors.append(str(rollback_exception))
        if rollback_errors:
            raise ArchiveWorkspaceError(
                "ATOMIC_CLOSE_ROLLBACK_FAILED",
                "闭合失败且无法完整恢复调用前状态：" + "；".join(rollback_errors),
            ) from exception
        raise
