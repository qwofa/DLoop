from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
from typing import Any, Iterable


TRACE_SCHEMA_VERSION = 1
SCENARIO_SCHEMA_VERSION = 1
EXECUTION_MODE = "model-protocol-dry-run"
TRACE_CAPTURE = "self-reported-semantic-trace"
EVENT_TYPES = {"read", "decision", "command", "write", "state", "stop", "handoff"}
RULE_TYPES = {
    "present",
    "absent",
    "ordered",
    "final",
    "final_nonempty",
    "all_reads_in",
    "all_writes_under",
    "no_event_after",
}
EVENT_REQUIRED_STRINGS = {
    "read": ("category", "target"),
    "decision": ("name",),
    "command": ("name",),
    "write": ("target",),
    "state": ("to",),
    "stop": ("reason",),
    "handoff": ("to", "summary"),
}
FINAL_FIELDS = {"state", "stop_reason", "handoff_to", "summary", "blockers"}
COMPLIANCE_CATEGORIES = (
    "action_selection",
    "state_transition",
    "material_boundary",
    "handoff_quality",
)


class EvalContractError(ValueError):
    pass


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _relative_path_parts(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        return None
    normalized = value[:-1] if value.endswith("/") else value
    if not normalized or normalized.startswith("/"):
        return None
    parts = tuple(normalized.split("/"))
    if any(not part or part in {".", ".."} or ":" in part for part in parts):
        return None
    return parts


def _path_is_under(value: object, prefixes: Iterable[object]) -> bool:
    target = _relative_path_parts(value)
    if target is None:
        return False
    for prefix_value in prefixes:
        prefix = _relative_path_parts(prefix_value)
        if prefix is not None and target[:len(prefix)] == prefix:
            return True
    return False


def _validate_event_matcher(value: object, label: str) -> None:
    if not isinstance(value, dict) or value.get("type") not in EVENT_TYPES:
        raise EvalContractError(f"{label}必须是带合法 type 的事件匹配器")


def _validate_rule(rule: object, scenario_id: str, *, is_check: bool) -> str:
    if not isinstance(rule, dict):
        raise EvalContractError(f"场景规则必须是对象：{scenario_id}")
    rule_id = rule.get("id")
    if not _nonempty_string(rule_id):
        raise EvalContractError(f"场景规则缺少 id：{scenario_id}")
    rule_type = rule.get("type")
    if rule_type not in RULE_TYPES:
        raise EvalContractError(f"场景规则类型非法：{scenario_id}/{rule_id}")
    label = f"场景规则 {scenario_id}/{rule_id}"
    if rule_type in {"present", "absent"}:
        _validate_event_matcher(rule.get("event"), label)
    elif rule_type == "ordered":
        events = rule.get("events")
        if not isinstance(events, list) or not events:
            raise EvalContractError(f"{label}的 events 必须是非空数组")
        for event in events:
            _validate_event_matcher(event, label)
    elif rule_type == "final":
        if not isinstance(rule.get("fields"), dict) or not rule["fields"]:
            raise EvalContractError(f"{label}的 fields 必须是非空对象")
    elif rule_type == "final_nonempty":
        fields = rule.get("fields")
        if (
            not isinstance(fields, list)
            or not fields
            or not all(_nonempty_string(field) for field in fields)
        ):
            raise EvalContractError(f"{label}的 fields 必须是非空字符串数组")
    elif rule_type == "all_reads_in":
        categories = rule.get("categories")
        if (
            not isinstance(categories, list)
            or not categories
            or not all(_nonempty_string(category) for category in categories)
        ):
            raise EvalContractError(f"{label}的 categories 必须是非空字符串数组")
    elif rule_type == "all_writes_under":
        prefixes = rule.get("prefixes")
        if (
            not isinstance(prefixes, list)
            or not prefixes
            or not all(_relative_path_parts(prefix) is not None for prefix in prefixes)
        ):
            raise EvalContractError(f"{label}的 prefixes 必须是规范相对路径数组")
    elif rule_type == "no_event_after":
        _validate_event_matcher(rule.get("anchor"), label)
        _validate_event_matcher(rule.get("event"), label)
    if is_check:
        if rule.get("category") not in COMPLIANCE_CATEGORIES:
            raise EvalContractError(f"场景合规分类非法：{scenario_id}/{rule_id}")
        weight = rule.get("weight", 1)
        if (
            isinstance(weight, bool)
            or not isinstance(weight, (int, float))
            or weight <= 0
        ):
            raise EvalContractError(f"场景权重必须大于零：{scenario_id}/{rule_id}")
    return rule_id


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise EvalContractError(f"无法读取 JSON：{path}：{error}") from error
    if not isinstance(value, dict):
        raise EvalContractError(f"JSON 顶层必须是对象：{path}")
    return value


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def load_scenarios(root: Path, include_holdout: bool) -> list[dict[str, Any]]:
    scenario_root = root / "scenarios"
    paths = sorted((scenario_root / "public").glob("*.json"))
    if include_holdout:
        paths.extend(sorted((scenario_root / "holdout").glob("*.json")))
    scenarios = [_read_json(path) for path in paths]
    seen: set[str] = set()
    for scenario, path in zip(scenarios, paths):
        if scenario.get("schema_version") != SCENARIO_SCHEMA_VERSION:
            raise EvalContractError(f"场景版本错误：{path}")
        scenario_id = scenario.get("id")
        if not isinstance(scenario_id, str) or not scenario_id:
            raise EvalContractError(f"场景缺少 id：{path}")
        if scenario_id in seen:
            raise EvalContractError(f"场景 id 重复：{scenario_id}")
        seen.add(scenario_id)
        if scenario.get("visibility") not in {"public", "holdout"}:
            raise EvalContractError(f"场景 visibility 非法：{scenario_id}")
        if scenario["visibility"] != path.parent.name:
            raise EvalContractError(f"场景 visibility 与目录不一致：{scenario_id}")
        for field in ("title", "task"):
            if not _nonempty_string(scenario.get(field)):
                raise EvalContractError(f"场景缺少 {field}：{scenario_id}")
        if not isinstance(scenario.get("fixture"), dict):
            raise EvalContractError(f"场景 fixture 必须是对象：{scenario_id}")
        if not isinstance(scenario.get("hard_gates"), list):
            raise EvalContractError(f"场景 hard_gates 必须是数组：{scenario_id}")
        if not isinstance(scenario.get("checks"), list):
            raise EvalContractError(f"场景 checks 必须是数组：{scenario_id}")
        rule_ids = [
            *(
                _validate_rule(rule, scenario_id, is_check=False)
                for rule in scenario["hard_gates"]
            ),
            *(
                _validate_rule(rule, scenario_id, is_check=True)
                for rule in scenario["checks"]
            ),
        ]
        if len(rule_ids) != len(set(rule_ids)):
            raise EvalContractError(f"场景规则 id 重复：{scenario_id}")
    return scenarios


def _skill_digest(skill_root: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(path for path in skill_root.rglob("*") if path.is_file())
    if not files:
        raise EvalContractError(f"skill 目录没有文件：{skill_root}")
    for path in files:
        relative = path.relative_to(skill_root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        payload = path.read_bytes()
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return "sha256:" + digest.hexdigest()


def _skill_inventory(skill_root: Path) -> dict[str, Any]:
    files = {
        path.relative_to(skill_root).as_posix(): path.stat().st_size
        for path in sorted(skill_root.rglob("*"))
        if path.is_file()
    }
    return {
        "entry_bytes": files.get("SKILL.md", 0),
        "reference_bytes": sum(
            size for path, size in files.items() if path.startswith("references/")
        ),
        "total_bytes": sum(files.values()),
        "files": files,
        "interpretation": "静态材料表面，不等同于实际上下文 token",
    }


def _task_text(scenario: dict[str, Any]) -> str:
    return (
        f"# {scenario['title']}\n\n"
        f"场景标识：`{scenario['id']}`\n\n"
        f"{scenario['task']}\n\n"
        "先读取运行包根的 `trace-vocabulary.md`。随后只读取本运行包的 `skill-snapshot/`、"
        "本场景 `fixture.json`、对应的空 trace 模板，以及 fixture 明确授权的模拟项目事实。"
        "不得读取评测源码、其他场景、判分规则或真实用户工作树。不要实际调用真实 DLoop CLI；"
        "按 trace 模板记录在这些事实下会发生的读取、选择、命令、写入、状态迁移、停止点和交接。"
        "命令事件必须使用业务命令名；模拟写入只能使用 fixture 中的相对路径。"
    )


def _trace_template(scenario_id: str, variant: str) -> dict[str, Any]:
    return {
        "schema_version": TRACE_SCHEMA_VERSION,
        "scenario_id": scenario_id,
        "variant": variant,
        "execution": {
            "status": "not_run",
            "mode": EXECUTION_MODE,
            "executor": None,
            "started_at": None,
            "completed_at": None,
            "capture": TRACE_CAPTURE,
        },
        "events": [],
        "metrics": {"turns": None, "context_tokens": None, "elapsed_ms": None},
        "final": {
            "state": None,
            "stop_reason": None,
            "handoff_to": None,
            "summary": None,
            "blockers": [],
        },
        "manual_review": {
            "status": "pending",
            "judgment_quality": None,
            "notes": None,
        },
    }


def prepare_run(
    eval_root: Path,
    skill_root: Path,
    run_dir: Path,
    *,
    variant: str,
    include_holdout: bool,
) -> dict[str, Any]:
    if run_dir.exists():
        raise EvalContractError(f"运行目录已存在，不覆盖：{run_dir}")
    scenarios = load_scenarios(eval_root, include_holdout)
    run_dir.mkdir(parents=True)
    snapshot = run_dir / "skill-snapshot"
    shutil.copytree(skill_root, snapshot)
    shutil.copy2(eval_root / "trace-vocabulary.md", run_dir / "trace-vocabulary.md")
    manifest = {
        "schema_version": 1,
        "variant": variant,
        "skill_digest": _skill_digest(snapshot),
        "execution_mode": EXECUTION_MODE,
        "trace_capture": TRACE_CAPTURE,
        "skill_material": _skill_inventory(snapshot),
        "holdout_included": include_holdout,
        "scenario_ids": [scenario["id"] for scenario in scenarios],
    }
    _write_json(run_dir / "manifest.json", manifest)
    for scenario in scenarios:
        task_dir = run_dir / "tasks" / scenario["id"]
        task_dir.mkdir(parents=True)
        (task_dir / "task.md").write_text(
            _task_text(scenario), encoding="utf-8", newline="\n"
        )
        _write_json(task_dir / "fixture.json", scenario["fixture"])
        _write_json(
            run_dir / "traces" / f"{scenario['id']}.json",
            _trace_template(scenario["id"], variant),
        )
    return manifest


def _validate_trace(trace: dict[str, Any], scenario_id: str, variant: str) -> None:
    if trace.get("schema_version") != TRACE_SCHEMA_VERSION:
        raise EvalContractError(f"trace 版本错误：{scenario_id}")
    if trace.get("scenario_id") != scenario_id:
        raise EvalContractError(f"trace 场景不匹配：{scenario_id}")
    if trace.get("variant") != variant:
        raise EvalContractError(f"trace variant 不匹配：{scenario_id}")
    execution = trace.get("execution")
    if not isinstance(execution, dict) or execution.get("status") not in {
        "completed", "not_run", "error"
    }:
        raise EvalContractError(f"trace execution 非法：{scenario_id}")
    if execution.get("mode") != EXECUTION_MODE:
        raise EvalContractError(f"trace execution mode 非法：{scenario_id}")
    if execution.get("capture") != TRACE_CAPTURE:
        raise EvalContractError(f"trace capture 非法：{scenario_id}")
    if execution["status"] == "completed":
        for field in ("executor", "started_at", "completed_at"):
            if not _nonempty_string(execution.get(field)):
                raise EvalContractError(f"已完成 trace 缺少 execution.{field}：{scenario_id}")
    events = trace.get("events")
    if not isinstance(events, list):
        raise EvalContractError(f"trace events 必须是数组：{scenario_id}")
    if execution["status"] == "not_run" and events:
        raise EvalContractError(f"未运行 trace 不得包含事件：{scenario_id}")
    previous = 0
    for event in events:
        if not isinstance(event, dict) or event.get("type") not in EVENT_TYPES:
            raise EvalContractError(f"trace event 非法：{scenario_id}")
        sequence = event.get("sequence")
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence <= previous
        ):
            raise EvalContractError(f"trace sequence 必须严格递增：{scenario_id}")
        previous = sequence
        for field in EVENT_REQUIRED_STRINGS[event["type"]]:
            if not _nonempty_string(event.get(field)):
                raise EvalContractError(
                    f"trace {event['type']} 事件缺少 {field}：{scenario_id}"
                )
        if event["type"] == "write" and _relative_path_parts(event["target"]) is None:
            raise EvalContractError(f"trace write 目标不是规范相对路径：{scenario_id}")
        if "mode" in event and not _nonempty_string(event["mode"]):
            raise EvalContractError(f"trace event mode 非法：{scenario_id}")
        if "category" in event and not _nonempty_string(event["category"]):
            raise EvalContractError(f"trace event category 非法：{scenario_id}")
        if "args" in event and not isinstance(event["args"], dict):
            raise EvalContractError(f"trace event args 必须是对象：{scenario_id}")
        if (
            "from" in event
            and event["from"] is not None
            and not _nonempty_string(event["from"])
        ):
            raise EvalContractError(f"trace state from 非法：{scenario_id}")
        if (
            "handoff_to" in event
            and event["handoff_to"] is not None
            and not _nonempty_string(event["handoff_to"])
        ):
            raise EvalContractError(f"trace event handoff_to 非法：{scenario_id}")
        if "blockers" in event and (
            not isinstance(event["blockers"], list)
            or not all(_nonempty_string(item) for item in event["blockers"])
        ):
            raise EvalContractError(f"trace event blockers 非法：{scenario_id}")
    if not isinstance(trace.get("metrics"), dict):
        raise EvalContractError(f"trace metrics 必须是对象：{scenario_id}")
    final = trace.get("final")
    if not isinstance(final, dict):
        raise EvalContractError(f"trace final 必须是对象：{scenario_id}")
    if not FINAL_FIELDS.issubset(final):
        raise EvalContractError(f"trace final 字段不完整：{scenario_id}")
    if not isinstance(final["blockers"], list) or not all(
        _nonempty_string(item) for item in final["blockers"]
    ):
        raise EvalContractError(f"trace final blockers 非法：{scenario_id}")
    for field in ("state", "stop_reason", "handoff_to", "summary"):
        if final[field] is not None and not _nonempty_string(final[field]):
            raise EvalContractError(f"trace final {field} 非法：{scenario_id}")
    if execution["status"] == "completed":
        for field in ("state", "stop_reason", "summary"):
            if not _nonempty_string(final[field]):
                raise EvalContractError(f"已完成 trace 缺少 final.{field}：{scenario_id}")
    if execution["status"] == "not_run" and (
        any(
            execution.get(field) is not None
            for field in ("executor", "started_at", "completed_at")
        )
        or any(
            final[field] is not None
            for field in ("state", "stop_reason", "handoff_to", "summary")
        )
        or final["blockers"]
    ):
        raise EvalContractError(f"未运行 trace 不得包含最终结果：{scenario_id}")


def _matches(value: dict[str, Any], matcher: dict[str, Any]) -> bool:
    coordinator_aliases = {"coordinator", "main_coordinator"}
    for key, expected in matcher.items():
        actual = value.get(key)
        if key in {"handoff_to", "to"} and actual in coordinator_aliases and expected in coordinator_aliases:
            continue
        if actual != expected:
            return False
    return True


def _find_order(events: list[dict[str, Any]], matchers: Iterable[dict[str, Any]]) -> bool:
    offset = 0
    for matcher in matchers:
        for index in range(offset, len(events)):
            if _matches(events[index], matcher):
                offset = index + 1
                break
        else:
            return False
    return True


def _read_is_allowed(
    event: dict[str, Any],
    allowed: set[str],
    allowed_project_facts: set[str] | None = None,
) -> bool:
    category = event.get("category")
    target = event.get("target")
    category_allowed = category in allowed or (
        "role_view" in allowed
        and isinstance(target, str)
        and (
            target == "delivery_view"
            or target.startswith("delivery_view:")
            or target.startswith("delivery_view.")
        )
    )
    if not category_allowed:
        return False
    if category == "project_fact" and allowed_project_facts is not None:
        return isinstance(target, str) and target in allowed_project_facts
    return True


def _evaluate_rule(
    rule: dict[str, Any], trace: dict[str, Any], scenario: dict[str, Any]
) -> tuple[bool, str]:
    events = trace["events"]
    rule_type = rule.get("type")
    if rule_type == "present":
        passed = any(_matches(event, rule["event"]) for event in events)
    elif rule_type == "absent":
        passed = not any(_matches(event, rule["event"]) for event in events)
    elif rule_type == "ordered":
        passed = _find_order(events, rule["events"])
    elif rule_type == "final":
        passed = _matches(trace["final"], rule["fields"])
    elif rule_type == "final_nonempty":
        passed = all(trace["final"].get(field) for field in rule["fields"])
    elif rule_type == "all_reads_in":
        allowed = set(rule["categories"])
        project_facts = scenario["fixture"].get("allowed_project_facts")
        allowed_project_facts = (
            set(project_facts) if project_facts is not None else None
        )
        passed = all(
            _read_is_allowed(event, allowed, allowed_project_facts)
            for event in events
            if event["type"] == "read"
        )
    elif rule_type == "all_writes_under":
        passed = all(
            _path_is_under(event.get("target"), rule["prefixes"])
            for event in events
            if event["type"] == "write"
        )
    elif rule_type == "no_event_after":
        anchor_indexes = [
            index for index, event in enumerate(events) if _matches(event, rule["anchor"])
        ]
        passed = bool(anchor_indexes) and not any(
            _matches(event, rule["event"])
            for event in events[anchor_indexes[-1] + 1 :]
        )
    else:
        raise EvalContractError(
            f"未知规则类型 {rule_type!r}：{scenario['id']}/{rule.get('id')}"
        )
    return passed, rule.get("description", rule.get("id", rule_type))


def _efficiency(trace: dict[str, Any], scenario: dict[str, Any]) -> dict[str, Any]:
    events = trace["events"]
    allowed = set(scenario.get("efficiency", {}).get("allowed_read_categories", []))
    project_facts = scenario["fixture"].get("allowed_project_facts")
    allowed_project_facts = set(project_facts) if project_facts is not None else None
    reads = [event for event in events if event["type"] == "read"]
    unrelated = [
        event
        for event in reads
        if not _read_is_allowed(event, allowed, allowed_project_facts)
    ]
    read_keys = [(event.get("category"), event.get("target")) for event in reads]
    duplicate_reads = len(read_keys) - len(set(read_keys))
    commands = [event for event in events if event["type"] == "command"]
    command_keys = [
        (
            event.get("name"),
            event.get("mode"),
            json.dumps(event.get("args", {}), sort_keys=True, ensure_ascii=False),
        )
        for event in commands
    ]
    duplicate_commands = len(command_keys) - len(set(command_keys))
    score = max(0, 100 - 15 * len(unrelated) - 8 * duplicate_reads - 8 * duplicate_commands)
    optional_metrics = {
        key: trace.get("metrics", {}).get(key)
        for key in ("turns", "context_tokens", "elapsed_ms")
    }
    return {
        "score": score,
        "unrelated_reads": len(unrelated),
        "duplicate_reads": duplicate_reads,
        "duplicate_commands": duplicate_commands,
        "optional_metrics": optional_metrics,
        "optional_metrics_scored": False,
    }


def _grade_scenario(scenario: dict[str, Any], trace: dict[str, Any]) -> dict[str, Any]:
    status = trace["execution"]["status"]
    if status != "completed":
        return {
            "scenario_id": scenario["id"],
            "title": scenario["title"],
            "visibility": scenario["visibility"],
            "status": "not_run" if status == "not_run" else "error",
            "hard_gate_passed": None,
            "hard_gate_failures": [],
            "compliance": None,
            "efficiency": None,
            "manual_review": trace.get("manual_review", {"status": "pending"}),
            "trace": trace,
        }

    hard_results = []
    for rule in scenario["hard_gates"]:
        passed, description = _evaluate_rule(rule, trace, scenario)
        hard_results.append({"id": rule["id"], "passed": passed, "description": description})

    category_scores: dict[str, dict[str, float]] = {
        category: {"earned": 0.0, "available": 0.0}
        for category in COMPLIANCE_CATEGORIES
    }
    check_results = []
    for rule in scenario["checks"]:
        category = rule["category"]
        if category not in category_scores:
            raise EvalContractError(f"未知合规分类：{scenario['id']}/{category}")
        weight = float(rule.get("weight", 1))
        passed, description = _evaluate_rule(rule, trace, scenario)
        category_scores[category]["available"] += weight
        if passed:
            category_scores[category]["earned"] += weight
        check_results.append(
            {
                "id": rule["id"],
                "category": category,
                "passed": passed,
                "weight": weight,
                "description": description,
            }
        )
    category_percent = {
        category: (
            round(values["earned"] * 100 / values["available"], 1)
            if values["available"]
            else None
        )
        for category, values in category_scores.items()
    }
    available = sum(values["available"] for values in category_scores.values())
    earned = sum(values["earned"] for values in category_scores.values())
    hard_passed = all(result["passed"] for result in hard_results)
    return {
        "scenario_id": scenario["id"],
        "title": scenario["title"],
        "visibility": scenario["visibility"],
        "status": "passed" if hard_passed else "failed",
        "hard_gate_passed": hard_passed,
        "hard_gate_failures": [
            result for result in hard_results if not result["passed"]
        ],
        "hard_gate_results": hard_results,
        "compliance": {
            "score": round(earned * 100 / available, 1) if available else None,
            "categories": category_percent,
            "checks": check_results,
        },
        "efficiency": _efficiency(trace, scenario),
        "manual_review": trace.get("manual_review", {"status": "pending"}),
        "trace": trace,
    }


def grade_run(eval_root: Path, run_dir: Path) -> dict[str, Any]:
    manifest = _read_json(run_dir / "manifest.json")
    if manifest.get("schema_version") != 1:
        raise EvalContractError("运行清单版本错误")
    if not _nonempty_string(manifest.get("variant")):
        raise EvalContractError("运行清单缺少 variant")
    if manifest.get("execution_mode") != EXECUTION_MODE:
        raise EvalContractError("运行清单 execution_mode 非法")
    if manifest.get("trace_capture") != TRACE_CAPTURE:
        raise EvalContractError("运行清单 trace_capture 非法")
    if not isinstance(manifest.get("holdout_included"), bool):
        raise EvalContractError("运行清单 holdout_included 非法")
    scenarios = load_scenarios(eval_root, bool(manifest.get("holdout_included")))
    scenario_ids = [scenario["id"] for scenario in scenarios]
    if manifest.get("scenario_ids") != scenario_ids:
        raise EvalContractError("运行清单场景集合与当前评测不一致")
    results = []
    for scenario in scenarios:
        trace = _read_json(run_dir / "traces" / f"{scenario['id']}.json")
        _validate_trace(trace, scenario["id"], manifest["variant"])
        results.append(_grade_scenario(scenario, trace))
    completed = [result for result in results if result["status"] in {"passed", "failed"}]
    return {
        "schema_version": 1,
        "variant": manifest["variant"],
        "skill_digest": manifest["skill_digest"],
        "execution_mode": manifest["execution_mode"],
        "trace_capture": manifest["trace_capture"],
        "skill_material": _skill_inventory(run_dir / "skill-snapshot"),
        "summary": {
            "total": len(results),
            "run": len(completed),
            "passed": sum(result["status"] == "passed" for result in completed),
            "failed": sum(result["status"] == "failed" for result in completed),
            "not_run": sum(result["status"] == "not_run" for result in results),
            "errors": sum(result["status"] == "error" for result in results),
            "hard_gate_passed": bool(completed)
            and all(result["hard_gate_passed"] for result in completed),
            "compliance_score": (
                round(
                    sum(result["compliance"]["score"] for result in completed) / len(completed),
                    1,
                )
                if completed
                else None
            ),
            "efficiency_score": (
                round(
                    sum(result["efficiency"]["score"] for result in completed) / len(completed),
                    1,
                )
                if completed
                else None
            ),
        },
        "scenarios": results,
    }


def render_report(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        f"# DLoop 行为评测：{report['variant']}",
        "",
        f"- Skill 摘要：`{report['skill_digest']}`",
        f"- 执行模式：`{report['execution_mode']}`",
        f"- Trace 采集：`{report['trace_capture']}`",
        f"- 静态材料表面：入口 {report['skill_material']['entry_bytes']} 字节；references {report['skill_material']['reference_bytes']} 字节；合计 {report['skill_material']['total_bytes']} 字节（不等同于实际上下文 token）",
        f"- 场景：{summary['total']}；已运行：{summary['run']}；通过：{summary['passed']}；失败：{summary['failed']}；未运行：{summary['not_run']}；错误：{summary['errors']}",
        f"- 硬门槛：{'通过' if summary['hard_gate_passed'] else '未通过或无已运行数据'}",
        f"- 合规分：{summary['compliance_score'] if summary['compliance_score'] is not None else '未运行'}",
        f"- 效率分：{summary['efficiency_score'] if summary['efficiency_score'] is not None else '未运行'}",
        "",
        "耗时、轮次和上下文成本只在执行器可靠提供时记录，当前不参与自动判分。人工评审保持独立，不替代机器可判定事实。",
        "",
        "| 场景 | 可见性 | 状态 | 硬门槛 | 合规 | 效率 | 人工评审 |",
        "| --- | --- | --- | --- | ---: | ---: | --- |",
    ]
    for result in report["scenarios"]:
        compliance = result["compliance"]
        efficiency = result["efficiency"]
        lines.append(
            "| {title} | {visibility} | {status} | {hard} | {compliance} | {efficiency} | {manual} |".format(
                title=result["title"],
                visibility=result["visibility"],
                status=result["status"],
                hard=(
                    "通过" if result["hard_gate_passed"] is True
                    else "失败" if result["hard_gate_passed"] is False
                    else "未运行"
                ),
                compliance=compliance["score"] if compliance else "—",
                efficiency=efficiency["score"] if efficiency else "—",
                manual=result.get("manual_review", {}).get("status", "pending"),
            )
        )
    failures = [
        (result["title"], item)
        for result in report["scenarios"]
        for item in result.get("hard_gate_failures", [])
    ]
    if failures:
        lines.extend(["", "## 硬门槛失败", ""])
        lines.extend(f"- {title}：{item['description']}" for title, item in failures)
    return "\n".join(lines) + "\n"


def compare_reports(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    baseline_by_id = {item["scenario_id"]: item for item in baseline["scenarios"]}
    candidate_by_id = {item["scenario_id"]: item for item in candidate["scenarios"]}
    scenario_ids = sorted(set(baseline_by_id) | set(candidate_by_id))
    comparisons = []
    for scenario_id in scenario_ids:
        before = baseline_by_id.get(scenario_id)
        after = candidate_by_id.get(scenario_id)
        before_compliance = before.get("compliance") if before else None
        after_compliance = after.get("compliance") if after else None
        before_efficiency = before.get("efficiency") if before else None
        after_efficiency = after.get("efficiency") if after else None
        baseline_status = before.get("status") if before else "missing"
        candidate_status = after.get("status") if after else "missing"
        compliance_delta = (
            round(after_compliance["score"] - before_compliance["score"], 1)
            if before_compliance and after_compliance
            else None
        )
        efficiency_delta = (
            after_efficiency["score"] - before_efficiency["score"]
            if before_efficiency and after_efficiency
            else None
        )
        comparable_statuses = {"passed", "failed"}
        if baseline_status not in comparable_statuses or candidate_status not in comparable_statuses:
            classification = "not_comparable"
        elif baseline_status == "passed" and candidate_status == "failed":
            classification = "regression"
        elif baseline_status == "failed" and candidate_status == "passed":
            classification = "improved"
        elif baseline_status == candidate_status == "passed":
            if (compliance_delta or 0) < 0 or (efficiency_delta or 0) < 0:
                classification = "degraded"
            elif (compliance_delta or 0) > 0 or (efficiency_delta or 0) > 0:
                classification = "improved"
            else:
                classification = "unchanged"
        else:
            classification = "not_comparable"
        comparisons.append(
            {
                "scenario_id": scenario_id,
                "baseline_status": baseline_status,
                "candidate_status": candidate_status,
                "compliance_delta": compliance_delta,
                "efficiency_delta": efficiency_delta,
                "classification": classification,
            }
        )
    regressions = [
        item
        for item in comparisons
        if item["classification"] == "regression"
    ]
    classification_counts = {
        classification: sum(
            item["classification"] == classification for item in comparisons
        )
        for classification in ("improved", "unchanged", "degraded", "regression", "not_comparable")
    }
    return {
        "schema_version": 1,
        "baseline": baseline["variant"],
        "candidate": candidate["variant"],
        "skill_material_delta": {
            key: candidate["skill_material"][key] - baseline["skill_material"][key]
            for key in ("entry_bytes", "reference_bytes", "total_bytes")
        },
        "regressions": regressions,
        "classification_counts": classification_counts,
        "scenarios": comparisons,
    }


def render_comparison(comparison: dict[str, Any]) -> str:
    material = comparison["skill_material_delta"]
    counts = comparison["classification_counts"]
    lines = [
        f"# DLoop 行为对比：{comparison['baseline']} → {comparison['candidate']}",
        "",
        f"- 常驻入口变化：{material['entry_bytes']:+d} 字节",
        f"- references 变化：{material['reference_bytes']:+d} 字节",
        f"- skill 静态材料合计变化：{material['total_bytes']:+d} 字节",
        f"- 硬门槛回归：{len(comparison['regressions'])}",
        f"- 行为结论：改善 {counts['improved']}；未变 {counts['unchanged']}；降级 {counts['degraded']}；不可比较 {counts['not_comparable']}",
        "",
        "静态材料字节数只表示可披露表面，不等同于实际上下文 token。场景分数来自外部模型 dry-run 的自报语义 trace，不冒充实际 CLI 集成运行。",
        "",
        "| 场景 | baseline | candidate | 合规变化 | 效率变化 | 结论 |",
        "| --- | --- | --- | ---: | ---: | --- |",
    ]
    for item in comparison["scenarios"]:
        lines.append(
            "| {scenario_id} | {baseline_status} | {candidate_status} | {compliance} | {efficiency} | {classification} |".format(
                scenario_id=item["scenario_id"],
                baseline_status=item["baseline_status"],
                candidate_status=item["candidate_status"],
                compliance=(
                    f"{item['compliance_delta']:+.1f}"
                    if item["compliance_delta"] is not None else "—"
                ),
                efficiency=(
                    f"{item['efficiency_delta']:+d}"
                    if item["efficiency_delta"] is not None else "—"
                ),
                classification=item["classification"],
            )
        )
    return "\n".join(lines) + "\n"
