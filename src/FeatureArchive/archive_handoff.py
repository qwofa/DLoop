"""交接双方共用的材料装配；只读现有事实，不维护交接状态。"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Sequence, Tuple

from archive_changes import _body_fingerprint, stale_reasons
from archive_slice_contract import reviewed_candidate


class ArchiveHandoffError(Exception):
    """材料尚未齐备，调用者必须在交棒前补齐。"""

    def __init__(self, blockers: Sequence[Mapping[str, object]]) -> None:
        self.blockers = tuple(blockers)
        self.code = str(blockers[0]["code"])
        self.message = "；".join(str(item["message"]) for item in blockers)
        super().__init__(self.message)


def slice_handoff_materials(graph, feature, workspace_root: Path, package) -> list[dict]:
    """一次定位任务材料、批准需求和设计，汇总可确定的缺项。"""

    requested = [dict(item) for item in package["context_materials"]]
    for suffix, purpose in (
        ("requirements.overview", "核对已批准场景的原意、完整结果与明确范围调整"),
        ("design.overview", "核对实施与批准设计的一致性"),
    ):
        source_id = f"{feature.feature_id}.{suffix}"
        if not any(item["source"] == source_id for item in requested):
            requested.append({
                "source": source_id,
                "purpose": purpose,
                "mode": "full",
            })
    materials = []
    blockers = []

    def missing(source: str, code: str, message: str, **details) -> None:
        blockers.append({
            "code": code,
            "source": source,
            "message": message,
            "owner": "upstream",
            "recovery": "在当前上游上下文补齐此材料后重新提交；超出当前授权时交还主协调者。",
            **details,
        })

    for material in requested:
        source = material["source"]
        document = graph.documents.get(source)
        paths = (
            [document.path]
            if document is not None
            else [feature.path / source, workspace_root / source]
        )
        path = next((item for item in paths if item.is_file()), None)
        if path is None:
            missing(source, "CONTEXT_MATERIAL_NOT_FOUND", f"交接材料“{source}”不存在。")
            continue
        try:
            if material["mode"] == "sections" or document is not None:
                body = path.read_text(encoding="utf-8")
            else:
                with path.open("rb") as stream:
                    stream.read(1)
                body = ""
        except (OSError, UnicodeError) as exception:
            missing(source, "CONTEXT_MATERIAL_UNREADABLE", f"无法读取交接材料“{source}”：{exception}")
            continue
        if document is not None and (
            document.content_status not in {"confirmed", "completed"}
            or _body_fingerprint(body, path) != document.content_fingerprint
            or stale_reasons(graph, document)
        ):
            missing(source, "EXECUTION_INPUT_NOT_READY", f"交接材料“{source}”尚未确认或已经过期。")
        if material["mode"] == "sections":
            headings = [line.lstrip("#").strip() for line in body.splitlines() if line.startswith("#")]
            for section in material["sections"]:
                count = headings.count(section)
                if count != 1:
                    missing(
                        source,
                        "CONTEXT_SECTION_MISSING" if count == 0 else "CONTEXT_SECTION_AMBIGUOUS",
                        f"材料“{source}”{'缺少' if count == 0 else '包含重复'}章节“{section}”。",
                        section=section,
                    )
        materials.append({**material, "path": str(path.resolve())})
    if blockers:
        raise ArchiveHandoffError(blockers)
    return materials


def _repair_role_projection(
    record: Mapping[str, object],
) -> Tuple[Mapping[str, object] | None, Sequence[Mapping[str, str]]]:
    handoff = record.get("repair_handoff")
    if not isinstance(handoff, dict):
        return None, ()
    original_candidate = reviewed_candidate(record)
    acceptance_scope = handoff.get("acceptance_scope")
    issues = handoff.get("issues")
    validations = (
        acceptance_scope.get("validations")
        if isinstance(acceptance_scope, dict)
        else None
    )
    if (
        not isinstance(original_candidate, dict)
        or original_candidate.get("candidate_id") != handoff.get("original_candidate_id")
        or original_candidate.get("candidate_digest") != handoff.get("original_candidate_digest")
        or not isinstance(issues, list)
        or not all(isinstance(issue, str) and issue.strip() for issue in issues)
        or not isinstance(validations, list)
        or not all(
            isinstance(validation, str) and validation.strip()
            for validation in validations
        )
    ):
        return None, ({
            "code": "REPAIR_HANDOFF_INCOMPLETE",
            "message": "正式返修交接无法投影完整原候选、失败场景和受影响验证。",
        },)
    return {
        **handoff,
        "original_candidate": original_candidate,
        "retest_requirements": [
            {
                "failed_scenario": issue,
                "affected_validations": list(validations),
            }
            for issue in issues
        ],
    }, ()
