"""DloopUI 必要输入清单、缺项与开工确认依据。"""

from __future__ import annotations

from html import escape
import hashlib
import json
from pathlib import Path

from archive_changes import _replace_files_atomically
from archive_configuration import ArchiveConfigurationError
from archive_paths import project_root_from_archive_root
from archive_workspace import serialized_workflow_state


BASELINE_PATH = "04-plan/ui-implementation-baseline.json"
REVIEW_PATH = "04-plan/ui-implementation-baseline.html"
CATEGORIES = {"prefabs": "预制体", "protocols": "协议", "configurations": "配置", "requirement_sources": "需求描述"}
STATUSES = {"verified", "missing", "ambiguous", "not_applicable"}


def _error(message):
    return ArchiveConfigurationError("INVALID_UI_BASELINE", message)


def _model(feature_path, state):
    if (state.get("configuration") or {}).get("id") != "dloop-ui-v1":
        raise _error("只有 DloopUI 使用开工清单。")
    return json.loads((feature_path / state["configuration"]["model_path"]).read_text(encoding="utf-8"))


def _facts(graph, feature_id, model):
    # 不绑定截图、标注或整个实现文件的字节；这些会随正常实施变化。
    requirements = []
    for item in model.get("requirements", []):
        prefabs = sorted(p["asset_path"] for p in model.get("prefabs", []) if item["id"] in p.get("requirement_ids", []))
        requirements.append({"key": item["identity_key"], "name": item["name_zh"],
                             "result": item["statement"], "prefabs": prefabs,
                             "sources": item.get("evidence_ids", []), "locator": item.get("source_locator", "")})
    document = graph.documents.get(f"{feature_id}.requirements.overview")
    return {"requirements": sorted(requirements, key=lambda item: item["key"]),
            "requirements_version": document.semantic_version if document else None}


def baseline_input_template(graph, feature_id, state):
    feature_path = graph.features[feature_id].path
    model = _model(feature_path, state)
    evidence = {item["id"]: item for item in model.get("evidence", [])}
    items = []
    for fact in _facts(graph, feature_id, model)["requirements"]:
        item = {"requirement_key": fact["key"]}
        for category in CATEGORIES:
            item[category] = [{"status": "missing", "reference": "", "purpose": ""}]
        if fact["prefabs"]:
            item["prefabs"] = [{"status": "verified", "reference": path, "purpose": fact["result"]} for path in fact["prefabs"]]
        sources = [evidence[key] for key in fact["sources"] if key in evidence]
        if sources:
            item["requirement_sources"] = [{"status": "verified", "reference": source["path"],
                                            "purpose": fact["locator"] or fact["result"]} for source in sources]
        items.append(item)
    return {"input_version": 1, "items": items}


def _normalize(value):
    if not isinstance(value, dict) or set(value) != {"input_version", "items"} or value["input_version"] != 1:
        raise _error("开工清单必须包含 input_version=1 和 items。")
    if not isinstance(value["items"], list):
        raise _error("items 必须为按业务需求组织的清单。")
    seen = set()
    for item in value["items"]:
        if not isinstance(item, dict) or set(item) != {"requirement_key", *CATEGORIES}:
            raise _error("每项必须包含需求键和全部四类材料。")
        key = item["requirement_key"]
        if not isinstance(key, str) or not key.strip() or key in seen:
            raise _error("需求键必须非空且唯一。")
        seen.add(key)
        for category in CATEGORIES:
            entries = item[category]
            if not isinstance(entries, list):
                raise _error("每类材料必须为列表。")
            for entry in entries:
                if not isinstance(entry, dict) or set(entry) != {"status", "reference", "purpose"}:
                    raise _error("材料必须包含 status、reference 和 purpose。")
                if not isinstance(entry["status"], str) or entry["status"] not in STATUSES or any(not isinstance(entry[field], str) for field in ("reference", "purpose")):
                    raise _error("材料状态或引用、用途格式不合法；不支持计划新增或延期占位。")
                for field in ("reference", "purpose"):
                    entry[field] = entry[field].strip()
    return {"input_version": 1, "items": sorted(value["items"], key=lambda item: item["requirement_key"])}


def _blockers(value, facts, project_root):
    blockers = []
    def add(key, category, message):
        blockers.append({"code": "UI_BASELINE_INCOMPLETE", "requirement_key": key,
                         "category": category, "message": message})
    items = {item["requirement_key"]: item for item in value["items"]}
    if not facts["requirements"]:
        add("", "requirement_sources", "尚未完成需求调查，请先提取完整业务需求。")
    current_keys = {fact["key"] for fact in facts["requirements"]}
    for key in items.keys() - current_keys:
        add(key, "requirement_sources", "清单包含已不在当前需求中的项目，请重新核对范围。")
    for fact in facts["requirements"]:
        key = fact["key"]
        item = items.get(key, {})
        for category, label in CATEGORIES.items():
            entries = item.get(category, [])
            if not entries:
                add(key, category, f"{fact['name']}：缺少{label}核对结果。")
            for entry in entries:
                status, reference, purpose = (entry[field] for field in ("status", "reference", "purpose"))
                if status in {"missing", "ambiguous"}:
                    add(key, category, f"{fact['name']}：{label}尚未落实。已查：{reference or '未记录'}；待补充：{purpose or '未说明'}")
                    continue
                if not purpose or purpose.casefold().startswith(("待填写", "待补充", "todo", "tbd")):
                    add(key, category, f"{fact['name']}：{label}缺少用途或不涉及的理由。")
                if status == "not_applicable":
                    if category not in {"protocols", "configurations"} or len(entries) != 1:
                        add(key, category, f"{fact['name']}：{label}不能标记为不涉及或与其他材料混用。")
                    continue
                path = Path(reference)
                if not path.is_absolute():
                    path = project_root / path
                if not reference or not path.is_file():
                    add(key, category, f"{fact['name']}：{label}引用必须是已存在的材料文件：{reference}。每条材料只引用一个文件，多个文件分别填写。")
                if category == "prefabs":
                    matched = {str(Path(p).resolve()) for p in fact["prefabs"]}
                    if str(path.resolve()) not in matched or path.suffix.lower() != ".prefab":
                        add(key, category, f"{fact['name']}：预制体必须与当前调查中的真实匹配一致。")
    return blockers


def _render(value, facts, blockers):
    lines = ["# DloopUI 开工确认", "", "请核对每项功能的预期结果、使用材料及用途。缺项未补齐时不能开工；补齐材料不代表已经确认。", ""]
    if blockers:
        lines += ["## 待补齐", "", *[f"- {item['message']}" for item in blockers], ""]
    names = {fact["key"]: fact for fact in facts["requirements"]}
    for item in value["items"]:
        fact = names.get(item["requirement_key"], {"name": item["requirement_key"], "result": "需求已变化"})
        lines += [f"## {fact['name']}", "", fact["result"], ""]
        for category, label in CATEGORIES.items():
            for entry in item[category]:
                status = {"verified": "已核实", "missing": "缺失", "ambiguous": "待选择", "not_applicable": "不涉及"}[entry["status"]]
                lines.append(f"- {label}（{status}）：{entry['reference']}；{entry['purpose']}")
        lines.append("")
    body = []
    for line in lines:
        if line.startswith("# "):
            body.append("<h1>" + escape(line[2:]) + "</h1>")
        elif line.startswith("## "):
            body.append("<h2>" + escape(line[3:]) + "</h2>")
        elif line:
            body.append("<p>" + escape(line.removeprefix("- ")) + "</p>")
    return ('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>DloopUI 开工确认</title><style>body{max-width:960px;margin:32px auto;padding:0 20px;'
            'font:16px/1.7 system-ui;color:#202630;background:#fafafa}p{overflow-wrap:anywhere}'
            'h2{border-top:1px solid #ddd;padding-top:20px}</style><body>' + "\n".join(body) + '</body></html>')


def _material_scope(value):
    return [{"requirement_key": item["requirement_key"],
             **{category: [{field: entry[field] for field in ("status", "reference")}
                           for entry in item[category]] for category in CATEGORIES}}
            for item in value["items"]]


def baseline_review(graph, feature_id, state):
    feature_path = graph.features[feature_id].path
    model = _model(feature_path, state)
    facts = _facts(graph, feature_id, model)
    path = feature_path / BASELINE_PATH
    if not path.is_file():
        return {"status": "missing", "blockers": [{"code": "UI_BASELINE_REQUIRED", "message": "请先调查并提交四类开工材料清单。"}]}
    saved = json.loads(path.read_text(encoding="utf-8"))
    value = _normalize({key: saved[key] for key in ("input_version", "items")})
    blockers = _blockers(value, facts, project_root_from_archive_root(graph.root))
    if saved["facts"] != facts:
        blockers.append({"code": "UI_BASELINE_STALE", "message": "需求范围或预制体匹配已变化，请重新提交开工清单。"})
    return {"status": "blocked" if blockers else "ready", "blockers": blockers,
            "reviewed_digest": saved["reviewed_digest"], "materials": [str(feature_path / REVIEW_PATH)]}


@serialized_workflow_state
def submit_ui_baseline(root, feature_id, input_path, semantic_change=True):
    from archive_approvals import _load_state, _require_complex_feature
    from archive_validation import validate_feature_archive
    graph = validate_feature_archive(root, feature_id)
    feature = _require_complex_feature(graph, feature_id)
    state = _load_state(feature.path, feature_id)
    model = _model(feature.path, state)
    try:
        value = _normalize(json.loads(input_path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exception:
        raise _error(f"无法读取开工清单：{exception}") from exception
    facts = _facts(graph, feature_id, model)
    blockers = _blockers(value, facts, project_root_from_archive_root(root))
    saved = {**value, "facts": facts}
    baseline_path = feature.path / BASELINE_PATH
    previous = json.loads(baseline_path.read_text(encoding="utf-8")) if baseline_path.is_file() else None
    if not semantic_change and (previous is None or previous["facts"] != facts
                                or _material_scope(previous) != _material_scope(value)):
        raise _error("非语义修订只允许修改已有清单的用途措辞；需求、材料引用或状态变化须按业务变更提交。")
    unchanged = previous is not None and all(previous[key] == saved[key] for key in saved)
    # 语义由 AI 明确声明；纯文案修订沿用业务依据摘要，不改写用户的原始确认记录。
    saved["reviewed_digest"] = (previous["reviewed_digest"] if unchanged or not semantic_change else
                                "sha256:" + hashlib.sha256(json.dumps(saved, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest())
    _replace_files_atomically(root, {
        baseline_path: json.dumps(saved, ensure_ascii=False, indent=2) + "\n",
        feature.path / REVIEW_PATH: _render(value, facts, blockers),
    })
    review = baseline_review(graph, feature_id, state)
    approval = state["approvals"].get("ui-baseline") or {}
    approval_preserved = (review["status"] == "ready" and approval.get("decision") == "approve"
                          and approval.get("reviewed_digest") == review["reviewed_digest"])
    next_action = {"command": "stage-action", "stage": "ui-baseline", "reviewed_digest": review["reviewed_digest"]}
    if blockers:
        next_action = {"kind": "human", "reason": "集中补齐缺项后重新提交清单；停止整个需求的实施。"}
    elif approval_preserved:
        next_action = {"command": "workflow-status", "arguments": {"feature_id": feature_id}}
    return {**review, "approval_preserved": approval_preserved, "next_action": next_action}
