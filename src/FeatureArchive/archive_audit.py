"""对指定交付项执行宿主无关的只读档案审计。"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Mapping

from archive_changes import ArchiveChangeError, _body_fingerprint, _normalized_content
from archive_validation import (
    ArchiveValidationError,
    validate_feature_archive,
)


def _relative_location(root: Path, location: Path | None, feature_id: str) -> str:
    if location is None:
        return feature_id
    try:
        return location.resolve().relative_to(root).as_posix()
    except (OSError, ValueError):
        return str(location)


def _validation_location(
    root: Path,
    exception: ArchiveValidationError,
    feature_id: str,
) -> str:
    if exception.location is not None:
        return _relative_location(root, exception.location, feature_id)
    for value in re.findall(r"“([^”]+)”", exception.message):
        candidate = Path(value)
        if not candidate.is_absolute():
            continue
        try:
            return candidate.resolve().relative_to(root).as_posix()
        except (OSError, ValueError):
            continue
    return feature_id


def _blocker(
    code: str,
    location: str,
    message: str,
    document_id: str | None = None,
) -> Mapping[str, object]:
    blocker: dict[str, object] = {
        "code": code,
        "location": location,
        "message": message,
    }
    if document_id is not None:
        blocker["document_id"] = document_id
    return blocker


def _result(
    feature_id: str,
    blockers: list[Mapping[str, object]],
) -> Mapping[str, object]:
    return {
        "status": "blocked" if blockers else "allowed",
        "feature_id": feature_id,
        "blockers": blockers,
    }


def audit_archive(root: Path, feature_id: str) -> Mapping[str, object]:
    """校验指定交付项的完整结构和全部正文确认状态。"""

    normalized_root = root.expanduser().resolve()
    try:
        graph = validate_feature_archive(normalized_root, feature_id)
    except ArchiveValidationError as exception:
        return _result(
            feature_id,
            [
                _blocker(
                    exception.code,
                    _validation_location(normalized_root, exception, feature_id),
                    exception.message,
                )
            ],
        )
    except (OSError, UnicodeError) as exception:
        return _result(
            feature_id,
            [
                _blocker(
                    "UNREADABLE_ARCHIVE",
                    feature_id,
                    f"无法读取交付档案“{feature_id}”：{exception}",
                )
            ],
        )

    blockers: list[Mapping[str, object]] = []
    for document_id in sorted(graph.documents):
        document = graph.documents[document_id]
        location = _relative_location(normalized_root, document.path, feature_id)
        try:
            fingerprint = _body_fingerprint(
                _normalized_content(document.path),
                document.path,
            )
        except ArchiveChangeError as exception:
            blockers.append(
                _blocker(exception.code, location, exception.message, document_id)
            )
            continue
        if fingerprint != document.content_fingerprint:
            blockers.append(
                _blocker(
                    "UNCONFIRMED_EDIT",
                    location,
                    f"文档“{document_id}”的正文与已确认内容指纹不一致。",
                    document_id,
                )
            )

    return _result(feature_id, blockers)
