"""快照中的 SVN 本地调度、属性及提交组；只操作明确受管路径。"""

from __future__ import annotations

import base64
from pathlib import Path
import shutil
import tempfile
from urllib.parse import unquote
import xml.etree.ElementTree as ET

from archive_svn import _run
from archive_workspace import ArchiveWorkspaceError


def _relative(project: Path, value: str) -> str:
    path = Path(value)
    return (path if path.is_absolute() else project / path).resolve().relative_to(project.resolve()).as_posix()


def _batches(paths):
    ordered = sorted(set(paths))
    for offset in range(0, len(ordered), 20):
        yield ordered[offset:offset + 20]


def capture_svn(project: Path, scopes) -> dict:
    nodes = {}
    scopes = list(scopes)
    parents = {parent.as_posix() for scope in scopes for parent in Path(scope).parents
               if parent.as_posix() != "." and not parent.as_posix().startswith(".scratch")}
    for depth, targets in (("infinity", scopes), ("empty", parents)):
        for batch in _batches(targets):
            document = ET.fromstring(_run(project, "status", "--xml", "--verbose", "--no-ignore", "--depth", depth,
                                           "--", *(str(project / path) + "@" for path in batch)))
            for parent in document:
                group = parent.get("name", "") if parent.tag == "changelist" else ""
                for entry in parent.findall("entry"):
                    status = entry.find("wc-status")
                    if status is None or status.get("item") in {"unversioned", "ignored", "none", "external"}:
                        continue
                    path = _relative(project, entry.attrib["path"])
                    nodes[path] = {"item": status.get("item"), "changelist": group}
    result = {}
    for batch in _batches(nodes):
        document = ET.fromstring(_run(project, "info", "--xml", "--", *(str(project / path) + "@" for path in batch)))
        for node in document.findall("entry"):
            path = _relative(project, node.attrib["path"])
            moved_from = node.findtext("wc-info/moved-from")
            result[path] = {"schedule": node.findtext("wc-info/schedule", "normal"),
                            "kind": node.get("kind", "file"), "properties": {}, "changelist": nodes[path]["changelist"],
                            "moved_from": _relative(project, str(Path(node.findtext("wc-info/wcroot-abspath")) / moved_from))
                            if moved_from else None,
                            "copy_from_url": node.findtext("wc-info/copy-from-url"),
                            "copy_from_rev": node.findtext("wc-info/copy-from-rev")}
    for batch in _batches(path for path in nodes if nodes[path]["item"] not in {"deleted", "missing"}):
        document = ET.fromstring(_run(project, "proplist", "--xml", "--verbose", "--depth", "empty", "--",
                                       *(str(project / path) + "@" for path in batch)))
        for target in document.findall("target"):
            path = _relative(project, target.attrib["path"])
            for prop in target.findall("property"):
                content = prop.text or ""
                raw = base64.b64decode(content) if prop.get("encoding") == "base64" else content.encode("utf-8")
                result[path]["properties"][prop.attrib["name"]] = base64.b64encode(raw).decode("ascii")
    return result


def restore_svn(project: Path, scopes, desired: dict, restore_files) -> None:
    current = capture_svn(project, scopes)
    # 只撤回本次受管路径的本地调度。正文随后从已保存的原始字节恢复。
    reverted = set()
    for path in sorted(current, key=lambda value: (value.count("/"), value)):
        if any(path.startswith(parent + "/") for parent in reverted):
            continue
        item = current[path]
        # 移动目录的子节点必须随父目录一起撤回；只有完整受管目录才递归恢复纳管。
        depth = "infinity" if (
            item["kind"] == "dir" and item["schedule"] in {"add", "delete", "replace"}
            and any(path == scope or path.startswith(scope.rstrip("/") + "/") for scope in scopes)
        ) else "empty"
        _run(project, "revert", "--depth", depth, "--", str(project / path) + "@")
        if depth == "infinity":
            reverted.add(path)
    # 普通复制先于移动重建，避免来源随后被移走；来源只读，正文仍以快照为准。
    copied = {path: item for path, item in desired.items()
              if item["copy_from_url"] and not item["moved_from"] and item["schedule"] in {"add", "replace"}}
    if copied:
        info = ET.fromstring(_run(project, "info", "--xml", "--", str(project) + "@"))
        working_root = Path(info.findtext("entry/wc-info/wcroot-abspath"))
        root_info = ET.fromstring(_run(project, "info", "--xml", "--", str(working_root) + "@"))
        root_url = root_info.findtext("entry/url").rstrip("/") + "/"
        for path, item in sorted(copied.items(), key=lambda value: (value[0].count("/"), value[0])):
            target = (project / path).resolve()
            if (target == project.resolve() or not target.is_relative_to(project.resolve())
                    or not any(target == (project / scope).resolve() or target.is_relative_to((project / scope).resolve())
                               for scope in scopes)):
                raise ArchiveWorkspaceError("SNAPSHOT_SCOPE_INVALID", "恢复复制只能修改受管目标路径。")
            if not item["copy_from_url"].startswith(root_url):
                raise ArchiveWorkspaceError("SNAPSHOT_COPY_SOURCE_UNAVAILABLE", "复制来源不在当前本地工作副本中，不能离线恢复。")
            origin = (working_root / unquote(item["copy_from_url"][len(root_url):])).resolve()
            source_info = ET.fromstring(_run(project, "info", "--xml", "--", str(origin) + "@"))
            source = source_info.find("entry")
            if (source.findtext("url") != item["copy_from_url"] or source.get("revision") != item["copy_from_rev"]):
                raise ArchiveWorkspaceError("SNAPSHOT_COPY_SOURCE_CHANGED", "本地复制来源与保存的路径或修订不符，不能恢复为其他来源。")
            if item["schedule"] == "replace":
                _run(project, "delete", "--force", "--keep-local", "--", str(target) + "@")
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
            _run(project, "copy", "--parents", "--", str(origin) + "@", str(target))
    # 先重建本地移动关系，随后恢复正文；普通新增无法保留改名前的来源历史。
    moved = {path: item["moved_from"] for path, item in desired.items() if item["moved_from"]}
    for path, source in sorted(moved.items(), key=lambda value: (value[0].count("/"), value[0])):
        target = (project / path).resolve()
        origin = (project / source).resolve()
        if not all(
            candidate.is_relative_to(project.resolve()) and candidate != project.resolve()
            and any(candidate == (project / scope).resolve() or candidate.is_relative_to((project / scope).resolve()) for scope in scopes)
            for candidate in (target, origin)
        ):
            raise ArchiveWorkspaceError("SNAPSHOT_SCOPE_INVALID", "恢复重命名必须同时覆盖原路径和目标路径。")
        if desired[path]["schedule"] == "replace":
            _run(project, "delete", "--force", "--keep-local", "--", str(target) + "@")
        # 旧现场已由快照事务保存；仅清理由上述范围检查确认的目标。
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
        _run(project, "move", "--parents", "--force", "--", str(origin) + "@", str(target))
    restore_files()
    deleted = set()
    for path, item in sorted(desired.items(), key=lambda value: (value[0].count("/"), value[0])):
        if any(path.startswith(parent + "/") for parent in deleted):
            continue
        target = project / path
        if item["schedule"] in {"add", "replace"} and path not in moved and path not in copied:
            if item["schedule"] == "replace":
                _run(project, "delete", "--force", "--keep-local", "--", str(target) + "@")
            if item["kind"] == "dir":
                target.mkdir(parents=True, exist_ok=True)
            _run(project, "add", "--parents", "--depth", "empty", "--force", "--no-ignore", "--", str(target) + "@")
        elif item["schedule"] == "delete":
            if path not in moved.values():
                _run(project, "delete", "--force", "--", str(target) + "@")
            deleted.add(path)
            continue
        if not target.exists():
            continue
        properties = ET.fromstring(_run(project, "proplist", "--xml", "--", str(target) + "@"))
        names = {prop.attrib["name"] for prop in properties.findall("target/property")}
        for name in names - set(item["properties"]):
            _run(project, "propdel", name, "--", str(target) + "@")
        for name, content in item["properties"].items():
            with tempfile.TemporaryDirectory(prefix="dloop-svn-property-") as temporary:
                value_path = Path(temporary) / "value"
                value_path.write_bytes(base64.b64decode(content))
                _run(project, "propset", name, "--file", str(value_path), "--", str(target) + "@")
    # 删除和缺失节点仍可属于提交组，不能随正文处理的提前结束而跳过。
    for path, item in desired.items():
        if item["kind"] == "file":
            target = project / path
            if item["changelist"]:
                _run(project, "changelist", "--", item["changelist"], str(target) + "@")
            else:
                _run(project, "changelist", "--remove", "--", str(target) + "@")
    for path, item in sorted(current.items(), reverse=True):
        target = project / path
        if path not in desired and item["kind"] == "dir" and item["schedule"] == "add" and target.is_dir():
            try:
                target.rmdir()
            except OSError:
                pass
