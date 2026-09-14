"""切片方案检查与原子批准入口。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from archive_approvals import (
    _load_state,
    _require_complex_feature,
    _state_path,
    _utc_now,
    _write_state,
    invalidate_final_approval,
)
from archive_execution import _execution_state
from archive_slice_plan import SlicePlanError, approve_plan, evaluate_plan, normalize_plan
from archive_validation import validate_feature_archive
from archive_workspace import serialized_workflow_state


def _read_plan(path: Path, feature_id: str) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exception:
        raise SlicePlanError("INVALID_SLICE_PLAN", f"无法读取切片方案“{path}”：{exception}") from exception
    if not isinstance(value, dict):
        raise SlicePlanError("INVALID_SLICE_PLAN", "切片方案必须是 JSON 对象。")
    return normalize_plan(value, feature_id)


def check_slice_plan(root: Path, feature_id: str, plan_file: Path) -> Mapping[str, object]:
    graph = validate_feature_archive(root, feature_id)
    feature = _require_complex_feature(graph, feature_id, require_writable=False)
    state = _load_state(feature.path, feature_id)
    execution = _execution_state(state)
    plan = _read_plan(plan_file, feature_id)
    evaluation = evaluate_plan(plan, execution["slices"])
    return {
        **evaluation,
        "next_action": {
            "command": "approve-slice-plan",
            "arguments": {"feature_id": feature_id, "plan_file": str(plan_file),
                          "plan_digest": evaluation["plan_digest"]},
            "when": "协调者按现有规则确认方案后调用；批准时重新检查当前事实。",
        },
    }


@serialized_workflow_state
def approve_slice_plan(
    root: Path,
    feature_id: str,
    plan_file: Path,
    expected_digest: str,
) -> Mapping[str, object]:
    graph = validate_feature_archive(root, feature_id)
    feature = _require_complex_feature(graph, feature_id)
    state = _load_state(feature.path, feature_id)
    execution = _execution_state(state)
    plan = _read_plan(plan_file, feature_id)
    evaluation, idempotent = approve_plan(
        execution["slice_plan"],
        plan,
        expected_digest,
        execution["slices"],
        _utc_now(),
    )
    if not idempotent:
        invalidate_final_approval(state)
        _write_state(_state_path(feature.path), state)
    return {**evaluation, "status": "approved", "idempotent": idempotent}
