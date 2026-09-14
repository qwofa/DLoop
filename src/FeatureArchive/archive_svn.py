"""按当前交付项的明确产物清单维护本地 SVN 提交组，不提交服务器。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
from typing import Mapping
import xml.etree.ElementTree as ET

from archive_paths import canonical_archive_root, canonical_share_root
from archive_failure_attribution import friction_log_path
from archive_slice_contract import reviewed_candidate
from archive_workspace import (
    ArchiveWorkspaceError, _svn_executable, active_modification_lease,
    current_workspace_snapshot, snapshot_changes, serialized_workflow_state, workspace_vcs,
)


RECORD_NAME = "svn-changelist.json"


def _run(project: Path, *args: str) -> str:
    executable = _svn_executable("svn")
    if executable is None:
        raise ArchiveWorkspaceError("SVN_CHANGELIST_UNAVAILABLE", "没有可用的 SVN 客户端，无法整理提交组。")
    result = subprocess.run(
        [executable, *args], cwd=project, capture_output=True,
        text=True, encoding="utf-8", errors="replace", check=False,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    if result.returncode:
        raise ArchiveWorkspaceError(
            "SVN_CHANGELIST_FAILED", f"SVN 提交组整理失败：{result.stderr.strip()}；修复后重新运行 sync-svn-changelist。",
        )
    return result.stdout


def _status(project: Path) -> dict[str, dict[str, str]]:
    document = ET.fromstring(_run(project, "status", "--xml", "--no-ignore", str(project) + "@"))
    entries = {}
    for parent in document:
        group = parent.get("name", "") if parent.tag == "changelist" else ""
        for entry in parent.findall("entry"):
            path = Path(entry.attrib["path"])
            path = path if path.is_absolute() else project / path
            relative = path.resolve().relative_to(project).as_posix()
            status = entry.find("wc-status")
            if status is not None:
                entries[relative] = {**status.attrib, "changelist": group}
    return entries


def _retained(path: Path) -> bool:
    return not (
        "__pycache__" in path.parts or path.suffix.lower() in {".pyc", ".tmp", ".lock"}
        or path.name == ".feature-archive-workspace-state.json"
    )


def _collect(project: Path, root: Path, feature_id: str) -> tuple[set[str], set[str]]:
    feature = root / feature_id
    files = set()
    for directory in (feature, canonical_share_root(project) / feature_id):
        if directory.exists():
            files.update(
                path.relative_to(project).as_posix() for path in directory.rglob("*")
                if path.is_file() and _retained(path)
            )
    # 根清单及共享索引也是交付产物；锁和恢复缓存不进入版本管理。
    files.update(path.relative_to(project).as_posix() for path in root.iterdir() if path.is_file() and _retained(path))
    friction = friction_log_path(root)
    if friction.is_file():
        files.add(friction.relative_to(project).as_posix())
    state = json.loads((feature / "workflow-state.json").read_text(encoding="utf-8"))
    for record in state["execution"]["slices"].values():
        if record.get("status") in {"interrupted", "abandoned", "released"}:
            continue
        candidate = reviewed_candidate(record) if record.get("status") == "accepted" else record.get("candidate")
        if isinstance(candidate, dict):
            files.update(item["path"] for item in candidate.get("changes", []))
    mixed = set()
    lease = active_modification_lease(root, feature_id)
    if lease is not None:
        if Path(str(lease["workspace_root"])).resolve() != project:
            raise ArchiveWorkspaceError("SVN_CHANGELIST_WORKSPACE_MISMATCH", "提交组必须在当前交付项的实施工作副本中整理。")
        changed = {item["path"] for item in snapshot_changes(lease["baseline_snapshot"], current_workspace_snapshot(lease))}
        files.update(changed)
    # 只补齐文件自身的元数据；新目录的元数据由授权范围内的实际差异纳入。
    for relative in tuple(files):
        meta = Path(str(project / relative) + ".meta")
        if meta.is_file():
            files.add(meta.relative_to(project).as_posix())
    if lease is not None:
        mixed.update(files.intersection(lease["baseline_guard_snapshot"]["entries"]))
    return {relative for relative in files if _retained(Path(relative))}, mixed


@serialized_workflow_state
def sync_svn_changelist(root: Path, feature_id: str, project: Path) -> Mapping[str, object]:
    project = project.resolve()
    if workspace_vcs(project) != "svn":
        return {"status": "not-applicable", "reason": "项目未使用 SVN", "committed": False}
    if root.resolve() != canonical_archive_root(project):
        raise ArchiveWorkspaceError("SVN_CHANGELIST_ROOT_MISMATCH", "只能整理当前项目规范档案中的产物。")
    _run(project, "info", "--show-item", "wc-root", str(project) + "@")
    feature = root / feature_id
    manifest = json.loads((feature / "feature.json").read_text(encoding="utf-8"))
    files, mixed = _collect(project, root, feature_id)
    before = _status(project)
    record_path = feature / RECORD_NAME
    if record_path.exists():
        record = json.loads(record_path.read_text(encoding="utf-8"))
        name = record["name"]
        mixed.update(record["pre_existing_changes"])
        files.update(record["files"])
    else:
        title = re.sub(r"\s+", " ", manifest["title"]).strip()[:80]
        base = f"{title} [{feature_id}]"
        name = base
        ordinal = 2
        while any(item["changelist"] == name and path not in files for path, item in before.items()):
            name = f"{base} ({ordinal})"
            ordinal += 1
    files.add(record_path.relative_to(project).as_posix())
    withdrawn = set()
    for relative in files:
        target = (project / relative).resolve()
        if not target.is_relative_to(project):
            raise ArchiveWorkspaceError("WORKSPACE_SCOPE_ESCAPE", f"产物越出项目：{relative}")
        item = before.get(relative, {}).get("item")
        if item in {"conflicted", "obstructed", "incomplete"} or before.get(relative, {}).get("props") == "conflicted" or before.get(relative, {}).get("tree-conflicted") == "true":
            raise ArchiveWorkspaceError("SVN_CHANGELIST_CONFLICT", f"产物存在 SVN 冲突：{relative}")
        if item == "missing":
            info = ET.fromstring(_run(project, "info", "--xml", str(target) + "@"))
            if info.findtext("entry/wc-info/schedule") == "add":
                # 正常切片撤回后文件正文已移除，只取消该新增文件的 SVN 调度。
                withdrawn.add(relative)
    for relative in sorted(withdrawn):
        _run(project, "revert", "--depth", "empty", "--", str(project / relative) + "@")
    files.difference_update(withdrawn)
    files = {relative for relative in files if (project / relative).is_file() or before.get(relative, {}).get("item") in {"deleted", "missing"} or relative == record_path.relative_to(project).as_posix()}
    record = {"name": name, "files": sorted(files), "pre_existing_changes": sorted(mixed)}
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for relative in sorted(files):
        target = project / relative
        item = before.get(relative, {}).get("item")
        if item == "deleted" and target.is_file():
            # 中断恢复已找回正文，取消先前删除调度时保留恢复的原有修改。
            content = target.read_bytes()
            _run(project, "revert", "--depth", "empty", "--", str(target) + "@")
            target.write_bytes(content)
        elif item == "missing":
            _run(project, "delete", "--", str(target) + "@")
        elif target.is_file() and (relative not in before or item in {"unversioned", "ignored", "none"}):
            _run(project, "add", "--parents", "--depth", "empty", "--force", "--no-ignore", "--", str(target) + "@")
    # 显式逐批文件清单，避免 Windows 命令行长度限制及目录递归纳入。
    ordered = sorted(files)
    for start in range(0, len(ordered), 20):
        _run(project, "changelist", "--", name, *(str(project / path) + "@" for path in ordered[start:start + 20]))
    after = _status(project)
    absent = [path for path in ordered if after.get(path, {}).get("changelist") != name]
    if absent:
        raise ArchiveWorkspaceError("SVN_CHANGELIST_INCOMPLETE", "未归组的产物：" + "、".join(absent))
    directories = sorted(
        path for path, item in after.items()
        if item["item"] in {"added", "deleted", "replaced", "missing"}
        and path not in files and any(file.startswith(path + "/") for file in files)
    )
    return {
        "status": "grouped", "name": name, "files": ordered,
        "directory_operations": directories, "pre_existing_changes": sorted(mixed),
        "committed": False,
        "directory_note": "SVN 提交组不支持目录，实际提交时须同时包含列出的目录操作。",
    }
