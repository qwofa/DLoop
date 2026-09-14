"""追加记录可归因、可去重并能自动关闭的工作流事件。"""

from __future__ import annotations

from datetime import datetime, timezone
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
from typing import Mapping, Sequence

from archive_workspace import ArchiveWorkspaceError, workspace_state_lock


FRICTION_LOG_NAME = "friction.jsonl"
EVENT_SCHEMA_VERSION = 2
TOOL_VERSION = Path(__file__).with_name("VERSION").read_text(encoding="utf-8").strip()
OPERATION_EVENTS = {"failure", "attempt", "guard", "resolved"}


class ArchiveFrictionError(Exception):
    """表示补记本身缺少必要事实；不会触发另一条摩擦。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _serialized_when_archive_exists(function):
    """已有正式档案参与发布锁；失败的初始化不因此创建档案根。"""

    @wraps(function)
    def wrapped(root: Path, *args, **kwargs):
        normalized = root.expanduser().resolve()
        try:
            if not normalized.is_dir():
                return function(normalized, *args, **kwargs)
            with workspace_state_lock(normalized):
                return function(normalized, *args, **kwargs)
        except (OSError, UnicodeError, ArchiveWorkspaceError) as exception:
            return _not_recorded(f"摩擦记录不可用：{exception}")

    return wrapped


def friction_log_path(root: Path) -> Path:
    """把运行记录放在正式档案根之外，避免影响审批与冻结。"""

    return root.expanduser().resolve().parent / FRICTION_LOG_NAME


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(prefix: str, value: object) -> str:
    return prefix + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _append(path: Path, entry: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (_canonical(entry) + "\n").encode("utf-8")
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("摩擦日志写入没有取得进展。")
            remaining = remaining[written:]
    finally:
        os.close(descriptor)


def _not_recorded(message: str) -> Mapping[str, object]:
    return {"status": "not-recorded", "warning": message}


def _read_entries(path: Path) -> tuple[list[Mapping[str, object]], str | None]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return [], None
    except (OSError, UnicodeError) as exception:
        return [], f"无法读取摩擦运行记录：{exception}"

    entries: list[Mapping[str, object]] = []
    try:
        for line in lines:
            value = json.loads(line)
            if not isinstance(value, dict):
                return [], "摩擦运行记录包含非对象条目。"
            if value.get("schema_version") == EVENT_SCHEMA_VERSION and value.get("workflow_version") == TOOL_VERSION:
                entries.append(value)
    except (json.JSONDecodeError, UnicodeError) as exception:
        return [], f"摩擦运行记录不是合法 JSON Lines：{exception}"
    return entries, None


def read_feature_friction(
    root: Path,
    feature_id: str,
) -> tuple[list[Mapping[str, object]], str | None]:
    """读取当前交付项可明确归属的摩擦事件，不猜测旧的无归属记录。"""

    entries, warning = _read_entries(friction_log_path(root))
    if warning is not None:
        return [], warning
    return [entry for entry in entries if entry.get("feature_id") == feature_id], None


def _normalized_target(target: Mapping[str, object] | None) -> Mapping[str, str]:
    if target is None:
        return {}
    return {
        str(key): str(value)
        for key, value in sorted(target.items())
        if value is not None and str(value).strip()
    }


def _normalized_request_input(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _normalized_request_input(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_normalized_request_input(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _correlation(
    feature_id: str | None,
    command: str,
    code: str,
    category: str,
    target: Mapping[str, str],
) -> Mapping[str, object]:
    return {
        "workflow_version": TOOL_VERSION,
        "feature_id": feature_id or "",
        "command": command,
        "code": code,
        "category": category,
        "target": target,
    }


@_serialized_when_archive_exists
def record_friction(
    root: Path,
    friction: str,
    *,
    feature_id: str | None = None,
    command: str = "unknown",
    code: str = "WORKFLOW_FAILURE",
    target: Mapping[str, object] | None = None,
    request_input: Mapping[str, object] | None = None,
    expected_guard: bool = False,
    category: str = "workflow",
    context: Mapping[str, object] | None = None,
) -> Mapping[str, object]:
    """记录一次确定性事件；相同未关闭输入不会重复追加。"""

    message = friction.strip()
    if not message:
        return _not_recorded("摩擦内容为空，未写入运行记录。")
    normalized_target = _normalized_target(target)
    correlation = _correlation(feature_id, command, code, category, normalized_target)
    incident_id = _digest("f-", correlation)
    fingerprint = _digest(
        "sha256:",
        {
            **correlation,
            "message": message,
            "request_input": _normalized_request_input(request_input or {}),
        },
    )
    path = friction_log_path(root)
    entries, warning = _read_entries(path)
    if warning is not None:
        return _not_recorded(warning)
    related = [entry for entry in entries if entry.get("incident_id") == incident_id
               and entry.get("event") in OPERATION_EVENTS]
    last = related[-1] if related else None
    if (
        isinstance(last, dict)
        and last.get("event") in {"failure", "attempt", "guard"}
        and last.get("input_fingerprint") == fingerprint
    ):
        return {
            "status": "unchanged",
            "event": last.get("event"),
            "incident_id": incident_id,
        }
    event = (
        "guard"
        if expected_guard
        else "attempt"
        if related and isinstance(last, dict) and last.get("event") != "resolved"
        else "failure"
    )
    entry = {
        "schema_version": EVENT_SCHEMA_VERSION,
        "workflow_version": TOOL_VERSION,
        "time": _timestamp(),
        "event": event,
        "incident_id": incident_id,
        "feature_id": feature_id,
        "command": command,
        "source": "cli",
        "code": code,
        "category": category,
        "expected_guard": expected_guard,
        "target": normalized_target,
        "message": message,
        "input_fingerprint": fingerprint,
        "context": _normalized_target(context),
    }
    try:
        _append(path, entry)
    except (OSError, UnicodeError, TypeError, ValueError) as exception:
        return _not_recorded(f"无法写入摩擦运行记录：{exception}")
    return {"status": "recorded", "event": event, "incident_id": incident_id}


@_serialized_when_archive_exists
def record_operation_success(
    root: Path,
    *,
    feature_id: str | None,
    command: str,
    target: Mapping[str, object] | None,
    successful_operation: str,
    context: Mapping[str, object] | None = None,
) -> Mapping[str, object]:
    """自动关闭同一功能、命令和目标下仍未关闭的非预期问题。"""

    path = friction_log_path(root)
    entries, warning = _read_entries(path)
    if warning is not None:
        return _not_recorded(warning)
    normalized_target = _normalized_target(target)
    latest: dict[str, Mapping[str, object]] = {}
    for entry in entries:
        incident_id = entry.get("incident_id")
        if isinstance(incident_id, str) and entry.get("event") in OPERATION_EVENTS:
            latest[incident_id] = entry
    open_incidents = [
        (incident_id, entry)
        for incident_id, entry in latest.items()
        if entry.get("event") in {"failure", "attempt"}
        and entry.get("feature_id") == feature_id
        and entry.get("command") == command
        and entry.get("target") == normalized_target
        and entry.get("expected_guard") is False
    ]
    if not open_incidents:
        return {"status": "unchanged", "resolved_incidents": []}
    resolved = []
    try:
        for incident_id, entry in sorted(open_incidents):
            _append(
                path,
                {
                    "schema_version": EVENT_SCHEMA_VERSION,
                    "workflow_version": TOOL_VERSION,
                    "time": _timestamp(),
                    "event": "resolved",
                    "incident_id": incident_id,
                    "feature_id": feature_id,
                    "command": command,
                    "source": "cli",
                    "code": entry.get("code"),
                    "category": entry.get("category"),
                    "expected_guard": False,
                    "target": normalized_target,
                    "successful_operation": successful_operation.strip(),
                    "context": _normalized_target(context),
                },
            )
            resolved.append(incident_id)
    except (OSError, UnicodeError, TypeError, ValueError) as exception:
        return _not_recorded(f"无法写入成功操作：{exception}")
    return {"status": "recorded", "resolved_incidents": resolved}


def friction_note_contract(feature_id: str, *, incident_id: str | None = None,
                           context: Mapping[str, object] | None = None) -> Mapping[str, object]:
    """随角色材料提供最小补记入口，不读取日志或加入业务门禁。"""

    arguments = {"feature_id": feature_id, **_normalized_target(context)}
    if incident_id is not None:
        arguments["incident_id"] = incident_id
    return {
        "command": "friction-note",
        "arguments": arguments,
        "when": "实际返工时补记。",
        "required_inputs": (["recovery 或 cost 或 summary+extra_work"] if incident_id else ["summary", "extra_work", "evidence"]),
        "input_guidance": "细则：workflow-rules --action friction。",
        "boundary": "旁路记录，不阻断交付。",
    }


@_serialized_when_archive_exists
def record_friction_note(
    root: Path, *, feature_id: str, summary: str | None = None,
    extra_work: str | None = None, evidence: Sequence[str] = (),
    recovery: str | None = None, cost: str | None = None,
    incident_id: str | None = None, source: str = "role-report",
    context: Mapping[str, object] | None = None,
) -> Mapping[str, object]:
    """一次记录观察或补充恢复说明；不修改自动事件的生命周期。"""

    summary = (summary or "").strip()
    extra_work = (extra_work or "").strip()
    recovery = (recovery or "").strip()
    cost = (cost or "").strip()
    references = list(dict.fromkeys(item.strip() for item in evidence if item.strip()))
    if source not in {"role-report", "user-feedback"}:
        raise ArchiveFrictionError("INVALID_FRICTION_NOTE", "记录来源必须是角色观察或用户明确反馈。")
    entries, warning = _read_entries(friction_log_path(root))
    if warning is not None:
        return _not_recorded(warning)
    if incident_id is not None:
        if not any(entry.get("incident_id") == incident_id and entry.get("feature_id") == feature_id for entry in entries):
            raise ArchiveFrictionError("FRICTION_NOT_FOUND", "当前交付项不存在该摩擦记录。")
        if not recovery and not cost and not (summary and extra_work):
            raise ArchiveFrictionError("INVALID_FRICTION_NOTE", "补记需要恢复方式、已知代价，或目标卡点与额外动作。")
    elif not summary or not extra_work or (not references and source != "user-feedback"):
        raise ArchiveFrictionError("INVALID_FRICTION_NOTE", "新观察需要目标卡点、额外动作和证据；仅用户反馈时明确使用 user-feedback 来源。")
    normalized_context = _normalized_target(context)
    if incident_id is None:
        incident_id = _digest("f-", {
            "workflow_version": TOOL_VERSION, "feature_id": feature_id, "source": source,
            "summary": summary, "extra_work": extra_work, "evidence": references,
            "context": normalized_context,
        })
    payload = {
        "schema_version": EVENT_SCHEMA_VERSION,
        "workflow_version": TOOL_VERSION,
        "event": "recovery-note" if recovery and not summary else "observation",
        "incident_id": incident_id, "feature_id": feature_id, "source": source,
        "summary": summary, "extra_work": extra_work, "evidence": references,
        "recovery": recovery, "cost": cost, "context": normalized_context,
    }
    fingerprint = _digest("sha256:", payload)
    if any(entry.get("input_fingerprint") == fingerprint for entry in entries):
        return {"status": "unchanged", "incident_id": incident_id, "event": payload["event"]}
    _append(friction_log_path(root), {**payload, "time": _timestamp(), "input_fingerprint": fingerprint})
    return {"status": "recorded", "incident_id": incident_id, "event": payload["event"]}


def _evidence_status(root: Path, reference: str) -> Mapping[str, str]:
    """只检查显式文件引用的存在性，标识和反馈不冒充已验证证据。"""

    if not reference.startswith("path:"):
        return {"reference": reference, "status": "not-checked"}
    path = Path(reference[5:])
    if not path.is_absolute():
        path = root.parent.parent.parent / path
    try:
        status = "available" if path.is_file() else "missing"
    except OSError:
        status = "unavailable"
    return {"reference": reference, "status": status}


def friction_summary(root: Path, feature_id: str) -> Mapping[str, object]:
    """只读当前交付项的全部摩擦，不以业务档案校验作为复盘前提。"""

    entries, warning = read_feature_friction(root, feature_id)
    groups: dict[str, dict] = {}
    timeline = []
    labels = {
        "failure": "操作失败", "attempt": "输入变化后的尝试", "guard": "正常门禁",
        "resolved": "本次操作恢复", "observation": "额外劳动观察", "recovery-note": "恢复方式补记",
    }
    for entry in entries:
        incident_id = entry["incident_id"]
        group = groups.setdefault(incident_id, {
            "incident_id": incident_id, "operation_status": "not-applicable",
            "recovery_status": "not-recorded", "events": [], "evidence": [],
            "cost_status": "unknown", "reported_costs": [], "extra_work": [],
        })
        event = entry["event"]
        if event in OPERATION_EVENTS:
            group["operation_status"] = {
                "resolved": "recovered", "guard": "expected-guard",
                "failure": "failed", "attempt": "failed",
            }[event]
        if entry.get("recovery"):
            group["recovery_status"] = "recorded"
        if entry.get("cost"):
            group["cost_status"] = "reported"
            if entry["cost"] not in group["reported_costs"]:
                group["reported_costs"].append(entry["cost"])
        if entry.get("extra_work") and entry["extra_work"] not in group["extra_work"]:
            group["extra_work"].append(entry["extra_work"])
        rendered = {**entry, "label": labels.get(event, event)}
        group["events"].append(rendered)
        timeline.append(rendered)
        references = list(entry.get("evidence", []))
        for key, value in entry.get("target", {}).items():
            if key.endswith("_file") or key == "integration_confirmation":
                references.append(f"path:{value}")
            else:
                references.append(f"{key}:{value}")
        for reference in references:
            if reference not in group["evidence"]:
                group["evidence"].append(reference)
    for group in groups.values():
        group["repeat_status"] = "observed" if sum(item["event"] in {"failure", "guard", "attempt"} for item in group["events"]) > 1 else "unknown"
        group["evidence"] = [_evidence_status(root, item) for item in group["evidence"]]
    warnings = [warning] if warning else []
    execution_breakers = []
    state_path = root / feature_id / "workflow-state.json"
    if state_path.is_file():
        try:
            # 熔断已由检查点持久化，复盘直接投影，不再保存一套日志状态。
            state = json.loads(state_path.read_text(encoding="utf-8"))
            for package_id, record in state.get("execution", {}).get("slices", {}).items():
                for report in record.get("breaker_reports", []):
                    execution_breakers.append({
                        **report,
                        "source": "workflow-state",
                        "package_id": package_id,
                        "slice_status": record["status"],
                        "evidence": str(state_path),
                    })
        except (OSError, UnicodeError, ValueError, AttributeError, KeyError, TypeError) as exception:
            warnings.append(f"无法读取执行熔断记录：{exception}")
    return {
        "status": "available" if warning is None else "unavailable",
        "workflow_version": TOOL_VERSION, "feature_id": feature_id,
        "timeline": timeline, "incidents": list(groups.values()),
        "execution_breakers": execution_breakers,
        "supplement_candidates": [
            {"incident_id": item["incident_id"], "friction_note": friction_note_contract(feature_id, incident_id=item["incident_id"]),
             "reason": "操作记录未包含额外动作或恢复说明；确实发生返工时再补记。"}
            for item in groups.values()
            if item["operation_status"] != "not-applicable" and not item["extra_work"] and item["recovery_status"] == "not-recorded"
        ],
        "warnings": warnings,
        "limitations": [
            "记录覆盖不完整；事件数不是调用次数、发生率、成功率或实际投入。",
            "本次操作恢复不代表工作流根因已修复；恢复方式未记录不代表作业仍被阻塞。",
            "补记来自角色观察或用户反馈；文件存在不表示证据内容已验证。",
            "执行熔断直接来自现有账本；切片已终止或继续不代表熔断根因已修复。",
        ],
    }
