from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _feature_archive_support import FeatureArchiveCliTestCase, write_candidate, write_issues
import archive_approvals
import archive_context
import archive_slice_flow


class FeatureArchiveWorkflowEfficiencyTests(FeatureArchiveCliTestCase):
    def setUp(self):
        super().setUp()
        self.feature = self.init_complex()
        self.approve_requirements()
        self.prepare_execution_inputs()
        self.run_cli("transition-lifecycle", "--feature-id", "reliable-delivery", "--to", "active")

    def start(self):
        self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery", "--execution-id", "exec-1",
            "--package-file", self.write_package("slice-1"), "--workspace-root", self.workspace,
        )

    def prepare_future(self, package_id, ready=False):
        self.run_cli(
            "prepare-slice-contract", "--feature-id", "reliable-delivery",
            "--package-file", self.write_package(package_id),
        )
        if ready:
            self.run_cli(
                "check-slice-contract", "--feature-id", "reliable-delivery", "--package-id", package_id,
            )

    def context(self, execution="exec-1"):
        return self.run_cli(
            "context-summary", "--feature-id", "reliable-delivery", "--action", "implementation",
            "--role", "implementation", "--execution-id", execution,
        )

    def handoff(self, role):
        prepared = self.run_cli(
            "prepare-handoff", "--feature-id", "reliable-delivery", "--action", "implementation",
            "--role", role,
        )
        self.assertTrue(prepared["handoff_ready"], prepared)
        command = prepared["context_command"]
        return self.run_cli(command["command"], *command["arguments"])

    def test_future_packages_do_not_block_implementation_review_or_repair(self):
        self.start()
        self.prepare_future("slice-2")
        self.prepare_future("slice-3", ready=True)
        view = self.handoff("implementation")
        self.assertEqual("implementation", view["delivery_view"]["current_stage"])
        self.assertEqual("exec-1", view["role_view"]["execution_id"])
        self.assertEqual("slice-1", view["role_view"]["baton"])
        wrong = self.context("wrong-execution")
        self.assertNotIn("role_view", wrong)
        self.assertIn("EXECUTION_ID_MISMATCH", [item["code"] for item in wrong["blockers"]])

        (self.workspace / "slice-1.txt").write_text("implementation\n", encoding="utf-8")
        self.record_checkpoint("exec-1")
        self.run_cli(
            "submit-slice", "--feature-id", "reliable-delivery", "--execution-id", "exec-1",
            "--status", "completed", "--candidate-file",
            write_candidate(self.feature / "05-implementation" / "candidate.json", "candidate-1"),
        )
        view = self.handoff("review")
        self.assertEqual("candidate-review", view["delivery_view"]["current_stage"])
        self.assertEqual("slice-1", view["role_view"]["baton"])
        self.run_cli(
            "review-slice", "--feature-id", "reliable-delivery", "--package-id", "slice-1",
            "--candidate-id", "candidate-1", "--review-execution-id", "review-1", "--result", "failed",
            "--issues-file", write_issues(self.feature / "05-implementation" / "issues.json", "边界需修正"),
        )
        status = self.run_cli("workflow-status", "--feature-id", "reliable-delivery")
        self.assertEqual("slice-1", status["delivery_view"]["next_action"]["package_id"])
        self.run_cli(
            "resolve-slice", "--feature-id", "reliable-delivery", "--package-id", "slice-1",
            "--action", "retry", "--execution-id", "exec-2",
        )
        repaired = self.handoff("implementation")
        self.assertEqual("exec-2", repaired["role_view"]["execution_id"])
        self.assertIn("repair_handoff", repaired["role_view"])

    def test_multiple_future_packages_still_require_selection_without_active_execution(self):
        self.prepare_future("slice-1", ready=True)
        self.prepare_future("slice-2")
        status = self.run_cli("workflow-status", "--feature-id", "reliable-delivery")
        delivery = status["delivery_view"]
        self.assertIsNone(delivery["next_action"])
        self.assertIn("AMBIGUOUS_CURRENT_SLICE", [item["code"] for item in delivery["blockers"]])

    def test_context_reads_archive_and_state_once_per_command(self):
        self.start()
        with mock.patch.object(
            archive_context, "validate_feature_archive", wraps=archive_context.validate_feature_archive,
        ) as context_validation, mock.patch.object(
            archive_approvals, "validate_feature_archive", wraps=archive_approvals.validate_feature_archive,
        ) as approval_validation, mock.patch.object(
            archive_context, "_load_state", wraps=archive_context._load_state,
        ) as context_state, mock.patch.object(
            archive_approvals, "_load_state", wraps=archive_approvals._load_state,
        ) as approval_state:
            self.assertIn("role_view", self.context())
            self.assertEqual(1, context_validation.call_count + approval_validation.call_count)
            self.assertEqual(1, context_state.call_count + approval_state.call_count)
            self.run_cli(
                "stage-action", "--feature-id", "reliable-delivery", "--stage", "requirements",
                "--decision", "reject",
            )
            context_validation.reset_mock()
            approval_validation.reset_mock()
            context_state.reset_mock()
            approval_state.reset_mock()
            rejected = self.context()
            self.assertNotIn("role_view", rejected)
            self.assertIn("REQUIREMENTS_APPROVAL_REQUIRED", [item["code"] for item in rejected["blockers"]])
            self.assertEqual(1, context_validation.call_count + approval_validation.call_count)
            self.assertEqual(1, context_state.call_count + approval_state.call_count)

    def test_checkpoint_reuses_snapshots_but_checks_each_new_command(self):
        self.start()
        with mock.patch.object(
            archive_slice_flow, "current_workspace_snapshot", wraps=archive_slice_flow.current_workspace_snapshot,
        ) as workspace_snapshot, mock.patch.object(
            archive_slice_flow, "current_workspace_guard_snapshot", wraps=archive_slice_flow.current_workspace_guard_snapshot,
        ) as guard_snapshot:
            first = self.record_checkpoint("exec-1")
            self.assertEqual(1, workspace_snapshot.call_count)
            self.assertEqual(1, guard_snapshot.call_count)
            repeated = self.record_checkpoint("exec-1")
            self.assertTrue(repeated["idempotent"])
            self.assertEqual(2, workspace_snapshot.call_count)
            self.assertEqual(2, guard_snapshot.call_count)
            (self.workspace / "slice-1.txt").write_text("new implementation\n", encoding="utf-8")
            changed = self.record_checkpoint("exec-1")
            self.assertNotEqual(first["checkpoint"]["checkpoint_id"], changed["checkpoint"]["checkpoint_id"])
            self.assertEqual(3, workspace_snapshot.call_count)
            self.assertEqual(3, guard_snapshot.call_count)
