#!/usr/bin/env python3
"""DloopUI requirement-first planning, capture, annotation, and audit helpers."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol
from urllib.parse import quote


SCHEMA_VERSION = 5
BRIEF_INPUT_VERSION = 1
REVIEW_INPUT_VERSION = 1
CAPTURE_SCHEMA_VERSION = 1
STAGES = ("requirements", "investigation", "plan")
COUNT_FIELDS = (
    "requirements",
    "matched_prefabs",
    "annotations",
    "change_annotations",
    "reuse_annotations",
    "skipped_requirements",
)
ENTITY_SPECS = {
    "requirements": ("RQ", None),
    "prefabs": ("PF", None),
    "annotations": ("AN", "prefab_id"),
    "skips": ("SK", "requirement_id"),
    "evidence": ("EV", None),
}
TOP_LEVEL_FIELDS = {
    "schema_version",
    "feature",
    "planning_scope",
    "delivery_review",
    "delivery_materials",
    "acceptance_scenarios",
    *ENTITY_SPECS,
    "computed_counts",
}
ENTITY_FIELDS = {
    "requirements": {
        "identity_key", "id", "name_zh", "statement", "source_locator",
        "target_clues", "inference_level", "confidence", "evidence_ids",
    },
    "prefabs": {
        "identity_key", "id", "asset_path", "asset_guid", "asset_sha256",
        "name_zh", "requirement_ids", "match_reason", "capture",
        "capture_error", "evidence_ids",
    },
    "annotations": {
        "identity_key", "id", "prefab_id", "requirement_ids", "disposition",
        "title", "target", "instruction", "expected", "interaction", "feature_point", "related_elements",
        "inference_level", "confidence", "evidence_ids",
    },
    "skips": {
        "identity_key", "id", "requirement_id", "reason", "detail",
        "target_hint", "searched_paths", "candidates", "prefab_id",
    },
    "evidence": {
        "identity_key", "id", "kind", "path", "locator", "summary",
        "status", "sha256", "asset_guid",
    },
}
INFERENCE_LEVELS = {"explicit", "inferred"}
CONFIDENCE_LEVELS = {"high", "medium"}
ANNOTATION_DISPOSITIONS = {"change", "reuse"}
SKIP_REASONS = {
    "prefab-not-found",
    "ambiguous-match",
    "capture-failed",
    "target-not-visible",
}
FILE_EVIDENCE_KINDS = {
    "source_document",
    "capture_manifest",
    "prefab_screenshot",
    "delivery_source",
}
ID_PATTERN = re.compile(r"^[A-Z]{2}-[0-9A-F]{12}$")
IDENTITY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._/\-:]*$")
GUID_PATTERN = re.compile(r"^guid:\s*([0-9a-fA-F]{32})\s*$", re.MULTILINE)


class DloopUiError(ValueError):
    """表示业务输入或 Unity 截图 seam 无法形成可靠结果。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class CaptureAdapter(Protocol):
    def capture(
        self,
        request_path: Path,
        project_root: Path,
        output_dir: Path,
    ) -> Path:
        """运行一次截图，并返回截图清单。"""


class UnityEditorCaptureAdapter:
    """通过当前项目安装的 Unity Editor 包执行真实截图。"""

    def __init__(self, executable: str | Path, timeout_seconds: int = 600) -> None:
        self.executable = Path(canonical_path(executable))
        self.timeout_seconds = timeout_seconds

    def capture(
        self,
        request_path: Path,
        project_root: Path,
        output_dir: Path,
    ) -> Path:
        if not self.executable.is_file():
            raise DloopUiError(
                "DLOOP_UI_UNITY_REQUIRED",
                f"Unity Editor 不存在：{self.executable}",
            )
        environment = os.environ.copy()
        environment["DLOOP_UI_CAPTURE_REQUEST"] = str(request_path)
        try:
            completed = subprocess.run(
                [
                    str(self.executable),
                    "-batchmode",
                    "-quit",
                    "-projectPath",
                    str(project_root),
                    "-executeMethod",
                    "Dloop.Editor.DloopUiCapture.CaptureFromEnvironment",
                    "-logFile",
                    "-",
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exception:
            raise DloopUiError(
                "DLOOP_UI_UNITY_UNAVAILABLE",
                f"无法完成 Unity 截图：{exception}",
            ) from exception
        if completed.returncode != 0:
            details = (completed.stdout + completed.stderr).strip()
            if len(details) > 2000:
                details = details[-2000:]
            raise DloopUiError(
                "DLOOP_UI_UNITY_FAILED",
                "Unity 截图进程失败。" + (f"\n{details}" if details else ""),
            )
        manifest_path = output_dir / "capture-manifest.json"
        if not manifest_path.is_file():
            raise DloopUiError(
                "DLOOP_UI_CAPTURE_MANIFEST_MISSING",
                "Unity 已结束，但没有生成截图清单。",
            )
        return manifest_path


class ManifestCaptureAdapter:
    """接收当前编辑器生成的清单，后续仍执行统一的资产和截图校验。"""

    def __init__(self, manifest_path: Path) -> None:
        self.manifest_path = manifest_path

    def capture(self, request_path: Path, project_root: Path, output_dir: Path) -> Path:
        try:
            read_json(self.manifest_path)
        except (OSError, UnicodeError, ValueError) as exception:
            raise DloopUiError(
                "DLOOP_UI_CAPTURE_PROTOCOL_INVALID",
                f"无法读取当前编辑器的截图清单：{exception}",
            ) from exception
        return self.manifest_path


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"{path} 必须包含 JSON 对象")
    return value


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def canonical_path(value: str | Path, base: Path | None = None) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute() and base is not None:
        path = base / path
    return path.resolve(strict=False).as_posix()


def path_key(value: str | Path) -> str:
    return canonical_path(value).casefold()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def stable_id(prefix: str, collection: str, identity_key: str, parent: str = "") -> str:
    material = f"{collection}|{parent}|{identity_key}".encode("utf-8")
    return f"{prefix}-{hashlib.sha256(material).hexdigest()[:12].upper()}"


def stable_token(value: str, length: int = 12) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def issue(
    code: str,
    message: str,
    entity_id: str = "",
    severity: str = "error",
) -> dict[str, str]:
    return {
        "severity": severity,
        "code": code,
        "entity_id": entity_id,
        "message": message,
    }


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def ensure_collections(model: dict[str, Any]) -> None:
    for collection in ENTITY_SPECS:
        model.setdefault(collection, [])
    model.setdefault("computed_counts", {field: 0 for field in COUNT_FIELDS})


def new_model(feature_id: str, title: str) -> dict[str, Any]:
    model: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "feature": {"id": feature_id, "title": title},
        "planning_scope": {
            "mode": "existing-prefab-annotation",
            "existing_prefab_behavior": "preserve-unless-explicitly-changed",
            "missing_prefab": "skip",
        },
        "delivery_review": None,
        "delivery_materials": [],
        "acceptance_scenarios": [],
        "requirements": [],
        "prefabs": [],
        "annotations": [],
        "skips": [],
        "evidence": [],
        "computed_counts": {field: 0 for field in COUNT_FIELDS},
    }
    sync_ids(model)
    return model


def collection_map(model: dict[str, Any], collection: str) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("id", "")): item
        for item in as_list(model.get(collection))
        if isinstance(item, dict) and str(item.get("id", ""))
    }


def sync_ids(model: dict[str, Any]) -> list[dict[str, str]]:
    """Assign missing stable IDs and refresh derived counts."""

    ensure_collections(model)
    problems: list[dict[str, str]] = []
    for collection, (prefix, parent_field) in ENTITY_SPECS.items():
        seen: set[tuple[str, str]] = set()
        for index, record in enumerate(as_list(model.get(collection))):
            if not isinstance(record, dict):
                problems.append(
                    issue("MODEL_RECORD_TYPE", f"{collection}[{index}] 必须是对象")
                )
                continue
            identity_key = str(record.get("identity_key", "")).strip()
            parent = str(record.get(parent_field, "")).strip() if parent_field else ""
            if identity_key:
                key = (parent, identity_key)
                if key in seen:
                    problems.append(
                        issue(
                            "IDENTITY_KEY_DUPLICATE",
                            f"{collection} 内身份键重复：{identity_key}",
                            str(record.get("id", "")),
                        )
                    )
                seen.add(key)
                if not record.get("id"):
                    record["id"] = stable_id(
                        prefix,
                        collection,
                        identity_key,
                        parent,
                    )
            elif not record.get("id"):
                problems.append(
                    issue(
                        "IDENTITY_KEY_MISSING",
                        f"{collection}[{index}] 缺少 identity_key",
                    )
                )
    model["computed_counts"] = compute_counts(model)
    return problems


def compute_counts(model: dict[str, Any]) -> dict[str, int]:
    annotations = [
        item
        for item in as_list(model.get("annotations"))
        if isinstance(item, dict)
    ]
    skipped_requirement_ids = {
        str(item.get("requirement_id", ""))
        for item in as_list(model.get("skips"))
        if isinstance(item, dict) and str(item.get("requirement_id", ""))
    }
    return {
        "requirements": sum(
            1 for item in as_list(model.get("requirements")) if isinstance(item, dict)
        ),
        "matched_prefabs": sum(
            1 for item in as_list(model.get("prefabs")) if isinstance(item, dict)
        ),
        "annotations": len(annotations),
        "change_annotations": sum(
            1 for item in annotations if item.get("disposition") == "change"
        ),
        "reuse_annotations": sum(
            1 for item in annotations if item.get("disposition") == "reuse"
        ),
        "skipped_requirements": len(skipped_requirement_ids),
    }


def evidence_identity(kind: str, path: str, locator: str = "") -> str:
    material = f"{kind}|{path_key(path)}|{locator.strip()}"
    return "evidence." + stable_token(material, 24)


def upsert_evidence(
    model: dict[str, Any],
    record: dict[str, Any],
) -> dict[str, Any]:
    ensure_collections(model)
    identity_key = str(record.get("identity_key", "")).strip()
    for existing in model["evidence"]:
        if isinstance(existing, dict) and existing.get("identity_key") == identity_key:
            existing_id = str(existing.get("id", ""))
            existing.update(record)
            existing["id"] = existing_id or stable_id(
                "EV",
                "evidence",
                identity_key,
            )
            return existing
    value = dict(record)
    if not value.get("id"):
        value["id"] = stable_id("EV", "evidence", identity_key)
    model["evidence"].append(value)
    return value


def build_file_evidence(
    kind: str,
    path_value: str | Path,
    locator: str = "",
    summary: str = "",
) -> dict[str, Any]:
    path = Path(canonical_path(path_value))
    exists = path.is_file()
    normalized = path.as_posix()
    return {
        "identity_key": evidence_identity(kind, normalized, locator),
        "id": "",
        "kind": kind,
        "path": normalized,
        "locator": locator,
        "summary": summary,
        "status": "verified" if exists else "missing",
        "sha256": sha256_file(path) if exists else "",
    }


def build_file_evidence_at(
    kind: str,
    source_path_value: str | Path,
    recorded_path_value: str | Path,
    locator: str = "",
    summary: str = "",
) -> dict[str, Any]:
    """从暂存文件取证，但把最终档案路径写入模型。"""

    source_path = Path(canonical_path(source_path_value))
    recorded_path = canonical_path(recorded_path_value)
    exists = source_path.is_file()
    return {
        "identity_key": evidence_identity(kind, recorded_path, locator),
        "id": "",
        "kind": kind,
        "path": recorded_path,
        "locator": locator,
        "summary": summary,
        "status": "verified" if exists else "missing",
        "sha256": sha256_file(source_path) if exists else "",
    }


def expand_source_documents(values: Iterable[str | Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for value in values:
        path = Path(canonical_path(value))
        candidates = (
            sorted(
                item
                for item in path.rglob("*")
                if item.is_file() and item.suffix.casefold() in {".md", ".json", ".txt"}
            )
            if path.is_dir()
            else [path]
        )
        for candidate in candidates:
            key = path_key(candidate)
            if key not in seen:
                seen.add(key)
                result.append(candidate)
    return result


def unity_meta_guid(prefab_path: Path) -> str:
    meta_path = Path(str(prefab_path) + ".meta")
    if not meta_path.is_file():
        return ""
    match = GUID_PATTERN.search(meta_path.read_text(encoding="utf-8", errors="replace"))
    return match.group(1).lower() if match else ""


def _prefab_identity(path: Path) -> str:
    return "prefab." + stable_token(path_key(path), 16)


def _skip_identity(
    requirement_id: str,
    reason: str,
    target_hint: str,
) -> str:
    return "skip." + stable_token(f"{requirement_id}|{reason}|{target_hint}", 16)


def _upsert_skip(
    model: dict[str, Any],
    requirement_id: str,
    reason: str,
    detail: str,
    target_hint: str = "",
    searched_paths: Iterable[str] = (),
    prefab_id: str = "",
    candidates: Iterable[str] = (),
) -> None:
    identity_key = _skip_identity(requirement_id, reason, target_hint)
    value = next(
        (
            item
            for item in as_list(model.get("skips"))
            if isinstance(item, dict) and item.get("identity_key") == identity_key
        ),
        None,
    )
    if value is None:
        value = {
            "identity_key": identity_key,
            "id": "",
            "requirement_id": requirement_id,
        }
        model["skips"].append(value)
    value.update(
        {
            "reason": reason,
            "detail": detail,
            "target_hint": target_hint,
            "searched_paths": sorted(
                {canonical_path(path) for path in searched_paths if str(path).strip()}
            ),
            "candidates": sorted(
                {canonical_path(path) for path in candidates if str(path).strip()}
            ),
            "prefab_id": prefab_id,
        }
    )


def prepare_capture_request(
    model: dict[str, Any],
    request_path_value: str | Path,
    project_root_value: str | Path,
    output_dir_value: str | Path,
) -> list[dict[str, str]]:
    """Write the compact request consumed by the Unity capture adapter."""

    problems = sync_ids(model)
    project_root = Path(canonical_path(project_root_value))
    request_path = Path(canonical_path(request_path_value))
    output_dir = Path(canonical_path(output_dir_value))
    if not project_root.is_dir() or not project_root.joinpath("Assets").is_dir():
        problems.append(
            issue("UNITY_PROJECT_ROOT_INVALID", "Unity 项目根目录必须包含 Assets")
        )
    try:
        output_dir.relative_to(project_root)
    except ValueError:
        problems.append(
            issue("CAPTURE_OUTPUT_OUTSIDE_PROJECT", "截图输出目录必须位于 Unity 项目内")
        )
    prefabs: list[dict[str, str]] = []
    removed_prefab_ids: set[str] = set()
    assets_root = project_root / "Assets"
    for prefab in as_list(model.get("prefabs")):
        if not isinstance(prefab, dict):
            continue
        asset_path = Path(canonical_path(str(prefab.get("asset_path", ""))))
        if not asset_path.is_file() or asset_path.suffix.casefold() != ".prefab":
            removed_prefab_ids.add(str(prefab.get("id", "")))
            prefab["capture"] = {}
            prefab["capture_error"] = "Prefab 在截图前已不存在。"
            for requirement_id in as_list(prefab.get("requirement_ids")):
                _upsert_skip(
                    model,
                    str(requirement_id),
                    "prefab-not-found",
                    "已匹配 Prefab 在截图前不存在，因此不生成替代资产或占位界面。",
                    asset_path.as_posix(),
                    [asset_path.as_posix()],
                    str(prefab.get("id", "")),
                )
            problems.append(
                issue(
                    "CAPTURE_PREFAB_MISSING_SKIPPED",
                    "已匹配 Prefab 在截图前不存在，已跳过。",
                    str(prefab.get("id", "")),
                    severity="warning",
                )
            )
            continue
        try:
            asset_path.relative_to(assets_root)
        except ValueError:
            problems.append(
                issue(
                    "CAPTURE_PREFAB_OUTSIDE_PROJECT",
                    "已匹配 Prefab 不在当前 Unity 项目的 Assets 内",
                    str(prefab.get("id", "")),
                )
            )
            continue
        prefabs.append(
            {
                "prefab_id": str(prefab.get("id", "")),
                "asset_path": asset_path.as_posix(),
            }
        )
    if removed_prefab_ids:
        model["prefabs"] = [
            item
            for item in as_list(model.get("prefabs"))
            if not (
                isinstance(item, dict)
                and str(item.get("id", "")) in removed_prefab_ids
            )
        ]
        model["annotations"] = [
            item
            for item in as_list(model.get("annotations"))
            if not (
                isinstance(item, dict)
                and str(item.get("prefab_id", "")) in removed_prefab_ids
            )
        ]
        for skip in as_list(model.get("skips")):
            if isinstance(skip, dict) and str(skip.get("prefab_id", "")) in removed_prefab_ids:
                skip["prefab_id"] = ""
    if not prefabs:
        problems.append(
            issue(
                "CAPTURE_NOT_NEEDED",
                "没有已匹配且真实存在的 Prefab，无需运行 Unity 截图。",
                severity="warning",
            )
        )
    sync_ids(model)
    if any(item["severity"] == "error" for item in problems):
        return problems
    write_json(
        request_path,
        {
            "schema_version": CAPTURE_SCHEMA_VERSION,
            "project_root": project_root.as_posix(),
            "output_dir": output_dir.as_posix(),
            "prefabs": prefabs,
        },
    )
    return problems


def _capture_path(value: object, manifest_path: Path) -> Path:
    raw = str(value or "").strip()
    return Path(canonical_path(raw, manifest_path.parent)) if raw else Path("")


def _capture_rect(value: object) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, float] = {}
    for field in ("x", "y", "width", "height"):
        item = value.get(field)
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return {}
        result[field] = round(float(item), 3)
    return result


def _remove_capture_skip(
    model: dict[str, Any],
    requirement_id: str,
    prefab_id: str,
) -> None:
    model["skips"] = [
        item
        for item in as_list(model.get("skips"))
        if not (
            isinstance(item, dict)
            and item.get("requirement_id") == requirement_id
            and item.get("prefab_id") == prefab_id
            and item.get("reason") == "capture-failed"
        )
    ]


def import_capture(
    model: dict[str, Any],
    manifest_path_value: str | Path,
    *,
    manifest_evidence_path: str | Path | None = None,
    screenshot_source_paths: Mapping[str, str | Path] | None = None,
    screenshot_evidence_paths: Mapping[str, str | Path] | None = None,
) -> list[dict[str, str]]:
    """Import screenshots and visible rectangles without interpreting Prefab behaviour."""

    sync_ids(model)
    manifest_path = Path(canonical_path(manifest_path_value))
    manifest = read_json(manifest_path)
    if manifest.get("schema_version") != CAPTURE_SCHEMA_VERSION:
        return [
            issue(
                "CAPTURE_SCHEMA_VERSION",
                f"截图清单 schema_version 必须是 {CAPTURE_SCHEMA_VERSION}",
            )
        ]
    raw_prefabs = manifest.get("prefabs")
    if not isinstance(raw_prefabs, list):
        return [issue("CAPTURE_PREFABS_INVALID", "截图清单 prefabs 必须是数组")]
    prefabs = collection_map(model, "prefabs")
    problems: list[dict[str, str]] = []
    recorded_manifest_path = manifest_evidence_path or manifest_path
    manifest_evidence = upsert_evidence(
        model,
        build_file_evidence_at(
            "capture_manifest",
            manifest_path,
            recorded_manifest_path,
            locator="Unity capture manifest",
            summary="Unity 导出的已有 Prefab 截图清单",
        ),
    )

    for raw_error in as_list(manifest.get("errors")):
        if isinstance(raw_error, dict):
            prefab_id = str(raw_error.get("prefab_id", "")).strip()
            message = str(raw_error.get("message", "")).strip() or "Unity 截图失败"
        else:
            prefab_id = ""
            message = str(raw_error).strip() or "Unity 截图失败"
        prefab = prefabs.get(prefab_id)
        if prefab is not None:
            prefab["capture_error"] = message
            for requirement_id in as_list(prefab.get("requirement_ids")):
                _upsert_skip(
                    model,
                    str(requirement_id),
                    "capture-failed",
                    f"已有 Prefab 截图失败，已跳过该界面的标注：{message}",
                    str(prefab.get("asset_path", "")),
                    [str(prefab.get("asset_path", ""))],
                    prefab_id,
                )
        problems.append(
            issue(
                "CAPTURE_FAILED_SKIPPED",
                message,
                prefab_id,
                severity="warning",
            )
        )

    seen_prefab_ids: set[str] = set()
    for index, raw_prefab in enumerate(raw_prefabs):
        if not isinstance(raw_prefab, dict):
            problems.append(
                issue(
                    "CAPTURE_PREFAB_INVALID",
                    f"截图清单第 {index + 1} 个 Prefab 不是对象",
                )
            )
            continue
        prefab_id = str(raw_prefab.get("prefab_id", "")).strip()
        prefab = prefabs.get(prefab_id)
        if prefab is None:
            problems.append(
                issue(
                    "CAPTURE_PREFAB_UNMATCHED",
                    "截图清单包含未经过需求匹配的 Prefab",
                    prefab_id,
                )
            )
            continue
        if prefab_id in seen_prefab_ids:
            problems.append(
                issue("CAPTURE_PREFAB_DUPLICATE", "截图清单重复包含 Prefab", prefab_id)
            )
            continue
        seen_prefab_ids.add(prefab_id)
        captured_path = str(raw_prefab.get("asset_path", "")).strip()
        if not captured_path or path_key(captured_path) != path_key(
            str(prefab.get("asset_path", ""))
        ):
            problems.append(
                issue(
                    "CAPTURE_PREFAB_PATH_MISMATCH",
                    "截图清单与已匹配 Prefab 的资产路径不一致",
                    prefab_id,
                )
            )
            continue
        captured_guid = str(raw_prefab.get("asset_guid", "")).strip().lower()
        captured_sha = str(raw_prefab.get("asset_sha256", "")).strip()
        if prefab.get("asset_guid") and captured_guid != prefab.get("asset_guid"):
            problems.append(
                issue(
                    "CAPTURE_PREFAB_GUID_MISMATCH",
                    "截图清单与已匹配 Prefab 的 Unity GUID 不一致",
                    prefab_id,
                )
            )
            continue
        if captured_sha != prefab.get("asset_sha256"):
            problems.append(
                issue(
                    "CAPTURE_PREFAB_SHA_MISMATCH",
                    "截图清单与已匹配 Prefab 的内容摘要不一致",
                    prefab_id,
                )
            )
            continue
        screenshot_path = Path(
            canonical_path(
                (screenshot_source_paths or {}).get(
                    prefab_id,
                    _capture_path(raw_prefab.get("screenshot_path"), manifest_path),
                )
            )
        )
        recorded_screenshot_path = Path(
            canonical_path(
                (screenshot_evidence_paths or {}).get(prefab_id, screenshot_path)
            )
        )
        width = raw_prefab.get("width")
        height = raw_prefab.get("height")
        if not screenshot_path.is_file():
            problems.append(
                issue("CAPTURE_SCREENSHOT_MISSING", "Prefab 原始截图不存在", prefab_id)
            )
            continue
        if (
            isinstance(width, bool)
            or not isinstance(width, int)
            or width <= 0
            or isinstance(height, bool)
            or not isinstance(height, int)
            or height <= 0
        ):
            problems.append(
                issue("CAPTURE_SIZE_INVALID", "Prefab 截图宽高必须是正整数", prefab_id)
            )
            continue
        raw_regions = raw_prefab.get("regions", [])
        if not isinstance(raw_regions, list):
            problems.append(
                issue("CAPTURE_REGIONS_INVALID", "Prefab 可见区域必须是数组", prefab_id)
            )
            continue
        regions: list[dict[str, Any]] = []
        seen_objects: set[str] = set()
        region_invalid = False
        for region_index, raw_region in enumerate(raw_regions):
            if not isinstance(raw_region, dict):
                problems.append(
                    issue(
                        "CAPTURE_REGION_INVALID",
                        f"Prefab 第 {region_index + 1} 个可见区域不是对象",
                        prefab_id,
                    )
                )
                region_invalid = True
                continue
            object_id = str(raw_region.get("object_id", "")).strip()
            node_name = str(raw_region.get("node_name", "")).strip()
            hierarchy_path = str(raw_region.get("hierarchy_path", "")).strip()
            rect = _capture_rect(raw_region.get("rect"))
            if not object_id or not node_name or not hierarchy_path or not rect:
                problems.append(
                    issue(
                        "CAPTURE_REGION_INVALID",
                        "Prefab 可见区域缺少身份、名称、层级路径或矩形",
                        prefab_id,
                    )
                )
                region_invalid = True
                continue
            if object_id in seen_objects:
                problems.append(
                    issue(
                        "CAPTURE_REGION_DUPLICATE",
                        f"Prefab 可见区域重复：{hierarchy_path}",
                        prefab_id,
                    )
                )
                region_invalid = True
                continue
            seen_objects.add(object_id)
            regions.append(
                {
                    "object_id": object_id,
                    "node_name": node_name,
                    "hierarchy_path": hierarchy_path,
                    "rect": rect,
                }
            )
        if region_invalid:
            continue
        screenshot_evidence = upsert_evidence(
            model,
            build_file_evidence_at(
                "prefab_screenshot",
                screenshot_path,
                recorded_screenshot_path,
                locator=f"raw capture for {prefab_id}",
                summary="已有 Prefab 的原始截图",
            ),
        )
        prefab["capture"] = {
            "manifest_evidence_id": manifest_evidence["id"],
            "screenshot_evidence_id": screenshot_evidence["id"],
            "width": width,
            "height": height,
            "regions": regions,
        }
        prefab["capture_error"] = ""
        prefab["evidence_ids"] = list(
            dict.fromkeys(
                as_list(prefab.get("evidence_ids"))
                + [manifest_evidence["id"], screenshot_evidence["id"]]
            )
        )
        for requirement_id in as_list(prefab.get("requirement_ids")):
            _remove_capture_skip(model, str(requirement_id), prefab_id)
    sync_ids(model)
    return problems


def _require_fields(
    value: object,
    required: set[str],
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != required:
        raise DloopUiError(
            "DLOOP_UI_INPUT_INVALID",
            f"{label} 字段必须是：{'、'.join(sorted(required))}",
        )
    return value


def _required_text(value: object, label: str) -> str:
    text = str(value).strip() if isinstance(value, str) else ""
    if not text:
        raise DloopUiError("DLOOP_UI_INPUT_INVALID", f"{label} 必须是非空文本。")
    return text


def _text_list(value: object, label: str, *, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise DloopUiError("DLOOP_UI_INPUT_INVALID", f"{label} 必须是文本数组。")
    result = list(dict.fromkeys(item.strip() for item in value))
    if not allow_empty and not result:
        raise DloopUiError("DLOOP_UI_INPUT_INVALID", f"{label} 不能为空。")
    return result


def _project_path(value: str, project_root: Path) -> Path:
    return Path(canonical_path(value, project_root))


def _require_project_asset(path: Path, project_root: Path, label: str) -> None:
    try:
        path.relative_to(project_root / "Assets")
    except ValueError as exception:
        raise DloopUiError(
            "DLOOP_UI_INPUT_INVALID",
            f"{label} 必须位于当前 Unity 项目的 Assets 内：{path}",
        ) from exception


def _refresh_registered_sources(model: dict[str, Any]) -> list[dict[str, Any]]:
    refreshed: list[dict[str, Any]] = []
    for evidence in as_list(model.get("evidence")):
        if not isinstance(evidence, dict) or evidence.get("kind") != "source_document":
            continue
        value = build_file_evidence(
            "source_document",
            str(evidence.get("path", "")),
            locator=str(evidence.get("locator", "")),
            summary=str(evidence.get("summary", "")) or "用户提供的需求或 DLoop 资料",
        )
        value["id"] = str(evidence.get("id", ""))
        refreshed.append(value)
    return refreshed


def apply_brief_snapshot(
    model: dict[str, Any],
    brief: dict[str, Any],
    project_root_value: str | Path,
) -> list[dict[str, str]]:
    """用一份完整业务简报替换需求、匹配和前置跳过判断。"""

    _require_fields(brief, {"input_version", "requirements"} | {name for name in ("refresh_prefabs", "unchanged_layout") if name in brief}, "界面调研简报")
    if "unchanged_layout" in brief and "refresh_prefabs" not in brief:
        raise DloopUiError("DLOOP_UI_INPUT_INVALID", "复用历史位置截图须同时指定 refresh_prefabs。")
    if brief.get("input_version") != BRIEF_INPUT_VERSION:
        raise DloopUiError(
            "DLOOP_UI_INPUT_VERSION",
            f"界面调研简报 input_version 必须是 {BRIEF_INPUT_VERSION}。",
        )
    raw_requirements = brief.get("requirements")
    if not isinstance(raw_requirements, list) or not raw_requirements:
        raise DloopUiError(
            "DLOOP_UI_INPUT_INVALID",
            "界面调研简报至少需要一条 UI 需求。",
        )
    feature = model.get("feature")
    if not isinstance(feature, dict):
        raise DloopUiError("DLOOP_UI_MODEL_INVALID", "UI 模型缺少功能身份。")
    model["delivery_review"] = None
    model["acceptance_scenarios"] = []
    project_root = Path(canonical_path(project_root_value))
    refreshed_sources = _refresh_registered_sources(model)
    sources_by_path = {
        path_key(str(item.get("path", ""))): item for item in refreshed_sources
    }
    replacement = new_model(
        _required_text(feature.get("id"), "功能标识"),
        _required_text(feature.get("title"), "功能标题"),
    )
    replacement["evidence"] = refreshed_sources
    requirement_matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    seen_keys: set[str] = set()

    for index, raw_requirement in enumerate(raw_requirements, start=1):
        requirement = _require_fields(
            raw_requirement,
            {
                "key", "name", "statement", "source", "target_clues",
                "inference_level", "confidence", "match",
            },
            f"第 {index} 条 UI 需求",
        )
        key = _required_text(requirement.get("key"), "需求 key")
        if not IDENTITY_PATTERN.fullmatch(key) or key in seen_keys:
            raise DloopUiError(
                "DLOOP_UI_INPUT_INVALID",
                f"需求 key 无效或重复：{key}",
            )
        seen_keys.add(key)
        source = _require_fields(
            requirement.get("source"),
            {"path", "locator"},
            f"需求 {key} 的来源",
        )
        source_path = _project_path(
            _required_text(source.get("path"), f"需求 {key} 的来源路径"),
            project_root,
        )
        source_evidence = sources_by_path.get(path_key(source_path))
        if source_evidence is None:
            raise DloopUiError(
                "DLOOP_UI_SOURCE_NOT_REGISTERED",
                f"需求 {key} 引用的来源未在初始化时登记：{source_path}",
            )
        target_clues = _text_list(
            requirement.get("target_clues"),
            f"需求 {key} 的定位线索",
        )
        inference_level = requirement.get("inference_level")
        confidence = requirement.get("confidence")
        if inference_level not in INFERENCE_LEVELS or confidence not in CONFIDENCE_LEVELS:
            raise DloopUiError(
                "DLOOP_UI_INPUT_INVALID",
                f"需求 {key} 的判断依据或置信度无效。",
            )
        replacement["requirements"].append(
            {
                "identity_key": key,
                "id": "",
                "name_zh": _required_text(requirement.get("name"), f"需求 {key} 的名称"),
                "statement": _required_text(
                    requirement.get("statement"), f"需求 {key} 的可观察描述"
                ),
                "source_locator": _required_text(
                    source.get("locator"), f"需求 {key} 的来源定位"
                ),
                "target_clues": target_clues,
                "inference_level": inference_level,
                "confidence": confidence,
                "evidence_ids": [source_evidence["id"]],
            }
        )
        requirement_matches.append((replacement["requirements"][-1], requirement["match"]))

    problems = sync_ids(replacement)
    prefabs_by_path: dict[str, dict[str, Any]] = {}
    for requirement, raw_match in requirement_matches:
        match = raw_match if isinstance(raw_match, dict) else {}
        status = match.get("status")
        requirement_id = str(requirement["id"])
        target_hint = "；".join(requirement["target_clues"])
        if status == "matched":
            _require_fields(match, {"status", "prefabs"}, f"需求 {requirement['identity_key']} 的匹配")
            raw_prefabs = match.get("prefabs")
            if not isinstance(raw_prefabs, list) or not raw_prefabs:
                raise DloopUiError(
                    "DLOOP_UI_INPUT_INVALID",
                    f"需求 {requirement['identity_key']} 的 matched 结果必须包含 Prefab。",
                )
            for raw_prefab in raw_prefabs:
                prefab_input = _require_fields(
                    raw_prefab,
                    {"path", "name", "reason"},
                    f"需求 {requirement['identity_key']} 的 Prefab",
                )
                asset_path = _project_path(
                    _required_text(prefab_input.get("path"), "Prefab 路径"),
                    project_root,
                )
                _require_project_asset(asset_path, project_root, "Prefab")
                if not asset_path.is_file() or asset_path.suffix.casefold() != ".prefab":
                    _upsert_skip(
                        replacement,
                        requirement_id,
                        "prefab-not-found",
                        "未找到需求可复用的已有 Prefab，因此不生成替代资产或占位界面。",
                        target_hint or asset_path.name,
                        [asset_path.as_posix()],
                    )
                    problems.append(
                        issue(
                            "PREFAB_NOT_FOUND_SKIPPED",
                            f"Prefab 不存在，已跳过：{asset_path}",
                            requirement_id,
                            severity="warning",
                        )
                    )
                    continue
                key = path_key(asset_path)
                prefab = prefabs_by_path.get(key)
                reason = _required_text(prefab_input.get("reason"), "Prefab 匹配依据")
                if prefab is None:
                    prefab = {
                        "identity_key": _prefab_identity(asset_path),
                        "id": "",
                        "asset_path": asset_path.as_posix(),
                        "asset_guid": unity_meta_guid(asset_path),
                        "asset_sha256": sha256_file(asset_path),
                        "name_zh": _required_text(prefab_input.get("name"), "Prefab 名称"),
                        "requirement_ids": [],
                        "match_reason": reason,
                        "capture": {},
                        "capture_error": "",
                        "evidence_ids": [],
                    }
                    replacement["prefabs"].append(prefab)
                    prefabs_by_path[key] = prefab
                elif reason not in str(prefab.get("match_reason", "")):
                    prefab["match_reason"] = f"{prefab['match_reason']}；{reason}"
                prefab["requirement_ids"] = list(
                    dict.fromkeys(as_list(prefab.get("requirement_ids")) + [requirement_id])
                )
                evidence = upsert_evidence(
                    replacement,
                    {
                        **build_file_evidence(
                            "prefab_asset",
                            asset_path,
                            locator="matched existing Prefab",
                            summary="需求匹配到的已有 Unity Prefab",
                        ),
                        "asset_guid": prefab["asset_guid"],
                    },
                )
                prefab["evidence_ids"] = [evidence["id"]]
        elif status == "not-found":
            _require_fields(
                match,
                {"status", "detail", "searched_paths"},
                f"需求 {requirement['identity_key']} 的匹配",
            )
            searched = [
                _project_path(item, project_root).as_posix()
                for item in _text_list(
                    match.get("searched_paths"),
                    f"需求 {requirement['identity_key']} 的搜索位置",
                    allow_empty=False,
                )
            ]
            _upsert_skip(
                replacement,
                requirement_id,
                "prefab-not-found",
                _required_text(match.get("detail"), "未找到 Prefab 的说明"),
                target_hint,
                searched,
            )
        elif status == "ambiguous":
            _require_fields(
                match,
                {"status", "detail", "candidates"},
                f"需求 {requirement['identity_key']} 的匹配",
            )
            candidates = [
                _project_path(item, project_root)
                for item in _text_list(
                    match.get("candidates"),
                    f"需求 {requirement['identity_key']} 的候选 Prefab",
                    allow_empty=False,
                )
            ]
            if len(candidates) < 2:
                raise DloopUiError(
                    "DLOOP_UI_INPUT_INVALID",
                    f"需求 {requirement['identity_key']} 的歧义匹配至少需要两个候选 Prefab。",
                )
            for candidate in candidates:
                _require_project_asset(candidate, project_root, "候选 Prefab")
                if not candidate.is_file() or candidate.suffix.casefold() != ".prefab":
                    raise DloopUiError(
                        "DLOOP_UI_INPUT_INVALID",
                        f"歧义候选必须是真实 Prefab：{candidate}",
                    )
            candidate_paths = [item.as_posix() for item in candidates]
            _upsert_skip(
                replacement,
                requirement_id,
                "ambiguous-match",
                _required_text(match.get("detail"), "歧义匹配说明"),
                target_hint,
                candidate_paths,
                candidates=candidate_paths,
            )
        else:
            raise DloopUiError(
                "DLOOP_UI_INPUT_INVALID",
                f"需求 {requirement['identity_key']} 的 match.status 必须是 matched、not-found 或 ambiguous。",
            )

    sync_ids(replacement)
    replacement["delivery_materials"] = model.get("delivery_materials", [])
    model.clear()
    model.update(replacement)
    return problems


def investigation_token(model: dict[str, Any]) -> str:
    snapshot = {
        "requirements": as_list(model.get("requirements")),
        "prefabs": as_list(model.get("prefabs")),
        "file_evidence": [
            {
                "id": item.get("id"),
                "kind": item.get("kind"),
                "sha256": item.get("sha256"),
            }
            for item in as_list(model.get("evidence"))
            if isinstance(item, dict) and item.get("kind") in (FILE_EVIDENCE_KINDS - {"delivery_source"})
        ],
        "skips": [
            item
            for item in as_list(model.get("skips"))
            if isinstance(item, dict) and item.get("reason") != "target-not-visible"
        ],
    }
    canonical = json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _normalized_capture_artifacts(
    model: dict[str, Any],
    manifest_path: Path,
    feature_path: Path,
    staging_path: Path,
    reused_captures: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, str], dict[str, bytes], dict[str, bytes], list[dict[str, str]]]:
    manifest = read_json(manifest_path)
    raw_prefabs = manifest.get("prefabs")
    if manifest.get("schema_version") != CAPTURE_SCHEMA_VERSION or not isinstance(raw_prefabs, list):
        raise DloopUiError(
            "DLOOP_UI_CAPTURE_PROTOCOL_INVALID",
            "Unity 截图清单不符合当前截图协议。",
        )
    raw_prefabs.extend(reused_captures or [])
    capture_relative = Path("02-investigation") / "ui-captures"
    final_manifest = feature_path / capture_relative / "capture-manifest.json"
    screenshot_sources: dict[str, Path] = {}
    screenshot_destinations: dict[str, Path] = {}
    binary_files: dict[str, bytes] = {}
    normalized = json.loads(json.dumps(manifest))
    for raw_source, raw_normalized in zip(raw_prefabs, normalized["prefabs"]):
        if not isinstance(raw_source, dict) or not isinstance(raw_normalized, dict):
            raise DloopUiError(
                "DLOOP_UI_CAPTURE_PROTOCOL_INVALID",
                "Unity 截图清单中的 Prefab 结果必须是对象。",
            )
        prefab_id = _required_text(raw_source.get("prefab_id"), "截图 Prefab 身份")
        source = _capture_path(raw_source.get("screenshot_path"), manifest_path)
        if not source.is_file():
            raise DloopUiError(
                "DLOOP_UI_CAPTURE_PROTOCOL_INVALID",
                f"Unity 截图清单引用的图片不存在：{source}",
            )
        relative = capture_relative / f"{prefab_id}-raw.png"
        destination = feature_path / relative
        raw_normalized["screenshot_path"] = destination.as_posix()
        screenshot_sources[prefab_id] = source
        screenshot_destinations[prefab_id] = destination
        binary_files[relative.as_posix()] = source.read_bytes()
    normalized_manifest_path = staging_path / "normalized-capture-manifest.json"
    normalized_content = json.dumps(normalized, ensure_ascii=False, indent=2) + "\n"
    with normalized_manifest_path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(normalized_content)
    problems = import_capture(
        model,
        normalized_manifest_path,
        manifest_evidence_path=final_manifest,
        screenshot_source_paths=screenshot_sources,
        screenshot_evidence_paths=screenshot_destinations,
    )
    for reused in reused_captures or []:
        if reused.get("reuse"):
            collection_map(model, "prefabs")[reused["prefab_id"]]["capture"]["reuse"] = dict(reused["reuse"])
    text_files = {capture_relative.joinpath("capture-manifest.json").as_posix(): normalized_content}
    evidence_sources = {
        final_manifest.as_posix(): normalized_content.encode("utf-8")
    }
    evidence_sources.update(
        {
            destination.as_posix(): binary_files[
                capture_relative.joinpath(f"{prefab_id}-raw.png").as_posix()
            ]
            for prefab_id, destination in screenshot_destinations.items()
        }
    )
    return text_files, binary_files, evidence_sources, problems


def _public_skips(model: dict[str, Any], project_root: Path) -> list[dict[str, Any]]:
    requirements = collection_map(model, "requirements")
    result: list[dict[str, Any]] = []
    for skip in as_list(model.get("skips")):
        if not isinstance(skip, dict):
            continue
        requirement = requirements.get(str(skip.get("requirement_id", "")), {})
        result.append(
            {
                "requirement": str(requirement.get("identity_key", "")),
                "reason": str(skip.get("reason", "")),
                "detail": str(skip.get("detail", "")),
                "target_hint": str(skip.get("target_hint", "")),
                "searched_paths": [
                    display_project_path(str(path), project_root)
                    for path in as_list(skip.get("searched_paths"))
                ],
                "candidates": [
                    display_project_path(str(path), project_root)
                    for path in as_list(skip.get("candidates"))
                ],
            }
        )
    return result


def _reusable_captures(previous, model, refresh_paths, project_root, unchanged_layout=None):
    """明确刷新部分 UI 时，沿用其他已核对且未变化的采集结果。"""
    requested = {path_key(canonical_path(path, project_root)) for path in
                 _text_list(refresh_paths, "需要更新截图的 Prefab", allow_empty=True)}
    current = {path_key(p["asset_path"]): p for p in model["prefabs"]}
    if requested - set(current):
        raise DloopUiError("DLOOP_UI_INPUT_INVALID", "需要更新截图的 Prefab 必须属于本次需求匹配。")
    old = {path_key(p["asset_path"]): p for p in previous["prefabs"]}
    evidence = collection_map(previous, "evidence")
    reviewed = {}
    if unchanged_layout is not None:
        if not isinstance(unchanged_layout, list):
            raise DloopUiError("DLOOP_UI_INPUT_INVALID", "unchanged_layout 必须是定位未变的核对说明列表。")
        for raw in unchanged_layout:
            item = _require_fields(raw, {"prefab", "reason"}, "历史截图复用核对")
            key = path_key(canonical_path(_required_text(item["prefab"], "复用截图的 Prefab"), project_root))
            if key not in current or key in requested or key in reviewed:
                raise DloopUiError("DLOOP_UI_INPUT_INVALID", "定位核对只能引用未补拍的当前 UI，且不能重复。")
            reviewed[key] = _required_text(item["reason"], "节点、布局及依赖未影响定位的实际核对依据")
    reused = []
    for key, prefab in current.items():
        if key in requested:
            continue
        original = old.get(key, {})
        capture = original.get("capture") or {}
        shot = evidence.get(capture.get("screenshot_evidence_id"), {})
        path = Path(shot.get("path", ""))
        changed = original.get("asset_sha256") != prefab["asset_sha256"]
        if (not capture or (changed and key not in reviewed)
                or original.get("asset_guid") != prefab["asset_guid"]
                or not path.is_file() or sha256_file(path) != shot.get("sha256")):
            raise DloopUiError("DLOOP_UI_CAPTURE_REFRESH_REQUIRED", f"{prefab['asset_path']} 没有可复用的截图；定位受影响时加入 refresh_prefabs，仅文案或逻辑变化且定位未变时提交 unchanged_layout 核对依据。")
        reuse = capture.get("reuse")
        if key in reviewed:
            reuse = {"captured_asset_sha256": (reuse or {}).get("captured_asset_sha256", original["asset_sha256"]),
                     "reviewed_asset_sha256": prefab["asset_sha256"], "reason": reviewed[key]}
        reused.append({"prefab_id": prefab["id"], "asset_path": prefab["asset_path"],
                       "asset_guid": prefab["asset_guid"], "asset_sha256": prefab["asset_sha256"],
                       "screenshot_path": str(path), "width": capture["width"], "height": capture["height"],
                       "regions": capture["regions"], **({"reuse": reuse} if reuse else {})})
    return reused


def investigate(
    model: dict[str, Any],
    brief: dict[str, Any],
    feature_path_value: str | Path,
    project_root_value: str | Path,
    capture_adapter: CaptureAdapter | None,
) -> dict[str, Any]:
    """从业务简报收敛需求与真实 Prefab，并返回可直接评审的截图包。"""

    feature_path = Path(canonical_path(feature_path_value))
    project_root = Path(canonical_path(project_root_value))
    previous = json.loads(json.dumps(model)) if "refresh_prefabs" in brief else None
    problems = apply_brief_snapshot(model, brief, project_root)
    reused = _reusable_captures(previous, model, brief["refresh_prefabs"], project_root, brief.get("unchanged_layout")) if previous is not None else []
    reused_ids = {item["prefab_id"] for item in reused}
    capture_model = {**model, "prefabs": [p for p in model["prefabs"] if p["id"] not in reused_ids]} if reused else model
    requirement_report = validate_model(model, "requirements")
    if requirement_report["status"] != "PASS":
        raise DloopUiError(
            "DLOOP_UI_BRIEF_BLOCKED",
            "界面调研简报未通过需求检查："
            + "；".join(item["message"] for item in requirement_report["errors"]),
        )
    text_files: dict[str, str] = {}
    binary_files: dict[str, bytes] = {}
    evidence_sources: dict[str, bytes] = {}
    if as_list(model.get("prefabs")):
        if capture_adapter is None and capture_model["prefabs"]:
            # 只生成可重做的请求材料，不把尚未截图的模型写成正式调研结果。
            capture_output = (
                feature_path.parent.parent / "captures" / feature_path.name
            )
            request_path = capture_output / "capture-request.json"
            capture_problems = prepare_capture_request(
                capture_model, request_path, project_root, capture_output,
            )
            if _has_errors(capture_problems):
                raise DloopUiError(
                    "DLOOP_UI_CAPTURE_REQUEST_BLOCKED",
                    "无法形成当前编辑器截图请求："
                    + "；".join(item["message"] for item in capture_problems if item["severity"] == "error"),
                )
            return {
                "status": "awaiting_capture",
                "capture_request": str(request_path),
                "capture_manifest": str(capture_output / "capture-manifest.json"),
                "project_root": str(project_root),
                "issues": problems + capture_problems,
            }
        scratch_root = project_root / ".scratch"
        scratch_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".dloop-ui-capture-", dir=scratch_root) as temporary:
            staging_path = Path(temporary)
            request_path = staging_path / "capture-request.json"
            capture_output = staging_path / "output"
            capture_output.mkdir()
            capture_problems = prepare_capture_request(
                capture_model,
                request_path,
                project_root,
                capture_output,
            )
            problems.extend(capture_problems)
            if _has_errors(capture_problems):
                raise DloopUiError(
                    "DLOOP_UI_CAPTURE_REQUEST_BLOCKED",
                    "无法形成 Unity 截图请求："
                    + "；".join(item["message"] for item in capture_problems if item["severity"] == "error"),
                )
            if as_list(model.get("prefabs")):
                if capture_model["prefabs"]:
                    manifest_path = capture_adapter.capture(request_path, project_root, capture_output)
                else:
                    manifest_path = staging_path / "reused-capture-manifest.json"
                    write_json(manifest_path, {"schema_version": CAPTURE_SCHEMA_VERSION, "prefabs": [], "errors": []})
                captured_text, captured_binary, sources, capture_import = _normalized_capture_artifacts(
                    model,
                    manifest_path,
                    feature_path,
                    staging_path,
                    reused,
                )
                text_files.update(captured_text)
                binary_files.update(captured_binary)
                evidence_sources.update(sources)
                problems.extend(capture_import)
                if _has_errors(capture_import):
                    raise DloopUiError(
                        "DLOOP_UI_CAPTURE_PROTOCOL_INVALID",
                        "Unity 截图结果无法导入："
                        + "；".join(item["message"] for item in capture_import if item["severity"] == "error"),
                    )
                investigation_report = validate_model(
                    model,
                    "investigation",
                    evidence_sources=evidence_sources,
                )
                if investigation_report["status"] != "PASS":
                    raise DloopUiError(
                        "DLOOP_UI_INVESTIGATION_BLOCKED",
                        "真实截图未通过调查检查："
                        + "；".join(item["message"] for item in investigation_report["errors"]),
                    )
    token = investigation_token(model)
    evidence = collection_map(model, "evidence")
    captures: list[dict[str, Any]] = []
    for prefab in as_list(model.get("prefabs")):
        if not isinstance(prefab, dict) or not isinstance(prefab.get("capture"), dict) or not prefab["capture"]:
            continue
        capture = prefab["capture"]
        screenshot = evidence.get(str(capture.get("screenshot_evidence_id", "")), {})
        try:
            screenshot_path = Path(str(screenshot.get("path", ""))).relative_to(feature_path).as_posix()
        except ValueError:
            screenshot_path = str(screenshot.get("path", ""))
        captures.append(
            {
                "prefab": display_project_path(str(prefab.get("asset_path", "")), project_root),
                "screenshot": screenshot_path,
                "width": capture.get("width"),
                "height": capture.get("height"),
                "regions": [dict(item) for item in as_list(capture.get("regions")) if isinstance(item, dict)],
            }
        )
    return {
        "status": "ready_for_review" if captures else "ready_to_publish",
        "investigation_token": token,
        "captures": captures,
        "skips": _public_skips(model, project_root),
        "issues": problems,
        "text_files": text_files,
        "binary_files": binary_files,
        "evidence_sources": evidence_sources,
    }


def apply_review_snapshot(
    model: dict[str, Any],
    review: dict[str, Any],
    project_root_value: str | Path,
) -> None:
    """用当前截图对应的完整业务评审替换标注和目标不可见结论。"""

    fields = {"input_version", "investigation_token"} | {key for key in ("outcomes", "scenarios", "delivery_review") if key in review}
    _require_fields(review, fields, "界面标注评审")
    if review.get("input_version") != REVIEW_INPUT_VERSION:
        raise DloopUiError(
            "DLOOP_UI_INPUT_VERSION",
            f"界面标注评审 input_version 必须是 {REVIEW_INPUT_VERSION}。",
        )
    if review.get("investigation_token") != investigation_token(model):
        raise DloopUiError(
            "DLOOP_UI_INVESTIGATION_CHANGED",
            "界面调研结果已经变化，请根据最新截图重新形成标注评审。",
        )
    raw_outcomes = review.get("outcomes", [])
    if not isinstance(raw_outcomes, list):
        raise DloopUiError("DLOOP_UI_INPUT_INVALID", "界面标注评审 outcomes 必须是数组。")
    project_root = Path(canonical_path(project_root_value))
    requirements_by_key = {
        str(item.get("identity_key", "")): item
        for item in as_list(model.get("requirements"))
        if isinstance(item, dict)
    }
    prefabs_by_path = {
        path_key(str(item.get("asset_path", ""))): item
        for item in as_list(model.get("prefabs"))
        if isinstance(item, dict)
    }
    model["delivery_review"] = None
    model["acceptance_scenarios"] = review.get("scenarios", [])
    if "outcomes" in review:
        model["annotations"] = []
    retained_evidence = {
        evidence_id
        for annotation in model["annotations"]
        for evidence_id in (annotation.get("interaction") or {}).get("evidence_ids", [])
    }
    model["evidence"] = [e for e in model["evidence"]
                         if e.get("kind") != "delivery_source" or e["id"] in retained_evidence]
    model["skips"] = model["skips"] if "outcomes" not in review else [
        item
        for item in as_list(model.get("skips"))
        if isinstance(item, dict) and item.get("reason") != "target-not-visible"
    ]
    seen_identity_keys: set[str] = set()

    for index, raw_outcome in enumerate(raw_outcomes, start=1):
        if not isinstance(raw_outcome, dict):
            raise DloopUiError("DLOOP_UI_INPUT_INVALID", f"第 {index} 条标注评审必须是对象。")
        result = raw_outcome.get("result")
        common_fields = {"key", "requirements", "prefab", "result", "title"}
        if result == "change":
            fields = common_fields | {
                "target", "instruction", "expected", "inference_level", "confidence"
            }
        elif result == "reuse":
            fields = common_fields | {"target", "inference_level", "confidence"}
        elif result == "target-not-visible":
            fields = common_fields | {"detail"}
        else:
            raise DloopUiError(
                "DLOOP_UI_INPUT_INVALID",
                f"第 {index} 条标注评审的 result 必须是 change、reuse 或 target-not-visible。",
            )
        fields |= {name for name in ("interaction", "feature_point", "related_elements") if name in raw_outcome}
        outcome = _require_fields(raw_outcome, fields, f"第 {index} 条标注评审")
        identity_key = _required_text(outcome.get("key"), "标注 key")
        if not IDENTITY_PATTERN.fullmatch(identity_key) or identity_key in seen_identity_keys:
            raise DloopUiError(
                "DLOOP_UI_INPUT_INVALID",
                f"标注 key 无效或重复：{identity_key}",
            )
        seen_identity_keys.add(identity_key)
        requirement_keys = _text_list(
            outcome.get("requirements"),
            f"标注 {identity_key} 的需求",
            allow_empty=False,
        )
        unknown = [key for key in requirement_keys if key not in requirements_by_key]
        if unknown:
            raise DloopUiError(
                "DLOOP_UI_INPUT_INVALID",
                "标注引用了未知需求：" + "、".join(unknown),
            )
        prefab_path = _project_path(str(outcome.get("prefab", "")), project_root)
        prefab = prefabs_by_path.get(path_key(prefab_path))
        if outcome.get("prefab") == "" and all(any(
            skip.get("requirement_id") == requirements_by_key[key]["id"]
            for skip in model["skips"]
        ) for key in requirement_keys):
            prefab = {"id": "", "requirement_ids": [requirements_by_key[key]["id"] for key in requirement_keys]}

        if prefab is None:
            raise DloopUiError(
                "DLOOP_UI_INPUT_INVALID",
                f"标注引用了当前调研未匹配的 Prefab：{prefab_path}",
            )
        capture = prefab.get("capture") if isinstance(prefab.get("capture"), dict) else {}
        if not capture and not (isinstance(outcome.get("target"), dict) and outcome["target"].get("unavailable_reason")):
            raise DloopUiError("DLOOP_UI_INPUT_INVALID", "没有成功截图时必须说明定位缺口，保留交互文字。")
        requirement_ids = [str(requirements_by_key[key]["id"]) for key in requirement_keys]
        if any(item not in as_list(prefab.get("requirement_ids")) for item in requirement_ids):
            raise DloopUiError(
                "DLOOP_UI_INPUT_INVALID",
                f"标注需求不属于指定 Prefab：{prefab_path}",
            )
        if result == "target-not-visible":
            detail = _required_text(outcome.get("detail"), "目标不可见说明")
            for requirement_id in requirement_ids:
                _upsert_skip(
                    model,
                    requirement_id,
                    "target-not-visible",
                    detail,
                    _required_text(outcome.get("title"), "目标名称"),
                    [str(prefab.get("asset_path", ""))],
                    str(prefab.get("id", "")),
                )
            continue
        inference_level = outcome.get("inference_level")
        confidence = outcome.get("confidence")
        if inference_level not in INFERENCE_LEVELS or confidence not in CONFIDENCE_LEVELS:
            raise DloopUiError(
                "DLOOP_UI_INPUT_INVALID",
                f"标注 {identity_key} 的判断依据或置信度无效。",
            )
        target_input = outcome.get("target")
        if not isinstance(target_input, dict) or set(target_input) not in (
            {"region", "label"},
            {"rect", "label"},
            {"unavailable_reason", "label"},
        ):
            raise DloopUiError(
                "DLOOP_UI_INPUT_INVALID",
                f"标注 {identity_key} 的 target 必须引用截图区域或提供截图内矩形。",
            )
        label = _required_text(target_input.get("label"), "标注目标名称")
        if "unavailable_reason" in target_input:
            target = {"label": label, "unavailable_reason": _required_text(target_input["unavailable_reason"], "定位缺口")}
        elif "region" in target_input:
            region_id = _required_text(target_input.get("region"), "截图区域身份")
            region = next(
                (
                    item
                    for item in as_list(capture.get("regions"))
                    if isinstance(item, dict) and item.get("object_id") == region_id
                ),
                None,
            )
            if region is None:
                raise DloopUiError(
                    "DLOOP_UI_INPUT_INVALID",
                    f"标注 {identity_key} 引用了未知截图区域。",
                )
            target = {
                "label": label,
                "object_id": region_id,
                "hierarchy_path": str(region.get("hierarchy_path", "")),
                "rect": dict(region.get("rect", {})),
            }
        else:
            target = {"label": label, "rect": _capture_rect(target_input.get("rect"))}
        if "unavailable_reason" not in target and not _valid_rect(target.get("rect"), int(capture.get("width", 0)), int(capture.get("height", 0))):
            raise DloopUiError(
                "DLOOP_UI_INPUT_INVALID",
                f"标注 {identity_key} 的矩形超出真实截图范围。",
            )
        if result == "change":
            instruction = _required_text(outcome.get("instruction"), "修改说明")
            expected = _required_text(outcome.get("expected"), "可观察结果")
        else:
            instruction = "现有界面已经满足关联需求，无需产品修改。"
            expected = "；".join(
                str(requirements_by_key[key].get("statement", "")) for key in requirement_keys
            )
        feature_point = None
        if outcome.get("feature_point") is not None:
            value = _require_fields(outcome["feature_point"], {"key", "title"}, "功能点")
            feature_point = {name: _required_text(value[name], "功能点 " + name) for name in ("key", "title")}
        related_elements = []
        if not isinstance(outcome.get("related_elements", []), list):
            raise DloopUiError("DLOOP_UI_INPUT_INVALID", "关联元素必须是列表。")
        for raw_element in outcome.get("related_elements", []):
            element = _require_fields(raw_element, {"region", "role"}, "关联元素")
            region = next((r for r in capture.get("regions", []) if r["object_id"] == element["region"]), None)
            if region is None:
                raise DloopUiError("DLOOP_UI_INPUT_INVALID", "关联元素必须引用当前采集中的真实节点。")
            related_elements.append({"name": region["node_name"], "path": region["hierarchy_path"],
                                     "role": _required_text(element["role"], "元素用途")})
        screenshot_id = str(capture.get("screenshot_evidence_id", ""))
        model["annotations"].append(
            {
                "identity_key": identity_key,
                "id": "",
                "prefab_id": str(prefab.get("id", "")),
                "requirement_ids": requirement_ids,
                "disposition": result,
                "title": _required_text(outcome.get("title"), "标注标题"),
                "target": target,
                "instruction": instruction,
                "expected": expected,
                "inference_level": inference_level,
                "confidence": confidence,
                "evidence_ids": [screenshot_id] if screenshot_id else [],
                "interaction": _interaction_input(model, outcome.get("interaction"), project_root),
                "feature_point": feature_point,
                "related_elements": related_elements,
            }
        )
    if review.get("delivery_review") is not None:
        checked = _require_fields(review["delivery_review"], {"requirements_check", "implementation_check", "evidence"}, "交付双向核对")
        model["delivery_review"] = {
            "requirements_check": _required_text(checked["requirements_check"], "需求到实现核对"),
            "implementation_check": _required_text(checked["implementation_check"], "实现到交互核对"),
            "evidence_ids": _delivery_evidence(model, checked["evidence"], project_root),
        }
    sync_ids(model)


def display_project_path(value: str, project_root: Path | None = None) -> str:
    path = Path(value)
    if project_root is not None:
        try:
            return path.relative_to(project_root).as_posix()
        except ValueError:
            pass
    parts = list(path.parts)
    for index, part in enumerate(parts):
        if part.casefold() == "assets":
            return Path(*parts[index:]).as_posix()
    return path.name or value


def publication_status(model: dict[str, Any], report: Mapping[str, Any]) -> str:
    if report.get("status") != "PASS":
        return "blocked"
    counts = compute_counts(model)
    if counts["matched_prefabs"] == 0 and counts["skipped_requirements"]:
        return "no_applicable_prefab"
    if counts["skipped_requirements"]:
        return "completed_with_skips"
    return "complete"


def _valid_rect(
    value: object,
    width: int,
    height: int,
) -> bool:
    rect = _capture_rect(value)
    return bool(
        rect
        and rect["x"] >= 0
        and rect["y"] >= 0
        and rect["width"] > 0
        and rect["height"] > 0
        and rect["x"] + rect["width"] <= width + 0.5
        and rect["y"] + rect["height"] <= height + 0.5
    )


def validate_model(
    model: dict[str, Any],
    stage: str = "plan",
    *,
    evidence_sources: Mapping[str, str | Path | bytes] | None = None,
) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    if stage not in STAGES:
        errors.append(issue("STAGE_INVALID", f"未知检查阶段：{stage}"))
    if model.get("schema_version") != SCHEMA_VERSION:
        errors.append(
            issue("SCHEMA_VERSION", f"schema_version 必须是 {SCHEMA_VERSION}")
        )
    if set(model) != TOP_LEVEL_FIELDS:
        errors.append(
            issue(
                "MODEL_FIELDS_UNSUPPORTED",
                "ui-model.json 顶层字段与当前轻量合同不一致",
            )
        )
    feature = model.get("feature")
    if (
        not isinstance(feature, dict)
        or not str(feature.get("id", "")).strip()
        or not str(feature.get("title", "")).strip()
    ):
        errors.append(issue("FEATURE_IDENTITY_MISSING", "功能标识和标题必须非空"))
    elif set(feature) != {"id", "title"}:
        errors.append(
            issue("FEATURE_FIELDS_UNSUPPORTED", "功能身份包含当前合同不支持的字段")
        )
    expected_scope = {
        "mode": "existing-prefab-annotation",
        "existing_prefab_behavior": "preserve-unless-explicitly-changed",
        "missing_prefab": "skip",
    }
    if model.get("planning_scope") != expected_scope:
        errors.append(
            issue(
                "PLANNING_SCOPE_INVALID",
                "规划范围必须固定为复用已有 Prefab、保留既有行为、缺失直接跳过",
            )
        )

    ensure_collections(model)
    maps: dict[str, dict[str, dict[str, Any]]] = {}
    all_ids: dict[str, str] = {}
    for collection, (prefix, parent_field) in ENTITY_SPECS.items():
        records = model.get(collection)
        if not isinstance(records, list):
            errors.append(issue("MODEL_COLLECTION_TYPE", f"{collection} 必须是数组"))
            maps[collection] = {}
            continue
        maps[collection] = collection_map(model, collection)
        identities: set[tuple[str, str]] = set()
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                errors.append(
                    issue("MODEL_RECORD_TYPE", f"{collection}[{index}] 必须是对象")
                )
                continue
            record_id = str(record.get("id", ""))
            if not set(record).issubset(ENTITY_FIELDS[collection]):
                errors.append(
                    issue(
                        "ENTITY_FIELDS_UNSUPPORTED",
                        f"{collection}[{index}] 包含当前轻量合同不支持的字段",
                        record_id,
                    )
                )
            identity_key = str(record.get("identity_key", "")).strip()
            parent = str(record.get(parent_field, "")).strip() if parent_field else ""
            if not identity_key or not IDENTITY_PATTERN.fullmatch(identity_key):
                errors.append(
                    issue(
                        "IDENTITY_KEY_INVALID",
                        f"{collection}[{index}] 缺少稳定身份或包含无效字符",
                        record_id,
                    )
                )
            elif (parent, identity_key) in identities:
                errors.append(
                    issue(
                        "IDENTITY_KEY_DUPLICATE",
                        f"{collection} 内身份键重复：{identity_key}",
                        record_id,
                    )
                )
            identities.add((parent, identity_key))
            if not ID_PATTERN.fullmatch(record_id) or not record_id.startswith(prefix + "-"):
                errors.append(
                    issue(
                        "STABLE_ID_INVALID",
                        f"{collection} 的稳定 ID 无效",
                        record_id,
                    )
                )
            elif record_id in all_ids:
                errors.append(
                    issue(
                        "STABLE_ID_DUPLICATE",
                        f"稳定 ID 同时出现在 {all_ids[record_id]} 和 {collection}",
                        record_id,
                    )
                )
            else:
                all_ids[record_id] = collection

    expected_counts = compute_counts(model)
    if model.get("computed_counts") != expected_counts:
        errors.append(
            issue("COUNT_CACHE_MISMATCH", "计算汇总与当前模型不一致")
        )

    requirements = maps.get("requirements", {})
    prefabs = maps.get("prefabs", {})
    annotations = maps.get("annotations", {})
    skips = maps.get("skips", {})
    evidence = maps.get("evidence", {})
    if not requirements:
        errors.append(
            issue("REQUIREMENTS_MISSING", "尚未从需求材料提取可观察的 UI 需求")
        )

    for evidence_id, item in evidence.items():
        kind = item.get("kind")
        status = item.get("status")
        path_text = str(item.get("path", "")).strip()
        if status not in {"verified", "missing"}:
            errors.append(
                issue("EVIDENCE_STATUS_INVALID", "证据状态无效", evidence_id)
            )
            continue
        if status == "missing":
            errors.append(
                issue("EVIDENCE_NOT_VERIFIED", "证据文件不存在", evidence_id)
            )
            continue
        if kind in FILE_EVIDENCE_KINDS:
            if stage == "requirements" and kind != "source_document":
                continue
            evidence_source = (evidence_sources or {}).get(path_text, path_text)
            if isinstance(evidence_source, bytes):
                actual_sha256 = "sha256:" + hashlib.sha256(evidence_source).hexdigest()
                source_exists = True
            else:
                path = Path(canonical_path(evidence_source))
                source_exists = path.is_file()
                actual_sha256 = sha256_file(path) if source_exists else ""
            if not source_exists:
                errors.append(
                    issue("EVIDENCE_FILE_MISSING", f"证据文件不存在：{path_text}", evidence_id)
                )
            elif item.get("sha256") != actual_sha256:
                errors.append(
                    issue("EVIDENCE_STALE", f"证据文件内容已变化：{path_text}", evidence_id)
                )
        elif kind == "prefab_asset":
            if not path_text or not str(item.get("sha256", "")).startswith("sha256:"):
                errors.append(
                    issue(
                        "PREFAB_EVIDENCE_INVALID",
                        "Prefab 历史证据缺少路径或摘要",
                        evidence_id,
                    )
                )
        else:
            errors.append(
                issue("EVIDENCE_KIND_INVALID", f"未知证据类型：{kind}", evidence_id)
            )

    for requirement_id, requirement in requirements.items():
        if not str(requirement.get("name_zh", "")).strip():
            errors.append(
                issue("REQUIREMENT_NAME_MISSING", "UI 需求缺少中文名称", requirement_id)
            )
        if not str(requirement.get("statement", "")).strip():
            errors.append(
                issue("REQUIREMENT_STATEMENT_MISSING", "UI 需求缺少可观察描述", requirement_id)
            )
        if not str(requirement.get("source_locator", "")).strip():
            errors.append(
                issue(
                    "REQUIREMENT_SOURCE_LOCATOR_MISSING",
                    "UI 需求缺少来源定位",
                    requirement_id,
                )
            )
        if requirement.get("inference_level") not in INFERENCE_LEVELS:
            errors.append(
                issue(
                    "REQUIREMENT_INFERENCE_INVALID",
                    "UI 需求必须区分明确需求或合理推断",
                    requirement_id,
                )
            )
        if requirement.get("confidence") not in CONFIDENCE_LEVELS:
            errors.append(
                issue(
                    "REQUIREMENT_CONFIDENCE_INVALID",
                    "UI 需求置信度必须是 high 或 medium",
                    requirement_id,
                )
            )
        target_clues = requirement.get("target_clues")
        if not isinstance(target_clues, list) or any(
            not isinstance(item, str) or not item.strip() for item in target_clues
        ):
            errors.append(
                issue(
                    "REQUIREMENT_TARGET_CLUES_INVALID",
                    "UI 需求定位线索必须是文本数组",
                    requirement_id,
                )
            )
        evidence_ids = as_list(requirement.get("evidence_ids"))
        if not evidence_ids or not any(
            evidence.get(item, {}).get("kind") == "source_document"
            for item in evidence_ids
        ):
            errors.append(
                issue(
                    "REQUIREMENT_SOURCE_MISSING",
                    "UI 需求必须引用来源材料",
                    requirement_id,
                )
            )
        if requirement.get("inference_level") == "inferred":
            warnings.append(
                issue(
                    "REQUIREMENT_INFERRED",
                    "该 UI 需求是依据材料形成的合理推断",
                    requirement_id,
                    severity="warning",
                )
            )

    capture_required = stage in {"investigation", "plan"}
    for prefab_id, prefab in prefabs.items():
        if stage == "requirements":
            continue
        asset_path = str(prefab.get("asset_path", "")).strip()
        if not asset_path.casefold().endswith(".prefab"):
            errors.append(
                issue("PREFAB_PATH_INVALID", "已匹配资产必须是 .prefab", prefab_id)
            )
        if not str(prefab.get("name_zh", "")).strip():
            errors.append(
                issue("PREFAB_NAME_MISSING", "已匹配 Prefab 缺少中文名称", prefab_id)
            )
        if not str(prefab.get("match_reason", "")).strip():
            errors.append(
                issue("PREFAB_MATCH_REASON_MISSING", "Prefab 缺少需求匹配依据", prefab_id)
            )
        requirement_ids = as_list(prefab.get("requirement_ids"))
        if not requirement_ids:
            errors.append(
                issue("PREFAB_REQUIREMENTS_MISSING", "Prefab 没有关联需求", prefab_id)
            )
        for requirement_id in requirement_ids:
            if requirement_id not in requirements:
                errors.append(
                    issue(
                        "PREFAB_REQUIREMENT_ORPHAN",
                        f"Prefab 引用不存在的需求：{requirement_id}",
                        prefab_id,
                    )
                )
        prefab_evidence = [
            evidence.get(item, {})
            for item in as_list(prefab.get("evidence_ids"))
        ]
        if not any(item.get("kind") == "prefab_asset" for item in prefab_evidence):
            errors.append(
                issue("PREFAB_EVIDENCE_MISSING", "Prefab 缺少历史资产证据", prefab_id)
            )
        capture = prefab.get("capture")
        if capture_required and not (
            isinstance(capture, dict) and capture
        ):
            if str(prefab.get("capture_error", "")).strip():
                warnings.append(
                    issue(
                        "PREFAB_CAPTURE_SKIPPED",
                        "该 Prefab 截图失败，关联需求已进入跳过清单。",
                        prefab_id,
                        severity="warning",
                    )
                )
            else:
                errors.append(
                    issue(
                        "PREFAB_CAPTURE_REQUIRED",
                        "已匹配 Prefab 尚未完成截图",
                        prefab_id,
                    )
                )
            continue
        if capture_required and isinstance(capture, dict):
            if set(capture) != {
                "manifest_evidence_id",
                "screenshot_evidence_id",
                "width",
                "height",
                "regions",
            } | ({"reuse"} if "reuse" in capture else set()):
                errors.append(
                    issue(
                        "PREFAB_CAPTURE_FIELDS_UNSUPPORTED",
                        "Prefab 截图包含当前轻量合同不支持的字段",
                        prefab_id,
                    )
                )
            reuse = capture.get("reuse")
            if reuse is not None and (not isinstance(reuse, dict)
                    or set(reuse) != {"captured_asset_sha256", "reviewed_asset_sha256", "reason"}
                    or reuse.get("reviewed_asset_sha256") != prefab.get("asset_sha256")
                    or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(reuse.get("captured_asset_sha256", "")))
                    or not isinstance(reuse.get("reason"), str) or not reuse["reason"].strip()):
                errors.append(issue("PREFAB_CAPTURE_REUSE_INVALID", "历史截图缺少当前定位核对或原始资产摘要", prefab_id))
            width = capture.get("width")
            height = capture.get("height")
            if (
                isinstance(width, bool)
                or not isinstance(width, int)
                or width <= 0
                or isinstance(height, bool)
                or not isinstance(height, int)
                or height <= 0
            ):
                errors.append(
                    issue("PREFAB_CAPTURE_SIZE_INVALID", "Prefab 截图宽高无效", prefab_id)
                )
                width = 0
                height = 0
            if evidence.get(capture.get("manifest_evidence_id"), {}).get("kind") != "capture_manifest":
                errors.append(
                    issue("PREFAB_CAPTURE_MANIFEST_INVALID", "Prefab 缺少截图清单证据", prefab_id)
                )
            if evidence.get(capture.get("screenshot_evidence_id"), {}).get("kind") != "prefab_screenshot":
                errors.append(
                    issue("PREFAB_SCREENSHOT_INVALID", "Prefab 缺少原始截图证据", prefab_id)
                )
            regions = capture.get("regions")
            if not isinstance(regions, list):
                errors.append(
                    issue("PREFAB_REGIONS_INVALID", "Prefab 可见区域清单无效", prefab_id)
                )
            else:
                seen_objects: set[str] = set()
                for region in regions:
                    if not isinstance(region, dict):
                        errors.append(
                            issue("PREFAB_REGION_INVALID", "Prefab 可见区域不是对象", prefab_id)
                        )
                        continue
                    if set(region) != {
                        "object_id", "node_name", "hierarchy_path", "rect"
                    }:
                        errors.append(
                            issue(
                                "PREFAB_REGION_FIELDS_UNSUPPORTED",
                                "Prefab 可见区域包含行为或其他非定位字段",
                                prefab_id,
                            )
                        )
                    object_id = str(region.get("object_id", "")).strip()
                    if (
                        not object_id
                        or object_id in seen_objects
                        or not str(region.get("node_name", "")).strip()
                        or not str(region.get("hierarchy_path", "")).strip()
                        or not _valid_rect(region.get("rect"), int(width or 0), int(height or 0))
                    ):
                        errors.append(
                            issue("PREFAB_REGION_INVALID", "Prefab 可见区域事实无效", prefab_id)
                        )
                    seen_objects.add(object_id)

    requirement_annotations: dict[str, list[str]] = defaultdict(list)
    annotated_scopes: set[tuple[str, str]] = set()
    for annotation_id, annotation in annotations.items():
        if stage != "plan":
            continue
        prefab_id = str(annotation.get("prefab_id", ""))
        prefab = prefabs.get(prefab_id)
        if not prefab_id and annotation.get("target", {}).get("unavailable_reason"):
            prefab = {"requirement_ids": annotation.get("requirement_ids", [])}
        if prefab is None:
            errors.append(
                issue("ANNOTATION_PREFAB_ORPHAN", "标注引用不存在的 Prefab", annotation_id)
            )
            continue
        requirement_ids = as_list(annotation.get("requirement_ids"))
        if not requirement_ids:
            errors.append(
                issue("ANNOTATION_REQUIREMENTS_MISSING", "标注没有关联需求", annotation_id)
            )
        for requirement_id in requirement_ids:
            if requirement_id not in requirements:
                errors.append(
                    issue(
                        "ANNOTATION_REQUIREMENT_ORPHAN",
                        f"标注引用不存在的需求：{requirement_id}",
                        annotation_id,
                    )
                )
            else:
                requirement_annotations[str(requirement_id)].append(annotation_id)
                annotated_scopes.add((str(requirement_id), prefab_id))
            if requirement_id not in as_list(prefab.get("requirement_ids")):
                errors.append(
                    issue(
                        "ANNOTATION_REQUIREMENT_PREFAB_MISMATCH",
                        "标注需求不在 Prefab 的匹配范围内",
                        annotation_id,
                    )
                )
        if annotation.get("disposition") not in ANNOTATION_DISPOSITIONS:
            errors.append(
                issue(
                    "ANNOTATION_DISPOSITION_INVALID",
                    "标注结果必须是 change 或 reuse",
                    annotation_id,
                )
            )
        if annotation.get("inference_level") not in INFERENCE_LEVELS:
            errors.append(
                issue(
                    "ANNOTATION_INFERENCE_INVALID",
                    "标注必须区分明确对应或推断对应",
                    annotation_id,
                )
            )
        if annotation.get("confidence") not in CONFIDENCE_LEVELS:
            errors.append(
                issue(
                    "ANNOTATION_CONFIDENCE_INVALID",
                    "标注置信度必须是 high 或 medium",
                    annotation_id,
                )
            )
        for field, message in (
            ("title", "标注缺少标题"),
            ("expected", "标注缺少可观察结果"),
        ):
            if not str(annotation.get(field, "")).strip():
                errors.append(issue("ANNOTATION_TEXT_MISSING", message, annotation_id))
        instruction = str(annotation.get("instruction", "")).strip()
        if annotation.get("disposition") == "change" and not instruction:
            errors.append(
                issue(
                    "ANNOTATION_CHANGE_INSTRUCTION_MISSING",
                    "修改标注必须说明本次工作",
                    annotation_id,
                )
            )
        elif annotation.get("disposition") == "reuse" and instruction != "现有界面已经满足关联需求，无需产品修改。":
            errors.append(
                issue(
                    "ANNOTATION_REUSE_CONTAINS_WORK",
                    "复用标注不能隐藏实施动作",
                    annotation_id,
                )
            )
        target = annotation.get("target")
        capture = prefab.get("capture") if isinstance(prefab.get("capture"), dict) else {}
        width = int(capture.get("width", 0) or 0)
        height = int(capture.get("height", 0) or 0)
        if (
            not isinstance(target, dict)
            or not set(target).issubset(
                {"label", "object_id", "hierarchy_path", "rect", "unavailable_reason"}
            )
            or not str(target.get("label", "")).strip()
            or (not target.get("unavailable_reason") and not _valid_rect(target.get("rect"), width, height))
        ):
            errors.append(
                issue(
                    "ANNOTATION_TARGET_INVALID",
                    "标注必须包含截图内有效区域和可读名称",
                    annotation_id,
                )
            )
        elif str(target.get("object_id", "")).strip():
            region_ids = {
                str(item.get("object_id", ""))
                for item in as_list(capture.get("regions"))
                if isinstance(item, dict)
            }
            if str(target.get("object_id")) not in region_ids:
                errors.append(
                    issue(
                        "ANNOTATION_TARGET_REGION_ORPHAN",
                        "标注引用的可见区域不在截图清单中",
                        annotation_id,
                    )
                )
        screenshot_id = capture.get("screenshot_evidence_id")
        if not target.get("unavailable_reason") and screenshot_id not in as_list(annotation.get("evidence_ids")):
            errors.append(
                issue(
                    "ANNOTATION_SCREENSHOT_EVIDENCE_MISSING",
                    "标注必须引用所属 Prefab 的原始截图",
                    annotation_id,
                )
            )
        if annotation.get("inference_level") == "inferred" or annotation.get("confidence") == "medium":
            warnings.append(
                issue(
                    "ANNOTATION_INFERRED",
                    "该标注包含合理推断，请在阅读标注计划时核对。",
                    annotation_id,
                    severity="warning",
                )
            )

    skipped_requirements: dict[str, list[str]] = defaultdict(list)
    skipped_scopes: set[tuple[str, str]] = set()
    for skip_id, skip in skips.items():
        if stage == "requirements":
            continue
        requirement_id = str(skip.get("requirement_id", ""))
        if requirement_id not in requirements:
            errors.append(
                issue("SKIP_REQUIREMENT_ORPHAN", "跳过项引用不存在的需求", skip_id)
            )
        else:
            skipped_requirements[requirement_id].append(skip_id)
        if skip.get("reason") not in SKIP_REASONS:
            errors.append(
                issue("SKIP_REASON_INVALID", "跳过原因无效", skip_id)
            )
        if not str(skip.get("detail", "")).strip():
            errors.append(
                issue("SKIP_DETAIL_MISSING", "跳过项必须说明原因", skip_id)
            )
        searched_paths = skip.get("searched_paths")
        if not isinstance(searched_paths, list) or any(
            not isinstance(item, str) or not item.strip() for item in searched_paths
        ):
            errors.append(
                issue("SKIP_SEARCH_PATHS_INVALID", "跳过项搜索范围必须是文本数组", skip_id)
            )
        candidates = skip.get("candidates")
        if not isinstance(candidates, list) or any(
            not isinstance(item, str) or not item.strip() for item in candidates
        ):
            errors.append(
                issue("SKIP_CANDIDATES_INVALID", "跳过项候选必须是文本数组", skip_id)
            )
            candidates = []
        reason = skip.get("reason")
        prefab_id = str(skip.get("prefab_id", ""))
        skipped_scopes.add((requirement_id, prefab_id))
        if reason == "prefab-not-found" and not searched_paths:
            errors.append(
                issue("SKIP_NOT_FOUND_EVIDENCE_MISSING", "未找到 Prefab 必须记录搜索位置", skip_id)
            )
        if reason == "ambiguous-match" and len(candidates) < 2:
            errors.append(
                issue("SKIP_AMBIGUOUS_CANDIDATES_MISSING", "歧义匹配至少需要两个候选 Prefab", skip_id)
            )
        if reason == "capture-failed":
            prefab = prefabs.get(prefab_id)
            if prefab is None or not str(prefab.get("capture_error", "")).strip():
                errors.append(
                    issue("SKIP_CAPTURE_FAILURE_UNPROVEN", "截图失败必须对应 Unity 返回的单项错误", skip_id)
                )
        if reason == "target-not-visible":
            prefab = prefabs.get(prefab_id)
            capture = prefab.get("capture") if isinstance(prefab, dict) else None
            if not isinstance(capture, dict) or not capture:
                errors.append(
                    issue("SKIP_TARGET_VISIBILITY_UNPROVEN", "目标不可见必须基于成功的真实截图", skip_id)
                )
        warnings.append(
            issue(
                "REQUIREMENT_SKIPPED",
                str(skip.get("detail", "")).strip(),
                requirement_id,
                severity="warning",
            )
        )

    if stage == "plan":
        successful_scopes = {
            (str(requirement_id), prefab_id)
            for prefab_id, prefab in prefabs.items()
            if isinstance(prefab.get("capture"), dict) and prefab["capture"]
            for requirement_id in as_list(prefab.get("requirement_ids"))
            if str(requirement_id) in requirements
        }
        for requirement_id, prefab_id in sorted(successful_scopes):
            if (
                (requirement_id, prefab_id) not in annotated_scopes
                and (requirement_id, prefab_id) not in skipped_scopes
            ):
                prefab_name = str(prefabs[prefab_id].get("name_zh", "")).strip() or prefab_id
                errors.append(
                    issue(
                        "REQUIREMENT_OUTCOME_MISSING",
                        f"UI 需求尚未对 Prefab“{prefab_name}”形成修改、复用或跳过结果",
                        requirement_id,
                    )
                )
        scoped_requirements = {requirement_id for requirement_id, _ in successful_scopes}
        for requirement_id in requirements:
            if (
                requirement_id not in scoped_requirements
                and not requirement_annotations.get(requirement_id)
                and not skipped_requirements.get(requirement_id)
            ):
                errors.append(
                    issue(
                        "REQUIREMENT_OUTCOME_MISSING",
                        "UI 需求尚未形成修改、复用或跳过结果",
                        requirement_id,
                    )
                )

    return {
        "status": "BLOCKED" if errors else "PASS",
        "stage": stage,
        "errors": errors,
        "warnings": warnings,
        "computed_counts": expected_counts,
    }


def md(value: Any) -> str:
    if isinstance(value, list):
        text = "；".join(str(item) for item in value)
    else:
        text = str(value or "")
    return text.replace("|", "\\|").replace("\n", "<br>")


def _feature_point_key(annotation: dict[str, Any]) -> str:
    point = annotation.get("feature_point")
    if point:
        return point["key"]
    target = annotation.get("target", {})
    return str(target.get("object_id") or target.get("hierarchy_path") or
               (json.dumps(target["rect"], sort_keys=True) if target.get("rect") else annotation["identity_key"]))


def _annotation_numbers(model: dict[str, Any]) -> dict[str, int]:
    groups: dict[str, dict[str, int]] = defaultdict(dict)
    result = {}
    for annotation in sorted(model["annotations"], key=lambda item: item["identity_key"]):
        numbers = groups[annotation["prefab_id"]]
        key = _feature_point_key(annotation)
        if key not in numbers:
            numbers[key] = len(numbers) + 1
        result[annotation["id"]] = numbers[key]
    return result


def annotation_media_relative_path(prefab_id: str) -> Path:
    return Path("04-plan") / "ui-annotations" / f"{prefab_id}-annotated.svg"


def render_views(model: dict[str, Any], report: dict[str, Any], feature_path: Path | None = None) -> dict[str, str]:
    pages = _presentation_pages(model, feature_path or Path.cwd())
    lines = ["# UI 交互说明", "", "[打开统一交互查看页](../06-validation/ui-delivery.html)", "",
             "交付核对材料" if model.get("delivery_review") else "内部草稿：交互随实施补全，不要求前置人工批准。",
             "", "截图仅作为采集时的位置参考，不证明运行效果。机器检查只核对数据与证据合同。", ""]
    evidence = collection_map(model, "evidence")
    lines += ["## 需求依据", ""]
    for requirement in model["requirements"]:
        source = next((evidence[e] for e in requirement["evidence_ids"] if evidence[e]["kind"] == "source_document"), {})
        lines.append(f"- {md(requirement['name_zh'])}：{md(requirement['statement'])}；来源：{md(Path(source.get('path', '')).name)} / {md(requirement['source_locator'])}")
    lines.append("")
    for page in pages:
        lines += ["## " + md(page["name"]), ""]
        if page.get("asset"):
            lines += ["Prefab：" + md(page["asset"]), ""]
        if page["image"]:
            lines += [f"![{md(page['name'])}编号截图](ui-annotations/{page['id']}-annotated.svg)", ""]
        else:
            lines += ["没有可用截图，以下交互仍保留完整说明。", ""]
        if page["capture_note"]:
            lines += [md(page["capture_note"]), ""]
        lines += ["| 编号 | 交互 | 条件 | 操作 | 即时反馈 | 可观察结果 | 实现与验证 | 定位说明 |",
                  "|---:|---|---|---|---|---|---|---|"]
        for item in page["items"]:
            values = [item["number"], item["title"], item["conditions"], item["action"], item["feedback"],
                      item["expected"], "\n".join(op["title"] + "：" + op["status"] + "；" + op["detail"] for op in item["operations"]), item["location_note"]]
            lines.append("| " + " | ".join(md(v) for v in values) + " |")
        lines += ["", "### 依据与本次工作", ""]
        for item in page["items"]:
            lines += [f"- {item['number']}：{md(item['instruction'])}；依据：{md(item['sources'])}"]
        lines.append("")
    lines += ["## 采集与定位缺口", ""]
    for skip in model["skips"]:
        lines.append(f"- {md(skip['target_hint'])}：{md(skip['detail'])}")
    if model.get("delivery_review"):
        lines += ["", "## 双向核对", "", md(model["delivery_review"]["requirements_check"]), "",
                  md(model["delivery_review"]["implementation_check"])]
    lines += ["", f"机器检查：{report['status']}（不代表运行交互已经验证）", ""]
    for error in report["errors"]:
        lines.append(f"- {md(error['message'])}")
    return {"ui-annotation-plan.md": "\n".join(lines) + "\n"}


def validate_annotation_media(
    model: dict[str, Any],
    feature_path: Path,
) -> list[dict[str, str]]:
    maps = {
        collection: collection_map(model, collection)
        for collection in ENTITY_SPECS
    }
    annotated_prefab_ids = {
        str(item.get("prefab_id", ""))
        for item in maps["annotations"].values()
        if not item.get("target", {}).get("unavailable_reason")
    }
    feature_root = feature_path.resolve()
    problems: list[dict[str, str]] = []
    for prefab_id in annotated_prefab_ids:
        prefab = maps["prefabs"].get(prefab_id, {})
        capture = prefab.get("capture") if isinstance(prefab.get("capture"), dict) else {}
        screenshot = maps["evidence"].get(
            str(capture.get("screenshot_evidence_id", "")),
            {},
        )
        screenshot_path = Path(str(screenshot.get("path", ""))).resolve(strict=False)
        try:
            screenshot_path.relative_to(feature_root)
        except ValueError:
            problems.append(
                issue(
                    "PREFAB_SCREENSHOT_OUTSIDE_ARCHIVE",
                    "用于标注的原始截图必须保存在当前交付档案内",
                    prefab_id,
                )
            )

    return problems


def render_annotation_media(model: dict[str, Any], feature_path: Path) -> dict[str, str]:
    """截图只绘制编号与定位，不再拼接文字说明。"""
    result = {}
    for page in _presentation_pages(model, feature_path):
        if not page["image"]:
            continue
        width, height = page["width"], page["height"]
        canvas_height = max([height] + [item["marker"]["y"] + 20 for item in page["items"] if item["marker"]])
        lines = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width + 52}" height="{canvas_height}" viewBox="0 0 {width + 52} {canvas_height}">',
                 f'<image href="{page["image"]}" width="{width}" height="{height}"/>']
        for item in page["items"]:
            marker = item["marker"]
            if marker is None:
                continue
            x, y = marker["x"], marker["y"]
            lines += [f'<line x1="{x}" y1="{y}" x2="{marker["target_x"]}" y2="{marker["target_y"]}" stroke="#ffb53c" stroke-width="2"/>',
                      f'<circle cx="{x}" cy="{y}" r="16" fill="#244aba" stroke="white" stroke-width="2"/>',
                      f'<text x="{x}" y="{y + 5}" text-anchor="middle" font-size="15" fill="white">{item["number"]}</text>']
        lines.append('</svg>')
        result[annotation_media_relative_path(page["id"]).as_posix()] = "\n".join(lines) + "\n"
    return result


def _delivery_evidence(model: dict[str, Any], values: object, project_root: Path) -> list[str]:
    if not isinstance(values, list) or not values:
        raise DloopUiError("DLOOP_UI_EVIDENCE_REQUIRED", "交互核对必须提供实际来源、实现或验证文件与定位。")
    ids = []
    for value in values:
        item = _require_fields(value, {"path", "locator"}, "交付证据")
        path = _project_path(_required_text(item["path"], "证据文件"), project_root)
        locator = _required_text(item["locator"], "证据定位")
        if not path.is_file():
            raise DloopUiError("DLOOP_UI_EVIDENCE_REQUIRED", f"证据文件不存在：{path}")
        evidence = upsert_evidence(model, build_file_evidence("delivery_source", path, locator=locator, summary=locator))
        ids.append(evidence["id"])
    return ids


def _interaction_input(model: dict[str, Any], value: object, project_root: Path) -> dict[str, Any] | None:
    if value is None:
        return None
    item = _require_fields(value, {"conditions", "action", "feedback", "status", "detail", "required_for_acceptance", "evidence"}, "交互说明")
    if item["status"] not in {"verified", "unverified", "not-implemented"}:
        raise DloopUiError("DLOOP_UI_INPUT_INVALID", "交互状态必须是 verified、unverified 或 not-implemented。")
    if not isinstance(item["required_for_acceptance"], bool):
        raise DloopUiError("DLOOP_UI_INPUT_INVALID", "必须明确该项是否属于本次必要验收。")
    return {
        **{name: _required_text(item[name], name) for name in ("conditions", "action", "feedback", "detail")},
        "status": item["status"], "required_for_acceptance": item["required_for_acceptance"],
        "evidence_ids": _delivery_evidence(model, item["evidence"], project_root),
    }


def delivery_errors(model: dict[str, Any]) -> list[dict[str, str]]:
    errors = []
    checked = model.get("delivery_review")
    if not isinstance(checked, dict):
        return [issue("UI_DELIVERY_REVIEW_REQUIRED", "交付前须从需求查实现、从实际实现查交互说明。")]
    evidence = collection_map(model, "evidence")
    for name in ("requirements_check", "implementation_check"):
        if not checked.get(name):
            errors.append(issue("UI_DELIVERY_REVIEW_REQUIRED", "缺少双向核对说明。"))
    if not checked.get("evidence_ids") or any(e not in evidence for e in checked.get("evidence_ids", [])):
        errors.append(issue("UI_DELIVERY_EVIDENCE_REQUIRED", "双向核对缺少实际证据。"))
    for material in model.get("delivery_materials", []):
        if material["kind"] == "verification" and material.get("required", True) and material["status"] != "passed":
            errors.append(issue("UI_REQUIRED_VERIFICATION_PENDING", "必要验证尚未通过：" + material["requirement"]))
    scenarios = model.get("acceptance_scenarios", [])
    if scenarios:
        for scenario in scenarios:
            if scenario["required"] and (scenario["status"] != "passed" or scenario["level"] != "runtime"):
                errors.append(issue("UI_REQUIRED_VERIFICATION_PENDING", scenario["scenario"] + "：必要实际环境验证尚未通过。"))
        for annotation in model["annotations"]:
            interaction = annotation.get("interaction")
            if interaction and interaction["required_for_acceptance"] and interaction["status"] != "verified":
                errors.append(issue("UI_REQUIRED_VERIFICATION_PENDING", "本次必要实现或验证尚未完成，不能宣称最终验收就绪。", annotation["id"]))
        return errors
    covered = set()
    for annotation in model["annotations"]:
        for requirement_id in annotation["requirement_ids"]:
            covered.add((requirement_id, annotation["prefab_id"]))
        interaction = annotation.get("interaction")
        if not isinstance(interaction, dict) or any(not interaction.get(k) for k in ("conditions", "action", "feedback", "detail", "evidence_ids")):
            errors.append(issue("UI_INTERACTION_INCOMPLETE", "交互须说明条件、操作、反馈、实现验证情况及证据。", annotation["id"]))
        elif any(e not in evidence for e in interaction["evidence_ids"]):
            errors.append(issue("UI_DELIVERY_EVIDENCE_REQUIRED", "交互引用的证据不存在。", annotation["id"]))
        elif interaction["required_for_acceptance"] and interaction["status"] != "verified":
            errors.append(issue("UI_REQUIRED_VERIFICATION_PENDING", "本次必要实现或验证尚未完成，不能宣称最终验收就绪。", annotation["id"]))
    for requirement in model["requirements"]:
        prefabs = [p["id"] for p in model["prefabs"] if requirement["id"] in p["requirement_ids"]] or [""]
        for prefab_id in prefabs:
            if (requirement["id"], prefab_id) not in covered:
                errors.append(issue("UI_INTERACTION_UNACCOUNTED", "交互不能因目标不可见或缺少截图而从最终说明中省略。", requirement["id"]))
    return errors


STATUS_LABELS = {"verified": "已实现并验证", "unverified": "尚未验证", "not-implemented": "尚未实现"}


def _presentation_pages(model: dict[str, Any], feature_path: Path) -> list[dict[str, Any]]:
    maps = {name: collection_map(model, name) for name in ENTITY_SPECS}
    numbers = _annotation_numbers(model)
    pages = []
    prefabs = list(maps["prefabs"].values())
    if any(not a.get("prefab_id") for a in model["annotations"]) or not prefabs:
        prefabs.append({"id": "", "name_zh": "未定位到界面的交互"})
    for prefab in sorted(prefabs, key=lambda p: (not bool(p["id"]), p["id"])):
        capture = prefab.get("capture", {})
        shot = maps["evidence"].get(capture.get("screenshot_evidence_id"), {})
        screenshot = Path(str(shot.get("path", "")))
        embedded = ""
        if screenshot.is_file() and screenshot.resolve().is_relative_to(feature_path.resolve()):
            embedded = "data:image/png;base64," + base64.b64encode(screenshot.read_bytes()).decode("ascii")
        asset = str(prefab.get("asset_path", "")).replace("\\", "/")
        if "/Assets/" in asset:
            asset = "Assets/" + asset.split("/Assets/", 1)[1]
        page = {"id": prefab["id"], "name": prefab["name_zh"], "image": embedded, "asset": asset,
                "asset_name": Path(asset).stem if asset else "",
                "capture_note": ("历史位置参考，未重新采集当前画面。定位复核：" + capture["reuse"]["reason"]) if capture.get("reuse") else "",
                "width": capture.get("width", 800), "height": capture.get("height", 600), "items": []}
        for annotation in sorted((a for a in model["annotations"] if a["prefab_id"] == prefab["id"]), key=lambda a: numbers[a["id"]]):
            interaction = annotation.get("interaction") or {}
            target = annotation["target"]
            path = target.get("hierarchy_path", "")
            elements = [{"name": path.rsplit("/", 1)[-1], "path": path, "role": target["label"]}] if path else []
            elements += annotation.get("related_elements", [])
            rect = target.get("rect")
            marker = None
            if embedded and rect and not target.get("unavailable_reason"):
                marker = {"x": page["width"] + 26, "y": rect["y"] + rect["height"] / 2,
                          "target_x": rect["x"] + rect["width"], "target_y": rect["y"] + rect["height"] / 2}
            sources = []
            for rid in annotation["requirement_ids"]:
                requirement = maps["requirements"][rid]
                sources.append(requirement["name_zh"] + "：" + requirement["statement"])
            for eid in interaction.get("evidence_ids", []):
                ev = maps["evidence"].get(eid, {})
                sources.append(Path(str(ev.get("path", ""))).name + " / " + str(ev.get("summary", "")))
            page["items"].append({
                "id": annotation["id"], "number": numbers[annotation["id"]], "title": annotation["title"],
                "conditions": interaction.get("conditions", "待结合实现补全"),
                "action": interaction.get("action", "待结合实现补全"),
                "feedback": interaction.get("feedback", "待结合实现补全"),
                "expected": annotation["expected"], "instruction": annotation["instruction"],
                "status": STATUS_LABELS.get(interaction.get("status"), "内部草稿"),
                "detail": interaction.get("detail", "尚未完成实施与双向核对"),
                "location_note": target.get("unavailable_reason", ""),
                "marker": marker, "rect": rect, "sources": sources, "elements": elements,
            })
        grouped = {}
        for annotation, item in zip(sorted((a for a in model["annotations"] if a["prefab_id"] == prefab["id"]), key=lambda a: numbers[a["id"]]), page["items"]):
            key = _feature_point_key(annotation)
            if key not in grouped:
                point = annotation.get("feature_point")
                grouped[key] = {**item, "title": point["title"] if point else annotation["target"]["label"], "operations": []}
            group = grouped[key]
            group["operations"].append(item)
            if group["marker"] is None and item["marker"]:
                group.update(marker=item["marker"], rect=item["rect"])
        page["items"] = list(grouped.values())
        previous_y = -20
        for item in sorted((i for i in page["items"] if i["marker"]), key=lambda i: i["marker"]["y"]):
            item["marker"]["y"] = max(20, item["marker"]["y"], previous_y + 40)
            previous_y = item["marker"]["y"]
        for item in page["items"]:
            elements = {}
            for operation in item["operations"]:
                for element in operation["elements"]:
                    key = element["path"]
                    if key not in elements:
                        elements[key] = {"name": element["name"], "path": key, "roles": []}
                    if element["role"] not in elements[key]["roles"]:
                        elements[key]["roles"].append(element["role"])
            item["elements"] = list(elements.values())
            if len(item["operations"]) > 1:
                for field in ("conditions", "action", "feedback", "expected", "instruction"):
                    item[field] = "\n".join(op["title"] + "：" + op[field] for op in item["operations"])
                item["location_note"] = "\n".join(op["title"] + "：" + op["location_note"] for op in item["operations"] if op["location_note"])
                item["sources"] = list(dict.fromkeys(source for op in item["operations"] for source in op["sources"]))
        pages.append(page)
    return pages


def render_delivery_html(model: dict[str, Any], feature_path: Path, *, delivery_ready: bool) -> str:
    template = Path(__file__).with_name("templates") / "ui-delivery.html"
    scenarios = []
    for item in model.get("acceptance_scenarios", []):
        refs = []
        for ref in item["evidence"]:
            path = Path(ref["path"])
            try:
                href = quote(os.path.relpath(path, feature_path / "06-validation").replace("\\", "/"), safe="/")
            except ValueError:
                href = path.as_uri()
            refs.append({"label": path.name + " · " + ref["locator"],
                         "href": href})
        scenarios.append({**item, "evidence": refs})
    data = {"title": model["feature"]["title"], "pages": _presentation_pages(model, feature_path),
            "scenarios": scenarios, "ready": delivery_ready}
    payload = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c").replace("&", "\\u0026")
    return template.read_text(encoding="utf-8").replace("{{DELIVERY_DATA}}", payload)


def _has_errors(problems: Iterable[dict[str, str]]) -> bool:
    return any(item.get("severity") == "error" for item in problems)
