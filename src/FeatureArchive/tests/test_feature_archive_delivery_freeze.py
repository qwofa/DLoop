from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _feature_archive_support import FeatureArchiveCliTestCase


class FeatureArchiveDeliveryFreezeTests(FeatureArchiveCliTestCase):
    real_snapshots = False

    def setUp(self):
        super().setUp()
        self.feature = self.init_complex()
        self.approve_requirements()
        self.prepare_execution_inputs()
        self.run_cli("transition-lifecycle", "--feature-id", "reliable-delivery", "--to", "active")
        self.confirmation = self.accept_test_implementation()
        for category in ("02-investigation", "05-implementation", "06-validation"):
            self.set_status(self.feature / category / "README.md", "completed")

    def status(self):
        return self.run_cli("workflow-status", "--feature-id", "reliable-delivery")["delivery_view"]

    def enter_validation(self):
        self.run_cli("transition-lifecycle", "--feature-id", "reliable-delivery", "--to", "validating")

    def freeze(self, *, conclusion="passed", confirmation=True, expected=0):
        arguments = [
            "transition-lifecycle", "--feature-id", "reliable-delivery", "--to", "frozen",
            "--validation-conclusion", conclusion, "--unverified-boundaries", "documented",
            "--residual-risks", "none",
        ]
        if confirmation:
            arguments.extend(("--integration-confirmation", self.confirmation))
        return self.run_cli(*arguments, expected=expected)

    def test_delivery_advances_and_freezes_without_creating_user_approval(self):
        view = self.status()
        self.assertEqual({"command": "transition-lifecycle", "to": "validating"}, view["next_action"])
        self.assertFalse(view["requires_human"]["required"])
        self.enter_validation()
        view = self.status()
        self.assertEqual("freeze-ready", view["current_stage"])
        self.assertEqual("incomplete", view["delivery_summary"]["completion_claim"])
        self.assertFalse(view["requires_human"]["required"])
        self.assertIn("integration_confirmation", view["next_action_contract"]["required_inputs"])
        handoff = self.run_cli(
            "prepare-handoff", "--feature-id", "reliable-delivery", "--action", "validation",
            "--role", "final-review", "--integration-confirmation", self.confirmation,
        )
        self.assertTrue(handoff["handoff_ready"])
        state_before = (self.feature / "workflow-state.json").read_bytes()
        frozen = self.freeze()
        self.assertEqual("frozen", frozen["lifecycle"])
        self.assertEqual("documented", frozen["validation"]["unverified_boundaries"])
        self.assertEqual(state_before, (self.feature / "workflow-state.json").read_bytes())
        summary = self.status()["delivery_summary"]
        self.assertEqual("pending", summary["acceptance"])
        self.assertEqual("incomplete", summary["completion_claim"])
        self.assertTrue(summary["archive_read_only"])
        shared = self.run_cli("export-share", "--feature-id", "reliable-delivery")
        status = json.loads((Path(shared["share_location"]["path"]) / "delivery-status.json").read_text(encoding="utf-8"))
        self.assertEqual("incomplete", status["delivery_status"]["delivery_summary"]["completion_claim"])

    def test_post_delivery_debug_does_not_reopen_or_invalidate_history(self):
        self.enter_validation()
        self.approve_final_with_confirmation()
        self.freeze(confirmation=False)
        before = self.status()
        files = {path: path.read_bytes() for path in self.feature.rglob("*") if path.is_file()}
        (self.workspace / "test-implementation.txt").write_text("用户交付后调试修改", encoding="utf-8")
        after = self.status()
        self.assertEqual(before, after)
        self.assertEqual("approve", after["delivery_summary"]["acceptance"])
        self.assertEqual("complete", after["delivery_summary"]["completion_claim"])
        self.assertEqual("frozen", after["current_stage"])
        self.assertEqual([], after["blockers"])
        self.assertIsNone(after["next_action"])
        self.assertFalse(after["requires_human"]["required"])
        shared = self.run_cli("export-share", "--feature-id", "reliable-delivery")
        status = json.loads((Path(shared["share_location"]["path"]) / "delivery-status.json").read_text(encoding="utf-8"))
        self.assertEqual("approve", status["delivery_status"]["delivery_summary"]["acceptance"])
        self.assertEqual("complete", status["delivery_status"]["delivery_summary"]["completion_claim"])
        self.assertEqual(files, {path: path.read_bytes() for path in self.feature.rglob("*") if path.is_file()})
        rejected = self.run_cli(
            "transition-lifecycle", "--feature-id", "reliable-delivery", "--to", "active", expected=1,
        )
        self.assertEqual("READ_ONLY_ARCHIVE", rejected["code"])

    def test_freeze_rejects_missing_failed_or_stale_integration_confirmation(self):
        self.enter_validation()
        before = (self.feature / "feature.json").read_bytes()
        self.assertEqual("LIFECYCLE_GATE_BLOCKED", self.freeze(confirmation=False, expected=1)["code"])
        for overrides in (
            {"result": "failed"},
            {"candidate_summary_digest": "sha256:stale"},
            {"workspace_guard_digest": "sha256:stale"},
        ):
            with self.subTest(overrides=overrides):
                self.write_integration_confirmation(**overrides)
                self.assertEqual("LIFECYCLE_GATE_BLOCKED", self.freeze(expected=1)["code"])
                self.assertEqual(before, (self.feature / "feature.json").read_bytes())
        self.write_integration_confirmation()
        self.freeze()

    def test_declared_but_unstarted_slice_blocks_delivery(self):
        self.ensure_approved_slice_plan("not-started")
        view = self.status()
        self.assertIsNone(view["next_action"])
        self.assertIn("SLICE_PLAN_INCOMPLETE", [item["code"] for item in view["blockers"]])
        self.enter_validation()
        view = self.status()
        self.assertEqual("validation-recovery", view["current_stage"])
        rejected = self.freeze(expected=1)
        self.assertTrue(any("not-started" in item["reason"] for item in rejected["blockers"]))

    def test_failed_validation_and_incomplete_documents_still_block_freeze(self):
        self.enter_validation()
        self.assertEqual("LIFECYCLE_GATE_BLOCKED", self.freeze(conclusion="failed", expected=1)["code"])
        document = self.feature / "02-investigation" / "README.md"
        original = document.read_text(encoding="utf-8")
        document.write_text(original.replace("content_status: completed", "content_status: draft"), encoding="utf-8")
        rejected = self.freeze(expected=1)
        self.assertTrue(any("content_status=draft" in item["reason"] for item in rejected["blockers"]))
        document.write_text(original, encoding="utf-8")
        self.freeze()

    def test_user_rejection_blocks_automatic_freeze(self):
        self.enter_validation()
        self.run_cli("stage-action", "--feature-id", "reliable-delivery", "--stage", "final", "--decision", "reject")
        view = self.status()
        self.assertEqual("validation-recovery", view["current_stage"])
        self.assertIsNone(view["next_action"])
        rejected = self.freeze(expected=1)
        self.assertTrue(any("用户已退回" in item["reason"] for item in rejected["blockers"]))
