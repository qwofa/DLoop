from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


FEATURE_ARCHIVE_ROOT = Path(__file__).resolve().parents[1]
if str(FEATURE_ARCHIVE_ROOT) not in sys.path:
    sys.path.insert(0, str(FEATURE_ARCHIVE_ROOT))

from archive_configuration import (  # noqa: E402
    ArchiveConfigurationError,
    DLOOP_UI_CONFIGURATION,
    available_configurations,
    configuration_evaluation,
    configuration_sync_stages,
    configuration_task_materials,
    initialize_configuration,
    require_configuration_task_materials,
    synchronize_configuration,
    validate_configuration,
)
from archive_initialization import initialize_archive  # noqa: E402
from feature_archive import main  # noqa: E402


class FeatureArchiveConfigurationTests(unittest.TestCase):
    def test_static_dloop_ui_registration_exposes_the_configuration_interface(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configuration, files = initialize_configuration(
                DLOOP_UI_CONFIGURATION,
                "feature",
                "测试功能",
            )
            feature_path = root / "feature"
            feature_path.mkdir()
            for relative_path, content in files.items():
                feature_path.joinpath(relative_path).write_text(content, encoding="utf-8")

            validate_configuration(configuration, feature_path)
            requirements = configuration_evaluation(
                feature_path,
                {"configuration": configuration},
                "requirements",
            )

            self.assertEqual((DLOOP_UI_CONFIGURATION,), available_configurations())
            self.assertEqual(
                ("requirements", "investigation", "design", "plan"),
                configuration_sync_stages(DLOOP_UI_CONFIGURATION),
            )
            self.assertEqual({Path("ui-model.json")}, set(files))
            self.assertIsNone(requirements)
            final = configuration_evaluation(feature_path, {"configuration": configuration}, "final")
            self.assertEqual("BLOCKED", final["status"])
            self.assertEqual("DLOOP_UI_PLAN_BLOCKED", final["code"])

    def test_configuration_files_reject_windows_drive_paths(self) -> None:
        def unsafe_provider(operation: str, **arguments: object):
            self.assertEqual("initialize", operation)
            return {
                "configuration": {"id": DLOOP_UI_CONFIGURATION},
                "files": {"C:/outside/escape.json": "{}\n"},
            }

        with patch(
            "archive_configuration._provider",
            return_value=unsafe_provider,
        ), self.assertRaises(ArchiveConfigurationError) as raised:
            initialize_configuration(
                DLOOP_UI_CONFIGURATION,
                "feature",
                "测试功能",
            )

        self.assertEqual("INVALID_CONFIGURATION_RESULT", raised.exception.code)

    def test_invalid_public_sync_result_does_not_change_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_root = root / "outputs"
            initialize_archive(
                archive_root,
                "configured-feature",
                "测试功能",
                DLOOP_UI_CONFIGURATION,
            )
            feature_path = archive_root / "configured-feature"
            state_before = feature_path.joinpath("workflow-state.json").read_bytes()
            model_before = feature_path.joinpath("ui-model.json").read_bytes()

            def invalid_provider(operation: str, **arguments: object):
                if operation == "validate":
                    return {"status": "valid"}
                if operation == "synchronize":
                    return {
                        "status": "PASS",
                        "configuration": arguments["configuration"],
                        "files": {"ui-model.json": "{\"changed\":true}\n"},
                        "result": {"configuration_status": "spoofed"},
                    }
                raise ValueError(operation)

            with patch("archive_configuration._provider", return_value=invalid_provider):
                with self.assertRaises(ArchiveConfigurationError) as raised:
                    synchronize_configuration(
                        archive_root,
                        "configured-feature",
                        "plan",
                    )

            self.assertEqual("INVALID_CONFIGURATION_RESULT", raised.exception.code)
            self.assertEqual(
                state_before,
                feature_path.joinpath("workflow-state.json").read_bytes(),
            )
            self.assertEqual(
                model_before,
                feature_path.joinpath("ui-model.json").read_bytes(),
            )

    def test_configuration_cannot_remove_files_outside_its_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_root = root / "outputs"
            initialize_archive(
                archive_root,
                "configured-feature",
                "测试功能",
                DLOOP_UI_CONFIGURATION,
            )
            feature_path = archive_root / "configured-feature"
            outside = archive_root / "outside.svg"
            outside.write_text("keep\n", encoding="utf-8")
            state_before = feature_path.joinpath("workflow-state.json").read_bytes()

            def unsafe_provider(operation: str, **arguments: object):
                if operation == "validate":
                    return {"status": "valid"}
                if operation == "synchronize":
                    return {
                        "status": "PASS",
                        "configuration": arguments["configuration"],
                        "files": {},
                        "remove_files": ["../outside.svg"],
                        "result": {"status": "synced"},
                    }
                raise ValueError(operation)

            with patch("archive_configuration._provider", return_value=unsafe_provider):
                with self.assertRaises(ArchiveConfigurationError) as raised:
                    synchronize_configuration(
                        archive_root,
                        "configured-feature",
                        "plan",
                    )

            self.assertEqual("INVALID_CONFIGURATION_RESULT", raised.exception.code)
            self.assertEqual("keep\n", outside.read_text(encoding="utf-8"))
            self.assertEqual(
                state_before,
                feature_path.joinpath("workflow-state.json").read_bytes(),
            )

    def test_plain_commands_do_not_load_the_registered_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_root = root / "outputs"

            with patch(
                "archive_configuration._provider",
                side_effect=AssertionError("普通 DLoop 不应加载 DloopUI"),
            ):
                initialize_archive(
                    archive_root,
                    "plain-feature",
                    "普通功能",
                )
                with (
                    patch(
                        "feature_archive.project_root_from_entrypoint",
                        return_value=archive_root.parent,
                    ),
                    patch(
                        "feature_archive.canonical_archive_root",
                        return_value=archive_root,
                    ),
                    redirect_stdout(io.StringIO()),
                    redirect_stderr(io.StringIO()),
                ):
                    exit_code = main(["validate"])

            self.assertEqual(0, exit_code)

    def test_source_documents_require_an_explicit_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "requirements.md"
            source.write_text("# 需求\n", encoding="utf-8")
            archive_root = root / "outputs"

            with self.assertRaises(ArchiveConfigurationError) as raised:
                initialize_archive(
                    archive_root,
                    "plain-feature",
                    "普通功能",
                    source_documents=(source,),
                )

            self.assertEqual("CONFIGURATION_REQUIRED", raised.exception.code)
            self.assertFalse(archive_root.exists())

    def test_unknown_configuration_is_not_discoverable(self) -> None:
        with self.assertRaises(ArchiveConfigurationError) as raised:
            configuration_sync_stages("fake-v1")

        self.assertEqual("INVALID_CONFIGURATION", raised.exception.code)

    def test_dloop_ui_task_material_requires_full_requirements_and_design(self) -> None:
        state = {"configuration": {"id": DLOOP_UI_CONFIGURATION}}
        required = configuration_task_materials(state, "feature")

        self.assertEqual({"feature.requirements.overview", "feature.design.overview"}, {item["source"] for item in required})
        with self.assertRaises(ArchiveConfigurationError) as raised:
            require_configuration_task_materials(
                state,
                "feature",
                [{
                    "source": "feature.requirements.overview",
                    "purpose": "只读一节",
                    "mode": "sections",
                }],
            )
        self.assertEqual("CONFIGURATION_TASK_MATERIAL_REQUIRED", raised.exception.code)
        require_configuration_task_materials(state, "feature", required)


if __name__ == "__main__":
    unittest.main()
