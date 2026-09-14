from __future__ import annotations

import base64
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _feature_archive_support import FeatureArchiveCliTestCase, write_candidate


class FeatureArchiveStatusOutputTests(FeatureArchiveCliTestCase):
    def setUp(self):
        super().setUp()
        self.feature = self.init_complex()
        self.approve_requirements()
        self.prepare_execution_inputs()
        self.run_cli("transition-lifecycle", "--feature-id", "reliable-delivery", "--to", "active")

    def start(self):
        self.original = ("需要保留的业务内容。\n" * 2048).encode("utf-8")
        self.target = self.workspace / "slice-1.txt"
        self.target.write_bytes(self.original)
        self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery", "--execution-id", "exec-1",
            "--package-file", self.write_package("slice-1"), "--workspace-root", self.workspace,
        )

    def state_bytes(self):
        return (
            (self.feature / "workflow-state.json").read_bytes(),
            (self.root / ".feature-archive-workspace-state.json").read_bytes(),
        )

    def status_pair(self):
        before = self.state_bytes()
        arguments = ("workflow-status", "--feature-id", "reliable-delivery")
        compact = self.run_cli(*arguments)
        expanded = self.run_cli(*arguments, "--include-details")
        self.assertEqual(compact, {key: value for key, value in expanded.items() if key != "details"})
        self.assertEqual(before, self.state_bytes())
        self.assertEqual(json.loads(before[0]), expanded["details"]["workflow_state"])
        self.assertEqual(
            json.loads(before[1])["modification_lease"], expanded["details"]["modification_lease"],
        )
        return compact, expanded

    def test_global_status_exposes_identity_without_recovery_content(self):
        self.assertIsNone(self.run_cli("workflow-status")["modification_lease"])
        self.start()
        before = self.state_bytes()
        compact = self.run_cli("workflow-status")
        expanded = self.run_cli("workflow-status", "--include-details")
        self.assertEqual(compact, {key: value for key, value in expanded.items() if key != "details"})
        self.assertEqual(
            {"feature_id", "package_id", "workspace_root", "status", "holder_execution_id", "current_candidate_id"},
            set(compact["modification_lease"]),
        )
        self.assertEqual("exec-1", compact["modification_lease"]["holder_execution_id"])
        self.assertEqual("reliable-delivery", compact["modification_lease"]["feature_id"])
        self.assertIn("baseline_content_snapshot", expanded["details"]["modification_lease"])
        self.assertEqual(before, self.state_bytes())

    def test_feature_status_keeps_next_action_and_does_not_damage_recovery(self):
        self.start()
        compact, expanded = self.status_pair()
        for field in ("approvals", "slices", "slice_plan", "modification_lease", "final_blockers", "details"):
            self.assertNotIn(field, compact)
        coordinator = self.run_cli(
            "context-summary", "--feature-id", "reliable-delivery",
            "--action", "implementation", "--role", "coordinator",
        )
        self.assertEqual(coordinator["delivery_view"], compact["delivery_view"])
        self.assertEqual("checkpoint-slice", compact["delivery_view"]["next_action_contract"]["command"])
        serialized = json.dumps(compact, ensure_ascii=False)
        self.assertNotIn("baseline_content_snapshot", serialized)
        self.assertNotIn(base64.b64encode(self.original).decode("ascii"), serialized)
        self.assertLess(len(serialized), len(json.dumps(expanded, ensure_ascii=False)) / 4)

        self.target.write_text("本次尚未交付的修改", encoding="utf-8")
        self.run_cli(
            "submit-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-1", "--status", "interrupted",
        )
        self.assertEqual(self.original, self.target.read_bytes())
        released, _ = self.status_pair()
        self.assertIsNone(released["delivery_view"]["trusted_machine_facts"]["modification_lease"])

    def test_candidate_drift_blocks_both_output_modes(self):
        self.start()
        self.target.write_text("提交的候选内容", encoding="utf-8")
        self.record_checkpoint("exec-1")
        self.run_cli(
            "submit-slice", "--feature-id", "reliable-delivery", "--execution-id", "exec-1",
            "--status", "completed", "--candidate-file",
            write_candidate(self.feature / "05-implementation" / "candidate.json", "candidate-1"),
        )
        ready, _ = self.status_pair()
        self.assertEqual("review-slice", ready["delivery_view"]["next_action_contract"]["command"])
        self.assertEqual(
            "candidate-1", ready["delivery_view"]["trusted_machine_facts"]["modification_lease"]["current_candidate_id"],
        )
        self.target.write_text("候选形成后发生的变化", encoding="utf-8")
        blocked, _ = self.status_pair()
        self.assertEqual("implementation-recovery", blocked["delivery_view"]["current_stage"])
        self.assertIsNone(blocked["delivery_view"]["next_action_contract"])
        self.assertTrue(blocked["delivery_view"]["requires_human"]["required"])

    def test_other_delivery_lease_blocks_final_approval_and_freeze_until_released(self):
        confirmation = self.accept_test_implementation()
        self.set_status(self.feature / "02-investigation" / "README.md", "completed")
        self.set_status(self.feature / "05-implementation" / "README.md", "completed")
        self.set_status(self.feature / "06-validation" / "README.md", "completed")
        self.run_cli("transition-lifecycle", "--feature-id", "reliable-delivery", "--to", "validating")
        self.init_complex("other-delivery")
        self.approve_requirements("other-delivery")
        self.prepare_execution_inputs("other-delivery")
        self.run_cli("transition-lifecycle", "--feature-id", "other-delivery", "--to", "active")
        package = self.write_package("other-slice", feature_id="other-delivery")

        for approved in (False, True):
            with self.subTest(final_approved=approved):
                if approved:
                    self.approve_final_with_confirmation()
                expected_stage = "freeze-ready"
                ready, _ = self.status_pair()
                self.assertEqual(expected_stage, ready["delivery_view"]["current_stage"])
                execution_id = f"other-exec-{approved}"
                if approved:
                    self.run_cli(
                        "resolve-slice", "--feature-id", "other-delivery", "--package-id", "other-slice",
                        "--action", "retry", "--execution-id", execution_id,
                    )
                else:
                    self.run_cli(
                        "start-slice", "--feature-id", "other-delivery", "--execution-id", execution_id,
                        "--package-file", package, "--workspace-root", self.workspace,
                    )
                blocked, expanded = self.status_pair()
                delivery = blocked["delivery_view"]
                self.assertEqual("validation-recovery", delivery["current_stage"])
                self.assertEqual(expanded["details"]["final_blockers"], delivery["blockers"])
                self.assertEqual(["MODIFICATION_LEASE_HELD"], [item["code"] for item in delivery["blockers"]])
                self.assertFalse(delivery["can_advance"])
                self.assertFalse(delivery["requires_human"]["required"])
                self.assertIsNone(delivery["next_action"])
                self.assertIsNone(delivery["next_action_contract"])
                coordinator = self.run_cli(
                    "context-summary", "--feature-id", "reliable-delivery",
                    "--action", "validation", "--role", "coordinator",
                )
                self.assertEqual(delivery, coordinator["delivery_view"])
                if not approved:
                    handoff = self.run_cli(
                        "prepare-handoff", "--feature-id", "reliable-delivery", "--action", "validation",
                        "--role", "final-review", "--integration-confirmation", confirmation,
                    )
                    self.assertFalse(handoff["handoff_ready"])
                    denied = self.approve_final_with_confirmation(expected=1)
                    self.assertEqual("FINAL_EXECUTION_BLOCKED", denied["code"])
                else:
                    denied = self.run_cli(
                        "transition-lifecycle", "--feature-id", "reliable-delivery", "--to", "frozen",
                        "--validation-conclusion", "passed", "--unverified-boundaries", "none",
                        "--residual-risks", "none",
                        expected=1,
                    )
                    self.assertTrue(any("工作区修改租约尚未释放" in item["reason"] for item in denied["blockers"]))
                self.run_cli(
                    "submit-slice", "--feature-id", "other-delivery",
                    "--execution-id", execution_id, "--status", "interrupted",
                )
                restored, _ = self.status_pair()
                self.assertEqual(expected_stage, restored["delivery_view"]["current_stage"])
                self.assertIsNotNone(restored["delivery_view"]["next_action"])
        self.run_cli(
            "transition-lifecycle", "--feature-id", "reliable-delivery", "--to", "frozen",
            "--validation-conclusion", "passed", "--unverified-boundaries", "none",
            "--residual-risks", "none",
        )
