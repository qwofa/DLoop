"""全局唯一修改租约与授权范围真实快照。"""

from __future__ import annotations

import base64
from contextlib import contextmanager
from functools import wraps
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tempfile
import threading
from typing import Callable, Dict, Mapping, Sequence, Tuple, TypeVar, cast
import xml.etree.ElementTree as ElementTree

from archive_profiles import WORKFLOW_STATE_SCHEMA_VERSION
from archive_slice_plan import pending_plan_slices
WORKSPACE_STATE_NAME = ".feature-archive-workspace-state.json"
WORKSPACE_LOCK_NAME = ".feature-archive-workspace-state.lock"
WORKSPACE_STATE_SCHEMA_VERSION = 3
_PROCESS_LOCKS: Dict[str, threading.RLock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()
_THREAD_LOCK_STATE = threading.local()
_F = TypeVar("_F", bound=Callable[..., object])


class ArchiveWorkspaceError(Exception):
    """表示工作区租约或快照不满足确定性合同。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _canonical_digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _state_path(root: Path) -> Path:
    return root.expanduser().resolve() / WORKSPACE_STATE_NAME


def _process_lock(path: Path) -> threading.RLock:
    key = str(path)
    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PROCESS_LOCKS[key] = lock
        return lock


def _lock_file(stream) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        return
    import fcntl

    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)


def _unlock_file(stream) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def workspace_state_lock(root: Path):
    """串行化同一档案根下的工作区状态事务。"""

    normalized_root = root.expanduser().resolve()
    normalized_root.mkdir(parents=True, exist_ok=True)
    path = normalized_root / WORKSPACE_LOCK_NAME
    key = str(path)
    process_lock = _process_lock(path)
    with process_lock:
        depths = getattr(_THREAD_LOCK_STATE, "depths", {})
        depth = depths.get(key, 0)
        if depth:
            depths[key] = depth + 1
            try:
                yield
            finally:
                depths[key] -= 1
            return
        with path.open("a+b") as stream:
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            _lock_file(stream)
            depths[key] = 1
            _THREAD_LOCK_STATE.depths = depths
            try:
                yield
            finally:
                depths.pop(key, None)
                _unlock_file(stream)


def serialized_workflow_state(function: _F) -> _F:
    """复用工作区事务锁，串行化一次完整的工作流状态读改写。"""

    @wraps(function)
    def wrapped(root: Path, *args, **kwargs):
        with workspace_state_lock(root):
            from archive_snapshots import snapshot_operation
            with snapshot_operation(function, root, args, kwargs):
                return function(root, *args, **kwargs)

    return cast(_F, wrapped)


def load_workspace_state(root: Path) -> Dict[str, object]:
    path = _state_path(root)
    if not path.exists():
        return {
            "schema_version": WORKSPACE_STATE_SCHEMA_VERSION,
            "modification_lease": None,
        }
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exception:
        raise ArchiveWorkspaceError(
            "INVALID_WORKSPACE_STATE",
            f"无法读取工作区状态“{path}”：{exception}",
        ) from exception
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != WORKSPACE_STATE_SCHEMA_VERSION
        or set(value) != {"schema_version", "modification_lease"}
    ):
        raise ArchiveWorkspaceError(
            "INVALID_WORKSPACE_STATE",
            f"工作区状态“{path}”结构不合法。",
        )
    lease = value.get("modification_lease")
    if lease is not None and not isinstance(lease, dict):
        raise ArchiveWorkspaceError(
            "INVALID_WORKSPACE_STATE",
            f"工作区状态“{path}”的全局修改租约不合法。",
        )
    return value


def active_modification_lease(
    root: Path,
    feature_id: str | None = None,
    package_id: str | None = None,
) -> Mapping[str, object] | None:
    lease = load_workspace_state(root).get("modification_lease")
    if not isinstance(lease, dict):
        return None
    if feature_id is not None and lease.get("feature_id") != feature_id:
        return None
    if package_id is not None and lease.get("package_id") != package_id:
        return None
    return lease


def modification_lease_summary(lease: Mapping[str, object] | None) -> Mapping[str, object] | None:
    """只投递定位当前修改占用所需的身份，不展开基线或恢复正文。"""
    if lease is None:
        return None
    return {
        field: lease.get(field)
        for field in (
            "feature_id", "package_id", "workspace_root", "status",
            "holder_execution_id", "current_candidate_id",
        )
    }


def _write_workspace_state(root: Path, value: Mapping[str, object]) -> None:
    path = _state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".workspace-state-", dir=path.parent))
    temporary = staging / path.name
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temporary, path)
    except Exception as exception:
        raise ArchiveWorkspaceError(
            "WORKSPACE_STATE_COMMIT_FAILED",
            f"工作区状态提交失败：{exception}",
        ) from exception
    finally:
        try:
            staging.rmdir()
        except OSError:
            pass


def _safe_relative_path(workspace_root: Path, relative: str) -> Path:
    normalized_root = workspace_root.expanduser().resolve()
    candidate = (normalized_root / relative).resolve()
    try:
        candidate.relative_to(normalized_root)
    except ValueError as exception:
        raise ArchiveWorkspaceError(
            "WORKSPACE_SCOPE_ESCAPE",
            f"授权写入范围“{relative}”越出工作区。",
        ) from exception
    return candidate


def _content_digest(path: Path) -> str:
    if not path.is_file():
        return "missing"
    try:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exception:
        raise ArchiveWorkspaceError(
            "WORKSPACE_SNAPSHOT_FAILED",
            f"无法读取工作区文件“{path}”：{exception}",
        ) from exception


def _relative_workspace_path(workspace_root: Path, path: Path) -> str:
    normalized_root = workspace_root.expanduser().resolve()
    normalized_path = path.expanduser().resolve()
    try:
        relative = normalized_path.relative_to(normalized_root)
    except ValueError as exception:
        raise ArchiveWorkspaceError(
            "WORKSPACE_SCOPE_ESCAPE",
            f"版本控制变化“{path}”越出工作区。",
        ) from exception
    return relative.as_posix() or "."


def _is_guard_excluded(relative: str) -> bool:
    parts = PurePosixPath(relative).parts
    managed = parts[:2] in ((".scratch", "dloop-v3"), (".scratch", "dloop-history"))
    return managed or _is_untracked_runtime_path(relative)


def _dirty_path_entries(
    workspace_root: Path,
    relative: str,
    state: str,
) -> Mapping[str, str]:
    if _is_guard_excluded(relative):
        return {}
    target = workspace_root if relative == "." else _safe_relative_path(workspace_root, relative)
    entries: Dict[str, str] = {}
    if target.is_dir() and state.startswith("unversioned"):
        for path in sorted(target.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            nested = _relative_workspace_path(workspace_root, path)
            if _is_guard_excluded(nested):
                continue
            entries[nested] = _canonical_digest({"state": state, "content": _content_digest(path)})
        if entries:
            return entries
    entries[relative] = _canonical_digest({"state": state, "content": _content_digest(target)})
    return entries


def _svn_executable(name: str) -> str | None:
    executable = shutil.which(name)
    if executable is not None:
        return executable
    fallback = Path(r"C:\Program Files\SlikSvn\bin") / f"{name}.exe"
    return str(fallback) if fallback.is_file() else None


def _svn_guard_entries(workspace_root: Path) -> Mapping[str, str]:
    executable = _svn_executable("svn")
    if executable is None:
        raise ArchiveWorkspaceError(
            "WORKSPACE_SVN_UNAVAILABLE",
            "当前环境没有可用的 SVN 客户端，不能启动 DLoop 实施切片。",
        )
    working_copy = subprocess.run(
        [executable, "info", "--show-item", "wc-root", str(workspace_root)],
        cwd=workspace_root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    if working_copy.returncode != 0 or not working_copy.stdout.strip():
        raise ArchiveWorkspaceError(
            "WORKSPACE_NOT_SVN",
            "工作区不是可用的 SVN 工作副本，不能启动 DLoop 实施切片。",
        )
    completed = subprocess.run(
        [executable, "status", "--xml", "--depth", "infinity", str(workspace_root)],
        cwd=workspace_root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    if completed.returncode != 0:
        raise ArchiveWorkspaceError(
            "WORKSPACE_SVN_STATUS_UNAVAILABLE",
            "无法取得 SVN 工作区状态，不能形成实施基线。",
        )
    try:
        document = ElementTree.fromstring(completed.stdout)
    except ElementTree.ParseError as exception:
        raise ArchiveWorkspaceError(
            "WORKSPACE_VCS_STATUS_INVALID",
            f"SVN 工作区状态无法解析：{exception}",
        ) from exception
    entries: Dict[str, str] = {}
    for entry in document.findall(".//entry"):
        raw_path = entry.get("path")
        status = entry.find("wc-status")
        if not raw_path or status is None:
            continue
        item = status.get("item", "unknown")
        props = status.get("props", "none")
        if item == "normal" and props in {"none", "normal"}:
            continue
        raw = Path(raw_path)
        target = raw if raw.is_absolute() else workspace_root / raw
        relative = _relative_workspace_path(workspace_root, target)
        state = f"{item}:{props}"
        # 新文件纳管所需的父目录不构成额外产品变化；目录属性仍受保护。
        if target.is_dir() and item == "added" and props in {"none", "normal"}:
            continue
        entries.update(_dirty_path_entries(workspace_root, relative, state))
    return entries


def _svn_revision(workspace_root: Path) -> str:
    executable = _svn_executable("svnversion")
    if executable is not None:
        completed = subprocess.run(
            [executable, str(workspace_root)],
            cwd=workspace_root,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        revision = completed.stdout.strip()
        if completed.returncode == 0 and revision:
            # M 包括已纳管文档变化；产品变化由上面的逐项状态与正文摘要绑定。
            return revision.replace("M", "")
    svn = _svn_executable("svn")
    if svn is None:
        raise ArchiveWorkspaceError(
            "WORKSPACE_SVN_UNAVAILABLE",
            "当前环境没有可用的 SVN 客户端，不能启动 DLoop 实施切片。",
        )
    completed = subprocess.run(
        [svn, "info", "--show-item", "revision", str(workspace_root)],
        cwd=workspace_root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    revision = completed.stdout.strip()
    if completed.returncode != 0 or not revision:
        raise ArchiveWorkspaceError(
            "WORKSPACE_SVN_REVISION_UNAVAILABLE",
            "无法取得 SVN 工作区修订，不能形成可复用验证绑定。",
        )
    return revision


def workspace_vcs(workspace_root: Path) -> str | None:
    """按最近的仓库边界识别类型，Git worktree 的 .git 是文件。"""
    normalized = workspace_root.expanduser().resolve()
    for directory in (normalized, *normalized.parents):
        if (directory / ".git").exists():
            return "git"
        if (directory / ".svn").is_dir():
            return "svn"
    return None


def _run_git(workspace_root: Path, *arguments: str) -> str:
    executable = shutil.which("git")
    if executable is None:
        raise ArchiveWorkspaceError("WORKSPACE_GIT_UNAVAILABLE", "当前环境没有可用的 Git 客户端。")

    completed = subprocess.run(
        [executable, "--no-optional-locks", *arguments],
        cwd=workspace_root, check=False, capture_output=True,
        encoding="utf-8", errors="strict",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    if completed.returncode:
        raise ArchiveWorkspaceError(
            "WORKSPACE_GIT_STATUS_UNAVAILABLE",
            "无法读取 Git 工作区与暂存状态：" + completed.stderr.strip(),
        )
    return completed.stdout


def _assert_git_write_scopes(workspace_root: Path, scopes: Sequence[str]) -> None:
    """授权范围不得跨入子模块，即使其中的产品被忽略或尚未生成。"""
    targets = tuple(_safe_relative_path(workspace_root, scope) for scope in scopes)
    # ls-files 默认返回相对于当前项目目录的路径；索引中的 gitlink 不依赖脏状态。
    for record in _run_git(workspace_root, "ls-files", "--stage", "-z", "--", ".").split("\0"):
        if not record.startswith("160000 "):
            continue
        relative = record.split("\t", 1)[1]
        module = _safe_relative_path(workspace_root, relative)
        if any(target.is_relative_to(module) or module.is_relative_to(target) for target in targets):
            raise ArchiveWorkspaceError(
                "WORKSPACE_GIT_SUBMODULE_SCOPE",
                f"授权写入范围跨入子模块“{relative}”；请在子模块项目内独立运行 DLoop。",
            )


def _git_guard_snapshot(workspace_root: Path) -> Tuple[Mapping[str, str], str]:
    repository = Path(_run_git(workspace_root, "rev-parse", "--show-toplevel").strip()).resolve()
    output = _run_git(
        workspace_root,
        "status", "--porcelain=v2", "--branch", "-z", "--untracked-files=all",
        "--no-renames", "--ignore-submodules=none", "--", ".",
    )
    entries: Dict[str, str] = {}
    revision = ""
    branch = ""
    for record in output.split("\0"):
        if record.startswith("# branch.oid "):
            revision = record.removeprefix("# branch.oid ")
        elif record.startswith("# branch.head "):
            branch = record.removeprefix("# branch.head ")
        elif record.startswith("? "):
            relative = _relative_workspace_path(workspace_root, repository / record[2:])
            entries.update(_dirty_path_entries(workspace_root, relative, "unversioned"))
        elif record.startswith("1 "):
            fields = record.split(" ", 8)
            relative = _relative_workspace_path(workspace_root, repository / fields[8])
            if _is_guard_excluded(relative):
                continue
            if fields[2].startswith("S"):
                raise ArchiveWorkspaceError(
                    "WORKSPACE_GIT_SUBMODULE_CHANGED",
                    f"子模块“{relative}”存在变化；请在子模块项目内独立运行 DLoop。",
                )
            # 状态、模式和暂存对象摘要都参与绑定，正文相同也不能掩盖暂存变化。
            entries.update(_dirty_path_entries(workspace_root, relative, " ".join(fields[:8])))
        elif record.startswith("u "):
            raise ArchiveWorkspaceError("WORKSPACE_GIT_CONFLICT", "Git 工作区存在未解决的合并冲突。")
        elif record and not record.startswith("# "):
            raise ArchiveWorkspaceError("WORKSPACE_VCS_STATUS_INVALID", "Git 工作区状态无法解析。")
    if not revision or not branch:
        raise ArchiveWorkspaceError("WORKSPACE_GIT_REVISION_UNAVAILABLE", "无法取得 Git 当前修订与分支。")
    return entries, revision + ":" + branch


def workspace_guard_snapshot(workspace_root: Path) -> Mapping[str, object]:
    """记录 Git 或 SVN 工作副本中的可观察变化。"""

    normalized_root = workspace_root.expanduser().resolve()
    if not normalized_root.is_dir():
        raise ArchiveWorkspaceError(
            "WORKSPACE_ROOT_INVALID",
            f"工作区“{normalized_root}”不存在或不是目录。",
        )
    source = workspace_vcs(normalized_root)
    if source == "git":
        entries, revision = _git_guard_snapshot(normalized_root)
    elif source == "svn":
        entries = _svn_guard_entries(normalized_root)
        revision = _svn_revision(normalized_root)
    else:
        raise ArchiveWorkspaceError(
            "WORKSPACE_VCS_REQUIRED", "实施工作区必须位于 Git 或 SVN 工作副本中。",
        )
    normalized_entries = {key: entries[key] for key in sorted(entries)}
    return {
        "workspace_root": str(normalized_root),
        "source": source,
        "revision": revision,
        "entries": normalized_entries,
        "digest": _canonical_digest({
            "source": source,
            "revision": revision,
            "entries": normalized_entries,
        }),
    }


def matching_workspace_guard_snapshot(
    workspace_root: Path,
    expected_workspace_root: object,
    expected_digest: object,
) -> Mapping[str, object] | None:
    """返回与固定候选完全一致的当前工作区快照。"""

    if not isinstance(expected_workspace_root, str) or not isinstance(expected_digest, str):
        return None
    normalized_root = workspace_root.expanduser().resolve()
    if normalized_root != Path(expected_workspace_root).expanduser().resolve():
        return None
    current = workspace_guard_snapshot(normalized_root)
    return current if current.get("digest") == expected_digest else None


def _path_in_scopes(path: str, scopes: Sequence[str]) -> bool:
    normalized = path.replace("\\", "/").rstrip("/")
    for scope in scopes:
        candidate = str(scope).replace("\\", "/").rstrip("/")
        if normalized == candidate or normalized.startswith(candidate + "/"):
            return True
    return False


def outside_scope_guard_changes(
    baseline: Mapping[str, object],
    current: Mapping[str, object],
    scopes: Sequence[str],
) -> Tuple[str, ...]:
    before = baseline.get("entries")
    after = current.get("entries")
    if not isinstance(before, dict) or not isinstance(after, dict):
        raise ArchiveWorkspaceError(
            "WORKSPACE_SNAPSHOT_INVALID",
            "全局工作区快照缺少变化摘要。",
        )
    return tuple(
        path
        for path in sorted(set(before) | set(after))
        if not _path_in_scopes(path, scopes) and before.get(path) != after.get(path)
    )


def _is_untracked_runtime_path(relative_path: str) -> bool:
    path = Path(relative_path)
    return (
        "__pycache__" in path.parts
        or path.suffix.lower() == ".pyc"
        or path.name in {WORKSPACE_STATE_NAME, WORKSPACE_LOCK_NAME}
    )


def assert_safe_write_scopes(scopes: Sequence[str], feature_id: str) -> None:
    """拒绝把 DLoop 自有状态或其父目录声明为产品写入范围。"""

    managed_roots = ((".scratch", "dloop-v3"), (".scratch", "dloop-history"))
    unsafe = []
    for scope in sorted(set(scopes)):
        normalized = PurePosixPath(str(scope).replace("\\", "/"))
        parts = tuple(part.casefold() for part in normalized.parts if part != ".")
        if not parts:
            unsafe.append(scope)
            continue
        if parts[-1] in {WORKSPACE_STATE_NAME.casefold(), WORKSPACE_LOCK_NAME.casefold()}:
            unsafe.append(scope)
            continue
        if any(len(parts) <= len(managed_root) and managed_root[:len(parts)] == parts for managed_root in managed_roots):
            unsafe.append(scope)
            continue
        if any(parts[:len(managed_root)] == managed_root for managed_root in managed_roots):
            unsafe.append(scope)
    if unsafe:
        raise ArchiveWorkspaceError(
            "DLOOP_MANAGED_SCOPE_FORBIDDEN",
            "产品写入范围不能包含 DLoop 自有状态或其父目录："
            + "、".join(unsafe)
            + "。UI 模型和采集产物由工作流管理，通过材料与候选绑定校验，不登记为产品修改。",
        )


def _capture_workspace(
    workspace_root: Path,
    scopes: Sequence[str],
    *,
    include_contents: bool,
) -> Mapping[str, object]:
    normalized_root = workspace_root.expanduser().resolve()
    if not normalized_root.is_dir():
        raise ArchiveWorkspaceError(
            "WORKSPACE_ROOT_INVALID",
            f"工作区“{normalized_root}”不存在或不是目录。",
        )
    entries: Dict[str, str] = {}
    contents: Dict[str, Mapping[str, str]] = {}
    for scope in sorted(set(scopes)):
        target = _safe_relative_path(normalized_root, scope)
        if not target.exists():
            continue
        if target.is_symlink():
            raise ArchiveWorkspaceError(
                "WORKSPACE_SCOPE_UNSUPPORTED",
                f"授权写入范围“{scope}”不能是链接。",
            )
        if target.is_file():
            candidates = () if target.suffix.lower() == ".pyc" else (target,)
        elif target.is_dir():
            candidates = tuple(
                path
                for path in sorted(target.rglob("*"))
                if (
                    path.is_file()
                    and not path.is_symlink()
                    and not _is_untracked_runtime_path(
                        path.relative_to(normalized_root).as_posix()
                    )
                )
            )
        else:
            raise ArchiveWorkspaceError(
                "WORKSPACE_SCOPE_UNSUPPORTED",
                f"授权写入范围“{scope}”不是普通文件或目录。",
            )
        for path in candidates:
            relative = path.relative_to(normalized_root).as_posix()
            if relative in entries:
                continue
            try:
                content = path.read_bytes()
                digest = hashlib.sha256(content).hexdigest()
            except OSError as exception:
                raise ArchiveWorkspaceError(
                    "WORKSPACE_SNAPSHOT_FAILED",
                    f"无法读取授权文件“{relative}”：{exception}",
                ) from exception
            entries[relative] = "sha256:" + digest
            if include_contents:
                contents[relative] = {
                    "digest": entries[relative],
                    "content_base64": base64.b64encode(content).decode("ascii"),
                }
    normalized_entries = {key: entries[key] for key in sorted(entries)}
    return {
        "workspace_root": str(normalized_root),
        "scopes": list(sorted(set(scopes))),
        "entries": normalized_entries,
        "digest": _canonical_digest(normalized_entries),
        **({"contents": contents} if include_contents else {}),
    }


def snapshot_workspace(
    workspace_root: Path,
    scopes: Sequence[str],
) -> Mapping[str, object]:
    return _capture_workspace(workspace_root, scopes, include_contents=False)


def workspace_content_snapshot(
    workspace_root: Path,
    scopes: Sequence[str],
) -> Mapping[str, object]:
    """保存授权范围正文，供中断时恢复执行前状态。"""

    digest_snapshot = _capture_workspace(workspace_root, scopes, include_contents=True)
    return {
        "workspace_root": digest_snapshot["workspace_root"],
        "scopes": list(digest_snapshot["scopes"]),
        "entries": digest_snapshot["contents"],
        "digest": digest_snapshot["digest"],
    }


def snapshot_changes(
    baseline: Mapping[str, object],
    current: Mapping[str, object],
) -> Tuple[Mapping[str, object], ...]:
    before = baseline.get("entries")
    after = current.get("entries")
    if not isinstance(before, dict) or not isinstance(after, dict):
        raise ArchiveWorkspaceError(
            "WORKSPACE_SNAPSHOT_INVALID",
            "工作区快照缺少文件摘要。",
        )
    changes = []
    tracked_paths = {
        path
        for path in set(before) | set(after)
        if not _is_untracked_runtime_path(path)
    }
    for path in sorted(tracked_paths):
        previous = before.get(path)
        result = after.get(path)
        if previous == result:
            continue
        if previous is None:
            changes.append({"path": path, "change": "added", "result_digest": result})
        elif result is None:
            changes.append({"path": path, "change": "deleted"})
        else:
            changes.append({"path": path, "change": "modified", "result_digest": result})
    return tuple(changes)


def acquire_modification_lease(
    root: Path,
    feature_id: str,
    package_id: str,
    workspace_root: Path,
    scopes: Sequence[str],
    acquired_at: str,
    *,
    holder_execution_id: str | None = None,
    restored_baseline: Mapping[str, object] | None = None,
) -> Mapping[str, object]:
    with workspace_state_lock(root):
        state = load_workspace_state(root)
        existing = state.get("modification_lease")
        normalized_workspace = workspace_root.expanduser().resolve()
        if isinstance(existing, dict):
            raise ArchiveWorkspaceError(
                "MODIFICATION_LEASE_CONFLICT",
                "当前工作流已有未收敛修改候选；一次只允许一个实施切片持有全局修改租约。",
            )
        if normalized_workspace.is_dir() and workspace_vcs(normalized_workspace) == "git":
            _assert_git_write_scopes(normalized_workspace, scopes)
        baseline_contents = workspace_content_snapshot(normalized_workspace, scopes)
        if restored_baseline:
            previous = restored_baseline["baseline_content_snapshot"]
            # 原范围沿用切片起点；新增范围保留本次授权前正文，移出范围不再撤销。
            entries = {
                path: entry for path, entry in baseline_contents["entries"].items()
                if not _path_in_scopes(path, previous["scopes"])
            }
            entries.update({
                path: entry for path, entry in previous["entries"].items()
                if _path_in_scopes(path, scopes)
            })
            baseline_contents = {
                **baseline_contents, "entries": entries,
                "digest": _canonical_digest({path: entry["digest"] for path, entry in entries.items()}),
            }
        baseline = {
            "workspace_root": baseline_contents["workspace_root"],
            "scopes": baseline_contents["scopes"],
            "entries": {path: entry["digest"] for path, entry in baseline_contents["entries"].items()},
            "digest": baseline_contents["digest"],
        }
        guard = workspace_guard_snapshot(normalized_workspace)
        if restored_baseline:
            previous_guard = restored_baseline["baseline_guard_snapshot"]
            # 只有继续授权的原范围使用旧防护基线，其余路径固定当前现场。
            entries = {
                path: entry for path, entry in guard["entries"].items()
                if not (_path_in_scopes(path, previous["scopes"]) and _path_in_scopes(path, scopes))
            }
            entries.update({
                path: entry for path, entry in previous_guard["entries"].items()
                if _path_in_scopes(path, previous["scopes"]) and _path_in_scopes(path, scopes)
            })
            guard = {
                **guard, "entries": entries,
                "digest": _canonical_digest({
                    "source": guard["source"], "revision": guard["revision"], "entries": entries,
                }),
            }
        from archive_snapshots import register_snapshot_scopes
        register_snapshot_scopes(root, feature_id, normalized_workspace, scopes)
        lease = {
            "workflow_state_schema_version": WORKFLOW_STATE_SCHEMA_VERSION,
            "feature_id": feature_id,
            "package_id": package_id,
            "workspace_root": baseline["workspace_root"],
            "write_scopes": list(baseline["scopes"]),
            "baseline_snapshot": baseline,
            "baseline_digest": baseline["digest"],
            "baseline_content_snapshot": baseline_contents,
            "baseline_guard_snapshot": guard,
            "baseline_guard_digest": guard["digest"],
            "status": "reserved",
            "holder_execution_id": holder_execution_id,
            "current_candidate_id": None,
            "acquired_at": acquired_at,
        }
        state["modification_lease"] = lease
        _write_workspace_state(root, state)
        return lease


def require_modification_lease(
    root: Path,
    feature_id: str,
    package_id: str | None = None,
) -> Tuple[Dict[str, object], Dict[str, object]]:
    state = load_workspace_state(root)
    lease = state.get("modification_lease")
    if (
        not isinstance(lease, dict)
        or lease.get("feature_id") != feature_id
        or (package_id is not None and lease.get("package_id") != package_id)
    ):
        raise ArchiveWorkspaceError(
            "MODIFICATION_LEASE_CONFLICT",
            "当前交付项不持有全局修改租约。",
        )
    return state, lease


def release_modification_lease(
    root: Path,
    feature_id: str,
    package_id: str,
) -> None:
    with workspace_state_lock(root):
        state, _ = require_modification_lease(root, feature_id, package_id)
        state["modification_lease"] = None
        _write_workspace_state(root, state)


def current_workspace_snapshot(lease: Mapping[str, object]) -> Mapping[str, object]:
    root = lease.get("workspace_root")
    scopes = lease.get("write_scopes")
    if not isinstance(root, str) or not isinstance(scopes, list):
        raise ArchiveWorkspaceError(
            "INVALID_WORKSPACE_STATE",
            "修改租约缺少工作区根或授权写入范围。",
        )
    return snapshot_workspace(Path(root), tuple(str(item) for item in scopes))


def current_workspace_guard_snapshot(lease: Mapping[str, object]) -> Mapping[str, object]:
    root = lease.get("workspace_root")
    if not isinstance(root, str):
        raise ArchiveWorkspaceError(
            "INVALID_WORKSPACE_STATE",
            "修改租约缺少工作区根。",
        )
    return workspace_guard_snapshot(Path(root))


def final_execution_blockers(
    root: Path,
    state: Mapping[str, object],
) -> Tuple[Mapping[str, str], ...]:
    execution = state.get("execution")
    if not isinstance(execution, dict):
        return ({"code": "EXECUTION_LEDGER_MISSING", "message": "执行账本不存在。"},)
    slices = execution.get("slices")
    blockers = []
    if not isinstance(slices, dict):
        blockers.append({"code": "EXECUTION_LEDGER_INVALID", "message": "实施切片账本不合法。"})
    elif slices and any(
        not isinstance(record, dict) or record.get("status") not in {"accepted", "abandoned"}
        for record in slices.values()
    ):
        blockers.append({"code": "SLICE_UNRESOLVED", "message": "仍有切片未验收或未明确放弃。"})
    if isinstance(slices, dict):
        pending = pending_plan_slices(execution)
        if pending:
            blockers.append({"code": "SLICE_PLAN_INCOMPLETE", "message": "批准方案仍有未收敛切片：" + "、".join(pending)})
    if active_modification_lease(root) is not None:
        message = "工作区修改租约尚未释放。"
        blockers.append({"code": "MODIFICATION_LEASE_HELD", "message": message})
    return tuple(blockers)
