from __future__ import annotations

import json
from pathlib import Path
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _feature_archive_support import FeatureArchiveCliTestCase, write_candidate, write_issues
import archive_slice_flow


class FeatureArchiveValidationSupplementTests(FeatureArchiveCliTestCase):
    def setUp(self):
        super().setUp()
        self.feature = self.init_complex()
        self.approve_requirements()
        self.prepare_execution_inputs()
        self.run_cli("transition-lifecycle", "--feature-id", "reliable-delivery", "--to", "active")
        self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery", "--execution-id", "exec-1",
            "--package-file", self.write_package("slice-1"), "--workspace-root", self.workspace,
        )
        self.product = self.workspace / "slice-1.txt"
        self.product.write_text("implemented result\n", encoding="utf-8")

    def supplement(self, execution="exec-1", level="module_full", expected=0):
        return self.run_cli(
            "supplement-slice-validation", "--feature-id", "reliable-delivery",
            "--execution-id", execution, "--validation-level", level,
            "--validation-method", "module regression", "--rationale", "shared caller impact discovered",
            expected=expected,
        )

    def state(self):
        return json.loads((self.feature / "workflow-state.json").read_text(encoding="utf-8"))

    def submit(self, execution="exec-1", candidate="candidate-1", expected=0):
        return self.run_cli(
            "submit-slice", "--feature-id", "reliable-delivery", "--execution-id", execution,
            "--status", "completed", "--candidate-file",
            write_candidate(self.feature / "05-implementation" / f"{candidate}.json", candidate),
            expected=expected,
        )

    def test_supplement_preserves_results_and_requires_current_complete_checkpoint(self):
        self.record_checkpoint("exec-1")
        draft = self.run_cli("prepare-action-input", "--feature-id", "reliable-delivery",
                             "--input-kind", "checkpoint", "--execution-id", "exec-1")
        draft_before = Path(draft["target"]).read_bytes()
        lease = self.root / ".feature-archive-workspace-state.json"
        lease_before = lease.read_bytes()
        before = self.state()["execution"]["slices"]["slice-1"]
        result = self.supplement()
        self.assertFalse(result["idempotent"])
        after = self.state()["execution"]["slices"]["slice-1"]
        for field in ("execution_id", "workspace_root", "status", "checkpoints", "review_snapshots", "slice_plan_binding"):
            self.assertEqual(before.get(field), after.get(field))
        self.assertEqual(lease_before, lease.read_bytes())
        self.assertEqual("implemented result\n", self.product.read_text(encoding="utf-8"))
        self.assertEqual(before["contract_history"], after["contract_history"][:-1])
        self.assertEqual("FINAL_CHECKPOINT_STALE", self.submit(expected=1)["code"])
        refreshed = self.run_cli("prepare-action-input", "--feature-id", "reliable-delivery",
                                 "--input-kind", "checkpoint", "--execution-id", "exec-1")
        self.assertNotEqual(draft["target"], refreshed["target"])
        self.assertEqual(draft_before, Path(draft["target"]).read_bytes())
        self.assertIn("module regression", [item["method"] for item in refreshed["template"]["validation_results"]])
        state_before = (self.feature / "workflow-state.json").read_bytes()
        self.assertTrue(self.supplement()["idempotent"])
        self.assertEqual(state_before, (self.feature / "workflow-state.json").read_bytes())
        self.record_checkpoint("exec-1", identifier="strengthened")
        self.submit()
        self.run_cli("review-slice", "--feature-id", "reliable-delivery", "--package-id", "slice-1",
                     "--candidate-id", "candidate-1", "--review-execution-id", "review-1", "--result", "passed")
        self.run_cli("validate")

    def test_rejected_updates_do_not_change_state_or_results(self):
        self.supplement()
        state_path = self.feature / "workflow-state.json"
        before = state_path.read_bytes()
        self.assertEqual("VALIDATION_DOWNGRADE_FORBIDDEN", self.supplement(level="targeted", expected=1)["code"])
        self.assertEqual("EXECUTION_NOT_ACTIVE", self.supplement(execution="wrong-id", expected=1)["code"])
        self.assertEqual(before, state_path.read_bytes())
        self.record_checkpoint("exec-1")
        self.submit()
        before = state_path.read_bytes()
        self.assertEqual("EXECUTION_NOT_ACTIVE", self.supplement(expected=1)["code"])
        self.assertEqual(before, state_path.read_bytes())

    def test_level_only_supplement_rejects_old_checkpoint_input(self):
        self.record_checkpoint("exec-1", identifier="original")
        self.run_cli(
            "supplement-slice-validation", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-1", "--validation-level", "module_full", "--rationale", "broader risk",
        )
        before = (self.feature / "workflow-state.json").read_bytes()
        rejected = self.run_cli(
            "checkpoint-slice", "--feature-id", "reliable-delivery", "--execution-id", "exec-1",
            "--checkpoint-file", self.feature / "05-implementation" / "exec-1-original.json", expected=1,
        )
        self.assertEqual("CHECKPOINT_CONTRACT_STALE", rejected["code"])
        self.assertEqual(before, (self.feature / "workflow-state.json").read_bytes())
        self.record_checkpoint("exec-1", identifier="current")
        self.submit()

    def test_write_failure_preserves_original_contract_and_lease(self):
        state_path = self.feature / "workflow-state.json"
        lease_path = self.root / ".feature-archive-workspace-state.json"
        before = (state_path.read_bytes(), lease_path.read_bytes())
        with mock.patch.object(archive_slice_flow, "_write_state", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                archive_slice_flow.supplement_slice_validation(
                    self.root, "reliable-delivery", "exec-1", "module_full", ["module regression"], "new impact",
                )
        self.assertEqual(before, (state_path.read_bytes(), lease_path.read_bytes()))

    def test_repair_can_add_validation_without_changing_original_review(self):
        self.record_checkpoint("exec-1")
        self.submit()
        self.run_cli("review-slice", "--feature-id", "reliable-delivery", "--package-id", "slice-1",
                     "--candidate-id", "candidate-1", "--review-execution-id", "review-1", "--result", "failed",
                     "--issues-file", write_issues(self.feature / "05-implementation" / "issues.json", "caller fails"))
        original = self.state()["execution"]["slices"]["slice-1"]["review_snapshots"]
        self.run_cli("resolve-slice", "--feature-id", "reliable-delivery", "--action", "retry",
                     "--package-id", "slice-1", "--execution-id", "exec-2")
        self.supplement(execution="exec-2")
        self.assertEqual(original, self.state()["execution"]["slices"]["slice-1"]["review_snapshots"])
        view = self.run_cli("context-summary", "--feature-id", "reliable-delivery", "--action", "implementation",
                            "--role", "implementation", "--execution-id", "exec-2")
        self.assertIn("role_view", view)
        self.record_checkpoint("exec-2")
        self.submit(execution="exec-2", candidate="candidate-2")
        self.run_cli("review-slice", "--feature-id", "reliable-delivery", "--package-id", "slice-1",
                     "--candidate-id", "candidate-2", "--review-execution-id", "review-2", "--result", "passed")
        self.run_cli("validate")
