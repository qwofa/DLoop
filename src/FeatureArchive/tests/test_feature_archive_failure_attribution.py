"""结构化摩擦事件、去重和自动关闭测试。"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


TOOL_ROOT = Path(__file__).resolve().parents[1]
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

from archive_failure_attribution import (  # noqa: E402
    friction_log_path,
    record_friction,
    record_operation_success,
)
from _feature_archive_support import FeatureArchiveCliTestCase  # noqa: E402


class FeatureArchiveFrictionLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "outputs"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _entries(self) -> list[dict[str, object]]:
        return [
            json.loads(line)
            for line in friction_log_path(self.root).read_text(encoding="utf-8").splitlines()
        ]

    def test_same_failed_input_reuses_one_open_incident(self) -> None:
        arguments = {
            "feature_id": "example",
            "command": "start-slice",
            "code": "WORKSPACE_DRIFT",
            "target": {"package_id": "slice-1"},
        }

        first = record_friction(self.root, "工作区发生漂移。", **arguments)
        second = record_friction(self.root, "工作区发生漂移。", **arguments)

        self.assertEqual("recorded", first["status"])
        self.assertEqual("unchanged", second["status"])
        self.assertEqual(first["incident_id"], second["incident_id"])
        self.assertEqual(1, len(self._entries()))

    def test_changed_input_and_success_form_an_append_only_lifecycle(self) -> None:
        arguments = {
            "feature_id": "example",
            "command": "start-slice",
            "code": "INVALID_TASK_PACKAGE",
            "target": {"package_file": "slice.json"},
        }
        failure = record_friction(
            self.root,
            "任务包缺少材料。",
            request_input={"package_file": {"sha256": "first"}},
            **arguments,
        )
        attempt = record_friction(
            self.root,
            "任务包缺少材料。",
            request_input={"package_file": {"sha256": "second"}},
            **arguments,
        )

        success = record_operation_success(
            self.root,
            feature_id="example",
            command="start-slice",
            target={"package_file": "slice.json"},
            successful_operation="补齐材料后成功启动。",
        )

        self.assertEqual(failure["incident_id"], attempt["incident_id"])
        self.assertEqual("recorded", success["status"])
        entries = self._entries()
        self.assertEqual(["failure", "attempt", "resolved"], [item["event"] for item in entries])
        self.assertEqual(entries[0]["incident_id"], entries[-1]["incident_id"])
        self.assertEqual("cli", entries[0]["source"])

    def test_expected_guard_is_visible_but_never_opened_as_friction(self) -> None:
        guard = record_friction(
            self.root,
            "需求尚未批准。",
            feature_id="example",
            command="start-slice",
            code="REQUIREMENTS_APPROVAL_REQUIRED",
            expected_guard=True,
        )

        self.assertEqual("guard", guard["event"])
        self.assertTrue(self._entries()[0]["expected_guard"])
        closure = record_operation_success(
            self.root,
            feature_id="example",
            command="start-slice",
            target={},
            successful_operation="需求批准后启动。",
        )
        self.assertEqual("unchanged", closure["status"])

    def test_log_write_failure_is_reported_without_raising(self) -> None:
        with patch("archive_failure_attribution.os.open", side_effect=OSError("只读")):
            result = record_friction(self.root, "无法写入状态。")

        self.assertEqual("not-recorded", result["status"])
        self.assertIn("warning", result)


class FeatureArchiveFrictionCliTests(FeatureArchiveCliTestCase):
    real_snapshots = False

    def test_failed_workflow_action_returns_a_structured_incident(self) -> None:
        result = self.run_cli(
            "init", "--feature-id", "INVALID", "--title", "非法标识",
            expected=1,
        )

        self.assertTrue(result["friction_id"].startswith("f-"))
        entry = json.loads(
            friction_log_path(self.root).read_text(encoding="utf-8").splitlines()[0]
        )
        self.assertEqual(result["friction_id"], entry["incident_id"])
        self.assertEqual("failure", entry["event"])
        self.assertEqual("init", entry["command"])
        self.assertEqual("cli", entry["source"])
        self.assertIn("功能标识", entry["message"])

    def test_changed_request_file_at_same_path_appends_an_attempt(self) -> None:
        source = self.base / "requirements.md"
        source.write_text("first request", encoding="utf-8")
        arguments = (
            "init", "--feature-id", "INVALID", "--title", "非法标识",
            "--source-document", source,
        )

        first = self.run_cli(*arguments, expected=1)
        source.write_text("second request", encoding="utf-8")
        second = self.run_cli(*arguments, expected=1)

        self.assertEqual(first["friction_id"], second["friction_id"])
        entries = [
            json.loads(line)
            for line in friction_log_path(self.root).read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(["failure", "attempt"], [entry["event"] for entry in entries])
        self.assertNotEqual(
            entries[0]["input_fingerprint"],
            entries[1]["input_fingerprint"],
        )


if __name__ == "__main__":
    unittest.main()
