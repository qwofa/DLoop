"""DloopUI 轻量需求、截图与标注配置 Adapter。"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import re
from types import ModuleType
from typing import Any, Mapping, Sequence

from archive_configuration import ArchiveConfigurationError, DLOOP_UI_CONFIGURATION


UI_MODEL_RELATIVE_PATH = "ui-model.json"
UI_DELIVERY_PATH = "06-validation/ui-delivery.html"
FINGERPRINT_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
ANNOTATION_MEDIA_PATTERN = re.compile(
    r"^04-plan/ui-annotations/PF-[0-9A-F]{12}-annotated\.svg$"
)
CAPTURE_MEDIA_PATTERN = re.compile(
    r"^02-investigation/ui-captures/(capture-manifest\.json|PF-[0-9A-F]{12}-raw\.png)$"
)
UI_VIEW_PATHS = {
    "ui-annotation-plan.md": "04-plan/ui-annotation-plan.md",
}
CURRENT_SUMMARY_REQUIRED = {
    "model_sha256",
    "requirements_audit_status",
    "plan_audit_status",
    "generated_paths",
}
CURRENT_SUMMARY_ALLOWED = CURRENT_SUMMARY_REQUIRED | {
    "audit_sha256",
    "computed_counts",
}


def _summary_format(summary: object) -> str:
    if not isinstance(summary, dict):
        return "invalid"
    fields = set(summary)
    if CURRENT_SUMMARY_REQUIRED.issubset(fields) and fields.issubset(
        CURRENT_SUMMARY_ALLOWED
    ):
        return "current"
    return "invalid"


def _runtime_path() -> Path:
    installed = Path(__file__).resolve().with_name("dloop_ui.py")
    source = (
        Path(__file__).resolve().parents[2]
        / "plugin"
        / "dloop"
        / "runtime"
        / "dloop_ui.py"
    )
    for candidate in (installed, source):
        if candidate.is_file():
            return candidate
    raise ArchiveConfigurationError(
        "DLOOP_UI_RUNTIME_MISSING",
        "当前发行物缺少 DloopUI 规划运行时。",
    )


def _runtime() -> ModuleType:
    path = _runtime_path()
    specification = importlib.util.spec_from_file_location(
        "dloop_ui_runtime",
        path,
    )
    if specification is None or specification.loader is None:
        raise ArchiveConfigurationError(
            "DLOOP_UI_RUNTIME_INVALID",
            "无法加载 DloopUI 规划运行时。",
        )
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _validate_configuration(
    configuration: Mapping[str, object],
    feature_path: Path,
) -> Path:
    if set(configuration) != {"id", "model_path", "summary"}:
        raise ArchiveConfigurationError(
            "INVALID_DLOOP_UI_CONFIGURATION",
            "DloopUI 配置只能保存标识、模型相对路径和审计摘要。",
        )
    if configuration.get("id") != DLOOP_UI_CONFIGURATION:
        raise ArchiveConfigurationError(
            "INVALID_DLOOP_UI_CONFIGURATION",
            "DloopUI 配置标识不合法。",
        )
    raw_path = configuration.get("model_path")
    if not isinstance(raw_path, str):
        raise ArchiveConfigurationError(
            "INVALID_DLOOP_UI_CONFIGURATION",
            "DloopUI 模型路径必须是相对路径。",
        )
    relative = PurePosixPath(raw_path.replace("\\", "/"))
    if relative.is_absolute() or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise ArchiveConfigurationError(
            "INVALID_DLOOP_UI_CONFIGURATION",
            "DloopUI 模型路径必须保持在交付档案内。",
        )
    model_path = feature_path.joinpath(*relative.parts)
    try:
        model_path.resolve().relative_to(feature_path.resolve())
    except ValueError as exception:
        raise ArchiveConfigurationError(
            "INVALID_DLOOP_UI_CONFIGURATION",
            "DloopUI 模型路径越过交付档案范围。",
        ) from exception
    if not model_path.is_file():
        raise ArchiveConfigurationError(
            "DLOOP_UI_MODEL_MISSING",
            "DloopUI 配置引用的 ui-model.json 不存在。",
        )

    summary = configuration.get("summary")
    summary_format = _summary_format(summary)
    if summary_format == "invalid":
        raise ArchiveConfigurationError(
            "INVALID_DLOOP_UI_CONFIGURATION",
            "DloopUI 审计摘要字段不合法。",
        )
    assert isinstance(summary, dict)
    if not FINGERPRINT_PATTERN.fullmatch(str(summary.get("model_sha256", ""))):
        raise ArchiveConfigurationError(
            "INVALID_DLOOP_UI_CONFIGURATION",
            "DloopUI 模型摘要不合法。",
        )
    for field in ("requirements_audit_status", "plan_audit_status"):
        if summary.get(field) not in {"blocked", "pass"}:
            raise ArchiveConfigurationError(
                "INVALID_DLOOP_UI_CONFIGURATION",
                f"DloopUI {field} 不合法。",
            )
    audit_sha256 = summary.get("audit_sha256")
    if audit_sha256 is not None and (
        not isinstance(audit_sha256, str)
        or not FINGERPRINT_PATTERN.fullmatch(audit_sha256)
    ):
        raise ArchiveConfigurationError(
            "INVALID_DLOOP_UI_CONFIGURATION",
            "DloopUI 计划审计摘要不合法。",
        )
    counts = summary.get("computed_counts")
    expected_count_fields = set(_runtime().COUNT_FIELDS)
    if counts is not None and (
        not isinstance(counts, dict)
        or set(counts) != expected_count_fields
        or any(not isinstance(field, str) or not field for field in counts)
        or any(
            not isinstance(item, int) or isinstance(item, bool) or item < 0
            for item in counts.values()
        )
    ):
        raise ArchiveConfigurationError(
            "INVALID_DLOOP_UI_CONFIGURATION",
            "DloopUI 数量摘要不合法。",
        )
    generated_paths = summary.get("generated_paths")
    if (
        not isinstance(generated_paths, list)
        or any(
            not isinstance(item, str)
            or not (
                item in {UI_VIEW_PATHS["ui-annotation-plan.md"], UI_DELIVERY_PATH}
                or ANNOTATION_MEDIA_PATTERN.fullmatch(item)
                or CAPTURE_MEDIA_PATTERN.fullmatch(item)
            )
            for item in generated_paths
        )
        or len(set(generated_paths)) != len(generated_paths)
        or generated_paths != sorted(generated_paths)
    ):
        raise ArchiveConfigurationError(
            "INVALID_DLOOP_UI_CONFIGURATION",
            "DloopUI 派生标注图归属清单不合法。",
        )
    return model_path


def _document(
    feature_id: str,
    relative_path: str,
    body: str,
    status: str,
    requirements_version: str,
) -> str:
    normalized_body = body.replace("\r\n", "\n").replace("\r", "\n").rstrip() + "\n"
    fingerprint = "sha256:" + hashlib.sha256(
        normalized_body.encode("utf-8")
    ).hexdigest()
    document_id = f"{feature_id}.plan.ui-annotation-plan"
    requirements_id = f"{feature_id}.requirements.overview"
    return (
        "---\n"
        f"document_id: {document_id}\n"
        "category: plan\n"
        f"content_status: {status}\n"
        "semantic_version: 0.1.0\n"
        f"content_fingerprint: {fingerprint}\n"
        f"dependencies: [{requirements_id}]\n"
        "dependency_versions: "
        + json.dumps({requirements_id: requirements_version}, ensure_ascii=False)
        + "\n"
        "related_documents: []\n"
        "---\n\n"
        + normalized_body
    )


def _requirements_version(feature_path: Path) -> str:
    path = feature_path / "01-requirements" / "README.md"
    if not path.is_file():
        raise ArchiveConfigurationError(
            "DLOOP_UI_REQUIREMENTS_MISSING",
            "交付项缺少需求总览。",
        )
    match = re.search(
        r"^semantic_version:\s*([^\s]+)\s*$",
        path.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    if match is None:
        raise ArchiveConfigurationError(
            "DLOOP_UI_REQUIREMENTS_INVALID",
            "需求总览缺少语义版本。",
        )
    return match.group(1)


def _require_model_feature(model: object, feature_id: str) -> None:
    if (
        not isinstance(model, dict)
        or not isinstance(model.get("feature"), dict)
        or model["feature"].get("id") != feature_id
    ):
        raise ArchiveConfigurationError(
            "DLOOP_UI_MODEL_FEATURE_MISMATCH",
            "ui-model.json 不属于当前 DLoop 交付项。",
        )


def _read_model(
    feature_path: Path,
    configuration: Mapping[str, object],
) -> tuple[ModuleType, Path, dict[str, object]]:
    model_path = _validate_configuration(configuration, feature_path)
    runtime = _runtime()
    try:
        model = runtime.read_json(model_path)
        _require_model_feature(model, feature_path.name)
        if model.get("schema_version") != runtime.SCHEMA_VERSION:
            raise ArchiveConfigurationError(
                "UNSUPPORTED_DLOOP_UI_FORMAT",
                f"DloopUI 只接受当前模型格式 {runtime.SCHEMA_VERSION}。",
            )
    except ArchiveConfigurationError:
        raise
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exception:
        raise ArchiveConfigurationError(
            "INVALID_DLOOP_UI_MODEL",
            f"无法读取 DloopUI 模型：{exception}",
        ) from exception
    return runtime, model_path, model


def _model_sha256(model: Mapping[str, object]) -> str:
    canonical = json.dumps(
        model,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def interaction_plan_snapshot(feature_path: Path, configuration: Mapping[str, object]) -> Mapping[str, object]:
    """交付页绑定完整交互、证据和已接受实现；不用于前置批准。"""
    _, _, model = _read_model(feature_path, configuration)
    review = model.get("delivery_review") or {}
    return {"plan_digest": _model_sha256(model), "candidate_summary": review.get("candidate_summary")}


def _evaluate(
    feature_path: Path,
    configuration: Mapping[str, object],
    stage: str,
) -> Mapping[str, object]:
    runtime, _, model = _read_model(feature_path, configuration)
    if stage not in {"requirements", "plan"}:
        raise ArchiveConfigurationError(
            "INVALID_CONFIGURATION_STAGE",
            f"未知 DloopUI 检查阶段：{stage}",
        )
    summary = configuration["summary"]
    assert isinstance(summary, Mapping)
    model_current = summary["model_sha256"] == _model_sha256(model)
    status_field = (
        "requirements_audit_status"
        if stage == "requirements"
        else "plan_audit_status"
    )
    sealed_status = summary[status_field] == "pass"
    current_report = runtime.validate_model(model, stage)
    if stage == "plan":
        current_report["errors"].extend(runtime.delivery_errors(model))
        if not (feature_path / UI_DELIVERY_PATH).is_file():
            current_report["errors"].append({"code": "UI_DELIVERY_MISSING", "message": "尚未生成统一交付页。"})
        if current_report["errors"]:
            current_report["status"] = "BLOCKED"
        media_errors = runtime.validate_annotation_media(model, feature_path)
        if media_errors:
            current_report["errors"].extend(media_errors)
            current_report["status"] = "BLOCKED"
    current_status = current_report["status"] == "PASS"
    blocked = not model_current or not sealed_status or not current_status
    if stage == "requirements":
        code = "DLOOP_UI_REQUIREMENTS_REQUIRED"
        message = (
            "ui-model.json 自上次需求同步后已变化，需要重新同步 UI 需求。"
            if not model_current
            else (
                "UI 需求证据已变化或缺失，需要修正后重新同步。"
                if sealed_status and not current_status
                else "上次同步的 UI 需求提取仍为 BLOCKED。"
            )
        )
    else:
        code = "DLOOP_UI_PLAN_BLOCKED"
        message = (
            "ui-model.json 自上次规划同步后已变化，需要重新同步 UI 标注计划。"
            if not model_current
            else (
                "UI 截图或需求证据已变化或缺失，需要修正后重新同步。"
                if sealed_status and not current_status
                else "交互交付说明或双向核对尚未完成，不能进入最终验收。"
            )
        )
    return {
        "status": "BLOCKED" if blocked else "PASS",
        "code": code if blocked else None,
        "message": message if blocked else None,
        "details": {
            "stage": stage,
            "model_path": str(configuration["model_path"]),
            "model_status": "current" if model_current else "changed",
            "requirements_audit_status": summary["requirements_audit_status"],
            "plan_audit_status": summary["plan_audit_status"],
            "computed_counts": dict(summary.get("computed_counts", {})),
            "current_errors": list(current_report["errors"]),
        },
    }


def _synchronize(
    feature_path: Path,
    configuration: Mapping[str, object],
    stage: str,
) -> Mapping[str, object]:
    runtime, model_path, model = _read_model(feature_path, configuration)
    try:
        runtime.sync_ids(model)
        reports = {
            name: runtime.validate_model(model, name)
            for name in runtime.STAGES
        }
        media_errors = runtime.validate_annotation_media(model, feature_path)
        if media_errors:
            for name in ("design", "plan", "validation"):
                reports[name]["errors"].extend(media_errors)
                reports[name]["status"] = "BLOCKED"
        plan_report = reports["plan"]
        views = runtime.render_views(model, plan_report, feature_path)
        annotation_media = runtime.render_annotation_media(model, feature_path)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exception:
        raise ArchiveConfigurationError(
            "INVALID_DLOOP_UI_MODEL",
            f"无法同步 DloopUI 模型：{exception}",
        ) from exception

    model_content = json.dumps(model, ensure_ascii=False, indent=2) + "\n"
    updated_configuration = dict(configuration)
    current_generated_paths = sorted(set(annotation_media) | _owned_capture_paths(model, feature_path) | {UI_VIEW_PATHS["ui-annotation-plan.md"], UI_DELIVERY_PATH})
    previous_summary = configuration["summary"]
    assert isinstance(previous_summary, Mapping)
    previous_generated_paths = {
        str(item) for item in previous_summary.get("generated_paths", [])
    }
    updated_configuration["summary"] = _configuration_summary(
        model,
        reports,
        current_generated_paths,
    )
    files = {
        model_path.relative_to(feature_path).as_posix(): model_content,
    }
    relative_path = UI_VIEW_PATHS["ui-annotation-plan.md"]
    files[relative_path] = _document(
        feature_path.name,
        relative_path,
        views["ui-annotation-plan.md"],
        "confirmed",
        _requirements_version(feature_path),
    )
    files.update(annotation_media)
    files[UI_DELIVERY_PATH] = runtime.render_delivery_html(model, feature_path)
    return {
        "status": reports[stage]["status"],
        "configuration": updated_configuration,
        "files": files,
        "remove_files": sorted(
            previous_generated_paths.difference(current_generated_paths)
        ),
        "result": {
            "status": "synced",
            "configuration": DLOOP_UI_CONFIGURATION,
            "stage": stage,
            "audit_status": reports[stage]["status"],
            "requirements_audit_status": reports["requirements"]["status"],
            "plan_audit_status": plan_report["status"],
            "computed_counts": dict(reports[stage]["computed_counts"]),
            "model_path": UI_MODEL_RELATIVE_PATH,
            "view_paths": [UI_DELIVERY_PATH, relative_path],
            "errors": list(reports[stage]["errors"]),
            "warnings": list(reports[stage]["warnings"]),
        },
    }


def _configuration_summary(
    model: Mapping[str, object],
    reports: Mapping[str, Mapping[str, object]],
    generated_paths: Sequence[str] = (),
) -> Mapping[str, object]:
    plan_report = reports["plan"]
    return {
        "model_sha256": _model_sha256(model),
        "requirements_audit_status": str(
            reports["requirements"]["status"]
        ).lower(),
        "plan_audit_status": str(plan_report["status"]).lower(),
        "generated_paths": sorted(set(generated_paths)),
        "audit_sha256": "sha256:"
        + hashlib.sha256(
            json.dumps(
                plan_report,
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest(),
        "computed_counts": dict(plan_report["computed_counts"]),
    }


def _read_business_input(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exception:
        raise ArchiveConfigurationError(
            "INVALID_DLOOP_UI_INPUT",
            f"无法读取{label}：{exception}",
        ) from exception
    if not isinstance(value, dict):
        raise ArchiveConfigurationError(
            "INVALID_DLOOP_UI_INPUT",
            f"{label}必须包含 JSON 对象。",
        )
    return value


def _capture_adapter(runtime: ModuleType, executable: Path | None):
    return runtime.UnityEditorCaptureAdapter(executable) if executable is not None else None


def _owned_capture_paths(
    model: Mapping[str, object],
    feature_path: Path,
) -> set[str]:
    result: set[str] = set()
    for evidence in model.get("evidence", []):
        if not isinstance(evidence, Mapping) or evidence.get("kind") not in {
            "capture_manifest",
            "prefab_screenshot",
        }:
            continue
        try:
            relative = Path(str(evidence.get("path", ""))).relative_to(feature_path)
        except ValueError:
            continue
        normalized = relative.as_posix()
        if CAPTURE_MEDIA_PATTERN.fullmatch(normalized):
            result.add(normalized)
    return result


def _reports(
    runtime: ModuleType,
    model: dict[str, object],
    feature_path: Path,
    *,
    evidence_sources: Mapping[str, str | Path | bytes] | None = None,
) -> dict[str, dict[str, object]]:
    reports = {
        name: runtime.validate_model(
            model,
            name,
            evidence_sources=evidence_sources,
        )
        for name in runtime.STAGES
    }
    media_errors = runtime.validate_annotation_media(model, feature_path)
    if media_errors:
        for name in ("design", "plan", "validation"):
            reports[name]["errors"].extend(media_errors)
            reports[name]["status"] = "BLOCKED"
    return reports


def _operation_result(
    configuration: Mapping[str, object],
    model: dict[str, object],
    reports: Mapping[str, Mapping[str, object]],
    generated_paths: Sequence[str],
    files: Mapping[str, str],
    result: Mapping[str, object],
    binary_files: Mapping[str, bytes] | None = None,
) -> Mapping[str, object]:
    updated_configuration = dict(configuration)
    updated_configuration["summary"] = _configuration_summary(
        model,
        reports,
        generated_paths,
    )
    previous_summary = configuration["summary"]
    assert isinstance(previous_summary, Mapping)
    previous_paths = {
        str(item) for item in previous_summary.get("generated_paths", [])
    }
    return {
        "status": "PASS",
        "configuration": updated_configuration,
        "files": dict(files),
        "binary_files": dict(binary_files or {}),
        "remove_files": sorted(previous_paths.difference(generated_paths)),
        "result": dict(result),
    }


def _investigate(
    feature_path: Path,
    configuration: Mapping[str, object],
    input_path: Path,
    project_root: Path,
    unity_executable: Path | None,
    capture_manifest: Path | None = None,
) -> Mapping[str, object]:
    runtime, model_path, model = _read_model(feature_path, configuration)
    brief = _read_business_input(input_path, "界面调研简报")
    try:
        operation = runtime.investigate(
            model,
            brief,
            feature_path,
            project_root,
            runtime.ManifestCaptureAdapter(capture_manifest)
            if capture_manifest is not None else _capture_adapter(runtime, unity_executable),
        )
    except runtime.DloopUiError as exception:
        raise ArchiveConfigurationError(exception.code, exception.message) from exception
    if operation["status"] == "awaiting_capture":
        return {
            "status": "AWAITING_CAPTURE",
            "result": {
                **operation,
                "next_action": {
                    "action": "capture-in-open-unity-editor",
                    "detail": "保持当前项目打开，用 Unity MCP 调用已有截图器，再提交生成的清单。",
                    "command": "ui-investigate",
                    "arguments": {
                        "feature_id": feature_path.name,
                        "input": str(input_path),
                        "capture_manifest": operation["capture_manifest"],
                    },
                },
            },
        }
    reports = _reports(
        runtime,
        model,
        feature_path,
        evidence_sources=operation["evidence_sources"],
    )
    capture_paths = sorted(
        set(operation["text_files"]).union(operation["binary_files"])
    )
    files = {
        model_path.relative_to(feature_path).as_posix(): (
            json.dumps(model, ensure_ascii=False, indent=2) + "\n"
        ),
        **operation["text_files"],
    }
    public_result = {
        "status": operation["status"],
        "configuration": DLOOP_UI_CONFIGURATION,
        "model_path": UI_MODEL_RELATIVE_PATH,
        "investigation_token": operation["investigation_token"],
        "captures": operation["captures"],
        "skips": operation["skips"],
        "issues": operation["issues"],
        "computed_counts": dict(reports["investigation"]["computed_counts"]),
    }
    return _operation_result(
        configuration,
        model,
        reports,
        capture_paths,
        files,
        public_result,
        operation["binary_files"],
    )


def _publish(
    feature_path: Path,
    configuration: Mapping[str, object],
    input_path: Path,
    project_root: Path,
    candidate_summary: Mapping[str, object],
    execution: Mapping[str, object],
) -> Mapping[str, object]:
    runtime, model_path, model = _read_model(feature_path, configuration)
    review = _read_business_input(input_path, "界面标注评审")
    from archive_delivery_materials import assemble_delivery
    from archive_handoff import ArchiveHandoffError
    try:
        materials = assemble_delivery(execution, review, feature_path, project_root, model) if review.get("delivery_review") else []
    except ArchiveHandoffError as exception:
        raise ArchiveConfigurationError(exception.code, exception.message) from exception
    try:
        runtime.apply_review_snapshot(model, review, project_root)
        if model.get("delivery_review"):
            if not candidate_summary.get("items"):
                raise runtime.DloopUiError("UI_ACCEPTED_IMPLEMENTATION_REQUIRED", "正式交付核对须在实施候选接受后进行；此前提交内部草稿。")
            model["delivery_review"]["candidate_summary"] = dict(candidate_summary)
            model["delivery_materials"] = materials
            for material in materials:
                evidence = runtime.upsert_evidence(model, runtime.build_file_evidence(
                    "delivery_source", Path(material["path"]),
                    locator=material["locator"], summary=f"任务 {material['package_id']} 的交付材料",
                ))
                model["delivery_review"]["evidence_ids"].append(evidence["id"])
            for relative in ("01-requirements/README.md", "03-design/README.md"):
                evidence = runtime.upsert_evidence(model, runtime.build_file_evidence(
                    "delivery_source", feature_path / relative,
                    locator="完整交付依据", summary="本次需求与设计依据",
                ))
                model["delivery_review"]["evidence_ids"].append(evidence["id"])
        reports = _reports(runtime, model, feature_path)
        plan_report = reports["plan"]
        if model.get("delivery_review"):
            plan_report["errors"].extend(runtime.delivery_errors(model))
            if plan_report["errors"]:
                plan_report["status"] = "BLOCKED"
        if model.get("delivery_review") and plan_report["status"] != "PASS":
            raise runtime.DloopUiError(
                "DLOOP_UI_PLAN_BLOCKED",
                "界面标注评审未形成完整结果："
                + "；".join(item["message"] for item in plan_report["errors"]),
            )
        views = runtime.render_views(model, plan_report, feature_path, project_root)
        annotation_media = runtime.render_annotation_media(model, feature_path)
    except runtime.DloopUiError as exception:
        raise ArchiveConfigurationError(exception.code, exception.message) from exception
    relative_plan = UI_VIEW_PATHS["ui-annotation-plan.md"]
    capture_paths = _owned_capture_paths(model, feature_path)
    generated_paths = sorted(capture_paths.union({relative_plan, UI_DELIVERY_PATH}, annotation_media))
    files = {
        model_path.relative_to(feature_path).as_posix(): (
            json.dumps(model, ensure_ascii=False, indent=2) + "\n"
        ),
        relative_plan: _document(
            feature_path.name,
            relative_plan,
            views["ui-annotation-plan.md"],
            "confirmed",
            _requirements_version(feature_path),
        ),
        **annotation_media,
        UI_DELIVERY_PATH: runtime.render_delivery_html(model, feature_path),
    }
    status = runtime.publication_status(model, plan_report)
    return _operation_result(
        configuration,
        model,
        reports,
        generated_paths,
        files,
        {
            "status": status,
            "configuration": DLOOP_UI_CONFIGURATION,
            "model_path": UI_MODEL_RELATIVE_PATH,
            "artifacts": [UI_DELIVERY_PATH, relative_plan, *sorted(annotation_media)],
            "delivery_ready": bool(model.get("delivery_review")),
            "skips": runtime._public_skips(model, project_root),
            "computed_counts": dict(plan_report["computed_counts"]),
        },
    )


def _initial_configuration(
    model: Mapping[str, object],
    reports: Mapping[str, Mapping[str, object]],
) -> Mapping[str, object]:
    return {
        "id": DLOOP_UI_CONFIGURATION,
        "model_path": UI_MODEL_RELATIVE_PATH,
        "summary": _configuration_summary(model, reports),
    }


def _initialize(
    feature_id: str,
    title: str,
    source_documents: Sequence[Path],
) -> Mapping[str, object]:
    runtime = _runtime()
    model = runtime.new_model(feature_id, title)
    for source in runtime.expand_source_documents(source_documents):
        runtime.upsert_evidence(
            model,
            runtime.build_file_evidence(
                "source_document",
                source,
                summary="用户提供的需求或 DLoop 资料",
            ),
        )
    runtime.sync_ids(model)
    reports = {
        name: runtime.validate_model(model, name)
        for name in runtime.STAGES
    }
    model_content = json.dumps(model, ensure_ascii=False, indent=2) + "\n"
    return {
        "configuration": _initial_configuration(model, reports),
        "files": {UI_MODEL_RELATIVE_PATH: model_content},
    }


def handle(operation: str, **arguments: object) -> Mapping[str, object]:
    """实现通用配置 seam；UI 细节保持在本 Adapter 内。"""

    if operation == "initialize":
        feature_id = arguments.get("feature_id")
        title = arguments.get("title")
        source_documents = arguments.get("source_documents")
        if (
            not isinstance(feature_id, str)
            or not isinstance(title, str)
            or not isinstance(source_documents, list)
            or any(not isinstance(path, Path) for path in source_documents)
        ):
            raise ArchiveConfigurationError(
                "INVALID_CONFIGURATION_INPUT",
                "DloopUI 初始化缺少功能标识、标题或来源材料列表。",
            )
        return _initialize(feature_id, title, source_documents)

    feature_path = arguments.get("feature_path")
    configuration = arguments.get("configuration")
    if not isinstance(feature_path, Path) or not isinstance(configuration, Mapping):
        raise ArchiveConfigurationError(
            "INVALID_CONFIGURATION_INPUT",
            "DloopUI 操作缺少交付档案或配置。",
        )
    if operation == "validate":
        _validate_configuration(configuration, feature_path)
        return {"status": "valid"}
    if operation in {"investigate", "publish"}:
        input_path = arguments.get("input_path")
        project_root = arguments.get("project_root")
        if not isinstance(input_path, Path) or not isinstance(project_root, Path):
            raise ArchiveConfigurationError(
                "INVALID_CONFIGURATION_INPUT",
                "DloopUI 业务操作缺少输入文件或当前 Unity 项目。",
            )
        if operation == "investigate":
            unity_executable = arguments.get("unity_executable")
            capture_manifest = arguments.get("capture_manifest")
            if unity_executable is not None and not isinstance(unity_executable, Path):
                raise ArchiveConfigurationError(
                    "INVALID_CONFIGURATION_INPUT",
                    "Unity Editor 路径不合法。",
                )
            return _investigate(
                feature_path,
                configuration,
                input_path,
                project_root,
                unity_executable,
                capture_manifest,
            )
        return _publish(
            feature_path,
            configuration,
            input_path,
            project_root,
            arguments.get("candidate_summary", {}),
            arguments.get("execution", {}),
        )
    if operation == "evaluate":
        stage = arguments.get("stage")
        if not isinstance(stage, str):
            raise ArchiveConfigurationError(
                "INVALID_CONFIGURATION_INPUT",
                "DloopUI 检查缺少阶段。",
            )
        return _evaluate(feature_path, configuration, stage)
    if operation == "synchronize":
        stage = arguments.get("stage")
        if not isinstance(stage, str):
            raise ArchiveConfigurationError(
                "INVALID_CONFIGURATION_INPUT",
                "DloopUI 同步缺少阶段。",
            )
        return _synchronize(feature_path, configuration, stage)
    raise ArchiveConfigurationError(
        "INVALID_CONFIGURATION_OPERATION",
        f"DloopUI 不支持操作：{operation}",
    )
