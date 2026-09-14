"""按固定动作一次投递功能交付流参考规则。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Mapping, Sequence


WORKFLOW_RULES_SCHEMA_VERSION = 2
SKILL_RELATIVE_PATH = (
    Path(".agents") / "skills" / "dloop" / "SKILL.md"
)
REFERENCES_RELATIVE_DIRECTORY = SKILL_RELATIVE_PATH.parent / "references"

REQUIREMENTS_REFERENCE = "requirements-and-terminology.md"
DOCUMENTS_REFERENCE = "documents-and-dependencies.md"
REVIEWS_REFERENCE = "reviews-and-approvals.md"
EXECUTION_REFERENCE = "execution.md"
VALIDATION_POLICY_REFERENCE = "validation-policy.md"
VALIDATION_REFERENCE = "validation-and-cleanup.md"
FRICTION_REFERENCE = "friction-records.md"
SNAPSHOT_REFERENCE = "snapshots.md"

WORKFLOW_RULE_ACTIONS: Mapping[str, tuple[str, ...]] = {
    "snapshot": (SNAPSHOT_REFERENCE,),
    "friction": (FRICTION_REFERENCE,),
    "create": (REQUIREMENTS_REFERENCE,),
    "resume": (REQUIREMENTS_REFERENCE,),
    "requirements": (REQUIREMENTS_REFERENCE,),
    "terminology": (REQUIREMENTS_REFERENCE,),
    "requirements-review": (REQUIREMENTS_REFERENCE, REVIEWS_REFERENCE),
    "ui-interaction-review": (REQUIREMENTS_REFERENCE, REVIEWS_REFERENCE),
    "investigation": (DOCUMENTS_REFERENCE,),
    "design": (DOCUMENTS_REFERENCE,),
    "plan": (DOCUMENTS_REFERENCE, VALIDATION_POLICY_REFERENCE),
    "documentation": (DOCUMENTS_REFERENCE,),
    "diagram": (DOCUMENTS_REFERENCE,),
    "dependency-refresh": (DOCUMENTS_REFERENCE,),
    "architecture-review": (DOCUMENTS_REFERENCE, REVIEWS_REFERENCE),
    "cold-read": (REVIEWS_REFERENCE,),
    "slice-plan": (VALIDATION_POLICY_REFERENCE, EXECUTION_REFERENCE),
    "slice-start": (VALIDATION_POLICY_REFERENCE, EXECUTION_REFERENCE),
    "slice-submit": (VALIDATION_POLICY_REFERENCE, EXECUTION_REFERENCE),
    "candidate-review": (VALIDATION_POLICY_REFERENCE, EXECUTION_REFERENCE),
    "slice-rework": (VALIDATION_POLICY_REFERENCE, EXECUTION_REFERENCE),
    "slice-release": (EXECUTION_REFERENCE,),
    "share": (VALIDATION_REFERENCE,),
    "validation": (VALIDATION_POLICY_REFERENCE, VALIDATION_REFERENCE),
    "final-review": (
        VALIDATION_POLICY_REFERENCE,
        VALIDATION_REFERENCE,
        REVIEWS_REFERENCE,
    ),
    "freeze": (VALIDATION_POLICY_REFERENCE, VALIDATION_REFERENCE),
    "retention": (VALIDATION_REFERENCE,),
    "cleanup": (VALIDATION_REFERENCE,),
}


class ArchiveWorkflowRulesError(Exception):
    """表示规则动作或固定技能材料不满足投递合同。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _read_rule_file(path: Path, label: str) -> tuple[bytes, str]:
    if not path.is_file() or path.is_symlink():
        raise ArchiveWorkflowRulesError(
            "WORKFLOW_RULES_UNAVAILABLE",
            f"{label}不存在、不是普通文件或是符号链接：{path}",
        )
    try:
        payload = path.read_bytes()
        content = payload.decode("utf-8")
    except (OSError, UnicodeError) as error:
        raise ArchiveWorkflowRulesError(
            "WORKFLOW_RULES_UNAVAILABLE",
            f"无法按 UTF-8 读取{label}：{path}：{error}",
        ) from error
    return payload, content


def workflow_rules(
    project_root: Path,
    actions: Sequence[str],
) -> Mapping[str, object]:
    """按动作顺序稳定去重，并完整返回命中的规则正文。"""

    requested_actions = list(actions)
    unknown_actions = [
        action for action in requested_actions if action not in WORKFLOW_RULE_ACTIONS
    ]
    if unknown_actions:
        raise ArchiveWorkflowRulesError(
            "INVALID_WORKFLOW_RULE_ACTION",
            "不受支持的工作流规则动作：" + "、".join(unknown_actions),
        )

    normalized_root = project_root.expanduser().resolve()
    if not normalized_root.is_dir():
        raise ArchiveWorkflowRulesError(
            "WORKFLOW_RULES_UNAVAILABLE",
            f"项目根目录不存在或不是目录：{normalized_root}",
        )

    entry_path = normalized_root / SKILL_RELATIVE_PATH
    _read_rule_file(entry_path, "唯一常驻入口")

    selected_references: list[str] = []
    selected_set: set[str] = set()
    for action in requested_actions:
        for reference in WORKFLOW_RULE_ACTIONS[action]:
            if reference in selected_set:
                continue
            selected_set.add(reference)
            selected_references.append(reference)
    if any(action in {
        "create", "resume", "requirements", "investigation", "design", "plan",
        "documentation", "dependency-refresh", "requirements-review", "architecture-review",
        "ui-interaction-review", "slice-start", "slice-submit", "slice-rework", "slice-release",
        "candidate-review", "validation", "final-review", "freeze", "cleanup",
    } for action in requested_actions) and SNAPSHOT_REFERENCE not in selected_set:
        selected_references.append(SNAPSHOT_REFERENCE)

    references = []
    for reference in selected_references:
        relative_path = REFERENCES_RELATIVE_DIRECTORY / reference
        payload, content = _read_rule_file(
            normalized_root / relative_path,
            f"参考规则“{reference}”",
        )
        references.append(
            {
                "path": relative_path.as_posix(),
                "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
                "content": content,
            }
        )

    return {
        "schema_version": WORKFLOW_RULES_SCHEMA_VERSION,
        "status": "ready",
        "requested_actions": requested_actions,
        "references": references,
    }
