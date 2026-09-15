"""版本化切片方案、派生关系图与实时资格。"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Dict, Mapping, MutableMapping, Sequence


PLAN_SCHEMA_VERSION = 1


class SlicePlanError(Exception):
    """表示切片方案声明、图关系或换版约束不合法。"""

    def __init__(self, code: str, message: str, *, relation_chain: Sequence[Mapping[str, str]] = ()) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.relation_chain = tuple(relation_chain)


def digest(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def empty_plan_state() -> Mapping[str, object]:
    return {"current_version": None, "history": []}


def slice_plan_input_template(feature_id: str, plan_state: Mapping[str, object]) -> Mapping[str, object]:
    """由批准事实准备下一版草稿，不改变历史或推断切片关系。"""

    previous = current_plan(plan_state)
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "plan_id": feature_id,
        "version": previous["version"] + 1 if previous else 1,
        "slices": deepcopy(previous["plan"]["slices"]) if previous else [],
    }


def slice_plan_input_guidance() -> Mapping[str, object]:
    return {
        "slices": {
            "type": "object-list",
            "allow_empty": False,
            "fields": {
                "slice_id": "当前方案内唯一的切片标识",
                "prerequisites": "必须先通过验收的前置切片标识列表；没有时填空列表",
                "replacements": "替代当前切片的切片标识列表；没有时填空列表",
            },
            "rules": "所有引用必须在方案内，不能引用自己、重复引用或形成循环；关系由协调者按业务填写。",
            "coverage": "在现有计划正文从已批准场景反查承接切片；后续处理、复用和依赖缺失项注明去向、影响及恢复条件。关系检查不证明整体业务覆盖，不新增方案字段或独立账本。",
            "example": [
                {"slice_id": "reward-config", "prerequisites": [], "replacements": []},
                {"slice_id": "reward-ui", "prerequisites": ["reward-config"], "replacements": []},
            ],
        },
    }


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SlicePlanError("INVALID_SLICE_PLAN", f"切片方案字段 {field} 必须是非空字符串。")
    return value.strip()


def _positive_integer(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise SlicePlanError("INVALID_SLICE_PLAN", f"切片方案字段 {field} 必须是正整数。")
    return value


def _normalize_relations(
    raw: object,
    field: str,
    slice_id: str,
    relation_type: str,
) -> Sequence[str]:
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise SlicePlanError("INVALID_SLICE_PLAN", f"切片 {slice_id} 的 {field} 必须是列表。")
    result = []
    seen = set()
    for item in raw:
        target = _required_string(item, f"slices.{slice_id}.{field}")
        if target == slice_id:
            raise SlicePlanError(
                "SLICE_PLAN_SELF_REFERENCE",
                f"切片 {slice_id} 不能通过 {relation_type} 关系引用自己。",
                relation_chain=({"from": slice_id, "to": slice_id, "type": relation_type},),
            )
        if target in seen:
            raise SlicePlanError(
                "SLICE_PLAN_DUPLICATE_RELATION",
                f"切片 {slice_id} 的 {relation_type} 关系重复引用 {target}。",
                relation_chain=({"from": target, "to": slice_id, "type": relation_type},),
            )
        seen.add(target)
        result.append(target)
    return tuple(sorted(result))


def normalize_plan(value: Mapping[str, object], feature_id: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or set(value) != {"schema_version", "plan_id", "version", "slices"}:
        raise SlicePlanError("INVALID_SLICE_PLAN", "切片方案字段不完整或包含未知字段。")
    if value.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise SlicePlanError("INVALID_SLICE_PLAN", "切片方案 schema_version 不受支持。")
    plan_id = _required_string(value.get("plan_id"), "plan_id")
    if plan_id != feature_id:
        raise SlicePlanError("SLICE_PLAN_ID_MISMATCH", "切片方案标识必须与交付项标识相同。")
    version = _positive_integer(value.get("version"), "version")
    raw_slices = value.get("slices")
    if not isinstance(raw_slices, list) or not raw_slices:
        raise SlicePlanError("INVALID_SLICE_PLAN", "切片方案必须至少包含一个切片。")
    normalized = []
    seen = set()
    for raw in raw_slices:
        if not isinstance(raw, dict) or set(raw) != {"slice_id", "prerequisites", "replacements"}:
            raise SlicePlanError("INVALID_SLICE_PLAN", "切片声明字段不完整或包含未知字段。")
        slice_id = _required_string(raw.get("slice_id"), "slices.slice_id")
        if slice_id in seen:
            raise SlicePlanError("SLICE_PLAN_DUPLICATE_SLICE", f"切片方案重复声明 {slice_id}。")
        seen.add(slice_id)
        normalized.append({
            "slice_id": slice_id,
            "prerequisites": list(_normalize_relations(
                raw.get("prerequisites"), "prerequisites", slice_id, "prerequisite"
            )),
            "replacements": list(_normalize_relations(
                raw.get("replacements"), "replacements", slice_id, "replacement"
            )),
        })
    normalized.sort(key=lambda item: item["slice_id"])
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "plan_id": plan_id,
        "version": version,
        "slices": normalized,
    }


def _edges(plan: Mapping[str, object]) -> Sequence[Mapping[str, str]]:
    edges = []
    for item in plan["slices"]:
        slice_id = str(item["slice_id"])
        edges.extend(
            {"from": related, "to": slice_id, "type": "prerequisite"}
            for related in item["prerequisites"]
        )
        edges.extend(
            {"from": related, "to": slice_id, "type": "replacement"}
            for related in item["replacements"]
        )
    return tuple(sorted(edges, key=lambda item: (item["from"], item["to"], item["type"])))


def _missing_reference(plan: Mapping[str, object]) -> Mapping[str, str] | None:
    known = {str(item["slice_id"]) for item in plan["slices"]}
    return next((edge for edge in _edges(plan) if edge["from"] not in known), None)


def _shortest_cycle(
    nodes: Sequence[str], edges: Sequence[Mapping[str, str]]
) -> Sequence[Mapping[str, str]] | None:
    adjacency: Dict[str, list[Mapping[str, str]]] = {node: [] for node in nodes}
    for edge in edges:
        adjacency.setdefault(edge["from"], []).append(edge)
    for outgoing in adjacency.values():
        outgoing.sort(key=lambda item: (item["to"], item["type"]))
    candidates = []
    for start in sorted(nodes):
        queue: list[tuple[str, list[Mapping[str, str]], frozenset[str]]] = [
            (start, [], frozenset({start}))
        ]
        position = 0
        while position < len(queue):
            current, path, visited = queue[position]
            position += 1
            for edge in adjacency.get(current, []):
                target = edge["to"]
                next_path = [*path, edge]
                if target == start:
                    candidates.append(next_path)
                    queue = []
                    break
                if target not in visited and len(next_path) < len(nodes):
                    queue.append((target, next_path, visited | {target}))
    if not candidates:
        return None
    return tuple(min(
        candidates,
        key=lambda path: (
            len(path),
            tuple((edge["from"], edge["to"], edge["type"]) for edge in path),
        ),
    ))


def validate_graph(plan: Mapping[str, object]) -> Sequence[Mapping[str, str]]:
    edges = _edges(plan)
    missing = _missing_reference(plan)
    if missing is not None:
        raise SlicePlanError(
            "SLICE_PLAN_MISSING_REFERENCE",
            f"关系引用了方案中不存在的切片 {missing['from']}。",
            relation_chain=(missing,),
        )
    nodes = tuple(str(item["slice_id"]) for item in plan["slices"])
    prerequisite_edges = tuple(edge for edge in edges if edge["type"] == "prerequisite")
    replacement_edges = tuple(edge for edge in edges if edge["type"] == "replacement")
    prerequisite_cycle = _shortest_cycle(nodes, prerequisite_edges)
    if prerequisite_cycle:
        raise SlicePlanError(
            "SLICE_PLAN_PREREQUISITE_CYCLE",
            "前置关系形成循环。",
            relation_chain=prerequisite_cycle,
        )
    replacement_cycle = _shortest_cycle(nodes, replacement_edges)
    if replacement_cycle:
        raise SlicePlanError(
            "SLICE_PLAN_REPLACEMENT_CYCLE",
            "替代关系形成循环。",
            relation_chain=replacement_cycle,
        )
    mixed_cycle = _shortest_cycle(nodes, edges)
    if mixed_cycle:
        raise SlicePlanError(
            "SLICE_PLAN_MIXED_CYCLE",
            "前置与替代关系合并后形成混合循环。",
            relation_chain=mixed_cycle,
        )
    return edges


def _accepted(slice_states: Mapping[str, object], slice_id: str) -> bool:
    value = slice_states.get(slice_id)
    return isinstance(value, dict) and value.get("status") == "accepted"


def eligibility(plan: Mapping[str, object], slice_states: Mapping[str, object]) -> Mapping[str, object]:
    items = []
    ready = []
    waiting_prerequisites = []
    waiting_replacements = []
    for declaration in plan["slices"]:
        slice_id = str(declaration["slice_id"])
        pending_prerequisites = [
            related for related in declaration["prerequisites"]
            if not _accepted(slice_states, related)
        ]
        pending_replacements = [
            related for related in declaration["replacements"]
            if not _accepted(slice_states, related)
        ]
        if not pending_prerequisites and not pending_replacements:
            ready.append(slice_id)
        if pending_prerequisites:
            waiting_prerequisites.append(slice_id)
        if pending_replacements:
            waiting_replacements.append(slice_id)
        items.append({
            "slice_id": slice_id,
            "can_start": not pending_prerequisites and not pending_replacements,
            "pending_prerequisites": pending_prerequisites,
            "pending_replacements": pending_replacements,
        })
    return {
        "can_start": ready,
        "waiting_prerequisite_acceptance": waiting_prerequisites,
        "waiting_replacement_completion": waiting_replacements,
        "items": items,
    }


def evaluate_plan(
    plan: Mapping[str, object],
    slice_states: Mapping[str, object],
) -> Mapping[str, object]:
    result: Dict[str, object] = {
        "status": "valid",
        "plan_id": plan["plan_id"],
        "version": plan["version"],
        "slice_count": len(plan["slices"]),
        "plan_digest": digest(plan),
    }
    edges = validate_graph(plan)
    result["graph"] = {
        "nodes": [str(item["slice_id"]) for item in plan["slices"]],
        "edges": list(edges),
    }
    result["eligibility"] = eligibility(plan, slice_states)
    return result


def _declarations(plan: Mapping[str, object]) -> Mapping[str, Mapping[str, object]]:
    return {str(item["slice_id"]): item for item in plan["slices"]}


def _assert_upgrade_allowed(
    previous: Mapping[str, object],
    current: Mapping[str, object],
    slice_states: Mapping[str, object],
) -> None:
    before = _declarations(previous)
    after = _declarations(current)
    for slice_id, old in before.items():
        record = slice_states.get(slice_id)
        if not isinstance(record, dict):
            continue
        status = record.get("status")
        if status in {"contract_draft", "contract_failed", "ready"} or (
            status == "abandoned" and record.get("execution_id") is None
        ):
            continue
        new = after.get(slice_id)
        if new is None:
            raise SlicePlanError("SLICE_PLAN_FACT_CONFLICT", f"新方案不能删除已进入实施的切片 {slice_id}。")
        if new == old:
            continue
        raise SlicePlanError("SLICE_PLAN_FACT_CONFLICT", f"新方案不能改变已进入实施的切片 {slice_id} 的既有关系。")


def approve_plan(
    plan_state: MutableMapping[str, object],
    normalized: Mapping[str, object],
    expected_digest: str,
    slice_states: Mapping[str, object],
    approved_at: str,
) -> tuple[Mapping[str, object], bool]:
    evaluation = evaluate_plan(normalized, slice_states)
    if expected_digest != evaluation["plan_digest"]:
        raise SlicePlanError("SLICE_PLAN_DIGEST_MISMATCH", "方案摘要与刚刚检查的声明不一致。")
    history = plan_state.get("history")
    current_version = plan_state.get("current_version")
    if not isinstance(history, list):
        raise SlicePlanError("INVALID_SLICE_PLAN_STATE", "切片方案历史不合法。")
    current = next((item for item in history if item.get("version") == current_version), None)
    if current is not None:
        if normalized["version"] < current_version:
            raise SlicePlanError("SLICE_PLAN_DOWNGRADE", "切片方案不能降级批准。")
        if normalized["version"] == current_version:
            if evaluation["plan_digest"] != current.get("plan_digest"):
                raise SlicePlanError("SLICE_PLAN_SILENT_REWRITE", "同一方案版本不能静默改写。")
            return evaluation, True
        _assert_upgrade_allowed(current["plan"], normalized, slice_states)
    history.append({
        "version": normalized["version"],
        "plan_digest": evaluation["plan_digest"],
        "approved_at": approved_at,
        "plan": deepcopy(normalized),
    })
    plan_state["current_version"] = normalized["version"]
    return evaluation, False


def current_plan(plan_state: Mapping[str, object]) -> Mapping[str, object] | None:
    version = plan_state.get("current_version")
    history = plan_state.get("history")
    if version is None or not isinstance(history, list):
        return None
    record = next((item for item in history if isinstance(item, dict) and item.get("version") == version), None)
    return record if isinstance(record, dict) else None


def pending_plan_slices(execution: Mapping[str, object]) -> Sequence[str]:
    """列出当前批准方案中尚未收敛的切片，包括未登记任务包的声明。"""
    record = current_plan(execution["slice_plan"])
    if record is None:
        return ()
    states = execution["slices"]
    return tuple(
        item["slice_id"] for item in record["plan"]["slices"]
        if states.get(item["slice_id"], {}).get("status") not in {"accepted", "abandoned"}
    )


def require_slice_eligible(
    plan_state: Mapping[str, object],
    slice_states: Mapping[str, object],
    slice_id: str,
) -> Mapping[str, object]:
    record = current_plan(plan_state)
    if record is None:
        raise SlicePlanError("SLICE_PLAN_APPROVAL_REQUIRED", "启动前必须存在当前已批准切片方案。")
    plan = record.get("plan")
    if not isinstance(plan, dict) or slice_id not in _declarations(plan):
        raise SlicePlanError("SLICE_NOT_IN_CURRENT_PLAN", f"当前已批准方案不包含切片 {slice_id}。")
    result = eligibility(plan, slice_states)
    item = next(value for value in result["items"] if value["slice_id"] == slice_id)
    if not item["can_start"]:
        raise SlicePlanError(
            "SLICE_NOT_ELIGIBLE",
            f"切片 {slice_id} 尚未满足前置验收或替代完成条件。",
        )
    return {
        "plan_version": record["version"],
        "plan_digest": record["plan_digest"],
        "approved_at": record["approved_at"],
        "plan": deepcopy(record["plan"]),
    }
