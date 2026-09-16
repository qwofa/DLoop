"""作业独立 Git 内容快照；历史只追加，恢复以新的执行轮次继续。"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import inspect
from itertools import chain
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
from typing import Mapping

from archive_workspace import ArchiveWorkspaceError, workspace_state_lock


STAGES = ("requirements", "investigation", "design", "plan", "implementation", "validation")
OPERATIONS = {
    "approve_stage": "stage-decision",
    "approve_slice_plan": "plan-approved",
    "record_contract": "contract-recorded",
    "start_execution": "slice-start",
    "record_checkpoint": "checkpoint",
    "supplement_slice_validation": "validation-supplemented",
    "submit_candidate": "candidate-submitted",
    "review_candidate": "candidate-reviewed",
    "retry_slice": "repair-start",
    "release_blocked_slice": "slice-released",
    "release_orphaned_lease": "lease-released",
    "confirm_change_batch": "documents-confirmed",
    "transition_lifecycle": "lifecycle",
    "investigate_ui": "ui-investigated",
    "publish_ui": "ui-published",
    "submit_ui_baseline": "ui-baseline-submitted",
    "sync_svn_changelist": "svn-grouped",
}
_LOCAL = threading.local()


def _error(code: str, message: str):
    return ArchiveWorkspaceError(code, message)


def _json(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _atomic(path: Path, content: bytes) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(content)
    os.replace(temporary, path)


def snapshot_repository(root: Path, feature_id: str) -> Path:
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", feature_id) or len(feature_id) > 64:
        raise _error("INVALID_FEATURE_ID", "保存点必须属于合法交付项。")
    return root.resolve().parent / "snapshots" / (feature_id + ".git")


def _git(repository: Path, *arguments: str, data: bytes | None = None) -> bytes:
    executable = shutil.which("git")
    if executable is None:
        raise _error("SNAPSHOT_GIT_REQUIRED", "中间态保存需要 Git 客户端；请安装 Git 后重试，SVN 项目也需要。")
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_OPTIONAL_LOCKS="0")
    result = subprocess.run(
        [executable, "--git-dir=" + str(repository), *arguments], input=data,
        capture_output=True, env=env,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    if result.returncode:
        raise _error("SNAPSHOT_GIT_FAILED", "快照保存或读取失败：" + result.stderr.decode("utf-8", "replace").strip())
    return result.stdout


def _ensure_repository(repository: Path) -> None:
    if not (repository / "HEAD").exists():
        repository.parent.mkdir(parents=True, exist_ok=True)
        _git(repository, "init", "--bare", "--initial-branch=history", "--object-format=sha1", "--quiet", str(repository))


def _head(repository: Path) -> str | None:
    # 只由本模块写入的直接引用，读取时不创建仓库或刷新源项目索引。
    path = repository / "refs/heads/history"
    if path.is_file():
        return path.read_text(encoding="ascii").strip()
    if (repository / "packed-refs").is_file():
        for line in (repository / "packed-refs").read_text(encoding="ascii").splitlines():
            if line.endswith(" refs/heads/history"):
                return line.split()[0]
    return None


def _manifest(repository: Path, commit: str) -> dict:
    return json.loads(_git(repository, "show", commit + ":snapshot.json"))


def _latest(repository: Path) -> tuple[str | None, dict]:
    commit = _head(repository)
    return commit, _manifest(repository, commit) if commit else {}


def _blob(content: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(content)).encode("ascii") + b"\0" + content).hexdigest()


def _retained(path: Path) -> bool:
    return not (
        {".git", ".svn", "__pycache__"}.intersection(path.parts)
        or path.parts[0] in {"Library", "Temp", "obj"}
        or path.suffix.lower() in {".pyc", ".tmp"}
        or any(part.startswith((".workflow-state-", ".workspace-restore-")) for part in path.parts)
    )


def _files(base: Path, scopes) -> dict[str, bytes]:
    base = base.resolve()
    files = {}
    for scope in scopes:
        target = (base / scope).resolve()
        if not target.is_relative_to(base.resolve()) or target.is_symlink():
            raise _error("SNAPSHOT_SCOPE_INVALID", "保存范围必须是项目内普通文件或目录。")
        paths = [target] if target.is_file() else target.rglob("*") if target.is_dir() else []
        for path in paths:
            relative = path.relative_to(base).as_posix()
            if path.is_file() and not path.is_symlink() and _retained(Path(relative)):
                files[relative] = path.read_bytes()
    return files


def _in_scopes(path: str, scopes) -> bool:
    return any(path == scope or path.startswith(scope.rstrip("/") + "/") for scope in scopes)


def _touches_scopes(path: str, scopes) -> bool:
    return _in_scopes(path, scopes) or any(scope.startswith(path + "/") for scope in scopes)


def _svn_scopes(root: Path, feature_id: str, workspace: Path, scopes) -> list:
    feature = (root / feature_id).resolve()
    return list(scopes) + ([feature.relative_to(workspace).as_posix()] if feature.is_relative_to(workspace) else [])


def _state(root: Path, feature_id: str) -> dict:
    path = root / feature_id / "workflow-state.json"
    if not path.is_file():
        raise _error("UNKNOWN_FEATURE", "交付项不存在，不能保存中间态。")
    return json.loads(path.read_bytes())


def _capture_inputs(raw: dict[str, bytes]) -> dict[str, str | None]:
    model = raw.get("archive/ui-model.json")
    inputs = {}
    if model is None:
        return inputs
    for evidence in json.loads(model).get("evidence", []):
        if evidence.get("kind") != "source_document":
            continue
        source = Path(evidence["path"])
        source_path = source.as_posix()
        if source_path in inputs:
            continue
        inputs[source_path] = None
        if source.is_file():
            identity = hashlib.sha256(source_path.encode("utf-8")).hexdigest()
            path = "inputs/" + identity + "/" + source.name
            raw[path] = source.read_bytes()
            inputs[source_path] = path
    return inputs


def _capture(root: Path, feature_id: str, previous: dict, workspace: Path | None, scopes) -> tuple[dict, dict]:
    from archive_workspace import workspace_guard_snapshot, workspace_vcs, load_workspace_state
    state = _state(root, feature_id)
    old_scopes = previous.get("scopes", [])
    scopes = sorted(set(old_scopes) | set(scopes))
    workspace = workspace or (Path(previous["workspace"]) if previous.get("workspace") else None)
    if workspace and previous.get("workspace") and str(workspace.resolve()) != previous["workspace"]:
        raise _error("SNAPSHOT_WORKSPACE_MISMATCH", "同一作业的保存点不能切换实施工作区。")
    raw = {"archive/" + path: content for path, content in _files(root / feature_id, ["."]).items()}
    # 已登记来源只作为输入正文保存与导出，不加入可覆盖的档案或实施范围。
    inputs = _capture_inputs(raw)
    initial = dict(previous.get("initial", {}))
    source = previous.get("source")
    svn = previous.get("svn", {})
    initial_svn = dict(previous.get("initial_svn", {}))
    if workspace is not None:
        workspace = workspace.resolve()
        products = _files(workspace, scopes)
        guard = workspace_guard_snapshot(workspace)
        source = {key: guard[key] for key in ("workspace_root", "source", "revision")}
        for path, content in products.items():
            raw["workspace/" + path] = content
            if not _in_scopes(path, old_scopes):
                initial[path] = _blob(content)
                raw["initial/" + path] = content
        # 基线对象即使后来文件被删除也保持可达。
        if workspace_vcs(workspace) == "svn":
            from archive_snapshot_svn import capture_svn
            svn = capture_svn(workspace, _svn_scopes(root, feature_id, workspace, scopes))
            for path, item in svn.items():
                if path not in initial_svn and _touches_scopes(path, scopes) and not _touches_scopes(path, old_scopes):
                    initial_svn[path] = item
    entries = {path: _blob(content) for path, content in raw.items()}
    for path, oid in initial.items():
        entries["initial/" + path] = oid
    value = {
        "version": "3.8.1", "feature_id": feature_id,
        "workspace": str(workspace) if workspace else None,
        "scopes": scopes, "initial": initial, "source": source,
        "initial_source": previous.get("initial_source") or source,
        "svn": svn, "initial_svn": initial_svn, "files": entries, "inputs": inputs,
        "workflow": state,
        "lease": load_workspace_state(root).get("modification_lease"),
    }
    if value["lease"] and value["lease"]["feature_id"] != feature_id:
        value["lease"] = None
    return value, raw


def _commit(repository: Path, value: dict, raw: dict, parent: str | None) -> str:
    entries = dict(value["files"])
    payload = _json(value)
    entries["snapshot.json"] = _blob(payload)
    raw = {**raw, "snapshot.json": payload}
    timestamp = int(datetime.now(timezone.utc).timestamp())
    stream = bytearray(
        f"commit refs/heads/history\ncommitter DLoop <dloop@localhost> {timestamp} +0000\ndata 16\nDLoop checkpoint\n".encode("ascii")
    )
    if parent:
        stream.extend(f"from {parent}\n".encode("ascii"))
    stream.extend(b"deleteall\n")
    for path, oid in sorted(entries.items()):
        quoted = json.dumps(path, ensure_ascii=False).encode("utf-8")
        if path in raw:
            content = raw[path]
            stream.extend(b"M 100644 inline " + quoted + b"\ndata " + str(len(content)).encode("ascii") + b"\n" + content + b"\n")
        else:
            stream.extend(b"M 100644 " + oid.encode("ascii") + b" " + quoted + b"\n")
    stream.extend(b"\ndone\n")
    _git(repository, "fast-import", "--quiet", "--done", data=bytes(stream))
    return str(_head(repository))


def _summary(commit: str, value: dict) -> dict:
    return {key: value.get(key) for key in ("id", "created_at", "reason", "name", "stage", "source_snapshot", "outcome")} | {"commit": commit}


def _save(root: Path, feature_id: str, reason: str, *, name: str = "", stage: str | None = None,
          request_id: str | None = None, workspace: Path | None = None, scopes=(),
          source_snapshot: str | None = None, outcome: object = None, reuse_unchanged: bool = False) -> dict:
    repository = snapshot_repository(root, feature_id)
    _ensure_repository(repository)
    parent, previous = _latest(repository)
    value, raw = _capture(root, feature_id, previous, workspace, scopes)
    signature = hashlib.sha256(_json(value)).hexdigest()
    if previous.get("signature") == signature and (
        reuse_unchanged or (
            previous.get("reason") == reason and previous.get("stage") == stage and previous.get("name") == name
            and (not request_id or previous.get("request_id") == request_id)
        )
    ):
        return {"status": "saved", "snapshot": _summary(str(parent), previous), "idempotent": True}
    if request_id:
        found = _find_request(repository, request_id)
        if found:
            commit, existing = found
            if existing["signature"] != signature or (existing["reason"], existing.get("stage"), existing["name"]) != (reason, stage, name):
                raise _error("SNAPSHOT_REQUEST_CHANGED", "相同保存请求的内容已变化，请使用新的请求标识。")
            return {"status": "saved", "snapshot": _summary(commit, existing), "idempotent": True}
    value.update(
        id=f"s{int(previous.get('id', 's000000')[1:]) + 1:06d}",
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        reason=reason, name=name, stage=stage, request_id=request_id,
        signature=signature, source_snapshot=source_snapshot, outcome=outcome,
    )
    raw = {path: content for path, content in raw.items() if previous.get("files", {}).get(path) != value["files"].get(path)}
    commit = _commit(repository, value, raw, parent)
    return {"status": "saved", "snapshot": _summary(commit, value), "idempotent": False}


def _history(repository: Path):
    head = _head(repository)
    if head:
        for commit in _git(repository, "rev-list", head).decode("ascii").splitlines():
            yield commit, _manifest(repository, commit)


def _find_request(repository: Path, request_id: str):
    return next(((commit, value) for commit, value in _history(repository) if value.get("request_id") == request_id), None)


def _target(repository: Path, snapshot_id: str | None, stage: str | None = None):
    for commit, value in _history(repository):
        if (snapshot_id and value["id"] == snapshot_id) or (
            stage and value.get("stage") == stage and value["reason"] == "stage-start"
        ):
            if value.get("version") != "3.8.1":
                raise _error("SNAPSHOT_VERSION_MISMATCH", "只能恢复当前版本创建的保存点。")
            return commit, value
    raise _error("SNAPSHOT_NOT_FOUND", "找不到指定保存点或该阶段的开始保存点；请先查看保存点列表。")


def _assert_idle(root: Path, feature_id: str) -> None:
    from archive_workspace import load_workspace_state
    lease = load_workspace_state(root).get("modification_lease")
    if lease and lease["feature_id"] != feature_id:
        raise _error("MODIFICATION_LEASE_CONFLICT", "其他作业正在修改工作区，必须先收口该执行。")


def _writable(root: Path, feature_id: str) -> None:
    manifest = json.loads((root / feature_id / "feature.json").read_bytes())
    if manifest["lifecycle"] in {"frozen", "pending_cleanup"}:
        raise _error("READ_ONLY_ARCHIVE", "已冻结作业仅可查看、导出或创建新的作业，不原地恢复。")


def save_snapshot(root: Path, feature_id: str, reason: str = "manual", *, name: str = "",
                  stage: str | None = None, request_id: str | None = None) -> Mapping[str, object]:
    with workspace_state_lock(root):
        _state(root, feature_id)
        _writable(root, feature_id)
        _assert_idle(root, feature_id)
        recover_snapshots(root, feature_id)
        if stage is not None and stage not in STAGES:
            raise _error("INVALID_SNAPSHOT_STAGE", "请选择现有交付阶段。")
        if reason == "stage-start" and stage is None:
            raise _error("INVALID_SNAPSHOT_STAGE", "阶段开始保存点必须指定阶段。")
        return _save(root, feature_id, reason, name=name, stage=stage, request_id=request_id)


def snapshot_status(root: Path, feature_id: str) -> dict:
    repository = snapshot_repository(root, feature_id)
    head, value = _latest(repository)
    return {
        "count": int(value.get("id", "s000000")[1:]),
        "latest": _summary(str(head), value) if head else None,
        "recovery_required": (repository / "restore.json").exists() or (repository / "pending.json").exists(),
        "repository": str(repository),
        "bytes": sum(path.stat().st_size for path in repository.rglob("*") if path.is_file()) if repository.exists() else 0,
    }


def require_snapshot_recovered(root: Path, feature_id: str) -> None:
    repository = snapshot_repository(root, feature_id)
    if (repository / "pending.json").exists() or (repository / "restore.json").exists():
        raise _error("SNAPSHOT_RECOVERY_REQUIRED", "中间态尚未保存或恢复完成，请先运行 snapshot-recover，再继续交接或实施。")


def list_snapshots(root: Path, feature_id: str) -> Mapping[str, object]:
    with workspace_state_lock(root):
        repository = snapshot_repository(root, feature_id)
        return {"status": "ready", **snapshot_status(root, feature_id),
                "snapshots": [_summary(commit, value) for commit, value in _history(repository)]}


def _read_files(repository: Path, entries: dict) -> dict[str, bytes]:
    if not entries:
        return {}
    oids = list(dict.fromkeys(entries.values()))
    data = _git(repository, "cat-file", "--batch", data=("\n".join(oids) + "\n").encode("ascii"))
    objects = {}
    offset = 0
    for oid in oids:
        end = data.index(b"\n", offset)
        header = data[offset:end].split()
        if len(header) != 3 or header[1] != b"blob":
            raise _error("SNAPSHOT_CONTENT_MISSING", "保存点缺少文件正文，不能恢复。")
        size = int(header[2])
        content = data[end + 1:end + 1 + size]
        if _blob(content) != oid:
            raise _error("SNAPSHOT_CONTENT_INVALID", "保存点文件内容校验失败。")
        objects[oid] = content
        offset = end + size + 2
    return {path: objects[oid] for path, oid in entries.items()}


def export_snapshot(root: Path, feature_id: str, snapshot_id: str, destination: Path) -> Mapping[str, object]:
    with workspace_state_lock(root):
        repository = snapshot_repository(root, feature_id)
        commit, value = _target(repository, snapshot_id)
        destination = destination.resolve()
        if destination.exists():
            raise _error("SNAPSHOT_EXPORT_EXISTS", "导出目录已经存在，请指定一个新目录。")
        if destination.is_relative_to(root.resolve()) or destination.is_relative_to(repository.parent):
            raise _error("SNAPSHOT_EXPORT_SCOPE", "导出目录必须与正式档案和快照库分开。")
        files = _read_files(repository, {path: oid for path, oid in value["files"].items() if not path.startswith("initial/")})
        destination.mkdir(parents=True)
        for path, content in files.items():
            target = destination / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        (destination / "snapshot.json").write_bytes(_json(value))
        return {"status": "exported", "path": str(destination), "snapshot": _summary(commit, value)}


def register_snapshot_scopes(root: Path, feature_id: str, workspace: Path, scopes) -> None:
    # 只对正式作业接入；底层租约也用于不带档案的独立范围校验。
    if not (root / feature_id / "workflow-state.json").is_file():
        return
    _save(root, feature_id, "scope-baseline", workspace=workspace, scopes=scopes)


def initialize_snapshots(root: Path, feature_id: str) -> None:
    with workspace_state_lock(root):
        repository = snapshot_repository(root, feature_id)
        recover_snapshots(root, feature_id)
        if _head(repository):
            return
        _ensure_repository(repository)
        _atomic(repository / "pending.json", _json({"reason": "stage-start", "stage": "requirements"}))
        _save(root, feature_id, "stage-start", stage="requirements")
        (repository / "pending.json").unlink()


@contextmanager
def snapshot_operation(function, root: Path, args: tuple, kwargs: dict):
    reason = OPERATIONS.get(function.__name__)
    if not reason or getattr(_LOCAL, "operation", False):
        yield
        return
    for journal in (root.resolve().parent / "snapshots").glob("*.git/restore.json"):
        recover_snapshots(root, journal.parent.name.removesuffix(".git"))
    bound = inspect.signature(function).bind(root, *args, **kwargs).arguments
    if reason == "svn-grouped":
        from archive_workspace import workspace_vcs
        if workspace_vcs(bound["project"]) != "svn":
            yield
            return
    feature_id = bound.get("feature_id")
    if function.__name__ == "confirm_change_batch":
        ids = bound.get("document_ids", ())
        features = {item.split(".", 1)[0] for item in ids}
        if len(features) != 1:
            # 多交付项确认分别保存，各自不混入其他作业内容。
            with _multiple_operations(root, sorted(features), reason):
                yield
            return
        feature_id = next(iter(features))
    if not isinstance(feature_id, str):
        yield
        return
    with _multiple_operations(root, [feature_id], reason, bound):
        yield


@contextmanager
def _multiple_operations(root: Path, features: list, reason: str, bound: dict | None = None):
    _LOCAL.operation = True
    repositories = []
    completed = False
    stage = (bound or {}).get("stage")
    stage = {"architecture": "design", "ui-baseline": "plan", "final": "validation"}.get(stage, stage)
    if reason in {"slice-start", "checkpoint", "candidate-submitted", "candidate-reviewed", "repair-start", "slice-released"}:
        stage = "implementation"
    elif reason in {"plan-approved", "ui-published", "ui-baseline-submitted"}:
        stage = "plan"
    elif reason == "ui-investigated":
        stage = "investigation"
    workspace = (bound or {}).get("project") if reason == "svn-grouped" else None
    pending = {"reason": reason, "stage": stage, "workspace": str(workspace) if workspace else None}
    try:
        for feature_id in features:
            repository = snapshot_repository(root, feature_id)
            # 在创建作业或任何正式写入之前检查 Git 可用性。
            if shutil.which("git") is None:
                raise _error("SNAPSHOT_GIT_REQUIRED", "中间态保存需要安装 Git 客户端。")
            if (root / feature_id / "workflow-state.json").is_file():
                recover_snapshots(root, feature_id)
                if reason in {"slice-released", "lease-released", "ui-investigated", "ui-published", "repair-start"} or (
                    reason == "candidate-submitted" and (bound or {}).get("status") == "interrupted"
                ):
                    _save(root, feature_id, "before-" + reason, reuse_unchanged=True)
                _ensure_repository(repository)
                _atomic(repository / "pending.json", _json(pending))
                repositories.append(repository)
        yield
        completed = True
        for feature_id in features:
            if (root / feature_id / "workflow-state.json").is_file():
                repository = snapshot_repository(root, feature_id)
                _ensure_repository(repository)
                if not (repository / "pending.json").exists():
                    _atomic(repository / "pending.json", _json(pending))
                outcome = (bound or {}).get("decision") or (bound or {}).get("target_lifecycle")
                if reason in {"checkpoint", "candidate-reviewed"}:
                    records = _state(root, feature_id)["execution"]["slices"]
                    record = records.get((bound or {}).get("package_id"))
                    if record is None:
                        record = next((item for item in records.values() if item.get("execution_id") == (bound or {}).get("execution_id")), {})
                    outcome = record.get("checkpoints", [])[-1].get("validation_results") if reason == "checkpoint" and record.get("checkpoints") else (bound or {}).get("result")
                _save(root, feature_id, reason, stage=stage, workspace=workspace, outcome=outcome,
                      reuse_unchanged=reason in {"contract-recorded", "checkpoint"})
                (snapshot_repository(root, feature_id) / "pending.json").unlink(missing_ok=True)
    except Exception:
        # 未完成的登记留在库外层；下一次写入前必须保存现有现场。
        if not completed:
            for repository in repositories:
                (repository / "pending.json").unlink(missing_ok=True)
        raise
    finally:
        _LOCAL.operation = False


def _restore_entries(target: dict, latest: dict) -> dict:
    entries = {path: oid for path, oid in target["files"].items() if path.startswith(("archive/", "workspace/"))}
    for path, oid in latest.get("initial", {}).items():
        if not _in_scopes(path, target["scopes"]):
            entries["workspace/" + path] = oid
    return entries


def _assert_source(target: dict, latest: dict) -> None:
    from archive_workspace import workspace_guard_snapshot
    source = target.get("source") or latest.get("source")
    if source:
        current = workspace_guard_snapshot(Path(source["workspace_root"]))
        # 后续保存可记录新修订，但不能改变累计恢复范围的首次基准。
        if any(current[key] != baseline[key]
               for baseline in (source, latest["initial_source"]) if baseline
               for key in ("workspace_root", "source", "revision")):
            raise _error("SNAPSHOT_SOURCE_CHANGED", "源仓库修订或分支已变化；请导出保存点并创建新的变更作业，不能直接覆盖。")


def _apply_files(root: Path, feature_id: str, workspace: Path | None, scopes, files: dict) -> None:
    destinations = {"archive/": root / feature_id}
    if workspace:
        destinations["workspace/"] = workspace
    for prefix, base in destinations.items():
        desired = {path.removeprefix(prefix): content for path, content in files.items() if path.startswith(prefix)}
        current = _files(base, ["."] if prefix == "archive/" else scopes)
        for path in sorted(set(current) - set(desired), reverse=True):
            (base / path).unlink()
        for path, content in desired.items():
            target = base / path
            if target.is_dir():
                for directory in sorted(target.rglob("*"), reverse=True):
                    if directory.is_dir():
                        directory.rmdir()
                target.rmdir()
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.is_file() or target.read_bytes() != content:
                _atomic(target, content)
        if any((base / path).read_bytes() != content for path, content in desired.items()):
            raise _error("SNAPSHOT_RESTORE_MISMATCH", "恢复后的文件与保存点内容不一致。")


def _restore_payload(root: Path, feature_id: str, value: dict, latest: dict, files: dict) -> None:
    workspace = Path(latest["workspace"]) if latest.get("workspace") else None
    if workspace and latest.get("source", {}).get("source") == "svn":
        from archive_snapshot_svn import restore_svn
        svn = dict(value.get("svn", {}))
        for path, item in latest.get("initial_svn", {}).items():
            if not _touches_scopes(path, value["scopes"]):
                svn[path] = item
        restore_svn(workspace, _svn_scopes(root, feature_id, workspace, latest["scopes"]), svn,
                    lambda: _apply_files(root, feature_id, workspace, latest["scopes"], files))
    else:
        _apply_files(root, feature_id, workspace, latest["scopes"], files)


def recover_snapshots(root: Path, feature_id: str) -> Mapping[str, object]:
    from archive_workspace import _state_path as lease_path
    with workspace_state_lock(root):
        repository = snapshot_repository(root, feature_id)
        journal = repository / "restore.json"
        if journal.is_file():
            _assert_idle(root, feature_id)
            recovery = json.loads(journal.read_bytes())
            _, value = _target(repository, recovery["backup"])
            _, latest = _latest(repository)
            files = _read_files(repository, _restore_entries(value, latest))
            _restore_payload(root, feature_id, value, latest, files)
            _atomic(lease_path(root), _json(recovery["lease"]))
            from archive_validation import rebuild_indexes, validate_archive_root
            rebuild_indexes(validate_archive_root(root))
            _save(root, feature_id, "restore-recovered", source_snapshot=recovery["backup"])
            journal.unlink()
        pending = repository / "pending.json"
        if pending.is_file():
            value = json.loads(pending.read_bytes())
            workspace = Path(value["workspace"]) if value.get("workspace") else None
            _save(root, feature_id, value["reason"], stage=value.get("stage"), workspace=workspace, outcome="recovered")
            pending.unlink()
        return {"status": "recovered", "feature_id": feature_id}


def restore_snapshot(root: Path, feature_id: str, snapshot_id: str | None = None, *,
                     stage: str | None = None, execute: bool = False) -> Mapping[str, object]:
    from archive_approvals import _write_state, _state_path
    from archive_slice_contract import upsert_contract_record
    from archive_workspace import load_workspace_state, _write_workspace_state
    with workspace_state_lock(root):
        _writable(root, feature_id)
        _assert_idle(root, feature_id)
        repository = snapshot_repository(root, feature_id)
        if (repository / "restore.json").exists() or (repository / "pending.json").exists():
            if not execute:
                raise _error("SNAPSHOT_RECOVERY_REQUIRED", "存在未完成的保存或恢复，请先运行 snapshot-recover。")
            recover_snapshots(root, feature_id)
        commit, target = _target(repository, snapshot_id, stage)
        _, latest = _latest(repository)
        _assert_source(target, latest)
        lease = load_workspace_state(root).get("modification_lease")
        if lease:
            from archive_workspace import workspace_guard_snapshot, outside_scope_guard_changes
            guard = workspace_guard_snapshot(Path(lease["workspace_root"]))
            outside = outside_scope_guard_changes(lease["baseline_guard_snapshot"], guard, lease["write_scopes"])
            if outside:
                raise _error("SNAPSHOT_OUTSIDE_SCOPE_CHANGED", "授权范围外仍有变化，须先处理原工作区阻断，不能通过恢复把它们纳入新基线：" + "、".join(outside))
        state = _state(root, feature_id)
        after = deepcopy(target["workflow"])
        after["approvals"]["final"] = None
        # 连续恢复可能把拒绝移出当前状态；同一材料沿用最近的真实决定。
        # 只查询正式决定保存点，恢复记录本身不构成用户重新批准。
        unresolved = {key for key, value in after["approvals"].items() if value}
        decisions = (value["workflow"] for _, value in _history(repository)
                     if value["reason"] == "stage-decision")
        for decided in chain((state,), decisions):
            for approval_stage in tuple(unresolved):
                current = decided["approvals"].get(approval_stage)
                earlier = after["approvals"][approval_stage]
                if (
                    current
                    and current.get("snapshot") == earlier.get("snapshot")
                    and current.get("reviewed_digest") == earlier.get("reviewed_digest")
                ):
                    after["approvals"][approval_stage] = deepcopy(current)
                    unresolved.remove(approval_stage)
            if not unresolved:
                break
        before_execution = state["execution"]
        execution = after["execution"]
        affected = sorted(key for key, value in before_execution["slices"].items()
                          if value != execution["slices"].get(key))
        for package_id, record in list(execution["slices"].items()):
            if record["status"] in {"active", "candidate", "review_failed", "failed", "interrupted", "circuit_open"}:
                fresh = {}
                new_record, _, _ = upsert_contract_record(fresh, record["package"], datetime.now(timezone.utc).isoformat(), evaluate=True)
                lease = target.get("lease")
                if lease and lease["package_id"] == package_id:
                    new_record["restored_baseline"] = {
                        key: deepcopy(lease[key]) for key in (
                            "baseline_snapshot", "baseline_digest", "baseline_content_snapshot",
                            "baseline_guard_snapshot", "baseline_guard_digest",
                        )
                    }
                execution["slices"][package_id] = new_record
                if package_id not in affected:
                    affected.append(package_id)
        execution["used_execution_ids"] = list(dict.fromkeys(before_execution["used_execution_ids"] + execution["used_execution_ids"]))
        history = list(state.get("restorations", []))
        history.append({"source_snapshot": target["id"], "round": len(history) + 1, "affected_slices": sorted(affected)})
        after["restorations"] = history
        entries = _restore_entries(target, latest)
        files = _read_files(repository, entries)
        files["archive/workflow-state.json"] = _json(after)
        entries["archive/workflow-state.json"] = _blob(files["archive/workflow-state.json"])
        current, _ = _capture(root, feature_id, latest, None, ())
        changed = sorted(path for path in set(entries) | set(current["files"])
                         if path.startswith(("archive/", "workspace/")) and entries.get(path) != current["files"].get(path))
        result = {"status": "preview", "snapshot": _summary(commit, target), "changed_files": changed,
                  "affected_slices": sorted(affected), "new_round": len(history), "validation": "required"}
        if not execute:
            return result
        backup = _save(root, feature_id, "before-restore")["snapshot"]["id"]
        _atomic(repository / "restore.json", _json({"backup": backup, "lease": load_workspace_state(root)}))
        try:
            _restore_payload(root, feature_id, target, latest, files)
            # 恢复文件中的旧租约从不生效，当前状态使用全新身份再启动。
            workspace_state = load_workspace_state(root)
            workspace_state["modification_lease"] = None
            _write_workspace_state(root, workspace_state)
            from archive_workspace import snapshot_workspace, workspace_guard_snapshot
            if latest.get("workspace"):
                workspace = Path(latest["workspace"])
                guard = workspace_guard_snapshot(workspace)
                for record in after["execution"]["slices"].values():
                    if record.get("restored_baseline") and record["status"] == "ready":
                        record["restore_binding"] = {
                            "scopes": list(record["package"]["write_scope"]),
                            "workspace_digest": snapshot_workspace(workspace, record["package"]["write_scope"])["digest"],
                            "guard_digest": guard["digest"],
                        }
            _write_state(_state_path(root / feature_id), after)
            from archive_validation import rebuild_indexes, validate_archive_root
            rebuild_indexes(validate_archive_root(root))
            saved = _save(root, feature_id, "restored", source_snapshot=target["id"], stage=stage)
            (repository / "restore.json").unlink()
        except Exception:
            recover_snapshots(root, feature_id)
            raise
        return {**result, "status": "restored", "backup_snapshot": backup, "saved": saved["snapshot"]}


def fork_snapshot(root: Path, source_feature_id: str, snapshot_id: str, feature_id: str, title: str) -> Mapping[str, object]:
    from archive_initialization import initialize_archive
    with workspace_state_lock(root):
        snapshot_repository(root, feature_id)
        if (root / feature_id).exists():
            raise _error("SNAPSHOT_FORK_EXISTS", "新作业标识已存在，请使用新的标识。")
        destination = root.parent / "snapshot-exports" / feature_id
        exported = export_snapshot(root, source_feature_id, snapshot_id, destination)
        result = initialize_archive(root, feature_id, title)
        origin = {"feature_id": source_feature_id, "snapshot_id": snapshot_id, "export": exported["path"]}
        (root / feature_id / "snapshot-origin.json").write_bytes(_json(origin))
        _save(root, feature_id, "forked", source_snapshot=source_feature_id + "/" + snapshot_id)
        return {**result, "origin": origin, "next_action": "从导出材料重新登记目标、范围和批准后实施；源作业保持不变。"}
