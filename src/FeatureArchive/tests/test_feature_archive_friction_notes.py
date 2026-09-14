"""通过公开入口验证旁路补记、恢复语义和业务隔离。"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _feature_archive_support import FeatureArchiveCliTestCase
from archive_failure_attribution import friction_log_path


class FeatureArchiveFrictionNoteTests(FeatureArchiveCliTestCase):
    def _note(self, *arguments, expected=0):
        return self.run_cli("friction-note", "--feature-id", "reliable-delivery", *arguments, expected=expected)

    def _summary(self):
        return self.run_cli("friction-summary", "--feature-id", "reliable-delivery")

    def _entries(self):
        return [json.loads(line) for line in friction_log_path(self.root).read_text(encoding="utf-8").splitlines()]

    def test_observation_can_include_recovery_without_failure_and_is_idempotent(self):
        self.init_complex()
        arguments = (
            "--summary", "冷读者需要明确术语，交接未提供术语入口",
            "--extra-work", "协调者再次定位术语表并重新交接",
            "--evidence", "path:.scratch/dloop-v3/outputs/reliable-delivery/01-requirements/terminology.md",
            "--recovery", "补上术语入口后冷读通过",
            "--cost", "据本次交接记录，额外进行一次交接",
            "--role", "coordinator", "--stage", "requirements",
        )
        first = self._note(*arguments)
        repeated = self._note(*arguments)
        self.assertEqual("unchanged", repeated["status"])
        self.assertEqual(first["incident_id"], repeated["incident_id"])
        before = friction_log_path(self.root).read_bytes()
        summary = self._summary()
        self.assertEqual(before, friction_log_path(self.root).read_bytes())
        self.assertEqual(1, len(summary["timeline"]))
        incident = summary["incidents"][0]
        self.assertEqual("not-applicable", incident["operation_status"])
        self.assertEqual("recorded", incident["recovery_status"])
        self.assertEqual("available", incident["evidence"][0]["status"])
        self.assertEqual("role-report", incident["events"][0]["source"])

    def test_cost_can_be_added_alone_without_duplicates_or_operation_changes(self):
        archive = self.init_complex()
        observation = self._note("--summary", "重复解释材料位置", "--extra-work", "用户再次说明入口",
                                 "--source", "user-feedback")
        failure = self.run_cli("prepare-slice-contract", "--feature-id", "reliable-delivery",
                               "--package-file", archive / "05-implementation" / "package.json", expected=1)
        for incident_id, operation_status in (
            (observation["incident_id"], "not-applicable"),
            (failure["friction_id"], "failed"),
        ):
            with self.subTest(operation_status=operation_status):
                cost = "据本次交接记录，额外进行一次交接"
                arguments = ("--incident-id", incident_id, "--cost", cost)
                added = self._note(*arguments)
                self.assertEqual("recorded", added["status"])
                self.assertEqual(incident_id, added["incident_id"])
                before = friction_log_path(self.root).read_bytes()
                self.assertEqual("unchanged", self._note(*arguments)["status"])
                invalid = self._note("--incident-id", incident_id, "--cost", "   ", expected=1)
                self.assertEqual("INVALID_FRICTION_NOTE", invalid["code"])
                self.assertEqual(before, friction_log_path(self.root).read_bytes())
                incident = next(item for item in self._summary()["incidents"] if item["incident_id"] == incident_id)
                self.assertEqual(operation_status, incident["operation_status"])
                self.assertEqual("not-recorded", incident["recovery_status"])
                self.assertEqual(2, len(incident["events"]))
                self.assertEqual(cost, incident["events"][-1]["cost"])
                self.assertEqual("", incident["events"][-1]["summary"])
                self.assertEqual("", incident["events"][-1]["extra_work"])
                self.assertEqual("", incident["events"][-1]["recovery"])

    def test_recovery_note_neither_closes_nor_masks_automatic_failure(self):
        archive = self.init_complex()
        path = archive / "05-implementation" / "package.json"
        arguments = ("prepare-slice-contract", "--feature-id", "reliable-delivery", "--package-file", path)
        first = self.run_cli(*arguments, expected=1)
        incident_id = first["friction_id"]
        self._note("--incident-id", incident_id, "--recovery", "已补写任务包，等待重新检查")
        repeated = self.run_cli(*arguments, expected=1)
        self.assertEqual(incident_id, repeated["friction_id"])
        self.assertEqual(["failure", "recovery-note"], [entry["event"] for entry in self._entries()])
        self.assertEqual("failed", self._summary()["incidents"][0]["operation_status"])
        package = self.write_package("slice-ready")
        path.write_bytes(package.read_bytes())
        recovered = self.run_cli(*arguments)
        self.assertIn(incident_id, recovered["resolved_friction_ids"])
        self.assertEqual(incident_id, recovered["friction_notes"][0]["arguments"]["incident_id"])
        self._note("--incident-id", incident_id, "--recovery", "按生成的任务包补齐字段后草稿登记成功")
        summary = self._summary()
        incident = next(item for item in summary["incidents"] if item["incident_id"] == incident_id)
        self.assertEqual("recovered", incident["operation_status"])
        self.assertEqual("recorded", incident["recovery_status"])
        self.assertEqual(2, sum(entry["event"] == "recovery-note" for entry in incident["events"]))

    def test_expected_guard_with_extra_work_remains_a_guard(self):
        self.init_complex()
        result = self.run_cli("context-summary", "--feature-id", "reliable-delivery",
                              "--action", "implementation", "--role", "implementation", "--execution-id", "missing")
        contract = result["friction_note"]
        self._note("--incident-id", contract["arguments"]["incident_id"],
                   "--summary", "需要进入实施但需求未批准", "--extra-work", "协调者重复查找批准位置",
                   "--evidence", "用户指出相同批准问题已被解释")
        incident = self._summary()["incidents"][0]
        self.assertEqual("expected-guard", incident["operation_status"])
        self.assertEqual("not-recorded", incident["recovery_status"])

    def test_user_feedback_and_missing_evidence_do_not_require_archive_validation(self):
        self._note("--summary", "重复解释材料位置", "--extra-work", "用户再次说明入口", "--source", "user-feedback")
        self._note("--summary", "引用暂时不可读", "--extra-work", "回到来源查找", "--evidence", "path:unavailable.md")
        summary = self._summary()
        self.assertEqual(2, len(summary["incidents"]))
        self.assertEqual("missing", summary["incidents"][1]["evidence"][0]["status"])
        self.assertTrue(all(item["recovery_status"] == "not-recorded" for item in summary["incidents"]))
        self.assertFalse(self.root.exists())

    def test_note_failure_never_recursively_records_or_changes_business_files(self):
        archive = self.init_complex()
        before = {path: path.read_bytes() for path in archive.rglob("*") if path.is_file()}
        with patch("archive_failure_attribution._append", side_effect=OSError("日志不可写")):
            result = self._note("--summary", "发现重复劳动", "--extra-work", "重复提供材料", "--source", "user-feedback", expected=1)
        self.assertEqual("not-recorded", result["status"])
        self.assertIn("日志不可写", result["warning"])
        self.assertEqual(before, {path: path.read_bytes() for path in archive.rglob("*") if path.is_file()})
        self.assertFalse(friction_log_path(self.root).exists())
        invalid = self._note("--incident-id", "not-existing", "--recovery", "说明", expected=1)
        self.assertEqual("FRICTION_NOT_FOUND", invalid["code"])
        self.assertFalse(friction_log_path(self.root).exists())

    def test_notes_between_checkpoint_submission_and_review_preserve_candidate(self):
        archive = self.init_complex()
        self.approve_requirements()
        self.prepare_execution_inputs()
        package = self.write_package("slice-notes")
        self.run_cli("start-slice", "--feature-id", "reliable-delivery", "--execution-id", "implementation-notes",
                     "--package-file", package, "--workspace-root", self.project_root)
        self.project_root.joinpath("slice-notes.txt").write_text("完成当前切片", encoding="utf-8")
        self.record_checkpoint("implementation-notes")
        state_before = (archive / "workflow-state.json").read_bytes()
        self._note("--summary", "实现者重复查询已确认材料", "--extra-work", "重新读取设计入口", "--source", "user-feedback")
        self.assertEqual(state_before, (archive / "workflow-state.json").read_bytes())
        candidate = self.run_cli("prepare-action-input", "--feature-id", "reliable-delivery",
                                 "--input-kind", "candidate", "--execution-id", "implementation-notes")
        payload = json.loads(Path(candidate["target"]).read_text(encoding="utf-8"))
        payload["verification"] = ["已完成登记验证"]
        payload["unverified_boundaries"] = []
        Path(candidate["target"]).write_text(json.dumps(payload), encoding="utf-8")
        self.run_cli("submit-slice", "--feature-id", "reliable-delivery", "--execution-id", "implementation-notes",
                     "--status", "completed", "--candidate-file", candidate["target"])
        self._note("--summary", "评审者重新找到了来源", "--extra-work", "再次打开设计入口", "--source", "user-feedback")
        result = self.run_cli("review-slice", "--feature-id", "reliable-delivery", "--review-execution-id", "review-notes",
                              "--package-id", "slice-notes",
                              "--candidate-id", payload["candidate_id"], "--result", "passed")
        self.assertEqual("accepted", result["status"])
        exported = self.run_cli("export-share", "--feature-id", "reliable-delivery")
        share = Path(exported["share_location"]["path"])
        exported_entries = [json.loads(line) for line in (share / "friction.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(2, sum(item["event"] == "observation" for item in exported_entries))

    def test_summary_projects_breaker_and_termination_without_another_log(self):
        archive = self.init_complex()
        self.approve_requirements()
        self.prepare_execution_inputs()
        package = self.write_package("slice-breaker")
        self.run_cli("start-slice", "--feature-id", "reliable-delivery", "--execution-id", "exec-breaker",
                     "--package-file", package, "--workspace-root", self.workspace)
        outside = self.workspace / "outside.txt"
        outside.write_text("实际越过授权边界的实施变化", encoding="utf-8")
        result = self.record_checkpoint("exec-breaker")
        self.assertEqual("circuit_open", result["status"])
        state_path = archive / "workflow-state.json"
        before = state_path.read_bytes()
        summary = self._summary()
        self.assertEqual(before, state_path.read_bytes())
        self.assertFalse(friction_log_path(self.root).exists())
        self.assertEqual([], summary["timeline"])
        report = summary["execution_breakers"][0]
        self.assertEqual("circuit_open", report["slice_status"])
        self.assertEqual("write_scope_violation", report["triggered_rules"][0]["rule"])
        outside.unlink()
        self.run_cli("resolve-slice", "--feature-id", "reliable-delivery", "--action", "release",
                     "--package-id", "slice-breaker", "--workspace-decision", "restored")
        closed = self._summary()["execution_breakers"]
        self.assertEqual(1, len(closed))
        self.assertEqual(report["report_id"], closed[0]["report_id"])
        self.assertEqual("abandoned", closed[0]["slice_status"])
        self.assertEqual(report["triggered_rules"], closed[0]["triggered_rules"])

    def test_document_failure_and_recovery_belong_to_the_document_feature(self):
        archive = self.init_complex()
        path = archive / "04-plan" / "README.md"
        original = path.read_text(encoding="utf-8")
        invalid = original.replace(
            "dependencies: []",
            "dependencies: [reliable-delivery.validation.overview]",
        ).replace('dependency_versions: {}', 'dependency_versions: {"reliable-delivery.validation.overview": "0.1.0"}')
        self.assertNotEqual(original, invalid)
        path.write_text(invalid, encoding="utf-8")
        arguments = ("confirm-change", "--document-id", "reliable-delivery.plan.overview", "--semantic-change", "false")
        failed = self.run_cli(*arguments, expected=1)
        self.assertEqual("INVALID_DEPENDENCY_DIRECTION", failed["code"])
        self.assertEqual("reliable-delivery", self._entries()[0]["feature_id"])
        path.write_text(original, encoding="utf-8")
        success = self.run_cli(*arguments)
        self.assertIn(failed["friction_id"], success["resolved_friction_ids"])
        self.assertEqual("recovered", self._summary()["incidents"][0]["operation_status"])


if __name__ == "__main__":
    import unittest
    unittest.main()
