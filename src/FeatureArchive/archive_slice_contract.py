"""切片契约的结构校验、实施前检查和版本记录。"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Dict, Mapping, MutableMapping, Sequence, Tuple


CONTRACT_SCHEMA_VERSION = 3
DEPENDENCY_FIELDS = {
    "name", "assumption", "strength", "availability", "long_term", "responsibility_domain",
}
DEPENDENCY_STRENGTHS = {"strong", "weak"}
DEPENDENCY_AVAILABILITIES = {"available", "unavailable", "unknown"}
UNKNOWN_FIELDS = {"id", "description", "structural", "status"}
UNKNOWN_STATUSES = {"open", "resolved"}
VALIDATION_LEVELS = ("targeted", "module_full", "project_full")
CONTRACT_APPROVAL_STATUSES = {"pending", "approved", "rejected"}
UNFILLED_INPUT_PREFIXES = ("待填写：", "待补充：")
CONTRACT_CHECK_CRITERIA = (
    "independently_acceptable",
    "strong_dependencies_available",
    "structural_unknowns_resolved",
    "impact_closed_loop",
    "observable_acceptance_covered",
    "regression_protection",
    "safe_rollback",
)
CHECK_FAILURE_ACTIONS = {
    "independently_acceptable": "new-slice",
    "strong_dependencies_available": "new-slice",
    "structural_unknowns_resolved": "investigate-before-slice",
    "impact_closed_loop": "supplement-contract",
    "observable_acceptance_covered": "supplement-contract",
    "regression_protection": "supplement-contract",
    "safe_rollback": "new-slice",
}
HARD_BREAKER_RULES = {
    "write_scope_violation": "工作区出现授权写入范围外的实际变化",
    "business_outcome_changed": "业务结果发生实质变化",
    "acceptance_changed": "验收标准发生实质变化",
    "out_of_scope_required": "明确范围外内容成为完成条件",
    "strong_dependency_invalid": "强依赖不存在或与契约假设不符",
    "public_data_change_required": "必须改变公共数据格式或存量数据",
    "invariant_break_required": "必须破坏契约不变量",
    "not_independently_acceptable": "当前结果无法独立验收",
    "rollback_invalid": "安全回退方式失效",
}
BREAKER_ALLOWED_ACTIONS = (
    {"action": "retry", "summary": "保持原契约和写入范围，修复后用新执行身份重试"},
    {"action": "terminate_restored", "summary": "人工恢复工作区基线后终止当前切片"},
)
SLICE_STATUS_TRANSITIONS = {
    "contract_draft": {"contract_failed", "ready", "abandoned"},
    "contract_failed": {"contract_failed", "ready", "abandoned"},
    "ready": {"active", "abandoned"},
    "active": {"candidate", "failed", "interrupted", "circuit_open"},
    "circuit_open": {"active", "abandoned"},
    "candidate": {"accepted", "review_failed", "active", "abandoned"},
    "review_failed": {"active", "abandoned"},
    "failed": {"active", "abandoned"},
    "interrupted": {"active", "abandoned"},
    "accepted": set(),
    "abandoned": set(),
}


class SliceContractError(Exception):
    """表示任务包中的切片契约或检查证据不合法。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def transition_slice_status(record: MutableMapping[str, object], target: str) -> None:
    current = record.get("status")
    allowed = SLICE_STATUS_TRANSITIONS.get(str(current))
    if allowed is None or target not in allowed:
        raise SliceContractError(
            "INVALID_SLICE_TRANSITION",
            f"切片状态不能从 {current} 转换为 {target}。",
        )
    record["status"] = target


def digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def latest_review_snapshot(record: Mapping[str, object]) -> Mapping[str, object] | None:
    snapshots = record.get("review_snapshots")
    if not isinstance(snapshots, list) or not snapshots:
        return None
    latest = snapshots[-1]
    return latest if isinstance(latest, dict) else None


def reviewed_candidate(record: Mapping[str, object]) -> Mapping[str, object] | None:
    snapshot = latest_review_snapshot(record)
    candidate = snapshot.get("candidate") if isinstance(snapshot, dict) else None
    return candidate if isinstance(candidate, dict) else None


def latest_review(record: Mapping[str, object]) -> Mapping[str, object] | None:
    snapshot = latest_review_snapshot(record)
    review = snapshot.get("review") if isinstance(snapshot, dict) else None
    return review if isinstance(review, dict) else None


def required_string(value: object, field: str, code: str = "INVALID_SLICE_CONTRACT") -> str:
    if not isinstance(value, str) or not value.strip():
        raise SliceContractError(code, f"字段 {field} 必须是非空字符串。")
    return value.strip()


def _reject_unfilled_input(value: object, field: str, code: str) -> None:
    if isinstance(value, str):
        normalized = value.strip()
        if any(normalized.startswith(prefix) for prefix in UNFILLED_INPUT_PREFIXES):
            raise SliceContractError(code, f"字段 {field} 仍为待填写占位内容。")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_unfilled_input(item, f"{field}.{key}", code)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _reject_unfilled_input(item, f"{field}[{index}]", code)


def string_list(
    value: object,
    field: str,
    *,
    allow_empty: bool = False,
    code: str = "INVALID_SLICE_CONTRACT",
) -> Sequence[str]:
    if not isinstance(value, list) or (not allow_empty and not value):
        raise SliceContractError(code, f"字段 {field} 必须是字符串列表。")
    result = [required_string(item, field, code) for item in value]
    if len(result) != len(set(result)):
        raise SliceContractError(code, f"字段 {field} 不能包含重复值。")
    return tuple(result)


def slice_contract_input_guidance() -> Mapping[str, object]:
    """只为填写契约提供对象示例，不向正式输入写入示例事实。"""

    return {
        "slice_contract.dependency_assumptions": {
            "type": "object-list",
            "allow_empty": True,
            "required_fields": sorted(DEPENDENCY_FIELDS),
            "fields": {
                "name": "非空且唯一的依赖名称",
                "assumption": "该依赖应当成立的具体事实",
                "strength": "strong 表示必须依赖，weak 表示弱依赖",
                "availability": "available 已核实可用；unavailable 不可用；unknown 尚未核实",
                "long_term": "布尔值，是否属于长期依赖",
                "responsibility_domain": "依赖事实由哪个责任域提供",
            },
            "allowed_values": {
                "strength": sorted(DEPENDENCY_STRENGTHS),
                "availability": sorted(DEPENDENCY_AVAILABILITIES),
                "long_term": [False, True],
            },
            "example": {
                "name": "奖励配置", "assumption": "提供当前物品编号及奖励数量",
                "strength": "strong", "availability": "unknown", "long_term": True,
                "responsibility_domain": "活动配置负责人",
            },
        },
        "slice_contract.unknowns": {
            "type": "object-list",
            "allow_empty": True,
            "required_fields": sorted(UNKNOWN_FIELDS),
            "fields": {
                "id": "非空且唯一的问题标识", "description": "尚需确认的具体问题",
                "structural": "布尔值，是否影响实施结构；结构性未知未解决时不能启动",
                "status": "open 尚未解决；resolved 已有事实结论",
            },
            "allowed_values": {"structural": [False, True], "status": sorted(UNKNOWN_STATUSES)},
            "example": {
                "id": "reward-source", "description": "正式奖励配置由谁提供",
                "structural": True, "status": "open",
            },
        },
    }


def _dependencies(value: object) -> Sequence[Mapping[str, object]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise SliceContractError("INVALID_SLICE_CONTRACT", "依赖假设必须是对象列表。")
    result = []
    names = set()
    for item in value:
        if set(item) != DEPENDENCY_FIELDS:
            raise SliceContractError("INVALID_SLICE_CONTRACT", "依赖假设字段不完整。")
        name = required_string(item.get("name"), "dependency_assumptions.name")
        if name in names:
            raise SliceContractError("INVALID_SLICE_CONTRACT", "依赖假设名称不能重复。")
        names.add(name)
        strength = required_string(item.get("strength"), "dependency_assumptions.strength")
        availability = required_string(item.get("availability"), "dependency_assumptions.availability")
        if strength not in DEPENDENCY_STRENGTHS:
            raise SliceContractError("INVALID_SLICE_CONTRACT", "依赖强度必须为 strong 或 weak。")
        if availability not in DEPENDENCY_AVAILABILITIES:
            raise SliceContractError("INVALID_SLICE_CONTRACT", "依赖可用性声明不合法。")
        if not isinstance(item.get("long_term"), bool):
            raise SliceContractError("INVALID_SLICE_CONTRACT", "依赖 long_term 必须为布尔值。")
        result.append({
            "name": name,
            "assumption": required_string(item.get("assumption"), "dependency_assumptions.assumption"),
            "strength": strength,
            "availability": availability,
            "long_term": item["long_term"],
            "responsibility_domain": required_string(
                item.get("responsibility_domain"), "dependency_assumptions.responsibility_domain"
            ),
        })
    return tuple(result)


def _unknowns(value: object) -> Sequence[Mapping[str, object]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise SliceContractError("INVALID_SLICE_CONTRACT", "未知项必须是对象列表。")
    result = []
    identifiers = set()
    for item in value:
        if set(item) != UNKNOWN_FIELDS:
            raise SliceContractError("INVALID_SLICE_CONTRACT", "未知项字段不完整。")
        identifier = required_string(item.get("id"), "unknowns.id")
        if identifier in identifiers:
            raise SliceContractError("INVALID_SLICE_CONTRACT", "未知项标识不能重复。")
        identifiers.add(identifier)
        if not isinstance(item.get("structural"), bool) or item.get("status") not in UNKNOWN_STATUSES:
            raise SliceContractError("INVALID_SLICE_CONTRACT", "未知项状态不合法。")
        result.append({
            "id": identifier,
            "description": required_string(item.get("description"), "unknowns.description"),
            "structural": item["structural"],
            "status": item["status"],
        })
    return tuple(result)


def normalize_contract(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise SliceContractError("SLICE_CONTRACT_REQUIRED", "任务包必须包含切片契约对象。")
    _reject_unfilled_input(value, "slice_contract", "INVALID_SLICE_CONTRACT")
    required = {
        "schema_version", "version", "revision_summary", "business_outcome",
        "acceptance_scenarios", "in_scope", "out_of_scope", "dependency_assumptions",
        "expected_impact_areas", "invariants", "validation_level", "validation_rationale",
        "validation_methods", "unknowns",
        "rollback_point", "circuit_breaker_conditions", "decision_owner", "approval_status",
    }
    if set(value) != required:
        missing = "、".join(sorted(required - set(value)))
        unknown = "、".join(sorted(set(value) - required))
        detail = "；".join(item for item in (f"缺少：{missing}" if missing else "", f"未知：{unknown}" if unknown else "") if item)
        raise SliceContractError("INVALID_SLICE_CONTRACT", f"切片契约字段不完整（{detail}）。")
    if value.get("schema_version") != CONTRACT_SCHEMA_VERSION:
        raise SliceContractError("INVALID_SLICE_CONTRACT", "切片契约格式版本不受支持。")
    version = value.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version <= 0:
        raise SliceContractError("INVALID_SLICE_CONTRACT", "契约 version 必须是正整数。")
    scope_fields = {"business_behaviors", "data_responsibilities", "system_boundaries", "lifecycle"}
    scope = value.get("in_scope")
    if not isinstance(scope, dict) or set(scope) != scope_fields:
        raise SliceContractError("INVALID_SLICE_CONTRACT", "范围内必须声明业务行为、数据责任、系统边界和生命周期。")
    impact_fields = {"modules", "resources", "data_boundaries"}
    impact = value.get("expected_impact_areas")
    if not isinstance(impact, dict) or set(impact) != impact_fields:
        raise SliceContractError("INVALID_SLICE_CONTRACT", "预计影响区域字段不完整。")
    normalized_impact = {
        field: list(string_list(impact.get(field), f"expected_impact_areas.{field}", allow_empty=True))
        for field in sorted(impact_fields)
    }
    if not any(normalized_impact.values()):
        raise SliceContractError("INVALID_SLICE_CONTRACT", "预计影响区域不能全部为空。")
    approval = required_string(value.get("approval_status"), "approval_status")
    if approval not in CONTRACT_APPROVAL_STATUSES:
        raise SliceContractError("INVALID_SLICE_CONTRACT", "契约批准状态不合法。")
    validation_level = required_string(value.get("validation_level"), "validation_level")
    if validation_level not in VALIDATION_LEVELS:
        raise SliceContractError("INVALID_SLICE_CONTRACT", "测试层级不合法。")
    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "version": version,
        "revision_summary": required_string(value.get("revision_summary"), "revision_summary"),
        "business_outcome": required_string(value.get("business_outcome"), "business_outcome"),
        "acceptance_scenarios": list(string_list(value.get("acceptance_scenarios"), "acceptance_scenarios")),
        "in_scope": {field: list(string_list(scope.get(field), f"in_scope.{field}")) for field in sorted(scope_fields)},
        "out_of_scope": list(string_list(value.get("out_of_scope"), "out_of_scope", allow_empty=True)),
        "dependency_assumptions": list(_dependencies(value.get("dependency_assumptions"))),
        "expected_impact_areas": normalized_impact,
        "invariants": list(string_list(value.get("invariants"), "invariants")),
        "validation_level": validation_level,
        "validation_rationale": required_string(
            value.get("validation_rationale"), "validation_rationale"
        ),
        "validation_methods": list(string_list(value.get("validation_methods"), "validation_methods")),
        "unknowns": list(_unknowns(value.get("unknowns"))),
        "rollback_point": required_string(value.get("rollback_point"), "rollback_point"),
        "circuit_breaker_conditions": list(string_list(value.get("circuit_breaker_conditions"), "circuit_breaker_conditions")),
        "decision_owner": required_string(value.get("decision_owner"), "decision_owner"),
        "approval_status": approval,
    }


def normalize_contract_check(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict) or set(value) != set(CONTRACT_CHECK_CRITERIA):
        raise SliceContractError("CONTRACT_CHECK_REQUIRED", "任务包必须完整提交七项实施前契约检查证据。")
    _reject_unfilled_input(value, "contract_check", "INVALID_CONTRACT_CHECK")
    result: Dict[str, object] = {}
    for criterion in CONTRACT_CHECK_CRITERIA:
        item = value.get(criterion)
        if not isinstance(item, dict) or set(item) != {"passed", "evidence"} or not isinstance(item.get("passed"), bool):
            raise SliceContractError("INVALID_CONTRACT_CHECK", f"契约检查 {criterion} 必须声明布尔结论和证据。")
        result[criterion] = {
            "passed": item["passed"],
            "evidence": required_string(item.get("evidence"), f"contract_check.{criterion}.evidence", "INVALID_CONTRACT_CHECK"),
        }
    return result


def normalize_task_contract_fields(package: Mapping[str, object]) -> Tuple[Mapping[str, object], Mapping[str, object]]:
    contract = normalize_contract(package.get("slice_contract"))
    check = normalize_contract_check(package.get("contract_check"))
    return contract, check


def evaluate_contract(contract: Mapping[str, object], check: Mapping[str, object], checked_at: str) -> Mapping[str, object]:
    normalized_contract = normalize_contract(contract)
    normalized_check = normalize_contract_check(check)
    reasons = []
    actions = []
    for criterion in CONTRACT_CHECK_CRITERIA:
        if normalized_check[criterion]["passed"] is False:
            reasons.append(f"{criterion} 未通过：{normalized_check[criterion]['evidence']}")
            actions.append(CHECK_FAILURE_ACTIONS[criterion])
    strong_available = all(
        item["availability"] == "available"
        for item in normalized_contract["dependency_assumptions"]
        if item["strength"] == "strong"
    )
    structural_resolved = all(
        item["status"] == "resolved"
        for item in normalized_contract["unknowns"]
        if item["structural"] is True
    )
    if normalized_check["strong_dependencies_available"]["passed"] != strong_available:
        reasons.append("强依赖可用性检查与契约事实不一致。")
        actions.append("prerequisite-slice")
    if normalized_check["structural_unknowns_resolved"]["passed"] != structural_resolved:
        reasons.append("结构性未知项检查与契约事实不一致。")
        actions.append("exploration-task")
    if normalized_contract["approval_status"] != "approved":
        reasons.append("切片契约尚未由决策负责人批准。")
        actions.append("supplement-contract")
    result = {
        "status": "passed" if not reasons else "failed",
        "contract_version": normalized_contract["version"],
        "contract_digest": digest(normalized_contract),
        "criteria": normalized_check,
        "failure_reasons": reasons,
        "recommended_actions": list(dict.fromkeys(actions)),
        "checked_at": checked_at,
    }
    result["check_digest"] = digest(result)
    return result


def task_package_digest(package: Mapping[str, object]) -> str:
    return digest({key: value for key, value in package.items() if key != "contract_check"})


def _history_entry(package: Mapping[str, object], check: Mapping[str, object] | None, recorded_at: str) -> Mapping[str, object]:
    contract = package["slice_contract"]
    return {
        "version": contract["version"],
        "revision_summary": contract["revision_summary"],
        "contract_digest": check["contract_digest"] if isinstance(check, dict) else digest(contract),
        "package_digest": task_package_digest(package),
        "recorded_at": recorded_at,
        "contract": deepcopy(contract),
    }


def upsert_contract_record(
    slices: MutableMapping[str, object],
    package: Mapping[str, object],
    recorded_at: str,
    *,
    evaluate: bool,
) -> Tuple[MutableMapping[str, object], Mapping[str, object] | None, bool]:
    package_id = str(package["package_id"])
    check = evaluate_contract(package["slice_contract"], package["contract_check"], recorded_at) if evaluate else None
    package_digest = task_package_digest(package)
    existing = slices.get(package_id)
    if existing is None:
        record: MutableMapping[str, object] = {
            "package": deepcopy(package),
            "execution_id": None,
            "workspace_root": None,
            "status": "ready" if check and check["status"] == "passed" else "contract_failed" if check else "contract_draft",
            "candidate": None,
            "review_snapshots": [],
            "contract_check": deepcopy(check),
            "contract_history": [_history_entry(package, check, recorded_at)],
            "checkpoints": [],
            "breaker_reports": [],
            "started_at": None,
            "updated_at": recorded_at,
        }
        slices[package_id] = record
        return record, check, False
    if not isinstance(existing, dict) or existing.get("status") not in {"contract_draft", "contract_failed", "ready"}:
        raise SliceContractError("SLICE_ALREADY_EXISTS", f"切片“{package_id}”已经进入实施或历史终态，不能静默改写契约。")
    old_package = existing.get("package")
    if not isinstance(old_package, dict) or not isinstance(old_package.get("slice_contract"), dict):
        raise SliceContractError("INVALID_EXECUTION_STATE", "已有切片缺少契约。")
    old_version = old_package["slice_contract"].get("version")
    new_version = package["slice_contract"].get("version")
    if new_version < old_version:
        raise SliceContractError("CONTRACT_VERSION_CONFLICT", "不能回退切片契约版本。")
    old_digest = task_package_digest(old_package)
    if new_version == old_version and package_digest != old_digest:
        raise SliceContractError("CONTRACT_SILENT_REWRITE_FORBIDDEN", "同一契约版本发生变化；必须提升版本并记录修订摘要。")
    idempotent = new_version == old_version and package_digest == old_digest
    if idempotent and (not evaluate or (
        existing.get("contract_check") is not None
        and existing["contract_check"]["criteria"] == package["contract_check"]
    )):
        return existing, existing.get("contract_check"), True
    if new_version > old_version:
        existing["contract_history"].append(_history_entry(package, check, recorded_at))
        existing["package"] = deepcopy(package)
    elif evaluate and existing.get("contract_history"):
        recorded = existing["contract_history"][-1]["recorded_at"]
        existing["contract_history"][-1] = _history_entry(package, check, recorded)
    existing["contract_check"] = deepcopy(check)
    existing["status"] = "ready" if check and check["status"] == "passed" else "contract_failed" if check else "contract_draft"
    existing["updated_at"] = recorded_at
    return existing, check, idempotent
