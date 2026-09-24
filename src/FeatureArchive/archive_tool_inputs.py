"""结构化输入由工具保存到当前交付项，复用现有文件输入校验。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Mapping

from archive_approvals import _require_complex_feature
from archive_changes import _replace_files_atomically
from archive_paths import WORKFLOW_ARTIFACT_DIRECTORIES
from archive_validation import validate_feature_archive


STRUCTURED_INPUTS = {
    "prepare-slice-contract": ("package_file", "task-package"),
    "checkpoint-slice": ("checkpoint_file", "checkpoint"),
    "submit-slice": ("candidate_file", "candidate"),
    "ui-baseline": ("input", "ui-baseline"),
}


def store_structured_input(root: Path, feature_id: str, command: str,
                           value: Mapping[str, object]) -> tuple[str, Path]:
    """调用者持有同一工作区锁；不同内容分别保存，同内容重试不覆写。"""

    feature = _require_complex_feature(validate_feature_archive(root, feature_id), feature_id)
    field, kind = STRUCTURED_INPUTS[command]
    content = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    identity = hashlib.sha256(content.encode("utf-8")).hexdigest()
    directory = WORKFLOW_ARTIFACT_DIRECTORIES.get(field, Path("04-plan"))
    target = feature.path / directory / f"tool-{kind}-{identity}.json"
    if not target.exists():
        _replace_files_atomically(root, {target: content})
    return field, target
