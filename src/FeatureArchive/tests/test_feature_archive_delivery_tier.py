from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _feature_archive_support import FeatureArchiveCliTestCase


class FeatureArchiveDeliveryTierTests(FeatureArchiveCliTestCase):
    real_snapshots = False

    def init_lite(self, feature_id: str = "reliable-delivery") -> Path:
        result = self.run_cli(
            "init", "--feature-id", feature_id, "--title", "可靠交付", "--tier", "lite",
        )
        self.assertEqual("lite", result["delivery_tier"])
        return self.root / feature_id

    def status(self):
        return self.run_cli("workflow-status", "--feature-id", "reliable-delivery")["delivery_view"]

    def test_lite_delivery_freezes_without_investigation_design_or_plan(self):
        feature = self.init_lite()
        manifest = json.loads((feature / "feature.json").read_text(encoding="utf-8"))
        self.assertEqual("lite", manifest["delivery_tier"])
        self.assertIn("轻量交付不单独维护本环节", (feature / "03-design" / "README.md").read_text(encoding="utf-8"))

        self.approve_requirements()
        self.run_cli("transition-lifecycle", "--feature-id", "reliable-delivery", "--to", "active")
        view = self.status()
        self.assertEqual("lite", view["trusted_machine_facts"]["delivery_tier"])
        self.assertEqual("implementation-required", view["current_stage"])

        # 实施、候选提交与独立评审都在调查、设计和计划仍为草稿时完成。
        confirmation = self.accept_test_implementation()
        for category in ("02-investigation", "03-design", "04-plan"):
            self.assertIn("content_status: draft", (feature / category / "README.md").read_text(encoding="utf-8"))
        for category in ("05-implementation", "06-validation"):
            self.set_status(feature / category / "README.md", "completed")

        self.assertEqual({"command": "transition-lifecycle", "to": "validating"}, self.status()["next_action"])
        self.run_cli("transition-lifecycle", "--feature-id", "reliable-delivery", "--to", "validating")
        self.assertEqual("freeze-ready", self.status()["current_stage"])
        frozen = self.run_cli(
            "transition-lifecycle", "--feature-id", "reliable-delivery", "--to", "frozen",
            "--validation-conclusion", "passed", "--unverified-boundaries", "documented",
            "--residual-risks", "none", "--integration-confirmation", confirmation,
        )
        self.assertEqual("frozen", frozen["lifecycle"])

    def test_lite_keeps_requirements_independent_review_and_validation_gates(self):
        feature = self.init_lite()
        # 需求共识仍是第一道人工门禁。
        self.assertEqual("requirements", self.status()["current_stage"])
        self.approve_requirements()
        self.run_cli("transition-lifecycle", "--feature-id", "reliable-delivery", "--to", "active")
        self.accept_test_implementation()
        # 验证总览仍须完成，轻量只跳过调查、设计和计划。
        self.run_cli("transition-lifecycle", "--feature-id", "reliable-delivery", "--to", "validating")
        view = self.status()
        self.assertEqual("validation", view["current_stage"])
        blocked = self.run_cli(
            "transition-lifecycle", "--feature-id", "reliable-delivery", "--to", "frozen",
            "--validation-conclusion", "passed", "--unverified-boundaries", "documented",
            "--residual-risks", "none",
            "--integration-confirmation", feature / "06-validation" / "integration-confirmation.json",
            expected=1,
        )
        self.assertEqual("LIFECYCLE_GATE_BLOCKED", blocked["code"])
        reasons = {item["document_id"] for item in blocked["blockers"]}
        self.assertIn("reliable-delivery.validation.overview", reasons)
        self.assertFalse(any(".design." in item or ".plan." in item or ".investigation." in item for item in reasons))

    def test_standard_delivery_still_requires_design_and_plan(self):
        self.init_complex()
        self.approve_requirements()
        self.run_cli("transition-lifecycle", "--feature-id", "reliable-delivery", "--to", "active")
        view = self.status()
        self.assertNotIn("delivery_tier", view["trusted_machine_facts"])
        self.assertEqual("design", view["current_stage"])

    def test_lite_rejects_architecture_review_and_planning_actions(self):
        self.init_lite()
        self.approve_requirements()
        self.run_cli("transition-lifecycle", "--feature-id", "reliable-delivery", "--to", "active")
        rejected = self.run_cli(
            "stage-action", "--feature-id", "reliable-delivery",
            "--stage", "architecture", "--decision", "approve", expected=1,
        )
        self.assertEqual("LITE_TIER_STAGE_NOT_APPLICABLE", rejected["code"])
        summary = self.run_cli(
            "context-summary", "--feature-id", "reliable-delivery",
            "--action", "design", "--role", "coordinator",
        )
        self.assertEqual(["implementation", "validation"], summary["allowed_actions"])
        self.assertIn("ACTION_NOT_AVAILABLE", [item["code"] for item in summary["blockers"]])

    def test_lite_task_package_guidance_omits_design_entry(self):
        self.init_lite()
        self.approve_requirements()
        self.run_cli("transition-lifecycle", "--feature-id", "reliable-delivery", "--to", "active")
        prepared = self.run_cli(
            "prepare-action-input", "--feature-id", "reliable-delivery",
            "--input-kind", "task-package", "--package-id", "slice-one",
        )
        encoded = json.dumps(prepared, ensure_ascii=False)
        self.assertNotIn("reliable-delivery.design.overview", encoded)
        self.assertIn("reliable-delivery.requirements.overview", encoded)

    def test_tier_is_fixed_at_creation_and_excludes_configurations(self):
        self.init_lite()
        self.assertEqual("unchanged", self.run_cli(
            "init", "--feature-id", "reliable-delivery", "--title", "可靠交付", "--tier", "lite",
        )["status"])
        conflict = self.run_cli(
            "init", "--feature-id", "reliable-delivery", "--title", "可靠交付", expected=1,
        )
        self.assertEqual("DELIVERY_TIER_CONFLICT", conflict["code"])
        ui = self.run_cli(
            "init", "--feature-id", "ui-delivery", "--title", "界面交付",
            "--tier", "lite", "--configuration", "dloop-ui-v1", expected=1,
        )
        self.assertEqual("DELIVERY_TIER_CONFIGURATION_CONFLICT", ui["code"])
        self.assertFalse((self.root / "ui-delivery").exists())


class FeatureArchiveDeliveryTierForkTests(FeatureArchiveCliTestCase):
    def test_fork_keeps_the_source_tier(self):
        import archive_snapshots

        self.run_cli("init", "--feature-id", "lite-source", "--title", "轻量来源", "--tier", "lite")
        point = archive_snapshots.save_snapshot(self.root, "lite-source")["snapshot"]["id"]
        forked = self.run_cli(
            "snapshot-fork", "--source-feature-id", "lite-source", "--snapshot-id", point,
            "--feature-id", "lite-rework", "--title", "轻量重做",
        )
        self.assertEqual("lite", forked["delivery_tier"])
        manifest = json.loads((self.root / "lite-rework" / "feature.json").read_text(encoding="utf-8"))
        self.assertEqual("lite", manifest["delivery_tier"])
