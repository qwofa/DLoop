from __future__ import annotations

import json
from pathlib import Path

try:
    from _feature_archive_support import FeatureArchiveCliTestCase, write_candidate
except ModuleNotFoundError:
    from ._feature_archive_support import FeatureArchiveCliTestCase, write_candidate


class FeatureArchiveApprovalTests(FeatureArchiveCliTestCase):
    def accept_modification(self) -> None:
        package = self.write_package("slice-1")
        self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-1", "--package-file", package,
            "--workspace-root", self.workspace,
        )
        self.workspace.joinpath("slice-1.txt").write_text("candidate-1", encoding="utf-8")
        self.record_checkpoint("exec-1")
        candidate = write_candidate(
            self.artifact_path("reliable-delivery", "05-implementation", "candidate.json"),
            "candidate-1",
        )
        self.run_cli(
            "submit-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-1", "--status", "completed",
            "--candidate-file", candidate,
        )
        self.run_cli(
            "review-slice", "--feature-id", "reliable-delivery",
            "--package-id", "slice-1", "--candidate-id", "candidate-1",
            "--review-execution-id", "review-1", "--result", "passed",
        )

    def test_requirements_can_be_approved_without_blind_or_content_review(self) -> None:
        self.init_complex()
        self.approve_requirements()
        status = self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery"
        )
        self.assertEqual("approve", status["delivery_view"]["conclusions"]["approvals"]["requirements"]["status"])
        help_result = self.run_help("stage-action")
        self.assertNotIn("decision-review", help_result)
        self.assertNotIn("content-review", help_result)

    def test_requirement_body_change_invalidates_approval(self) -> None:
        archive = self.init_complex()
        self.approve_requirements()
        overview = archive / "01-requirements" / "README.md"
        overview.write_text(overview.read_text(encoding="utf-8") + "\n新目标。\n", encoding="utf-8")
        result = self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery", expected=1
        )
        self.assertEqual("UNCONFIRMED_EDIT", result["code"])

    def test_stage_approvals_bind_exact_entry_snapshots(self) -> None:
        archive = self.init_complex()
        self.set_status(archive / "01-requirements" / "terminology.md", "confirmed")
        self.set_status(archive / "01-requirements" / "README.md", "confirmed")
        requirements = self.run_cli(
            "stage-action", "--feature-id", "reliable-delivery",
            "--stage", "requirements", "--decision", "approve",
        )
        self.set_status(archive / "03-design" / "README.md", "confirmed")
        architecture = self.run_cli(
            "stage-action", "--feature-id", "reliable-delivery",
            "--stage", "architecture", "--decision", "approve",
        )
        self.prepare_execution_inputs()
        self.accept_modification()
        self.set_status(archive / "06-validation" / "README.md", "completed")
        final = self.approve_final_with_confirmation()

        for result, document_id in (
            (requirements, "reliable-delivery.requirements.overview"),
            (architecture, "reliable-delivery.design.overview"),
            (final, "reliable-delivery.validation.overview"),
        ):
            self.assertEqual(
                {"document_id", "semantic_version", "content_fingerprint"},
                set(result["snapshot"]),
            )
            self.assertEqual(document_id, result["snapshot"]["document_id"])
            self.assertRegex(result["snapshot"]["semantic_version"], r"^\d+\.\d+\.\d+$")
            self.assertRegex(result["snapshot"]["content_fingerprint"], r"^sha256:[0-9a-f]{64}$")

    def test_version_only_changes_and_design_readiness_stale_approvals(self) -> None:
        requirements_archive = self.init_complex()
        self.approve_requirements()
        requirements = requirements_archive / "01-requirements" / "README.md"
        requirements.write_text(
            requirements.read_text(encoding="utf-8").replace(
                "semantic_version: 0.1.0",
                "semantic_version: 0.1.1",
            ),
            encoding="utf-8",
        )
        status = self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery"
        )
        self.assertEqual("stale", status["delivery_view"]["conclusions"]["approvals"]["requirements"]["status"])

        version_archive = self.init_complex("version-feature")
        self.approve_requirements("version-feature")
        version_design = version_archive / "03-design" / "README.md"
        self.set_status(version_design, "confirmed")
        self.run_cli(
            "stage-action", "--feature-id", "version-feature",
            "--stage", "architecture", "--decision", "approve",
        )
        version_design.write_text(
            version_design.read_text(encoding="utf-8").replace(
                "semantic_version: 0.1.0",
                "semantic_version: 0.1.1",
            ),
            encoding="utf-8",
        )
        status = self.run_cli(
            "workflow-status", "--feature-id", "version-feature"
        )
        self.assertEqual("stale", status["delivery_view"]["conclusions"]["approvals"]["architecture"]["status"])

        status_archive = self.init_complex("status-feature")
        self.approve_requirements("status-feature")
        status_design = status_archive / "03-design" / "README.md"
        self.set_status(status_design, "confirmed")
        self.run_cli(
            "stage-action", "--feature-id", "status-feature",
            "--stage", "architecture", "--decision", "approve",
        )
        status_design.write_text(
            status_design.read_text(encoding="utf-8").replace(
                "content_status: confirmed",
                "content_status: draft",
            ),
            encoding="utf-8",
        )
        result = self.run_cli(
            "workflow-status", "--feature-id", "status-feature", expected=1,
        )
        self.assertEqual("APPROVAL_DOCUMENT_NOT_READY", result["code"])

    def test_requirement_terminology_and_ready_status_remain_approval_inputs(self) -> None:
        archive = self.init_complex()
        self.approve_requirements()
        terminology = archive / "01-requirements" / "terminology.md"
        terminology.write_text(
            terminology.read_text(encoding="utf-8").replace(
                "只定义当前交付项需要跨环节保持一致的业务概念或职责，不承载状态矩阵、触发条件或实现方案。",
                "只定义当前交付项需要跨环节保持一致的业务概念、职责或验收，不承载状态矩阵、触发条件或实现方案。",
            ),
            encoding="utf-8",
        )
        self.run_cli(
            "confirm-change", "--document-id", "reliable-delivery.requirements.terminology",
            "--semantic-change", "true",
        )
        result = self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery", expected=1,
        )
        self.assertIn(result["code"], {"APPROVAL_DOCUMENT_NOT_READY", "STALE_APPROVAL_INPUT"})

        second = self.init_complex("second-feature")
        self.approve_requirements("second-feature")
        second_requirements = second / "01-requirements" / "README.md"
        second_requirements.write_text(
            second_requirements.read_text(encoding="utf-8").replace(
                "content_status: confirmed",
                "content_status: draft",
            ),
            encoding="utf-8",
        )
        result = self.run_cli(
            "workflow-status", "--feature-id", "second-feature", expected=1,
        )
        self.assertEqual("APPROVAL_DOCUMENT_NOT_READY", result["code"])

    def test_architecture_confirmation_is_optional_for_slice_start(self) -> None:
        self.init_complex()
        self.approve_requirements()
        self.prepare_execution_inputs()
        package = self.write_package("slice-1")
        result = self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-1", "--package-file", package, "--workspace-root", self.workspace,
        )
        self.assertEqual("active", result["status"])

    def test_slice_start_requires_confirmed_design_and_completed_plan(self) -> None:
        self.init_complex()
        self.approve_requirements()
        package = self.write_package("slice-1")
        blocked = self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-1", "--package-file", package,
            "--workspace-root", self.workspace, expected=1,
        )
        self.assertEqual("EXECUTION_INPUT_NOT_READY", blocked["code"])
        self.prepare_execution_inputs()
        started = self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-1", "--package-file", package,
            "--workspace-root", self.workspace,
        )
        self.assertEqual("active", started["status"])

    def test_architecture_decision_can_still_be_recorded_when_needed(self) -> None:
        archive = self.init_complex()
        self.approve_requirements()
        self.set_status(archive / "03-design" / "README.md", "confirmed")
        result = self.run_cli(
            "stage-action", "--feature-id", "reliable-delivery",
            "--stage", "architecture", "--decision", "approve",
        )
        self.assertEqual("approved", result["status"])

    def test_rejected_architecture_blocks_slice_and_final_approval(self) -> None:
        self.init_complex()
        self.approve_requirements()
        archive = self.root / "reliable-delivery"
        self.set_status(archive / "03-design" / "README.md", "confirmed")
        self.run_cli(
            "stage-action", "--feature-id", "reliable-delivery",
            "--stage", "architecture", "--decision", "reject",
        )
        package = self.write_package("slice-1")
        started = self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-1", "--package-file", package,
            "--workspace-root", self.workspace, expected=1,
        )
        self.assertEqual("ARCHITECTURE_APPROVAL_BLOCKED", started["code"])

        self.set_status(archive / "06-validation" / "README.md", "completed")
        final = self.run_cli(
            "stage-action", "--feature-id", "reliable-delivery",
            "--stage", "final", "--decision", "approve", expected=1,
        )
        self.assertEqual("ARCHITECTURE_APPROVAL_BLOCKED", final["code"])

    def test_stale_architecture_approval_blocks_new_slice(self) -> None:
        self.init_complex()
        self.approve_requirements()
        archive = self.root / "reliable-delivery"
        design = archive / "03-design" / "README.md"
        self.set_status(design, "confirmed")
        self.run_cli(
            "stage-action", "--feature-id", "reliable-delivery",
            "--stage", "architecture", "--decision", "approve",
        )
        design.write_text(design.read_text(encoding="utf-8") + "\n边界调整。\n", encoding="utf-8")
        self.run_cli(
            "confirm-change", "--document-id", "reliable-delivery.design.overview",
            "--semantic-change", "false",
        )
        package = self.write_package("slice-1")
        result = self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-1", "--package-file", package,
            "--workspace-root", self.workspace, expected=1,
        )
        self.assertEqual("ARCHITECTURE_APPROVAL_BLOCKED", result["code"])

    def test_stale_requirements_approval_blocks_final_approval(self) -> None:
        archive = self.init_complex()
        self.approve_requirements()
        self.prepare_execution_inputs()
        self.accept_modification()
        self.set_status(archive / "06-validation" / "README.md", "completed")
        requirements = archive / "01-requirements" / "README.md"
        requirements.write_text(
            requirements.read_text(encoding="utf-8") + "\n措辞调整。\n",
            encoding="utf-8",
        )
        self.run_cli(
            "confirm-change", "--document-id", "reliable-delivery.requirements.overview",
            "--semantic-change", "false",
        )
        result = self.run_cli(
            "stage-action", "--feature-id", "reliable-delivery",
            "--stage", "final", "--decision", "approve", expected=1,
        )
        self.assertEqual("REQUIREMENTS_APPROVAL_REQUIRED", result["code"])

    def test_requirements_change_after_final_approval_blocks_freeze(self) -> None:
        archive = self.init_complex()
        self.approve_requirements()
        self.prepare_execution_inputs()
        self.accept_modification()
        for category in archive.glob("??-*"):
            for document in category.glob("*.md"):
                self.set_status(document, "completed")
        self.run_cli(
            "transition-lifecycle", "--feature-id", "reliable-delivery", "--to", "active",
        )
        self.run_cli(
            "transition-lifecycle", "--feature-id", "reliable-delivery", "--to", "validating",
        )
        self.approve_final_with_confirmation()
        requirements = archive / "01-requirements" / "README.md"
        requirements.write_text(
            requirements.read_text(encoding="utf-8") + "\n最终验收后调整措辞。\n",
            encoding="utf-8",
        )
        self.run_cli(
            "confirm-change", "--document-id", "reliable-delivery.requirements.overview",
            "--semantic-change", "false",
        )
        status = self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery"
        )
        self.assertEqual("stale", status["delivery_view"]["conclusions"]["approvals"]["requirements"]["status"])
        self.assertEqual("stale", status["delivery_view"]["conclusions"]["approvals"]["final"]["status"])
        result = self.run_cli(
            "transition-lifecycle", "--feature-id", "reliable-delivery", "--to", "frozen",
            "--validation-conclusion", "passed", "--unverified-boundaries", "none",
            "--residual-risks", "none", expected=1,
        )
        self.assertEqual("LIFECYCLE_GATE_BLOCKED", result["code"])

    def test_new_slice_after_final_approval_invalidates_final_approval(self) -> None:
        archive = self.init_complex()
        self.approve_requirements()
        self.prepare_execution_inputs()
        self.accept_modification()
        self.set_status(archive / "06-validation" / "README.md", "completed")
        self.approve_final_with_confirmation()

        package = self.write_package("slice-after-final")
        self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-after-final", "--package-file", package,
            "--workspace-root", self.workspace,
        )
        status = self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery"
        )

        self.assertEqual("pending", status["delivery_view"]["conclusions"]["approvals"]["final"]["status"])

    def test_other_feature_lease_in_same_workspace_blocks_final_approval(self) -> None:
        archive = self.init_complex()
        self.approve_requirements()
        self.prepare_execution_inputs()
        self.accept_modification()
        self.set_status(archive / "06-validation" / "README.md", "completed")

        self.init_complex("second-feature")
        self.approve_requirements("second-feature")
        self.prepare_execution_inputs("second-feature")
        package = self.write_package("second-slice", feature_id="second-feature")
        package_value = json.loads(package.read_text(encoding="utf-8"))
        package_value["context_materials"][0]["source"] = (
            "second-feature.requirements.overview"
        )
        package.write_text(
            json.dumps(package_value, ensure_ascii=False),
            encoding="utf-8",
        )
        self.run_cli(
            "start-slice", "--feature-id", "second-feature",
            "--execution-id", "second-exec", "--package-file", package,
            "--workspace-root", self.workspace,
        )

        result = self.run_cli(
            "stage-action", "--feature-id", "reliable-delivery",
            "--stage", "final", "--decision", "approve", expected=1,
        )
        self.assertEqual("FINAL_EXECUTION_BLOCKED", result["code"])

    def test_zero_slice_delivery_cannot_receive_final_approval(self) -> None:
        archive = self.init_complex()
        self.approve_requirements()
        self.prepare_execution_inputs()
        self.set_status(archive / "05-implementation" / "README.md", "completed")
        self.set_status(archive / "06-validation" / "README.md", "completed")

        result = self.run_cli(
            "stage-action", "--feature-id", "reliable-delivery",
            "--stage", "final", "--decision", "approve", expected=1,
        )

        self.assertEqual("ACCEPTED_IMPLEMENTATION_REQUIRED", result["code"])

    def test_zero_slice_delivery_cannot_use_implementation_record_as_candidate(self) -> None:
        archive = self.init_complex()
        self.approve_requirements()
        self.prepare_execution_inputs()
        self.set_status(archive / "05-implementation" / "README.md", "confirmed")
        self.set_status(archive / "06-validation" / "README.md", "completed")

        result = self.run_cli(
            "stage-action", "--feature-id", "reliable-delivery",
            "--stage", "final", "--decision", "approve", expected=1,
        )

        self.assertEqual("ACCEPTED_IMPLEMENTATION_REQUIRED", result["code"])

    def test_workspace_change_after_final_approval_makes_approval_stale(self) -> None:
        archive = self.init_complex()
        self.approve_requirements()
        self.prepare_execution_inputs()
        self.accept_modification()
        self.set_status(archive / "06-validation" / "README.md", "completed")
        self.approve_final_with_confirmation()

        self.workspace.joinpath("outside-after-approval.txt").write_text("changed", encoding="utf-8")
        status = self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery"
        )

        self.assertEqual("stale", status["delivery_view"]["conclusions"]["approvals"]["final"]["status"])

    def test_final_approval_records_accepted_workspace_snapshot(self) -> None:
        archive = self.init_complex()
        self.approve_requirements()
        self.prepare_execution_inputs()
        self.accept_modification()
        self.set_status(archive / "06-validation" / "README.md", "completed")

        result = self.approve_final_with_confirmation()

        self.assertIn("workspace_snapshot", result)
        self.assertEqual(
            str(self.workspace.resolve()),
            result["workspace_snapshot"]["workspace_root"],
        )
        self.assertRegex(
            result["workspace_snapshot"]["digest"],
            r"^sha256:[0-9a-f]{64}$",
        )

    def test_final_approval_binds_accepted_candidate_and_review_summary(self) -> None:
        archive = self.init_complex()
        self.approve_requirements()
        self.prepare_execution_inputs()
        self.accept_modification()
        self.set_status(archive / "06-validation" / "README.md", "completed")

        result = self.approve_final_with_confirmation()

        summary = result["candidate_summary"]
        self.assertRegex(summary["digest"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(
            [{
                "package_id": "slice-1",
                "candidate_id": "candidate-1",
                "candidate_digest": summary["items"][0]["candidate_digest"],
                "review_execution_id": "review-1",
                "review_result": "passed",
            }],
            summary["items"],
        )

    def test_review_verification_details_do_not_enter_candidate_summary(self) -> None:
        archive = self.init_complex()
        self.approve_requirements()
        self.prepare_execution_inputs()
        self.accept_modification()
        state_path = archive / "workflow-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        review = state["execution"]["slices"]["slice-1"]["review_snapshots"][-1]["review"]
        self.set_status(archive / "06-validation" / "README.md", "completed")
        summary = self.approve_final_with_confirmation()["candidate_summary"]

        self.assertEqual([], review["verification"])
        self.assertNotIn("review_snapshot_id", summary["items"][0])
        self.assertNotIn("review_snapshot_digest", summary["items"][0])

    def test_final_approval_requires_integration_confirmation_for_accepted_candidate(self) -> None:
        archive = self.init_complex()
        self.approve_requirements()
        self.prepare_execution_inputs()
        self.accept_modification()
        self.set_status(archive / "06-validation" / "README.md", "completed")

        result = self.run_cli(
            "stage-action", "--feature-id", "reliable-delivery",
            "--stage", "final", "--decision", "approve", expected=1,
        )

        self.assertEqual("INTEGRATION_CONFIRMATION_REQUIRED", result["code"])

    def test_final_approval_keeps_the_integration_confirmation_specific_path_error(self) -> None:
        archive = self.init_complex()
        self.approve_requirements()
        self.prepare_execution_inputs()
        self.accept_modification()
        self.set_status(archive / "06-validation" / "README.md", "completed")
        canonical = self.write_integration_confirmation()
        outside = self.base / "outside-integration-confirmation.json"
        outside.write_bytes(canonical.read_bytes())

        rejected = self.run_cli(
            "stage-action", "--feature-id", "reliable-delivery",
            "--stage", "final", "--decision", "approve",
            "--integration-confirmation", outside, expected=1,
        )

        self.assertEqual("INVALID_INTEGRATION_CONFIRMATION", rejected["code"])

    def test_shared_integration_confirmation_keeps_the_specific_path_error(self) -> None:
        archive = self.init_complex()
        self.approve_requirements()
        self.prepare_execution_inputs()
        self.accept_modification()
        self.set_status(archive / "06-validation" / "README.md", "completed")
        exported = self.run_cli(
            "export-share",
            "--feature-id",
            "reliable-delivery",
        )
        shared_validation = (
            Path(exported["share_location"]["path"])
            / "06-validation"
            / "README.md"
        )

        rejected = self.run_cli(
            "stage-action",
            "--feature-id",
            "reliable-delivery",
            "--stage",
            "final",
            "--decision",
            "approve",
            "--integration-confirmation",
            shared_validation,
            expected=1,
        )

        self.assertEqual("INVALID_INTEGRATION_CONFIRMATION", rejected["code"])
        self.assertNotIn("canonical_target", rejected)
        self.assertNotIn("share_snapshot", rejected)

    def test_final_approval_binds_reusable_integration_confirmation(self) -> None:
        archive = self.init_complex()
        self.approve_requirements()
        self.prepare_execution_inputs()
        self.accept_modification()
        self.set_status(archive / "06-validation" / "README.md", "completed")

        result = self.approve_final_with_confirmation()
        binding = result["integration_confirmation"]

        self.assertEqual({"confirmation_path", "confirmation_digest"}, set(binding))
        self.assertRegex(binding["confirmation_digest"], r"^sha256:[0-9a-f]{64}$")
        report = json.loads(
            archive.joinpath(binding["confirmation_path"]).read_text(encoding="utf-8")
        )
        self.assertEqual(
            {"result", "candidate_summary_digest", "workspace_guard_digest"},
            set(report),
        )
        self.assertEqual(result["candidate_summary"]["digest"], report["candidate_summary_digest"])
        self.assertEqual(result["workspace_snapshot"]["digest"], report["workspace_guard_digest"])
        status = self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery"
        )
        self.assertEqual("approve", status["delivery_view"]["conclusions"]["approvals"]["final"]["status"])

    def test_final_approval_rejects_extra_integration_confirmation_fields(self) -> None:
        archive = self.init_complex()
        self.approve_requirements()
        self.prepare_execution_inputs()
        self.accept_modification()
        self.set_status(archive / "06-validation" / "README.md", "completed")

        result = self.approve_final_with_confirmation(
            expected=1,
            confirmation_overrides={"test_execution_report": "passed"},
        )

        self.assertEqual("INVALID_INTEGRATION_CONFIRMATION", result["code"])

    def test_changed_integration_confirmation_makes_final_approval_stale(self) -> None:
        archive = self.init_complex()
        self.approve_requirements()
        self.prepare_execution_inputs()
        self.accept_modification()
        self.set_status(archive / "06-validation" / "README.md", "completed")
        result = self.approve_final_with_confirmation()
        report = archive / result["integration_confirmation"]["confirmation_path"]
        report.write_text(report.read_text(encoding="utf-8") + "\n", encoding="utf-8")

        status = self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery"
        )

        self.assertEqual("stale", status["delivery_view"]["conclusions"]["approvals"]["final"]["status"])
        binding_recheck = next(
            item
            for item in status["delivery_view"]["required_rechecks"]
            if item["code"] == "final_validation_candidate_binding"
        )
        self.assertEqual("failed", binding_recheck["status"])

    def test_final_approval_rejects_stale_or_failed_integration_confirmation(self) -> None:
        archive = self.init_complex()
        self.approve_requirements()
        self.prepare_execution_inputs()
        self.accept_modification()
        self.set_status(archive / "06-validation" / "README.md", "completed")

        report = self.write_integration_confirmation(candidate_summary_digest="sha256:" + "0" * 64)
        stale = self.run_cli(
            "stage-action", "--feature-id", "reliable-delivery",
            "--stage", "final", "--decision", "approve",
            "--integration-confirmation", report, expected=1,
        )
        self.assertEqual("INTEGRATION_CONFIRMATION_CANDIDATE_MISMATCH", stale["code"])

        report = self.write_integration_confirmation(workspace_guard_digest="sha256:" + "0" * 64)
        stale_workspace = self.run_cli(
            "stage-action", "--feature-id", "reliable-delivery",
            "--stage", "final", "--decision", "approve",
            "--integration-confirmation", report, expected=1,
        )
        self.assertEqual("INTEGRATION_CONFIRMATION_WORKSPACE_MISMATCH", stale_workspace["code"])

        report = self.write_integration_confirmation(result="failed")
        failed = self.run_cli(
            "stage-action", "--feature-id", "reliable-delivery",
            "--stage", "final", "--decision", "approve",
            "--integration-confirmation", report, expected=1,
        )
        self.assertEqual("INTEGRATION_CONFIRMATION_NOT_PASSED", failed["code"])

    def test_final_candidate_summary_uses_stable_package_order(self) -> None:
        archive = self.init_complex()
        self.approve_requirements()
        self.prepare_execution_inputs()
        for package_id, execution_id, candidate_id, review_id in (
            ("z-first", "exec-1", "candidate-1", "review-1"),
            ("a-second", "exec-2", "candidate-2", "review-2"),
        ):
            package = self.write_package(package_id)
            self.run_cli(
                "start-slice", "--feature-id", "reliable-delivery",
                "--execution-id", execution_id, "--package-file", package,
                "--workspace-root", self.workspace,
            )
            self.workspace.joinpath(f"{package_id}.txt").write_text(candidate_id, encoding="utf-8")
            self.record_checkpoint(execution_id, identifier=f"{package_id}-final")
            candidate = write_candidate(
                self.artifact_path(
                    "reliable-delivery", "05-implementation", f"{candidate_id}.json"
                ),
                candidate_id,
            )
            self.run_cli(
                "submit-slice", "--feature-id", "reliable-delivery",
                "--execution-id", execution_id, "--status", "completed",
                "--candidate-file", candidate,
            )
            self.run_cli(
                "review-slice", "--feature-id", "reliable-delivery",
                "--package-id", package_id, "--candidate-id", candidate_id,
                "--review-execution-id", review_id, "--result", "passed",
            )
        self.set_status(archive / "06-validation" / "README.md", "completed")

        result = self.approve_final_with_confirmation()

        self.assertEqual(
            ["a-second", "z-first"],
            [item["package_id"] for item in result["candidate_summary"]["items"]],
        )

    def test_final_approval_without_candidate_summary_is_invalid(self) -> None:
        archive = self.init_complex()
        self.approve_requirements()
        self.prepare_execution_inputs()
        self.accept_modification()
        self.set_status(archive / "06-validation" / "README.md", "completed")
        self.approve_final_with_confirmation()
        state_path = archive / "workflow-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["approvals"]["final"].pop("candidate_summary")
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        status = self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery",
            expected=1,
        )

        self.assertEqual("INVALID_WORKFLOW_STATE", status["code"])

    def test_reviewed_state_uses_snapshot_as_single_authority(self) -> None:
        archive = self.init_complex()
        self.approve_requirements()
        self.prepare_execution_inputs()
        self.accept_modification()
        self.set_status(archive / "06-validation" / "README.md", "completed")
        self.approve_final_with_confirmation()
        state_path = archive / "workflow-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        slice_state = state["execution"]["slices"]["slice-1"]
        self.assertNotIn("review", slice_state)
        self.assertIsNone(slice_state["candidate"])
        slice_state["review_snapshots"][-1]["review"]["review_execution_id"] = "review-changed"
        state["execution"]["used_execution_ids"][-1] = "review-changed"
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        status = self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery",
            expected=1,
        )

        self.assertEqual("INVALID_WORKFLOW_STATE", status["code"])

    def test_previous_workflow_state_schema_is_rejected_without_compatibility_path(self) -> None:
        archive = self.init_complex()
        state_path = archive / "workflow-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["schema_version"] = 4
        state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        status = self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery",
            expected=1,
        )

        self.assertEqual("INVALID_WORKFLOW_STATE", status["code"])

    def test_final_approval_rejects_workspace_drift_after_last_accepted_candidate(self) -> None:
        archive = self.init_complex()
        self.approve_requirements()
        self.prepare_execution_inputs()
        self.accept_modification()
        self.set_status(archive / "06-validation" / "README.md", "completed")
        self.workspace.joinpath("after-review.txt").write_text("drift", encoding="utf-8")

        result = self.run_cli(
            "stage-action", "--feature-id", "reliable-delivery",
            "--stage", "final", "--decision", "approve", expected=1,
        )

        self.assertEqual("FINAL_CANDIDATE_CHANGED", result["code"])

    def test_validation_semantic_change_makes_final_approval_stale(self) -> None:
        archive = self.init_complex()
        self.approve_requirements()
        self.prepare_execution_inputs()
        self.accept_modification()
        validation = archive / "06-validation" / "README.md"
        self.set_status(validation, "completed")
        self.approve_final_with_confirmation()
        validation.write_text(
            validation.read_text(encoding="utf-8") + "\n新增验证结论。\n",
            encoding="utf-8",
        )
        self.run_cli(
            "confirm-change", "--document-id", "reliable-delivery.validation.overview",
            "--semantic-change", "true",
        )
        status = self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery"
        )
        self.assertEqual("stale", status["delivery_view"]["conclusions"]["approvals"]["final"]["status"])

    def test_frozen_complex_workflow_remains_queryable(self) -> None:
        archive = self.init_complex()
        self.approve_requirements()
        self.prepare_execution_inputs()
        self.accept_modification()
        for category in archive.glob("??-*"):
            for document in category.glob("*.md"):
                self.set_status(document, "completed")
        self.run_cli(
            "transition-lifecycle", "--feature-id", "reliable-delivery", "--to", "active",
        )
        self.run_cli(
            "transition-lifecycle", "--feature-id", "reliable-delivery", "--to", "validating",
        )
        self.approve_final_with_confirmation()
        self.run_cli(
            "transition-lifecycle", "--feature-id", "reliable-delivery", "--to", "frozen",
            "--validation-conclusion", "passed", "--unverified-boundaries", "none",
            "--residual-risks", "none",
        )

        status = self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery"
        )

        self.assertEqual("approve", status["delivery_view"]["conclusions"]["approvals"]["final"]["status"])
