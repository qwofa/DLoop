"""封装功能交付档案初始化的模板、预检、写入和回滚事务。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from datetime import datetime, timezone
from typing import Dict, Mapping, Sequence, Tuple

from archive_approvals import initial_workflow_state
from archive_profiles import ARCHIVE_SCHEMA_VERSION, STRICT_PROFILE
from archive_root import ArchiveRootError, ensure_root_contract
from archive_terminology import TERMINOLOGY_SCHEMA_VERSION
from archive_configuration import (
    available_configurations,
    initialize_configuration,
)


SCHEMA_VERSION = ARCHIVE_SCHEMA_VERSION
DEFAULT_RETENTION_DAYS = 30
FEATURE_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
DOCUMENT_ID_PATTERN = re.compile(
    r"^[a-z0-9]+(?:-[a-z0-9]+)*(?:\.[a-z0-9]+(?:-[a-z0-9]+)*)+$"
)
FRONT_MATTER_FIELD_PATTERN = re.compile(r"^([a-z_]+):\s*(.*?)\s*$")

CATEGORIES: Tuple[Tuple[str, str, str, str], ...] = (
    ("01-requirements", "requirements", "需求", "记录已确认的目标、范围、非目标和验收条件。"),
    ("02-investigation", "investigation", "调查", "记录仓库事实、证据、现状和实现限制。"),
    ("03-design", "design", "设计", "记录方案、权衡、接口边界和设计决策。"),
    ("04-plan", "plan", "计划", "记录可执行任务、顺序、依赖和回滚安排。"),
    ("05-implementation", "implementation", "实施", "记录实际修改、计划偏离和实施结果。"),
    ("06-validation", "validation", "验证", "记录验证证据、未验证边界、残留风险和结论。"),
)


class ArchiveError(Exception):
    """表示可以向调用方提供明确诊断的档案错误。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _validate_inputs(feature_id: str, title: str) -> Tuple[str, str]:
    normalized_id = feature_id.strip()
    normalized_title = title.strip()

    if not FEATURE_ID_PATTERN.fullmatch(normalized_id) or len(normalized_id) > 64:
        raise ArchiveError(
            "INVALID_FEATURE_ID",
            "功能标识必须是最多 64 个字符的小写英文、数字和单个连字符组合，"
            "例如 building-interaction。",
        )
    if not normalized_title:
        raise ArchiveError("INVALID_TITLE", "功能标题不能为空。")
    if any(character in normalized_title for character in ("\r", "\n", "\x00")):
        raise ArchiveError("INVALID_TITLE", "功能标题不能包含换行符或空字符。")

    return normalized_id, normalized_title


def _markdown_document(
    document_id: str,
    category: str,
    title: str,
    description: str,
    body_sections: str,
    dependencies: Sequence[str] = (),
    dependency_versions: Mapping[str, str] | None = None,
) -> str:
    if not DOCUMENT_ID_PATTERN.fullmatch(document_id):
        raise ArchiveError(
            "INVALID_DOCUMENT_ID",
            f"待生成的文档标识“{document_id}”不合法。",
        )
    body = (
        f"# {title}\n\n"
        f"> {description}\n\n"
        f"{body_sections.rstrip()}\n"
    )
    fingerprint = hashlib.sha256(body.encode("utf-8")).hexdigest()
    dependency_values = tuple(dependencies)
    consumed_versions = (
        dict(dependency_versions)
        if dependency_versions is not None
        else {}
    )
    front_matter = (
        "---\n"
        f"document_id: {document_id}\n"
        f"category: {category}\n"
        "content_status: draft\n"
        "semantic_version: 0.1.0\n"
        f"content_fingerprint: sha256:{fingerprint}\n"
        "dependencies: ["
        + ", ".join(dependency_values)
        + "]"
        + "\n"
        "dependency_versions: "
        + json.dumps(consumed_versions, ensure_ascii=False, sort_keys=True)
        + "\n"
        "related_documents: []\n"
        "---\n\n"
    )
    return front_matter + body


def _entry_document(
    feature_id: str,
    title: str,
) -> str:
    navigation_items = [
        f"[{chinese_name}总览]({directory}/README.md)"
        for directory, _, chinese_name, _ in CATEGORIES
    ]
    navigation_items.insert(1, "[业务术语表](01-requirements/terminology.md)")
    navigation = "\n".join(
        f"{index}. {item}"
        for index, item in enumerate(navigation_items, start=1)
    )
    body_sections = (
        "## 档案状态\n\n"
        "- 当前生命周期：草稿\n"
        "- 生命周期事实源：`feature.json`\n"
        "- 验收与验证程度：查看 [验证总览](06-validation/README.md)；生命周期与验收结果分开记录，验证中不等于尚未接受。\n"
        f"- 当前机器汇总：`workflow-status --feature-id {feature_id}` 的 `delivery_view.delivery_summary`；后续接入见 [统一清单](06-validation/README.md#后续接入清单)。\n"
        "- 文档依赖事实源：各文档的 `dependencies` 元数据\n\n"
        "## 人工导航\n\n"
        f"{navigation}\n\n"
        "## 使用约定\n\n"
        "- 需求确认直接阅读需求总览中的业务场景；最终验收直接阅读验证总览中的对应结果。\n"
        "- 其他阶段与专题文档按需追溯，无需先读完全部过程材料。\n"
        "- 简单功能只维护类别总览；需要更多证据时再新增专题文档。\n"
        "- 内容状态与功能生命周期分别管理，不要混用。\n"
    )
    return _markdown_document(
        f"{feature_id}.archive.entry",
        "archive",
        f"{title}功能交付档案",
        "本文件是功能交付档案的人工导航入口，不作为依赖关系的事实源。",
        body_sections,
    )


def _overview_document(
    feature_id: str,
    category_key: str,
    chinese_name: str,
    description: str,
) -> str:
    terminology_id = f"{feature_id}.requirements.terminology"
    dependencies: Tuple[str, ...] = ()
    dependency_versions: Mapping[str, str] = {}
    topic_summary = "当前没有专题文档。仅在内容复杂度确有需要时新增。"
    writing_guidance = ""
    if category_key == "requirements":
        dependencies = (terminology_id,)
        dependency_versions = {terminology_id: "0.1.0"}
        topic_summary = "- [业务术语表](terminology.md)"
        writing_guidance = (
            "<!-- 填写后删除本提示：先说明当前问题、期望变化和待决定事项，"
            "按可独立判断的业务场景写初始条件、关键操作与可观察结果。"
            "同一行为涉及多个显示位置或触发条件时，在该场景内分别说明处理和不处理的条件，保留原始来源定位；不要只写已关联某界面。"
            "影响批准的范围、代价和非目标直接可见；细节按需展开。"
            "沿用已有 Prefab 的表现不逐项枚举，材料按判断需要选择，不强制原型或媒体。 -->\n\n"
        )
    elif category_key == "validation":
        writing_guidance = (
            "<!-- 填写后删除本提示：沿用并链接已批准需求的场景名称，"
            "先呈现本次变化、实际偏差、未验证部分与待人工判断事项。"
            "按需给出真实体验入口、初始配置、简短操作与实际结果证据；"
            "不复制需求，不把示意或规划截图作为最终实现，不要求展示所有内部状态。 -->\n\n"
        )
    body_sections = (
        "## 当前摘要\n\n"
        "尚待补充。\n\n"
        f"{writing_guidance}"
        + ("## 后续接入清单\n\n"
           "<!-- 有缺项时集中填写；无缺项写无。只记录本次承诺的未完成或明确延期结果，不扩展范围。后续阶段引用本节，承接后更新去向。 -->\n\n"
           "| 影响的功能或场景 | 所需输入或待完成内容 | 提供方或承接任务 | 接入位置 | 补齐后的验证方式 | 当前状态 |\n"
           "|---|---|---|---|---|---|\n\n"
           if category_key == "validation" else "")
        +
        "## 专题文档\n\n"
        f"{topic_summary}\n"
    )
    return _markdown_document(
        f"{feature_id}.{category_key}.overview",
        category_key,
        f"{chinese_name}总览",
        description,
        body_sections,
        dependencies,
        dependency_versions,
    )


def _terminology_document(feature_id: str) -> str:
    body_sections = (
        "## 已确认术语\n\n"
        "当前没有已确认术语。\n\n"
        "## 候选术语\n\n"
        "当前没有待确认术语。\n"
    )
    return _markdown_document(
        f"{feature_id}.requirements.terminology",
        "requirements",
        "业务术语表",
        "只定义当前交付项需要跨环节保持一致的业务概念或职责，"
        "不承载状态矩阵、触发条件或实现方案。",
        body_sections,
    )


def _manifest(
    feature_id: str,
    title: str,
    created_at: str,
) -> str:
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "terminology_schema_version": TERMINOLOGY_SCHEMA_VERSION,
        "feature_id": feature_id,
        "title": title,
        "lifecycle": "draft",
        "created_at": created_at,
        "updated_at": created_at,
        "frozen_at": None,
        "retention_days": DEFAULT_RETENTION_DAYS,
        "retain_reason": None,
        "validation": {
            "conclusion": "pending",
            "unverified_boundaries": "pending",
            "residual_risks": "pending",
        },
        "categories": [
            {
                "order": index,
                "id": category_key,
                "directory": directory,
                "title": chinese_name,
            }
            for index, (directory, category_key, chinese_name, _) in enumerate(
                CATEGORIES, start=1
            )
        ],
    }
    manifest["workflow_profile"] = STRICT_PROFILE
    return json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"


def _templates(
    feature_id: str,
    title: str,
    created_at: str,
    configuration: str | None = None,
    source_documents: Sequence[Path] = (),
) -> Dict[Path, str]:
    templates: Dict[Path, str] = {
        Path("README.md"): _entry_document(
            feature_id,
            title,
        ),
        Path("feature.json"): _manifest(
            feature_id,
            title,
            created_at,
        ),
    }
    for directory, category_key, chinese_name, description in CATEGORIES:
        templates[Path(directory) / "README.md"] = _overview_document(
            feature_id,
            category_key,
            chinese_name,
            description,
        )
    templates[Path("01-requirements") / "terminology.md"] = _terminology_document(feature_id)
    workflow_configuration, configuration_files = initialize_configuration(
        configuration,
        feature_id,
        title,
        source_documents,
    )
    templates.update(configuration_files)
    templates[Path("workflow-state.json")] = initial_workflow_state(
        feature_id,
        workflow_configuration,
    )
    return templates


def _parse_front_matter(path: Path) -> Mapping[str, str]:
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exception:
        raise ArchiveError(
            "UNREADABLE_DOCUMENT",
            f"无法读取已有文档“{path}”：{exception}",
        ) from exception

    lines = content.splitlines()
    if not lines or lines[0] != "---":
        raise ArchiveError(
            "INVALID_EXISTING_DOCUMENT",
            f"已有文档“{path}”缺少 YAML 元数据起始标记“---”。",
        )

    fields: Dict[str, str] = {}
    for line in lines[1:]:
        if line == "---":
            break
        match = FRONT_MATTER_FIELD_PATTERN.fullmatch(line)
        if match:
            fields[match.group(1)] = match.group(2)
    else:
        raise ArchiveError(
            "INVALID_EXISTING_DOCUMENT",
            f"已有文档“{path}”缺少 YAML 元数据结束标记“---”。",
        )

    return fields


def _expected_document_ids(
    feature_id: str,
) -> Mapping[Path, str]:
    expected: Dict[Path, str] = {
        Path("README.md"): f"{feature_id}.archive.entry",
        Path("01-requirements") / "terminology.md": (
            f"{feature_id}.requirements.terminology"
        ),
    }
    for directory, category_key, _, _ in CATEGORIES:
        expected[Path(directory) / "README.md"] = (
            f"{feature_id}.{category_key}.overview"
        )
    return expected


def _check_existing_manifest(
    target: Path,
    feature_id: str,
) -> None:
    manifest_path = target / "feature.json"
    if not manifest_path.exists():
        raise ArchiveError(
            "INCOMPLETE_ARCHIVE",
            f"已有目录“{target}”缺少生命周期清单，不能隐式补全为交付档案。",
        )
    if not manifest_path.is_file():
        raise ArchiveError(
            "PATH_CONFLICT",
            f"需要写入文件“{manifest_path}”，但该路径已经是目录。",
        )

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exception:
        raise ArchiveError(
            "INVALID_EXISTING_MANIFEST",
            f"已有生命周期清单“{manifest_path}”不是可读取的 UTF-8 JSON：{exception}",
        ) from exception

    if not isinstance(manifest, dict) or manifest.get("feature_id") != feature_id:
        raise ArchiveError(
            "FEATURE_ID_CONFLICT",
            f"已有生命周期清单“{manifest_path}”的 feature_id 与“{feature_id}”不一致。",
        )
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ArchiveError(
            "INCOMPATIBLE_ARCHIVE_VERSION",
            f"已有生命周期清单“{manifest_path}”不属于当前 DLoop 合同，且不提供历史兼容。",
        )
    if manifest.get("terminology_schema_version") != TERMINOLOGY_SCHEMA_VERSION:
        raise ArchiveError(
            "INCOMPATIBLE_TERMINOLOGY_VERSION",
            f"已有生命周期清单“{manifest_path}”缺少 DLoop 术语合同。",
        )
    workflow_profile = manifest.get("workflow_profile")
    if workflow_profile != STRICT_PROFILE:
        raise ArchiveError(
            "INVALID_EXISTING_MANIFEST",
            f"已有生命周期清单“{manifest_path}”不属于 DLoop 唯一严格合同。",
        )


def _check_existing_documents(
    target: Path,
    feature_id: str,
) -> None:
    expected_ids = _expected_document_ids(feature_id)
    occupied_ids: Dict[str, Path] = {}

    for path in target.rglob("*.md"):
        if not path.is_file():
            continue
        relative_path = path.relative_to(target)
        expected_id = expected_ids.get(relative_path)
        if expected_id is None:
            try:
                first_line = path.read_text(encoding="utf-8").splitlines()[:1]
            except (OSError, UnicodeError):
                continue
            if first_line != ["---"]:
                continue
            try:
                fields = _parse_front_matter(path)
            except ArchiveError:
                continue
        else:
            fields = _parse_front_matter(path)
        document_id = fields.get("document_id")
        if document_id:
            previous_path = occupied_ids.get(document_id)
            if previous_path is not None and previous_path != relative_path:
                raise ArchiveError(
                    "DUPLICATE_DOCUMENT_ID",
                    f"文档标识“{document_id}”同时出现在“{previous_path}”和"
                    f"“{relative_path}”。",
                )
            occupied_ids[document_id] = relative_path

        if expected_id is None:
            continue
        missing_fields = [
            field
            for field in (
                "document_id",
                "category",
                "content_status",
                "semantic_version",
                "content_fingerprint",
                "dependencies",
                "dependency_versions",
                "related_documents",
            )
            if field not in fields
        ]
        if missing_fields:
            raise ArchiveError(
                "INVALID_EXISTING_DOCUMENT",
                f"已有标准文档“{path}”缺少元数据字段：{', '.join(missing_fields)}。",
            )
        if fields["document_id"] != expected_id:
            raise ArchiveError(
                "DOCUMENT_ID_CONFLICT",
                f"已有标准文档“{path}”的 document_id 应为“{expected_id}”，"
                f"实际为“{fields['document_id']}”。",
            )

    for relative_path, expected_id in expected_ids.items():
        if (target / relative_path).exists():
            continue
        conflicting_path = occupied_ids.get(expected_id)
        if conflicting_path is not None:
            raise ArchiveError(
                "DOCUMENT_ID_CONFLICT",
                f"待生成文档“{relative_path}”的标识“{expected_id}”已被"
                f"“{conflicting_path}”占用。",
            )


def _check_existing_layout(target: Path) -> None:
    if not target.is_dir():
        raise ArchiveError(
            "PATH_CONFLICT",
            f"功能档案目标“{target}”已经存在，但不是目录。",
        )

    for directory, _, _, _ in CATEGORIES:
        category_path = target / directory
        if category_path.exists() and not category_path.is_dir():
            raise ArchiveError(
                "PATH_CONFLICT",
                f"类别路径“{category_path}”已经存在，但不是目录。"
                "请移动冲突文件后重试。",
            )


def _preflight_existing(
    target: Path,
    templates: Mapping[Path, str],
    feature_id: str,
) -> None:
    _check_existing_layout(target)

    for relative_path in templates:
        destination = target / relative_path
        if destination.exists() and not destination.is_file():
            raise ArchiveError(
                "PATH_CONFLICT",
                f"需要写入文件“{destination}”，但该路径已经是目录。"
                "请移动冲突目录后重试。",
            )

    _check_existing_manifest(target, feature_id)
    _check_existing_documents(target, feature_id)
    missing_paths = [
        relative_path.as_posix()
        for relative_path in templates
        if not (target / relative_path).exists()
    ]
    missing_paths.extend(
        directory
        for directory, _, _, _ in CATEGORIES
        if not (target / directory).exists()
    )
    if missing_paths:
        raise ArchiveError(
            "INCOMPLETE_ARCHIVE",
            f"已有交付档案“{feature_id}”缺少必需内容："
            + "、".join(sorted(set(missing_paths))),
        )


def _write_tree(base: Path, templates: Mapping[Path, str]) -> None:
    for relative_path, content in templates.items():
        destination = base / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8", newline="\n")


def initialize_archive(
    root: Path,
    feature_id: str,
    title: str,
    configuration: str | None = None,
    source_documents: Sequence[Path] = (),
) -> Mapping[str, object]:
    """以幂等事务创建交付档案，或确认已有档案完整。"""

    normalized_id, normalized_title = _validate_inputs(feature_id, title)
    root = root.expanduser().resolve()
    target = root / normalized_id
    created_at = _utc_now()
    if configuration is not None and configuration not in available_configurations():
        raise ArchiveError("INVALID_CONFIGURATION", f"未知 DLoop 配置：{configuration}")
    templates = _templates(
        normalized_id,
        normalized_title,
        created_at,
        configuration,
        source_documents,
    )
    if shutil.which("git") is None:
        raise ArchiveError("SNAPSHOT_GIT_REQUIRED", "中间态保存需要 Git 客户端，SVN 项目也需要。")
    from archive_snapshots import initialize_snapshots

    root_created = False
    if root.exists() and not root.is_dir():
        raise ArchiveError("INVALID_ROOT", f"档案父目录“{root}”已经存在，但不是目录。")
    if not root.exists():
        try:
            root.mkdir(parents=True)
            root_created = True
        except OSError as exception:
            raise ArchiveError("CREATE_ROOT_FAILED", f"无法创建档案父目录“{root}”：{exception}") from exception

    try:
        ensure_root_contract(root)
    except ArchiveRootError as exception:
        if root_created:
            try:
                root.rmdir()
            except OSError:
                pass
        raise ArchiveError(exception.code, exception.message) from exception

    if target.exists():
        try:
            _preflight_existing(target, templates, normalized_id)
        except Exception:
            if root_created:
                try:
                    root.rmdir()
                except OSError:
                    pass
            raise
        initialize_snapshots(root, normalized_id)
        return {
            "status": "unchanged",
            "feature_id": normalized_id,
            "path": str(target),
            "created_files": 0,
            "workflow_profile": STRICT_PROFILE,
            "configuration": configuration,
        }

    staging = None
    try:
        staging = Path(tempfile.mkdtemp(prefix=f".{normalized_id}.init-", dir=root))
        _write_tree(staging, templates)
        os.replace(staging, target)
        staging = None
    except Exception as exception:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        if root_created:
            try:
                root.rmdir()
            except OSError:
                pass
        raise ArchiveError(
            "INITIALIZATION_FAILED",
            f"初始化功能档案“{target}”失败，临时内容已清理：{exception}",
        ) from exception

    initialize_snapshots(root, normalized_id)
    return {
        "status": "created",
        "feature_id": normalized_id,
        "path": str(target),
        "created_files": len(templates),
        "workflow_profile": STRICT_PROFILE,
        "configuration": configuration,
    }
