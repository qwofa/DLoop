"""功能交付档案的变更批次确认、新鲜度检查和逐层传播。"""

from __future__ import annotations

import hashlib
import heapq
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple

from archive_validation import (
    ArchiveGraph,
    DocumentRecord,
    validate_archive_root,
    validate_feature_archive,
)
from archive_workspace import serialized_workflow_state


FIELD_PATTERN = re.compile(r"^([a-z_]+):")
READ_ONLY_LIFECYCLES = {"frozen", "pending_cleanup"}


class ArchiveChangeError(Exception):
    """表示变更批次或新鲜度检查中可操作的确定性错误。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _normalized_content(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").replace("\r\n", "\n").replace(
            "\r", "\n"
        )
    except (OSError, UnicodeError) as exception:
        raise ArchiveChangeError(
            "UNREADABLE_DOCUMENT",
            f"无法读取文档“{path}”：{exception}",
        ) from exception


def _front_matter_end(lines: Sequence[str], path: Path) -> int:
    if not lines or lines[0] != "---":
        raise ArchiveChangeError(
            "INVALID_DOCUMENT",
            f"文档“{path}”缺少 YAML 元数据起始标记“---”。",
        )
    for index in range(1, len(lines)):
        if lines[index] == "---":
            return index
    raise ArchiveChangeError(
        "INVALID_DOCUMENT",
        f"文档“{path}”缺少 YAML 元数据结束标记“---”。",
    )


def _body_fingerprint(content: str, path: Path) -> str:
    lines = content.splitlines()
    end = _front_matter_end(lines, path)
    body = "\n".join(lines[end + 1 :])
    if body.startswith("\n"):
        body = body[1:]
    if content.endswith("\n"):
        body += "\n"
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def _rewrite_front_matter(
    content: str,
    path: Path,
    updates: Mapping[str, str],
    removals: Iterable[str] = (),
) -> str:
    lines = content.splitlines()
    had_final_newline = content.endswith("\n")
    end = _front_matter_end(lines, path)
    removal_set = set(removals)
    pending = dict(updates)
    rewritten = ["---"]
    index = 1

    while index < end:
        line = lines[index]
        match = FIELD_PATTERN.match(line)
        if match is None:
            rewritten.append(line)
            index += 1
            continue

        field = match.group(1)
        block_end = index + 1
        while block_end < end and FIELD_PATTERN.match(lines[block_end]) is None:
            block_end += 1

        if field in removal_set:
            index = block_end
            continue
        replacement = pending.pop(field, None)
        if replacement is not None:
            rewritten.append(f"{field}: {replacement}")
        else:
            rewritten.extend(lines[index:block_end])
        index = block_end

    for field, value in pending.items():
        if field not in removal_set:
            rewritten.append(f"{field}: {value}")
    rewritten.append("---")
    rewritten.extend(lines[end + 1 :])
    result = "\n".join(rewritten)
    if had_final_newline:
        result += "\n"
    return result


def _bump_patch(version: str) -> str:
    major, minor, patch = (int(value) for value in version.split("."))
    return f"{major}.{minor}.{patch + 1}"


def _dependency_versions(
    document: DocumentRecord,
    semantic_versions: Mapping[str, str],
) -> Mapping[str, str]:
    return {
        dependency_id: semantic_versions[dependency_id]
        for dependency_id in sorted(document.dependencies)
    }


def _version_mismatches(
    graph: ArchiveGraph,
    document: DocumentRecord,
) -> Tuple[str, ...]:
    return tuple(
        dependency_id
        for dependency_id in document.dependencies
        if document.dependency_versions.get(dependency_id)
        != graph.documents[dependency_id].semantic_version
    )


def stale_reasons(
    graph: ArchiveGraph,
    document: DocumentRecord,
) -> Tuple[str, ...]:
    """返回文档不能作为可靠输入的确定性原因。"""

    reasons: List[str] = []
    if document.content_status == "stale":
        reasons.append("content_status=stale")
    for dependency_id in _version_mismatches(graph, document):
        consumed = document.dependency_versions.get(dependency_id, "未记录")
        current = graph.documents[dependency_id].semantic_version
        reasons.append(f"{dependency_id}: {consumed} -> {current}")
    return tuple(reasons)


def _topological_document_ids(graph: ArchiveGraph) -> Tuple[str, ...]:
    indegrees = {
        document_id: len(document.dependencies)
        for document_id, document in graph.documents.items()
    }
    ready = [
        document_id for document_id, degree in indegrees.items() if degree == 0
    ]
    heapq.heapify(ready)
    ordered: List[str] = []
    while ready:
        document_id = heapq.heappop(ready)
        ordered.append(document_id)
        for dependent_id in graph.dependents[document_id]:
            indegrees[dependent_id] -= 1
            if indegrees[dependent_id] == 0:
                heapq.heappush(ready, dependent_id)
    return tuple(ordered)


def refresh_queue(
    graph: ArchiveGraph,
    feature_id: str | None = None,
) -> Tuple[str, ...]:
    """按全图拓扑顺序返回当前待刷新的活跃文档。"""

    if feature_id is not None and feature_id not in graph.features:
        raise ArchiveChangeError(
            "UNKNOWN_FEATURE",
            f"功能档案“{feature_id}”不存在。",
        )
    queue = []
    for document_id in _topological_document_ids(graph):
        document = graph.documents[document_id]
        feature = graph.features[document.feature_id]
        if feature_id is not None and document.feature_id != feature_id:
            continue
        if feature.lifecycle in READ_ONLY_LIFECYCLES:
            continue
        if stale_reasons(graph, document):
            queue.append(document_id)
    return tuple(queue)


def assert_documents_fresh(
    graph: ArchiveGraph,
    document_ids: Sequence[str],
) -> Mapping[str, object]:
    normalized_ids = _validate_document_ids(graph, document_ids)
    stale = {
        document_id: list(stale_reasons(graph, graph.documents[document_id]))
        for document_id in normalized_ids
        if stale_reasons(graph, graph.documents[document_id])
    }
    if stale:
        details = "；".join(
            f"{document_id}（{', '.join(reasons)}）"
            for document_id, reasons in stale.items()
        )
        raise ArchiveChangeError(
            "STALE_DOCUMENT",
            f"以下文档已过期，不能作为可靠执行输入：{details}",
        )
    return {
        "status": "fresh",
        "root": str(graph.root),
        "documents": list(normalized_ids),
    }


def _validate_document_ids(
    graph: ArchiveGraph,
    document_ids: Sequence[str],
) -> Tuple[str, ...]:
    if not document_ids:
        raise ArchiveChangeError(
            "MISSING_DOCUMENT",
            "至少需要明确指定一个 document_id。",
        )
    normalized = tuple(document_ids)
    if len(normalized) != len(set(normalized)):
        raise ArchiveChangeError(
            "DUPLICATE_DOCUMENT",
            "同一批次不能重复指定 document_id。",
        )
    missing = tuple(
        document_id
        for document_id in normalized
        if document_id not in graph.documents
    )
    if missing:
        raise ArchiveChangeError(
            "UNKNOWN_DOCUMENT",
            "找不到以下文档：" + "、".join(missing),
        )
    return normalized


@serialized_workflow_state
def _replace_files_atomically(
    root: Path,
    contents: Mapping[Path, str | bytes],
    deletions: Iterable[Path] = (),
) -> None:
    deletion_paths = tuple(dict.fromkeys(deletions))
    if set(contents).intersection(deletion_paths):
        raise ArchiveChangeError(
            "CHANGE_COMMIT_FAILED",
            "同一批次不能同时写入和删除同一文件。",
        )
    for path in (*contents, *deletion_paths):
        try:
            path.relative_to(root)
        except ValueError as exception:
            raise ArchiveChangeError(
                "CHANGE_COMMIT_FAILED",
                f"批次提交目标“{path}”越过规范档案根。",
            ) from exception
    staging = Path(tempfile.mkdtemp(prefix=".feature-archive-change-", dir=root))
    replaced: List[Path] = []
    backups: Dict[Path, Path] = {}
    try:
        for path, content in contents.items():
            relative_path = path.relative_to(root)
            staged_path = staging / "new" / relative_path
            staged_path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                staged_path.write_bytes(content)
            else:
                staged_path.write_text(content, encoding="utf-8", newline="\n")

        for path in sorted(contents, key=lambda item: item.as_posix()):
            if path.exists() and not path.is_file():
                raise ArchiveChangeError(
                    "CHANGE_COMMIT_FAILED",
                    f"批次提交目标“{path}”不是文件。",
                )
            relative_path = path.relative_to(root)
            if path.is_file():
                backup = staging / "backup" / relative_path
                backup.parent.mkdir(parents=True, exist_ok=True)
                os.replace(path, backup)
                backups[path] = backup
            path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging / "new" / relative_path, path)
            replaced.append(path)
        for path in sorted(deletion_paths, key=lambda item: item.as_posix()):
            if not path.exists():
                continue
            if not path.is_file():
                raise ArchiveChangeError(
                    "CHANGE_COMMIT_FAILED",
                    f"批次删除目标“{path}”不是文件。",
                )
            relative_path = path.relative_to(root)
            backup = staging / "backup" / relative_path
            backup.parent.mkdir(parents=True, exist_ok=True)
            os.replace(path, backup)
            backups[path] = backup
    except Exception as exception:
        for path in reversed(replaced):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        for path, backup in backups.items():
            if backup.exists():
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(backup, path)
                except OSError:
                    pass
        if isinstance(exception, ArchiveChangeError):
            raise
        raise ArchiveChangeError(
            "CHANGE_COMMIT_FAILED",
            f"变更批次提交失败，已尝试恢复原文件：{exception}",
        ) from exception
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _manifest_with_updated_at(path: Path, updated_at: str) -> str:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exception:
        raise ArchiveChangeError(
            "INVALID_MANIFEST",
            f"无法更新生命周期清单“{path}”：{exception}",
        ) from exception
    manifest["updated_at"] = updated_at
    return json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"


@serialized_workflow_state
def confirm_change_batch(
    root: Path,
    document_ids: Sequence[str],
    semantic_change: bool,
) -> Mapping[str, object]:
    """确认一次编辑批次，并只向直接依赖层传播语义变化。"""

    feature_ids = {document_id.split(".", 1)[0] for document_id in document_ids}
    graph = (
        validate_feature_archive(root, next(iter(feature_ids)))
        if len(feature_ids) == 1
        else validate_archive_root(root)
    )
    normalized_ids = _validate_document_ids(graph, document_ids)
    for document_id in normalized_ids:
        feature = graph.features[graph.documents[document_id].feature_id]
        if feature.lifecycle in READ_ONLY_LIFECYCLES:
            raise ArchiveChangeError(
                "READ_ONLY_ARCHIVE",
                f"功能档案“{feature.feature_id}”处于 {feature.lifecycle}，"
                "不能再提交文档变更。",
            )

    current_fingerprints = {
        document_id: _body_fingerprint(
            _normalized_content(graph.documents[document_id].path),
            graph.documents[document_id].path,
        )
        for document_id in normalized_ids
    }
    changed_ids = tuple(
        document_id
        for document_id in normalized_ids
        if (
            current_fingerprints[document_id]
            != graph.documents[document_id].content_fingerprint
            or graph.documents[document_id].content_status == "stale"
            or _version_mismatches(graph, graph.documents[document_id])
        )
    )
    if not changed_ids:
        return {
            "status": "unchanged",
            "root": str(graph.root),
            "semantic_change": semantic_change,
            "documents": list(normalized_ids),
            "invalidated_documents": [],
            "refresh_queue": list(refresh_queue(graph)),
        }

    semantic_versions = {
        document_id: document.semantic_version
        for document_id, document in graph.documents.items()
    }
    if semantic_change:
        for document_id in changed_ids:
            semantic_versions[document_id] = _bump_patch(
                semantic_versions[document_id]
            )

    contents: Dict[Path, str] = {}
    changed_id_set = set(changed_ids)
    for document_id in changed_ids:
        document = graph.documents[document_id]
        updates = {
            "content_fingerprint": current_fingerprints[document_id],
            "dependency_versions": json.dumps(
                _dependency_versions(document, semantic_versions),
                ensure_ascii=False,
                sort_keys=True,
            ),
        }
        removals: Tuple[str, ...] = ()
        if semantic_change:
            updates["semantic_version"] = semantic_versions[document_id]
        if document.content_status == "stale":
            if document.stale_from_status is None:
                raise ArchiveChangeError(
                    "INVALID_STALE_STATE",
                    f"文档“{document.document_id}”缺少 stale_from_status。",
                )
            updates["content_status"] = document.stale_from_status
            removals = ("stale_from_status",)
        content = _normalized_content(document.path)
        contents[document.path] = _rewrite_front_matter(
            content,
            document.path,
            updates,
            removals,
        )

    invalidated: List[str] = []
    if semantic_change:
        direct_dependents = sorted(
            {
                dependent_id
                for document_id in changed_ids
                for dependent_id in graph.dependents[document_id]
                if dependent_id not in changed_id_set
            }
        )
        for dependent_id in direct_dependents:
            dependent = graph.documents[dependent_id]
            feature = graph.features[dependent.feature_id]
            if feature.lifecycle in READ_ONLY_LIFECYCLES:
                continue
            invalidated.append(dependent_id)
            if dependent.content_status == "stale":
                continue
            content = _normalized_content(dependent.path)
            contents[dependent.path] = _rewrite_front_matter(
                content,
                dependent.path,
                {
                    "content_status": "stale",
                    "stale_from_status": dependent.content_status,
                },
            )

    touched_feature_ids = {
        graph.documents[document_id].feature_id
        for document_id in changed_ids
    }
    touched_feature_ids.update(
        graph.documents[document_id].feature_id for document_id in invalidated
    )
    updated_at = _utc_now()
    for feature_id in touched_feature_ids:
        manifest_path = graph.features[feature_id].path / "feature.json"
        contents[manifest_path] = _manifest_with_updated_at(
            manifest_path,
            updated_at,
        )

    _replace_files_atomically(graph.root, contents)
    updated_graph = (
        validate_feature_archive(graph.root, next(iter(feature_ids)))
        if len(feature_ids) == 1
        else validate_archive_root(graph.root)
    )
    return {
        "status": "committed",
        "root": str(updated_graph.root),
        "semantic_change": semantic_change,
        "documents": [
            {
                "document_id": document_id,
                "semantic_version": updated_graph.documents[
                    document_id
                ].semantic_version,
                "content_fingerprint": updated_graph.documents[
                    document_id
                ].content_fingerprint,
            }
            for document_id in changed_ids
        ],
        "invalidated_documents": invalidated,
        "refresh_queue": list(refresh_queue(updated_graph)),
    }
