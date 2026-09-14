"""为当前工作流动作生成严格、可编辑且不会覆盖既有内容的输入文件。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from archive_approvals import _load_state, _require_complex_feature
from archive_candidates import candidate_input_template
from archive_configuration import configuration_task_materials
from archive_execution import (
    ArchiveExecutionError,
    EXECUTION_ID_PATTERN,
    _execution_state,
    find_slice_by_execution,
    task_package_input_guidance,
    task_package_input_template,
)
from archive_slice_flow import checkpoint_input_template
from archive_slice_plan import slice_plan_input_guidance, slice_plan_input_template
from archive_paths import WORKFLOW_ARTIFACT_DIRECTORIES
from archive_validation import validate_feature_archive


ACTION_INPUT_KINDS = ("task-package", "slice-plan", "checkpoint", "candidate", "review-issues", "review-verification", "ui-delivery")


def action_input_preparation(feature_id: str, input_kind: str, **target) -> Mapping[str, object]:
    """为角色和下一动作提供同一输入准备入口。"""

    return {
        "command": "prepare-action-input",
        "arguments": {"feature_id": feature_id, "input_kind": input_kind, **target},
    }


class ArchiveActionInputError(Exception):
    """表示当前事实不足以形成所需动作输入。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _write_without_overwrite(path: Path, value: Mapping[str, object]) -> bool:
    content = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
    except FileExistsError:
        return False
    return True


def prepare_action_input(
    root: Path,
    feature_id: str,
    input_kind: str,
    *,
    package_id: str | None = None,
    execution_id: str | None = None,
) -> Mapping[str, object]:
    """从当前档案和执行事实生成一个结构正确的动作输入。"""

    if input_kind not in ACTION_INPUT_KINDS:
        raise ArchiveActionInputError("INVALID_ACTION_INPUT_KIND", "未知动作输入类型。")
    graph = validate_feature_archive(root, feature_id)
    feature = _require_complex_feature(graph, feature_id)
    implementation = feature.path / WORKFLOW_ARTIFACT_DIRECTORIES["package_file"]
    guidance = {}

    if input_kind == "slice-plan":
        if package_id is not None or execution_id is not None:
            raise ArchiveActionInputError("INVALID_ACTION_INPUT_TARGET", "切片方案只指定交付项。")
        state = _load_state(feature.path, feature_id)
        template = slice_plan_input_template(feature_id, _execution_state(state)["slice_plan"])
        target = feature.path / WORKFLOW_ARTIFACT_DIRECTORIES["plan_file"] / f"slice-plan-v{template['version']}.json"
        guidance = slice_plan_input_guidance()
        business_inputs = ["本版切片清单、前置关系和替代关系；至少包含一个实现切片"]
        next_action = {
            "command": "check-slice-plan",
            "arguments": {"feature_id": feature_id, "plan_file": str(target)},
        }
    elif input_kind == "ui-delivery":
        state = _load_state(feature.path, feature_id)
        if package_id is not None or execution_id is not None or (state.get("configuration") or {}).get("id") != "dloop-ui-v1":
            raise ArchiveActionInputError("INVALID_ACTION_INPUT_TARGET", "UI 交付输入只指定 DloopUI 交付项。")
        from archive_approvals import require_accepted_implementation
        from archive_slice_contract import reviewed_candidate
        require_accepted_implementation(state["execution"])
        template = {"input_version": 1, "delivery_review": {
            "requirements_check": "待填写：从完整需求核对实际实现的结论",
            "implementation_check": "待填写：从实际实现反查交互说明的结论",
            "evidence": [{"path": "待填写：整体核对证据文件", "locator": "待填写：核对位置"}],
        }, "material_updates": []}
        target = feature.path / "06-validation/ui-delivery-input.json"
        business_inputs = ["整体双向核对结论和证据；交互自动读取已接受任务的材料，材料返修只填写受影响引用"]
        guidance = {
            "submitted_materials": [{"package_id": key, **(reviewed_candidate(record) or {}).get("delivery", {})}
                                    for key, record in state["execution"]["slices"].items() if record.get("status") == "accepted"],
            "material_updates": "每项包含 package_id、requirement、kind、path、locator；验证材料另含 status。省略的材料沿用上次有效交接。",
        }
        model_path = feature.path / "ui-model.json"
        guidance["last_published_materials"] = json.loads(model_path.read_text(encoding="utf-8")).get("delivery_materials", [])
        next_action = {"command": "ui-publish", "arguments": {"feature_id": feature_id, "input": str(target)}}
    elif input_kind == "task-package":
        if not isinstance(package_id, str) or execution_id is not None:
            raise ArchiveActionInputError(
                "INVALID_ACTION_INPUT_TARGET",
                "任务包输入必须且只能指定任务包标识。",
            )
        try:
            template = dict(task_package_input_template(package_id))
        except ArchiveExecutionError as exception:
            raise ArchiveActionInputError(exception.code, exception.message) from exception
        state = _load_state(feature.path, feature_id)
        template["context_materials"] = [
            dict(item) for item in configuration_task_materials(state, feature_id)
        ]
        target = implementation / f"{package_id}.json"
        business_inputs = [
            "写入范围与必要上下文材料",
            "业务结果、验收场景、范围和依赖事实",
            "影响区域、不变量、验证策略与回退点",
            "七项实施前契约检查证据与批准状态",
        ]
        guidance = task_package_input_guidance(feature_id)
        if (state.get("configuration") or {}).get("id") == "dloop-ui-v1":
            template["delivery_requirements"] = []
            business_inputs.append("逐个验收场景声明必要交付材料；UI 场景通常包含交互、元素定位、截图及验证依据")
            guidance["delivery_requirements"] = {
                "description": "每项引用现有验收场景；非必要项须说明范围或延期依据。UI 材料使用当前调研生成的真实截图及节点。",
                "example": {"key": "feature.result", "scenario": "与契约中的验收场景原文一致",
                            "materials": ["interaction", "ui-location", "screenshot", "verification"], "required": True},
                "submission": "候选提交 delivery_materials 文件引用；发现同范围遗漏时追加 additional_delivery_requirements。",
            }
        next_action = {
            "command": "prepare-slice-contract",
            "arguments": {"feature_id": feature_id, "package_file": str(target)},
        }
    elif input_kind in {"review-issues", "review-verification"}:
        if not isinstance(execution_id, str) or package_id is not None or not EXECUTION_ID_PATTERN.fullmatch(execution_id):
            raise ArchiveActionInputError("INVALID_ACTION_INPUT_TARGET", "评审输入只指定当前独立评审执行标识。")
        state = _load_state(feature.path, feature_id)
        execution = _execution_state(state)
        candidates = [record for record in execution["slices"].values() if record.get("status") == "candidate"]
        if len(candidates) != 1:
            raise ArchiveActionInputError("FIXED_CANDIDATE_REQUIRED", "评审输入需要唯一待评审候选。")
        record = candidates[0]
        if execution_id == record["execution_id"] or execution_id in execution["used_execution_ids"]:
            raise ArchiveActionInputError("REVIEW_NOT_INDEPENDENT", "评审输入需要未使用且不同于实施者的执行标识。")
        field = "issues" if input_kind == "review-issues" else "verification"
        template = {field: []}
        artifact = "issues_file" if field == "issues" else "verification_file"
        target = feature.path / WORKFLOW_ARTIFACT_DIRECTORIES[artifact] / f"{execution_id}-{field}.json"
        business_inputs = ["独立评审的实际问题；通过时可为空" if field == "issues" else "独立执行的验证方式、证据与证明边界；没有证据时不要提交空文件"]
        guidance = {"result": {"allowed_values": ["passed", "failed", "unverified"],
                               "description": "由独立评审判断；生成输入不预设通过。"}}
        next_action = {
            "command": "review-slice",
            "arguments": {"feature_id": feature_id, "package_id": record["package"]["package_id"],
                          "candidate_id": record["candidate"]["candidate_id"], "review_execution_id": execution_id,
                          artifact: str(target)},
            "required_inputs": ["result", "issues_file（result!=passed 时）"] if field == "verification" else ["result"],
        }
    else:
        if not isinstance(execution_id, str) or package_id is not None:
            raise ArchiveActionInputError(
                "INVALID_ACTION_INPUT_TARGET",
                "检查点或候选输入必须且只能指定当前执行标识。",
            )
        state = _load_state(feature.path, feature_id)
        record = find_slice_by_execution(_execution_state(state), execution_id)
        if not isinstance(record, dict) or record.get("status") != "active":
            raise ArchiveActionInputError(
                "EXECUTION_NOT_ACTIVE",
                "只有实施中的当前执行能生成检查点或候选输入。",
            )
        if input_kind == "checkpoint":
            template = checkpoint_input_template(record)
            checkpoints = record.get("checkpoints")
            checkpoint_history = checkpoints if isinstance(checkpoints, list) else []
            ordinal = 1 + sum(
                1
                for item in checkpoint_history
                if isinstance(item, dict)
                and item.get("execution_id") == execution_id
            )
            version = record["package"]["slice_contract"]["version"]
            target = implementation / f"{execution_id}-checkpoint-{ordinal}-v{version}.json"
            business_inputs = [
                "本轮假设、实际变化与发现",
                "各契约验证方法的结论和证据",
                "下一步与已观察到的熔断事实",
            ]
            next_action = {
                "command": "checkpoint-slice",
                "arguments": {"feature_id": feature_id, "execution_id": execution_id, "checkpoint_file": str(target)},
            }
        else:
            template = candidate_input_template(record)
            target = implementation / f"{execution_id}-candidate.json"
            business_inputs = ["候选验证证据", "仍未验证的边界"]
            if "delivery_materials" in template:
                business_inputs.append("按任务材料要求填写实际文件路径和定位；验证状态如实记录，新增同范围要求一并登记")
            next_action = {
                "command": "submit-slice",
                "arguments": {"feature_id": feature_id, "execution_id": execution_id,
                              "status": "completed", "candidate_file": str(target)},
            }

    written = _write_without_overwrite(target, template)
    return {
        "status": "prepared" if written else "existing",
        "input_kind": input_kind,
        "target": str(target),
        "written": written,
        "template": template,
        "required_business_inputs": business_inputs,
        "input_guidance": guidance,
        "next_action": {**next_action, "when": "按填写说明补齐实际业务事实后调用；生成不表示通过或批准。"},
    }
