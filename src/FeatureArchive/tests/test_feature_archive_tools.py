"""结构化入口与现有 CLI 共用业务门禁、摩擦记录和材料保存。"""

import io
import json
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

from _feature_archive_support import FeatureArchiveCliTestCase, _svn_guard_test_double
from archive_tool_api import invoke_tool


class StructuredToolTests(FeatureArchiveCliTestCase):
    real_snapshots = False

    def call(self, name, **arguments):
        output = io.StringIO()
        with mock.patch("archive_workspace.workspace_guard_snapshot", side_effect=_svn_guard_test_double), \
                redirect_stdout(output), redirect_stderr(output):
            code, result = invoke_tool(self.project_root, name, arguments)
        self.assertEqual("", output.getvalue(), "结构化入口不能写入进程标准流")
        return code, result

    def test_template_does_not_write_file_and_package_uses_current_business_validation(self):
        archive = self.init_complex()
        self.approve_requirements()
        self.prepare_execution_inputs()
        self.run_cli("transition-lifecycle", "--feature-id", archive.name, "--to", "active")
        before = set(archive.rglob("*.json"))
        code, prepared = self.call("dloop_prepare_input", feature_id=archive.name,
                                   input_kind="task-package", package_id="slice-one")
        self.assertEqual(0, code)
        self.assertEqual("template", prepared["status"])
        self.assertEqual(before, set(archive.rglob("*.json")))
        self.assertNotIn("target", prepared)
        self.assertEqual("dloop_submit_task_package", prepared["next_action"]["tool_call"]["name"])
        code, invalid = self.call("dloop_submit_task_package", feature_id=archive.name, package=prepared["template"])
        self.assertEqual(1, code)
        self.assertIn("code", invalid)
        self.assertIn("friction", json.dumps(invalid))
        package_file = self.write_package("slice-one")
        package = json.loads(package_file.read_text(encoding="utf-8"))
        code, result = self.call("dloop_submit_task_package", feature_id=archive.name, package=package)
        self.assertEqual(0, code)
        self.assertEqual("contract_draft", result["status"])
        artifact = Path(result["input_artifact"])
        self.assertEqual((archive / "05-implementation").resolve(), artifact.parent)
        self.assertFalse(artifact.read_bytes().startswith(b"\xef\xbb\xbf"))
        self.assertEqual(package, json.loads(artifact.read_text(encoding="utf-8")))
        code, repeated = self.call("dloop_submit_task_package", feature_id=archive.name, package=package)
        self.assertEqual(0, code)
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(result["input_artifact"], repeated["input_artifact"])
        self.run_cli("check-slice-contract", "--feature-id", archive.name, "--package-id", "slice-one")
        code, status = self.call("dloop_status", feature_id=archive.name)
        self.assertEqual(0, code)
        self.assertEqual("start-slice", status["delivery_view"]["next_action_contract"]["command"])

    def test_registered_package_preparation_round_trips_without_relaxing_contract_edits(self):
        archive = self.init_complex()
        self.approve_requirements()
        self.prepare_execution_inputs()
        self.run_cli("transition-lifecycle", "--feature-id", archive.name, "--to", "active")
        package = json.loads(self.write_package("slice-one").read_text(encoding="utf-8"))
        package["generated_write_scope"] = list(package["write_scope"])
        code, _ = self.call("dloop_submit_task_package", feature_id=archive.name, package=package)
        self.assertEqual(0, code)
        self.run_cli("check-slice-contract", "--feature-id", archive.name, "--package-id", "slice-one")
        state_path = archive / "workflow-state.json"
        before = state_path.read_bytes()
        code, prepared = self.call("dloop_prepare_input", feature_id=archive.name,
                                   input_kind="task-package", package_id="slice-one")
        self.assertEqual(0, code)
        self.assertEqual(package, prepared["template"])
        self.assertEqual(before, state_path.read_bytes())
        cli = self.run_cli("prepare-action-input", "--feature-id", archive.name,
                           "--input-kind", "task-package", "--package-id", "slice-one")
        self.assertEqual(prepared["template"], cli["template"])
        code, result = self.call("dloop_submit_task_package", feature_id=archive.name, package=prepared["template"])
        self.assertEqual(0, code)
        self.assertTrue(result["idempotent"])
        prepared["template"]["write_scope"].append("additional.txt")
        code, result = self.call("dloop_submit_task_package", feature_id=archive.name, package=prepared["template"])
        self.assertEqual(1, code)
        self.assertEqual("CONTRACT_SILENT_REWRITE_FORBIDDEN", result["code"])
        code, new = self.call("dloop_prepare_input", feature_id=archive.name,
                              input_kind="task-package", package_id="slice-two")
        self.assertEqual(0, code)
        self.assertEqual(["待填写：授权写入范围"], new["template"]["write_scope"])

    def test_unknown_delivery_cannot_create_an_input_outside_the_archive(self):
        self.init_complex()
        code, result = self.call("dloop_submit_ui_baseline", feature_id="missing-delivery",
                                baseline={"input_version": 2, "items": [], "scope_exclusions": []})
        self.assertEqual(1, code)
        self.assertFalse((self.root / "missing-delivery").exists())
        self.assertNotIn("input_artifact", result)
