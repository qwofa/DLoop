"""沿用任务包和候选账本交接交付材料，不维护另一套交付状态。"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from archive_handoff import ArchiveHandoffError


MATERIAL_KINDS = {"interaction", "ui-location", "screenshot", "verification"}
KIND_LABELS = {"interaction": "交互说明", "ui-location": "UI 元素定位", "screenshot": "截图", "verification": "验证依据"}


def fail(code, message):
    raise ArchiveHandoffError([{"code": code, "message": message, "owner": "implementation",
                               "recovery": "补齐所列材料后重新提交；只更新受影响材料，不重新设计 HTML。"}])


def normalize_requirements(value, scenarios, *, complete=False):
    if not isinstance(value, list):
        fail("DELIVERY_REQUIREMENTS_REQUIRED", "任务包必须声明交付材料要求。")
    result, keys = [], set()
    for item in value:
        if not isinstance(item, dict) or set(item) not in (
            {"key", "scenario", "materials", "required"}, {"key", "scenario", "materials", "required", "reason"},
        ):
            fail("INVALID_DELIVERY_REQUIREMENT", "材料要求须包含 key、验收场景 scenario、材料类型 materials 和 required。")
        key = item["key"]
        if not isinstance(key, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", key) or key in keys:
            fail("INVALID_DELIVERY_REQUIREMENT", "材料要求标识无效或重复。")
        if item["scenario"] not in scenarios:
            fail("DELIVERY_SCENARIO_OUTSIDE_SCOPE", f"材料要求 {key} 必须引用本任务已登记的验收场景。")
        kinds = item["materials"]
        if not isinstance(kinds, list) or not kinds or any(not isinstance(k, str) or k not in MATERIAL_KINDS for k in kinds) or len(kinds) != len(set(kinds)):
            fail("INVALID_DELIVERY_REQUIREMENT", f"材料要求 {key} 的材料类型无效或重复。")
        if type(item["required"]) is not bool or (not item["required"] and (not isinstance(item.get("reason"), str) or not item["reason"].strip())):
            fail("DELIVERY_DEFERRAL_REASON_REQUIRED", f"材料要求 {key} 必须明确是否必要，非必要项须说明范围或延期依据。")
        keys.add(key)
        result.append(deepcopy(item))
    if complete:
        missing = set(scenarios) - {item["scenario"] for item in result}
        if missing:
            fail("DELIVERY_REQUIREMENTS_REQUIRED", "以下验收场景尚未指定交付材料：" + "；".join(sorted(missing)))
    return result


def require_task_delivery(state, package):
    if (state.get("configuration") or {}).get("id") == "dloop-ui-v1":
        normalize_requirements(package.get("delivery_requirements"), package["acceptance_conditions"], complete=True)


def material_input_template(requirements):
    return [{"requirement": item["key"], "kind": kind, "path": "待填写：实际材料文件路径",
             "locator": "待填写：对应功能或验证位置", **({"status": "unverified"} if kind == "verification" else {})}
            for item in requirements for kind in item["materials"] if item["required"]]


def _path(value, feature_path, workspace):
    if not isinstance(value, str) or not value.strip():
        fail("DELIVERY_MATERIAL_MISSING", "交付材料必须指向实际文件。")
    path = Path(value)
    if not path.is_absolute():
        path = next((base / path for base in (feature_path, workspace) if (base / path).is_file()), feature_path / path)
    return path.resolve()


def _read_review(path):
    try:
        review = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as error:
        fail("INVALID_DELIVERY_REVIEW", f"无法读取交互材料 {path}：{error}")
    if not isinstance(review, dict) or not isinstance(review.get("outcomes"), list) or not review["outcomes"] or "delivery_review" in review:
        fail("INVALID_DELIVERY_REVIEW", f"交互材料 {path} 须提交本任务的非空 outcomes，整体核对由协调者完成。")
    return review


def _bindings(runtime, model, review, workspace):
    bindings = {}
    for outcome in review["outcomes"]:
        prefab_path = outcome.get("prefab")
        if not prefab_path:
            continue
        path = runtime.canonical_path(prefab_path, workspace)
        prefab = next((p for p in model["prefabs"] if runtime.path_key(p["asset_path"]) == runtime.path_key(path)), None)
        if prefab is None:
            fail("DELIVERY_LOCATION_MISSING", f"交互材料引用了当前调研未登记的 UI：{path}")
        asset = Path(path)
        if not asset.is_file() or "sha256:" + hashlib.sha256(asset.read_bytes()).hexdigest() != prefab["asset_sha256"]:
            fail("DELIVERY_LOCATION_STALE", f"UI 资产 {path} 已变化，先更新调研；定位未变可提交 unchanged_layout 核对后复用旧图，否则补拍。")
        capture = prefab.get("capture") or {}
        evidence = runtime.collection_map(model, "evidence").get(capture.get("screenshot_evidence_id"), {})
        bindings[path] = {"asset_sha256": capture.get("reuse", {}).get("captured_asset_sha256", prefab["asset_sha256"]), "screenshot_sha256": evidence.get("sha256"),
                          "regions": capture.get("regions", [])}
    return bindings


def collect_materials(requirements, values, feature_path, workspace, *, final=False, current_tokens=True):
    """验证真实文件和现有 UI 输入合同；候选可以如实交回未验证状态。"""
    from archive_ui import _runtime
    from archive_paths import project_root_from_archive_root
    runtime = _runtime()
    project_root = project_root_from_archive_root(feature_path.parent)
    model_path = feature_path / "ui-model.json"
    model = runtime.read_json(model_path) if model_path.is_file() else None
    if not isinstance(values, list):
        fail("DELIVERY_MATERIALS_REQUIRED", "完成任务须交回 delivery_materials，不能只在对话中说明已完成。")
    by_key = {item["key"]: item for item in requirements}
    result, provided, problems = [], set(), []
    for item in values:
        if not isinstance(item, dict):
            fail("INVALID_DELIVERY_MATERIAL", "交付材料必须是结构化文件引用。")
        fields = {"requirement", "kind", "path", "locator"} | ({"status"} if item.get("kind") == "verification" else set())
        if set(item) != fields or not isinstance(item.get("locator"), str) or not item["locator"].strip():
            fail("INVALID_DELIVERY_MATERIAL", "材料须声明 requirement、kind、path、locator；验证材料还须声明 status。")
        if not isinstance(item["requirement"], str) or not isinstance(item["kind"], str):
            fail("INVALID_DELIVERY_MATERIAL", "材料要求标识和材料类型必须是文字。")
        requirement = by_key.get(item["requirement"])
        if requirement is None or item["kind"] not in requirement["materials"]:
            fail("DELIVERY_MATERIAL_UNDECLARED", "材料未对应已声明要求；实施中发现的要求须一并提交 additional_delivery_requirements。")
        key = (item["requirement"], item["kind"])
        if key in provided:
            fail("DUPLICATE_DELIVERY_MATERIAL", f"材料 {key} 重复；同类多项内容放入同一材料文件。")
        provided.add(key)
        path = _path(item["path"], feature_path, workspace)
        label = f"{requirement['scenario']} / {KIND_LABELS[item['kind']]}"
        if not path.is_file():
            problems.append(f"{label}：文件不存在 {path}")
            continue
        entry = {**item, "path": str(path), "sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()}
        if item["kind"] == "verification":
            if item["status"] not in {"passed", "failed", "unverified"}:
                fail("INVALID_DELIVERY_VERIFICATION", f"{label}：验证状态只能是 passed、failed 或 unverified。")
            if final and requirement["required"] and item["status"] != "passed":
                problems.append(f"{label}：必要验证尚未通过")
        elif item["kind"] in {"interaction", "ui-location"}:
            review = _read_review(path)
            if model is None:
                fail("DELIVERY_UI_INVESTIGATION_REQUIRED", f"{label}：先登记 UI 调研和截图，再提交交互与定位材料。")
            checked = deepcopy(review)
            if not current_tokens:
                checked["investigation_token"] = runtime.investigation_token(model)
            inspected = deepcopy(model)
            try:
                runtime.apply_review_snapshot(inspected, checked, project_root)
            except runtime.DloopUiError as error:
                fail(error.code, f"{label}：{error.message}")
            for annotation in inspected["annotations"]:
                if item["kind"] == "interaction" and not annotation.get("interaction"):
                    problems.append(f"{label}：{annotation['title']} 缺少条件、操作、反馈和验证依据")
                if item["kind"] == "ui-location" and not (annotation["target"].get("hierarchy_path") or annotation["target"].get("unavailable_reason")):
                    problems.append(f"{label}：{annotation['title']} 缺少真实节点关联或明确定位缺口")
            if not inspected["annotations"]:
                problems.append(f"{label}：不能只交跳过记录，应保留交互说明")
            entry["prefab_bindings"] = _bindings(runtime, model, review, project_root)
        elif item["kind"] == "screenshot":
            screenshots = [e for e in (model or {}).get("evidence", []) if e["kind"] == "prefab_screenshot"]
            if not any(_path(e["path"], feature_path, workspace) == path and e["sha256"] == entry["sha256"] for e in screenshots):
                problems.append(f"{label}：须引用当前 UI 调研采集并登记的截图")
        result.append(entry)
    for requirement in requirements:
        if requirement["required"]:
            for kind in requirement["materials"]:
                if (requirement["key"], kind) not in provided:
                    problems.append(f"{requirement['scenario']}：缺少{KIND_LABELS[kind]}")
    if problems:
        fail("DELIVERY_MATERIALS_INCOMPLETE", "；".join(problems))
    return result


def submitted_delivery(package, value, feature_path, workspace):
    additional = normalize_requirements(value.get("additional_delivery_requirements", []), package["acceptance_conditions"])
    requirements = normalize_requirements(package["delivery_requirements"] + additional, package["acceptance_conditions"], complete=True)
    materials = collect_materials(requirements, value.get("delivery_materials"), feature_path, workspace)
    return {"requirements": requirements, "materials": materials}


def delivery_paths(candidate):
    for item in candidate.get("delivery", {}).get("materials", []):
        path = Path(item["path"])
        if not path.is_file() or "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            fail("DELIVERY_MATERIAL_STALE", f"候选材料 {item['requirement']}/{item['kind']} 已变化，须重新提交当前材料后评审。")
    return [{"source": "delivery-material", "path": item["path"], "mode": "full",
             "purpose": f"核对 {item['requirement']} 的{KIND_LABELS[item['kind']]}"}
            for item in candidate.get("delivery", {}).get("materials", [])]


def assemble_delivery(execution, review, feature_path, workspace, model):
    """读取已接受候选交回的材料；材料返修只覆盖明确指定的同一任务材料。"""
    from archive_slice_contract import reviewed_candidate
    from archive_ui import _runtime
    runtime = _runtime()
    previous = {(m["package_id"], m["requirement"], m["kind"]): m for m in model.get("delivery_materials", [])}
    updates = review.pop("material_updates", [])
    if not isinstance(updates, list):
        fail("INVALID_DELIVERY_UPDATE", "material_updates 必须是材料引用列表。")
    update_map = {}
    for item in updates:
        if not isinstance(item, dict) or not all(k in item for k in ("package_id", "requirement", "kind")):
            fail("INVALID_DELIVERY_UPDATE", "更新材料必须指定 package_id、requirement 和 kind。")
        key = (item["package_id"], item["requirement"], item["kind"])
        if key in update_map:
            fail("INVALID_DELIVERY_UPDATE", "同一材料不能重复更新。")
        update_map[key] = {k: v for k, v in item.items() if k != "package_id"}
    outcomes, materials, used_updates = {}, [], set()
    for package_id, record in execution.get("slices", {}).items():
        if record.get("status") != "accepted":
            continue
        candidate = reviewed_candidate(record)
        delivery = (candidate or {}).get("delivery")
        if not isinstance(delivery, dict):
            fail("DELIVERY_HANDOFF_MISSING", f"任务 {package_id} 未通过提交入口交接材料。")
        requirements = {r["key"]: r for r in delivery["requirements"]}
        submitted = {(m["requirement"], m["kind"]): m for m in delivery["materials"]}
        for key, material in previous.items():
            if key[0] == package_id and material.get("candidate_digest") == candidate["candidate_digest"]:
                submitted[(key[1], key[2])] = {k: v for k, v in material.items() if k not in {"package_id", "candidate_digest"}}
        for key in update_map:
            if key[0] == package_id:
                submitted.setdefault((key[1], key[2]), {})
        values = []
        for (requirement_key, kind), original in submitted.items():
            key = (package_id, requirement_key, kind)
            updated = update_map.get(key)
            if updated is not None:
                if kind in {"interaction", "ui-location"}:
                    fresh = _read_review(_path(updated.get("path"), feature_path, workspace))
                    if fresh.get("investigation_token") != runtime.investigation_token(model):
                        fail("DELIVERY_LOCATION_STALE", f"更新材料 {requirement_key}/{kind} 须关联本次调研结果。")
                values.append(updated)
                used_updates.add(key)
                continue
            path = Path(original["path"])
            if not path.is_file() or "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() != original["sha256"]:
                fail("DELIVERY_MATERIAL_STALE", f"任务 {package_id} 的材料 {requirement_key}/{kind} 已变化或缺失；通过 material_updates 交回更新文件。")
            if kind in {"interaction", "ui-location"}:
                current = _bindings(runtime, model, _read_review(path), workspace)
                if current != original.get("prefab_bindings"):
                    fail("DELIVERY_LOCATION_STALE", f"任务 {package_id} 的 {requirement_key}/{kind} 关联 UI 已变化，须更新受影响截图和定位材料。")
            values.append({k: v for k, v in original.items() if k not in {"sha256", "prefab_bindings"}})
        checked = collect_materials(list(requirements.values()), values, feature_path, workspace, final=True, current_tokens=False)
        for item in checked:
            materials.append({"package_id": package_id, "candidate_digest": candidate["candidate_digest"], **item})
            if item["kind"] != "interaction":
                continue
            for outcome in _read_review(Path(item["path"]))["outcomes"]:
                outcome = deepcopy(outcome)
                if requirements[item["requirement"]]["required"] and outcome.get("interaction"):
                    outcome["interaction"]["required_for_acceptance"] = True
                key = outcome["key"]
                if key in outcomes and outcomes[key] != outcome:
                    fail("DELIVERY_INTERACTION_CONFLICT", f"功能交互 {key} 在多个材料中内容冲突；统一该交互的负责材料后再生成。")
                outcomes[key] = outcome
    if set(update_map) != used_updates:
        fail("INVALID_DELIVERY_UPDATE", "材料更新只能引用已接受任务及其已声明的材料要求。")
    if outcomes:
        if "outcomes" in review:
            fail("DELIVERY_DUPLICATED_INPUT", "已有任务交接的交互材料；最终生成无需再次填写 outcomes，修订使用 material_updates。")
        review["outcomes"] = list(outcomes.values())
        review["investigation_token"] = runtime.investigation_token(model)
    return materials
