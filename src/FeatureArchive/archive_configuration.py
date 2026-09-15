"""已登记工作流配置的初始化、校验、检查点和 UI 业务入口。"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Dict, Mapping, Sequence
from archive_workspace import serialized_workflow_state


DLOOP_UI_CONFIGURATION = "dloop-ui-v1"
_CONFIGURATIONS: Mapping[str, Mapping[str, object]] = {
    DLOOP_UI_CONFIGURATION: {
        "checkpoints": ("final",),
    },
}


class ArchiveConfigurationError(Exception):
    """表示可选配置无法安全接入当前交付项。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def available_configurations() -> tuple[str, ...]:
    return tuple(_CONFIGURATIONS)


def _configuration_definition(configuration_id: str) -> Mapping[str, object]:
    definition = _CONFIGURATIONS.get(configuration_id)
    if definition is None:
        raise ArchiveConfigurationError(
            "INVALID_CONFIGURATION",
            f"未知 DLoop 配置：{configuration_id}",
        )
    return definition


def _provider(configuration_id: str):
    _configuration_definition(configuration_id)
    from archive_ui import handle

    return handle


def _invoke(
    configuration_id: str,
    operation: str,
    **arguments: object,
) -> Mapping[str, object]:
    try:
        result = _provider(configuration_id)(
            operation=operation,
            **deepcopy(arguments),
        )
    except ArchiveConfigurationError:
        raise
    except Exception as exception:
        raise ArchiveConfigurationError(
            "CONFIGURATION_OPERATION_FAILED",
            f"可选配置“{configuration_id}”执行失败：{exception}",
        ) from exception
    if not isinstance(result, Mapping):
        raise ArchiveConfigurationError(
            "INVALID_CONFIGURATION_RESULT",
            f"可选配置“{configuration_id}”返回结果不合法。",
        )
    return deepcopy(dict(result))


def _configuration_id(value: object) -> str:
    if not isinstance(value, Mapping):
        raise ArchiveConfigurationError("INVALID_CONFIGURATION", "可选配置状态必须是对象。")
    configuration_id = value.get("id")
    if not isinstance(configuration_id, str) or not configuration_id:
        raise ArchiveConfigurationError("INVALID_CONFIGURATION", "可选配置状态缺少标识。")
    return configuration_id


def _relative_files(value: object) -> Mapping[Path, str]:
    if not isinstance(value, Mapping):
        raise ArchiveConfigurationError(
            "INVALID_CONFIGURATION_RESULT",
            "可选配置文件结果必须是对象。",
        )
    result: Dict[Path, str] = {}
    for raw_path, content in value.items():
        if not isinstance(raw_path, str) or not isinstance(content, str):
            raise ArchiveConfigurationError(
                "INVALID_CONFIGURATION_RESULT",
                "可选配置文件路径和内容必须是字符串。",
            )
        relative = PurePosixPath(raw_path.replace("\\", "/"))
        if (
            relative.is_absolute()
            or PureWindowsPath(raw_path).drive
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise ArchiveConfigurationError(
                "INVALID_CONFIGURATION_RESULT",
                "可选配置文件必须保持在交付档案内。",
            )
        result[Path(*relative.parts)] = content
    return result


def _relative_binary_files(value: object) -> Mapping[Path, bytes]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ArchiveConfigurationError(
            "INVALID_CONFIGURATION_RESULT",
            "可选配置二进制文件结果必须是对象。",
        )
    result: Dict[Path, bytes] = {}
    for raw_path, content in value.items():
        if not isinstance(raw_path, str) or not isinstance(content, bytes):
            raise ArchiveConfigurationError(
                "INVALID_CONFIGURATION_RESULT",
                "可选配置二进制文件路径必须是字符串，内容必须是字节。",
            )
        relative = PurePosixPath(raw_path.replace("\\", "/"))
        if (
            relative.is_absolute()
            or PureWindowsPath(raw_path).drive
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise ArchiveConfigurationError(
                "INVALID_CONFIGURATION_RESULT",
                "可选配置二进制文件必须保持在交付档案内。",
            )
        result[Path(*relative.parts)] = content
    return result


def _relative_removals(value: object) -> tuple[Path, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(
        not isinstance(item, str) for item in value
    ):
        raise ArchiveConfigurationError(
            "INVALID_CONFIGURATION_RESULT",
            "可选配置删除清单必须是字符串数组。",
        )
    result: list[Path] = []
    seen: set[Path] = set()
    for raw_path in value:
        relative = PurePosixPath(raw_path.replace("\\", "/"))
        if (
            relative.is_absolute()
            or PureWindowsPath(raw_path).drive
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise ArchiveConfigurationError(
                "INVALID_CONFIGURATION_RESULT",
                "可选配置删除目标必须保持在交付档案内。",
            )
        path = Path(*relative.parts)
        if path in seen:
            raise ArchiveConfigurationError(
                "INVALID_CONFIGURATION_RESULT",
                "可选配置删除清单不能包含重复路径。",
            )
        seen.add(path)
        result.append(path)
    return tuple(result)


def initialize_configuration(
    configuration_id: str | None,
    feature_id: str,
    title: str,
    source_documents: Sequence[Path] = (),
) -> tuple[Mapping[str, object] | None, Mapping[Path, str]]:
    if configuration_id is None:
        if source_documents:
            raise ArchiveConfigurationError(
                "CONFIGURATION_REQUIRED",
                "来源材料只能在显式选择可选配置时导入。",
            )
        return None, {}
    result = _invoke(
        configuration_id,
        "initialize",
        feature_id=feature_id,
        title=title,
        source_documents=[Path(path) for path in source_documents],
    )
    configuration = result.get("configuration")
    files = _relative_files(result.get("files"))
    if _configuration_id(configuration) != configuration_id:
        raise ArchiveConfigurationError(
            "INVALID_CONFIGURATION_RESULT",
            "可选配置初始化结果与所选配置不一致。",
        )
    return deepcopy(dict(configuration)), files


def validate_configuration(value: object, feature_path: Path) -> None:
    configuration_id = _configuration_id(value)
    _invoke(
        configuration_id,
        "validate",
        feature_path=Path(feature_path),
        configuration=dict(value),
    )


def configuration_evaluation(
    feature_path: Path,
    state: Mapping[str, object],
    checkpoint: str,
) -> Mapping[str, object] | None:
    value = state.get("configuration")
    if value is None:
        return None
    configuration_id = _configuration_id(value)
    if checkpoint not in _configuration_definition(configuration_id)["checkpoints"]:
        return None
    result = _invoke(
        configuration_id,
        "evaluate",
        feature_path=Path(feature_path),
        configuration=dict(value),
    )
    if result.get("status") not in {"PASS", "BLOCKED"}:
        raise ArchiveConfigurationError(
            "INVALID_CONFIGURATION_RESULT",
            "可选配置检查结果缺少有效状态。",
        )
    return result


def require_configuration_checkpoint(
    feature_path: Path,
    state: Mapping[str, object],
    checkpoint: str,
) -> None:
    result = configuration_evaluation(feature_path, state, checkpoint)
    if result is None or result.get("status") == "PASS":
        return
    code = result.get("code")
    message = result.get("message")
    if not isinstance(code, str) or not isinstance(message, str):
        raise ArchiveConfigurationError(
            "INVALID_CONFIGURATION_RESULT",
            "可选配置阻断结果缺少诊断。",
        )
    raise ArchiveConfigurationError(code, message)


def configuration_task_materials(
    state: Mapping[str, object],
    feature_id: str,
) -> tuple[Mapping[str, object], ...]:
    value = state.get("configuration")
    if value is None or _configuration_id(value) != DLOOP_UI_CONFIGURATION:
        return ()
    return tuple({"source": f"{feature_id}.{role}", "purpose": purpose, "mode": "full"}
                 for role, purpose in (("requirements.overview", "核对原始业务范围与遗漏"),
                                       ("design.overview", "读取内部设计并随实施补全交互")))



def require_configuration_task_materials(
    state: Mapping[str, object],
    feature_id: str,
    materials: Sequence[Mapping[str, object]],
) -> None:
    required = configuration_task_materials(state, feature_id)
    missing = [
        str(item["source"])
        for item in required
        if not any(
            material.get("source") == item["source"]
            and material.get("mode") == item["mode"]
            for material in materials
        )
    ]
    if missing:
        raise ArchiveConfigurationError(
            "CONFIGURATION_TASK_MATERIAL_REQUIRED",
            "DloopUI 任务包必须完整引用当前需求与设计：" + "、".join(missing),
        )


def _run_ui_operation(
    root: Path,
    feature_id: str,
    operation: str,
    input_path: Path,
    project_root: Path,
    unity_executable: Path | None = None,
    capture_manifest: Path | None = None,
) -> Mapping[str, object]:
    from archive_approvals import _load_state, _require_complex_feature
    from archive_changes import ArchiveChangeError, _replace_files_atomically
    from archive_validation import validate_feature_archive

    graph = validate_feature_archive(root, feature_id)
    feature = _require_complex_feature(graph, feature_id)
    state = _load_state(feature.path, feature_id)
    value = state.get("configuration")
    configuration_id = _configuration_id(value)
    if configuration_id != DLOOP_UI_CONFIGURATION:
        raise ArchiveConfigurationError(
            "INVALID_CONFIGURATION",
            "当前交付项没有选择 DloopUI 配置。",
        )
    arguments: dict[str, object] = {
        "feature_path": feature.path,
        "configuration": dict(value),
        "input_path": Path(input_path),
        "project_root": Path(project_root),
    }
    if operation == "publish":
        from archive_approvals import accepted_candidate_summary
        arguments["candidate_summary"] = accepted_candidate_summary(state["execution"])
        arguments["execution"] = state["execution"]
    if unity_executable is not None:
        arguments["unity_executable"] = Path(unity_executable)
    if capture_manifest is not None:
        arguments["capture_manifest"] = Path(capture_manifest)
    result = _invoke(configuration_id, operation, **arguments)
    if operation == "investigate" and result.get("status") == "AWAITING_CAPTURE":
        return deepcopy(dict(result["result"]))
    if result.get("status") != "PASS":
        raise ArchiveConfigurationError(
            "INVALID_CONFIGURATION_RESULT",
            "DloopUI 业务操作缺少有效状态。",
        )
    updated_configuration = result.get("configuration")
    if _configuration_id(updated_configuration) != configuration_id:
        raise ArchiveConfigurationError(
            "INVALID_CONFIGURATION_RESULT",
            "DloopUI 业务操作返回了错误的配置身份。",
        )
    response = result.get("result")
    if not isinstance(response, Mapping) or not isinstance(response.get("status"), str):
        raise ArchiveConfigurationError(
            "INVALID_CONFIGURATION_RESULT",
            "DloopUI 业务操作缺少公开结果。",
        )
    relative_files = _relative_files(result.get("files"))
    relative_binary_files = _relative_binary_files(result.get("binary_files"))
    relative_removals = _relative_removals(result.get("remove_files"))
    written = set(relative_files).union(relative_binary_files)
    if set(relative_files).intersection(relative_binary_files) or written.intersection(relative_removals):
        raise ArchiveConfigurationError(
            "INVALID_CONFIGURATION_RESULT",
            "DloopUI 业务操作不能重复写入或同时写入、删除同一文件。",
        )
    state["configuration"] = deepcopy(dict(updated_configuration))
    contents: dict[Path, str | bytes] = {
        feature.path / relative: content
        for relative, content in relative_files.items()
    }
    contents.update(
        {
            feature.path / relative: content
            for relative, content in relative_binary_files.items()
        }
    )
    contents[feature.path / "workflow-state.json"] = (
        json.dumps(state, ensure_ascii=False, indent=2) + "\n"
    )
    try:
        _replace_files_atomically(
            graph.root,
            contents,
            deletions=(feature.path / relative for relative in relative_removals),
        )
    except ArchiveChangeError as exception:
        raise ArchiveConfigurationError(exception.code, exception.message) from exception
    return deepcopy(dict(response))


@serialized_workflow_state
def investigate_ui(
    root: Path,
    feature_id: str,
    input_path: Path,
    project_root: Path,
    unity_executable: Path | None = None,
    capture_manifest: Path | None = None,
) -> Mapping[str, object]:
    return _run_ui_operation(
        root,
        feature_id,
        "investigate",
        input_path,
        project_root,
        unity_executable,
        capture_manifest,
    )


@serialized_workflow_state
def publish_ui(
    root: Path,
    feature_id: str,
    input_path: Path,
    project_root: Path,
) -> Mapping[str, object]:
    return _run_ui_operation(
        root,
        feature_id,
        "publish",
        input_path,
        project_root,
    )
