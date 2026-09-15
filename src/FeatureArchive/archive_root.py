"""DLoop 档案根版本边界。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from archive_profiles import (
    ARCHIVE_SCHEMA_VERSION,
    ROOT_MANIFEST_NAME,
    ROOT_SCHEMA_VERSION,
    STRICT_PROFILE,
    WORKFLOW_VERSION,
)


class ArchiveRootError(Exception):
    """表示档案根不是当前严格合同。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def root_manifest() -> Mapping[str, object]:
    return {
        "root_schema_version": ROOT_SCHEMA_VERSION,
        "workflow_version": WORKFLOW_VERSION,
        "archive_schema_version": ARCHIVE_SCHEMA_VERSION,
        "workflow_profile": STRICT_PROFILE,
    }


def require_root_contract(root: Path) -> None:
    path = root / ROOT_MANIFEST_NAME
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exception:
        raise ArchiveRootError(
            "INCOMPATIBLE_ARCHIVE_ROOT",
            f"档案根“{root}”缺少 DLoop 根清单；历史档案不会被自动读取或迁移。",
        ) from exception
    except (OSError, UnicodeError, json.JSONDecodeError) as exception:
        raise ArchiveRootError(
            "INVALID_ARCHIVE_ROOT",
            f"DLoop 根清单“{path}”无法读取：{exception}",
        ) from exception
    if value != root_manifest():
        raise ArchiveRootError(
            "INCOMPATIBLE_ARCHIVE_ROOT",
            f"档案根“{root}”不属于 DLoop 严格合同；历史档案不会被自动读取或迁移。",
        )


def ensure_root_contract(root: Path) -> None:
    path = root / ROOT_MANIFEST_NAME
    if path.exists():
        require_root_contract(root)
        return
    if any(root.iterdir()):
        raise ArchiveRootError(
            "INCOMPATIBLE_ARCHIVE_ROOT",
            f"档案根“{root}”包含未版本化内容；请为 DLoop 使用独立空目录。",
        )
    path.write_text(
        json.dumps(root_manifest(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
