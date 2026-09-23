from __future__ import annotations

import json
from pathlib import Path
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import archive_candidates
import archive_execution
import archive_workspace
import archive_workspace_close
from archive_execution import TaskPackageTarget
from archive_workspace import ArchiveWorkspaceError, acquire_modification_lease, workspace_state_lock
try:
    from _feature_archive_support import (
        FeatureArchiveCliTestCase,
        _svn_guard_test_double,
        write_candidate,
        write_issues,
        write_review_verification,
    )
except ModuleNotFoundError:
    from ._feature_archive_support import (
        FeatureArchiveCliTestCase,
        _svn_guard_test_double,
        write_candidate,
        write_issues,
        write_review_verification,
    )


class FeatureArchiveExecutionTests(FeatureArchiveCliTestCase):
    real_snapshots = False

    def start_modification(self, package_id: str = "slice-1", execution_id: str = "exec-1"):
        package = self.write_package(package_id)
        return self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", execution_id, "--package-file", package, "--workspace-root", self.workspace,
        )

    def submit(self, package_id: str, execution_id: str, candidate_id: str):
        target = self.workspace / f"{package_id}.txt"
        target.write_text(candidate_id, encoding="utf-8")
        self.record_checkpoint(execution_id, identifier=f"{candidate_id}-final")
        candidate = write_candidate(
            self.artifact_path(
                "reliable-delivery", "05-implementation", f"{candidate_id}.json"
            ),
            candidate_id,
        )
        return self.run_cli(
            "submit-slice", "--feature-id", "reliable-delivery",
            "--execution-id", execution_id, "--status", "completed", "--candidate-file", candidate,
        )

    def setUp(self) -> None:
        super().setUp()
        self.init_complex()
        self.approve_requirements()
        self.prepare_execution_inputs()

    def test_failed_slice_cannot_restart_with_a_different_scope(self) -> None:
        self.start_modification()
        self.workspace.joinpath("slice-1.txt").write_text("failed", encoding="utf-8")
        self.run_cli(
            "submit-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-1", "--status", "failed",
        )
        replacement = self.write_package("slice-1", scope=["replacement.txt"])
        result = self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-2", "--package-file", replacement,
            "--workspace-root", self.workspace, expected=1,
        )
        self.assertEqual("SLICE_ALREADY_EXISTS", result["code"])
        lease = json.loads(
            self.root.joinpath(".feature-archive-workspace-state.json").read_text(encoding="utf-8")
        )["modification_lease"]
        self.assertEqual(["slice-1.txt"], lease["write_scopes"])

    def test_slice_without_write_scope_is_rejected(self) -> None:
        package = self.write_package("slice-1", scope=[])
        result = self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-1", "--package-file", package,
            "--workspace-root", self.workspace, expected=1,
        )
        self.assertEqual("INVALID_TASK_PACKAGE", result["code"])

    def test_glob_scope_is_rejected_during_preparation_and_before_start(self) -> None:
        for scope in ("UI/UITodo*.cs", "UI/UITodo?.cs", "UI/UITodo[12].cs", "UI/**/"):
            with self.subTest(scope=scope):
                package = self.write_package("slice-1", scope=[scope])
                state_path = self.root / "reliable-delivery" / "workflow-state.json"
                before = state_path.read_bytes()
                for command in ("prepare-slice-contract", "start-slice"):
                    arguments = [command, "--feature-id", "reliable-delivery", "--package-file", package]
                    if command == "start-slice":
                        arguments.extend(["--execution-id", "exec-1", "--workspace-root", self.workspace])
                    result = self.run_cli(*arguments, expected=1)
                    self.assertEqual("INVALID_TASK_SCOPE", result["code"])
                    self.assertIn("具体文件或目录", result["message"])
                    self.assertEqual(before, state_path.read_bytes())
                    self.assertFalse(self.root.joinpath(".feature-archive-workspace-state.json").exists())

    def test_exact_files_and_directory_scope_cover_generated_files(self) -> None:
        package = self.write_package("slice-1", scope=["UI/Todo.cs", "UI/Todo.cs.meta", "Generated/"])
        self.run_cli("start-slice", "--feature-id", "reliable-delivery", "--execution-id", "exec-1",
                     "--package-file", package, "--workspace-root", self.workspace)
        for relative in ("UI/Todo.cs", "UI/Todo.cs.meta", "Generated/Bind.cs", "Generated/Bind.cs.meta"):
            path = self.workspace / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("本切片生成内容", encoding="utf-8")
        checkpoint = self.record_checkpoint("exec-1")
        self.assertFalse(checkpoint["checkpoint"]["boundary_crossed"])

    def test_dloop_owned_state_is_rejected_before_a_lease_is_created(self) -> None:
        package = self.write_package(
            "slice-1",
            scope=[".scratch/dloop-v3/v3.9.3/outputs/reliable-delivery/workflow-state.json"],
        )

        result = self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-1", "--package-file", package,
            "--workspace-root", self.workspace, expected=1,
        )

        self.assertEqual("DLOOP_MANAGED_SCOPE_FORBIDDEN", result["code"])
        self.assertFalse(self.root.joinpath(".feature-archive-workspace-state.json").exists())

    def test_ui_model_is_managed_evidence_not_a_product_scope(self) -> None:
        model = self.workspace / ".scratch/dloop-v3/v3.9.3/outputs/reliable-delivery/ui-model.json"
        model.parent.mkdir(parents=True)
        model.write_text("{}\n", encoding="utf-8")
        package = self.write_package(
            "slice-ui",
            scope=[".scratch/dloop-v3/v3.9.3/outputs/reliable-delivery/ui-model.json"],
        )

        result = self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-ui", "--package-file", package,
            "--workspace-root", self.workspace,
            expected=1,
        )

        self.assertEqual("DLOOP_MANAGED_SCOPE_FORBIDDEN", result["code"])

    def test_generator_preflight_reports_missing_prefab_and_meta_before_start(self) -> None:
        package = self.write_package("slice-1", scope=["UI/Bind"])
        value = json.loads(package.read_bytes())
        value["generated_write_scope"] = ["UI/Bind/Panel.cs", "UI/Panel.prefab", "UI/Panel.prefab.meta"]
        package.write_text(json.dumps(value), encoding="utf-8")
        for command in ("check-slice-contract", "start-slice"):
            arguments = [command, "--feature-id", "reliable-delivery", "--package-file", package]
            if command == "start-slice":
                arguments += ["--execution-id", "exec-1", "--workspace-root", self.workspace]
            blocked = self.run_cli(*arguments, expected=1)
            self.assertEqual("GENERATED_WRITE_SCOPE_INCOMPLETE", blocked["code"])
            self.assertIn("UI/Panel.prefab.meta", blocked["message"])
            self.assertFalse(self.root.joinpath(".feature-archive-workspace-state.json").exists())
        value["write_scope"] += ["UI/Panel.prefab", "UI/Panel.prefab.meta"]
        package.write_text(json.dumps(value), encoding="utf-8")
        started = self.run_cli("start-slice", "--feature-id", "reliable-delivery",
                               "--package-file", package, "--execution-id", "exec-1", "--workspace-root", self.workspace)
        self.assertEqual(value["generated_write_scope"], started["task"]["generated_write_scope"])
        for relative in value["generated_write_scope"]:
            target = self.workspace / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("generated", encoding="utf-8")
        self.assertFalse(self.record_checkpoint("exec-1")["checkpoint"]["boundary_crossed"])

    def test_nested_ui_model_name_does_not_bypass_managed_scope(self) -> None:
        package = self.write_package(
            "slice-ui",
            scope=[
                ".scratch/dloop-v3/v3.9.3/outputs/reliable-delivery/"
                "05-implementation/ui-model.json"
            ],
        )

        result = self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-ui", "--package-file", package,
            "--workspace-root", self.workspace, expected=1,
        )

        self.assertEqual("DLOOP_MANAGED_SCOPE_FORBIDDEN", result["code"])
        self.assertFalse(self.root.joinpath(".feature-archive-workspace-state.json").exists())

    def test_other_feature_ui_model_is_not_a_product_scope(self) -> None:
        package = self.write_package(
            "slice-ui",
            scope=[".scratch/dloop-v3/v3.9.3/outputs/other-feature/ui-model.json"],
        )

        result = self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-ui", "--package-file", package,
            "--workspace-root", self.workspace, expected=1,
        )

        self.assertEqual("DLOOP_MANAGED_SCOPE_FORBIDDEN", result["code"])
        self.assertFalse(self.root.joinpath(".feature-archive-workspace-state.json").exists())

    def test_task_contract_projection_rejects_manually_duplicated_fields(self) -> None:
        package = self.write_package("slice-1")
        value = json.loads(package.read_text(encoding="utf-8"))
        value["goal"] = value["slice_contract"]["business_outcome"]
        package.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

        result = self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-1", "--package-file", package,
            "--workspace-root", self.workspace, expected=1,
        )

        self.assertEqual("DUPLICATED_TASK_CONTRACT_FIELDS", result["code"])

    def test_second_feature_cannot_modify_same_workspace(self) -> None:
        self.start_modification()
        self.init_complex("second-feature")
        self.approve_requirements("second-feature")
        self.prepare_execution_inputs("second-feature")
        package = self.write_package("slice-2", feature_id="second-feature")
        package_value = json.loads(package.read_text(encoding="utf-8"))
        package_value["context_materials"][0]["source"] = (
            "second-feature.requirements.overview"
        )
        package.write_text(
            json.dumps(package_value, ensure_ascii=False),
            encoding="utf-8",
        )
        result = self.run_cli(
            "start-slice", "--feature-id", "second-feature",
            "--execution-id", "exec-2", "--package-file", package, "--workspace-root", self.workspace,
            expected=1,
        )
        self.assertEqual("MODIFICATION_LEASE_CONFLICT", result["code"])

    def test_execution_identity_cannot_be_shared_by_two_slices(self) -> None:
        first = self.write_package("slice-1")
        second = self.write_package("slice-2")
        self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-shared", "--package-file", first,
            "--workspace-root", self.workspace,
        )
        result = self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-shared", "--package-file", second,
            "--workspace-root", self.workspace, expected=1,
        )
        self.assertEqual("EXECUTION_ID_CONFLICT", result["code"])

    def test_identical_start_retry_returns_the_existing_handoff(self) -> None:
        package = self.write_package("slice-retry")
        arguments = (
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-retry", "--package-file", package,
            "--workspace-root", self.workspace,
        )

        first = self.run_cli(*arguments)
        second = self.run_cli(*arguments)

        self.assertEqual("active", first["status"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(first["execution_handoff"], second["execution_handoff"])
        workspace_state_path = self.root / ".feature-archive-workspace-state.json"
        workspace_state = json.loads(workspace_state_path.read_text(encoding="utf-8"))
        self.assertEqual(
            "exec-retry",
            workspace_state["modification_lease"]["holder_execution_id"],
        )
        state = json.loads(
            self.root.joinpath("reliable-delivery/workflow-state.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(["exec-retry"], state["execution"]["used_execution_ids"])

        workspace_state["modification_lease"]["holder_execution_id"] = "other-execution"
        workspace_state_path.write_text(
            json.dumps(workspace_state, ensure_ascii=False),
            encoding="utf-8",
        )
        conflict = self.run_cli(*arguments, expected=1)
        self.assertEqual("EXECUTION_ID_CONFLICT", conflict["code"])

    def test_concurrent_slice_start_is_serialized_before_identity_check(self) -> None:
        packages = (
            self.write_package("slice-1"),
            self.write_package("slice-2"),
        )
        def start(package: Path):
            try:
                result = archive_execution.start_execution(
                    self.root,
                    "reliable-delivery",
                    "exec-shared",
                    TaskPackageTarget(package_file=package),
                    self.workspace,
                )
                return 0, result
            except Exception as exception:
                return 1, {"code": getattr(exception, "code", type(exception).__name__)}

        with patch.object(
            archive_workspace,
            "workspace_guard_snapshot",
            side_effect=_svn_guard_test_double,
        ), ThreadPoolExecutor(max_workers=2) as executor:
            with workspace_state_lock(self.root):
                futures = [executor.submit(start, package) for package in packages]
                time.sleep(0.3)
                self.assertTrue(all(not future.done() for future in futures))
            completed = [future.result(timeout=10) for future in futures]
        self.assertEqual([0, 1], sorted(code for code, _ in completed))
        failure = next(result for code, result in completed if code == 1)
        self.assertEqual("EXECUTION_ID_CONFLICT", failure["code"])

    def test_candidate_requires_independent_review_and_rejects_drift(self) -> None:
        self.start_modification()
        candidate = self.submit("slice-1", "exec-1", "candidate-1")
        self.assertEqual("candidate", candidate["status"])
        self.workspace.joinpath("slice-1.txt").write_text("drift", encoding="utf-8")
        result = self.run_cli(
            "review-slice", "--feature-id", "reliable-delivery",
            "--package-id", "slice-1", "--candidate-id", "candidate-1",
            "--review-execution-id", "review-1", "--result", "passed", expected=1,
        )
        self.assertEqual("CANDIDATE_CHANGED", result["code"])

    def test_drifted_candidate_can_enter_existing_retry_path(self) -> None:
        self.start_modification()
        self.submit("slice-1", "exec-1", "candidate-1")
        self.workspace.joinpath("slice-1.txt").write_text("drift", encoding="utf-8")

        result = self.run_cli(
            "resolve-slice", "--feature-id", "reliable-delivery",
            "--action", "retry", "--package-id", "slice-1", "--execution-id", "exec-2",
        )

        self.assertEqual("active", result["status"])

    def test_drifted_candidate_can_be_released_after_baseline_restoration(self) -> None:
        self.start_modification()
        self.submit("slice-1", "exec-1", "candidate-1")
        self.workspace.joinpath("slice-1.txt").unlink()

        result = self.run_cli(
            "resolve-slice", "--feature-id", "reliable-delivery",
            "--action", "release", "--package-id", "slice-1",
            "--workspace-decision", "restored",
        )

        self.assertEqual("abandoned", result["status"])

    def test_implementer_cannot_accept_own_candidate(self) -> None:
        self.start_modification()
        self.submit("slice-1", "exec-1", "candidate-1")
        result = self.run_cli(
            "review-slice", "--feature-id", "reliable-delivery",
            "--package-id", "slice-1", "--candidate-id", "candidate-1",
            "--review-execution-id", "exec-1", "--result", "passed", expected=1,
        )
        self.assertEqual("REVIEW_NOT_INDEPENDENT", result["code"])

    def test_independent_review_can_record_its_own_negative_test_evidence(self) -> None:
        self.start_modification()
        self.submit("slice-1", "exec-1", "candidate-1")
        evidence = write_review_verification(
            self.artifact_path(
                "reliable-delivery", "06-validation", "review-verification.json"
            ),
            "边界外输入被拒绝",
            "候选工作区漂移时评审失败",
        )

        result = self.run_cli(
            "review-slice", "--feature-id", "reliable-delivery",
            "--package-id", "slice-1", "--candidate-id", "candidate-1",
            "--review-execution-id", "review-1", "--result", "passed",
            "--verification-file", evidence,
        )

        self.assertEqual(
            ["边界外输入被拒绝", "候选工作区漂移时评审失败"],
            result["verification"],
        )
        state = json.loads(
            self.root.joinpath("reliable-delivery", "workflow-state.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            result["verification"],
            state["execution"]["slices"]["slice-1"]["review_snapshots"][-1]["review"]["verification"],
        )

    def test_passed_review_allows_optional_verification(self) -> None:
        self.start_modification()
        self.submit("slice-1", "exec-1", "candidate-1")

        accepted = self.run_cli(
            "review-slice", "--feature-id", "reliable-delivery",
            "--package-id", "slice-1", "--candidate-id", "candidate-1",
            "--review-execution-id", "review-1", "--result", "passed",
        )

        self.assertEqual("accepted", accepted["status"])
        self.assertEqual([], accepted["verification"])
        self.assertIsNone(accepted["verification_not_applicable_reason"])

    def test_review_execution_identity_cannot_be_reused(self) -> None:
        self.start_modification()
        self.submit("slice-1", "exec-1", "candidate-1")
        self.run_cli(
            "review-slice", "--feature-id", "reliable-delivery",
            "--package-id", "slice-1", "--candidate-id", "candidate-1",
            "--review-execution-id", "review-1", "--result", "passed",
        )
        self.start_modification("slice-2", "exec-2")
        self.submit("slice-2", "exec-2", "candidate-2")

        result = self.run_cli(
            "review-slice", "--feature-id", "reliable-delivery",
            "--package-id", "slice-2", "--candidate-id", "candidate-2",
            "--review-execution-id", "review-1", "--result", "passed", expected=1,
        )

        self.assertEqual("EXECUTION_ID_CONFLICT", result["code"])

    def test_retry_cannot_reuse_historical_execution_identity(self) -> None:
        self.start_modification()
        self.run_cli(
            "submit-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-1", "--status", "failed",
        )
        self.run_cli(
            "resolve-slice", "--feature-id", "reliable-delivery",
            "--action", "retry", "--package-id", "slice-1", "--execution-id", "exec-2",
        )
        self.run_cli(
            "submit-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-2", "--status", "failed",
        )

        result = self.run_cli(
            "resolve-slice", "--feature-id", "reliable-delivery",
            "--action", "retry", "--package-id", "slice-1", "--execution-id", "exec-1",
            expected=1,
        )

        self.assertEqual("EXECUTION_ID_CONFLICT", result["code"])

    def test_failed_review_can_retry_without_machine_round_limit(self) -> None:
        self.start_modification()
        for round_index in range(1, 4):
            execution_id = f"exec-{round_index}"
            candidate_id = f"candidate-{round_index}"
            if round_index > 1:
                self.run_cli(
                    "resolve-slice", "--feature-id", "reliable-delivery",
                    "--action", "retry", "--package-id", "slice-1", "--execution-id", execution_id,
                )
            self.submit("slice-1", execution_id, candidate_id)
            issues = write_issues(
                self.artifact_path(
                    "reliable-delivery",
                    "05-implementation",
                    f"issues-{round_index}.json",
                ),
                "仍需修正",
            )
            self.run_cli(
                "review-slice", "--feature-id", "reliable-delivery",
                "--package-id", "slice-1", "--candidate-id", candidate_id,
                "--review-execution-id", f"review-{round_index}", "--result", "failed",
                "--issues-file", issues,
            )
        status = self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery"
        )
        self.assertEqual("review_failed", status["delivery_view"]["conclusions"]["slices"]["slice-1"]["status"])

    def test_accepted_candidate_releases_workspace(self) -> None:
        self.start_modification()
        self.submit("slice-1", "exec-1", "candidate-1")
        result = self.run_cli(
            "review-slice", "--feature-id", "reliable-delivery",
            "--package-id", "slice-1", "--candidate-id", "candidate-1",
            "--review-execution-id", "review-1", "--result", "passed",
        )
        self.assertEqual("accepted", result["status"])
        status = self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery", "--include-details",
        )
        self.assertIsNone(status["delivery_view"]["trusted_machine_facts"]["modification_lease"])
        self.assertEqual([], status["details"]["final_blockers"])

    def test_delivery_view_derives_candidate_review_from_fixed_candidate(self) -> None:
        self.start_modification()
        self.submit("slice-1", "exec-1", "candidate-1")

        view = self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery",
        )["delivery_view"]

        self.assertEqual("candidate-review", view["current_stage"])
        self.assertEqual(
            {"command": "review-slice", "package_id": "slice-1", "candidate_id": "candidate-1"},
            view["next_action"],
        )
        self.assertEqual(
            [
                "result",
                "issues_file（result!=passed 时）",
            ],
            view["next_action_contract"]["required_inputs"],
        )
        self.assertEqual(
            "slice-1-review",
            view["next_action_contract"]["arguments"]["review_execution_id"],
        )
        self.assertTrue(view["requires_human"]["required"])
        self.assertEqual("candidate-1", view["conclusions"]["current_candidate"]["candidate_id"])

    def test_delivery_view_rechecks_accepted_candidate_and_final_binding(self) -> None:
        self.start_modification()
        self.submit("slice-1", "exec-1", "candidate-1")
        self.run_cli(
            "review-slice", "--feature-id", "reliable-delivery",
            "--package-id", "slice-1", "--candidate-id", "candidate-1",
            "--review-execution-id", "review-1", "--result", "passed",
        )

        accepted = self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery",
        )["delivery_view"]
        self.assertEqual("implementation-complete", accepted["current_stage"])
        self.assertEqual({"command": "transition-lifecycle", "to": "active"}, accepted["next_action"])
        self.assertFalse(accepted["requires_human"]["required"])
        self.assertEqual(
            ["passed", "pending", "passed"],
            [item["status"] for item in accepted["required_rechecks"]],
        )
        review = accepted["conclusions"]["slices"]["slice-1"]["review"]
        self.assertEqual("passed", review["result"])
        self.assertEqual("review-1", review["review_execution_id"])

        archive = self.root / "reliable-delivery"
        self.set_status(archive / "06-validation" / "README.md", "completed")
        self.run_cli(
            "transition-lifecycle", "--feature-id", "reliable-delivery", "--to", "active",
        )
        self.run_cli(
            "transition-lifecycle", "--feature-id", "reliable-delivery", "--to", "validating",
        )
        self.approve_final_with_confirmation()

        approved = self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery",
        )["delivery_view"]
        self.assertEqual("freeze-ready", approved["current_stage"])
        self.assertEqual(
            ["passed", "passed", "passed"],
            [item["status"] for item in approved["required_rechecks"]],
        )

        self.workspace.joinpath("after-final.txt").write_text("drift", encoding="utf-8")
        drifted = self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery",
        )["delivery_view"]
        self.assertEqual("validation-recovery", drifted["current_stage"])
        self.assertIsNone(drifted["next_action"])
        self.assertTrue(drifted["requires_human"]["required"])
        self.assertEqual("failed", drifted["required_rechecks"][0]["status"])

    def test_next_slice_rejects_drift_after_last_accepted_candidate(self) -> None:
        self.start_modification()
        self.submit("slice-1", "exec-1", "candidate-1")
        self.run_cli(
            "review-slice", "--feature-id", "reliable-delivery",
            "--package-id", "slice-1", "--candidate-id", "candidate-1",
            "--review-execution-id", "review-1", "--result", "passed",
        )
        self.workspace.joinpath("between-slices.txt").write_text("drift", encoding="utf-8")

        package = self.write_package("slice-2")
        result = self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-2", "--package-file", package,
            "--workspace-root", self.workspace, expected=1,
        )

        self.assertEqual("LAST_ACCEPTED_CANDIDATE_CHANGED", result["code"])

    def test_last_accepted_candidate_uses_execution_identity_order(self) -> None:
        self.start_modification("z-first", "exec-1")
        self.submit("z-first", "exec-1", "candidate-1")
        self.run_cli(
            "review-slice", "--feature-id", "reliable-delivery",
            "--package-id", "z-first", "--candidate-id", "candidate-1",
            "--review-execution-id", "review-1", "--result", "passed",
        )
        self.start_modification("a-second", "exec-2")
        self.submit("a-second", "exec-2", "candidate-2")
        self.run_cli(
            "review-slice", "--feature-id", "reliable-delivery",
            "--package-id", "a-second", "--candidate-id", "candidate-2",
            "--review-execution-id", "review-2", "--result", "passed",
        )

        package = self.write_package("slice-3")
        result = self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-3", "--package-file", package,
            "--workspace-root", self.workspace,
        )

        self.assertEqual("active", result["status"])

    def test_ordinary_audit_remains_allowed_during_an_active_slice(self) -> None:
        self.start_modification()

        result = self.run_cli(
            "audit", "--feature-id", "reliable-delivery",
        )

        self.assertEqual("allowed", result["status"])
        self.assertEqual([], result["blockers"])

    def test_candidate_and_lease_roll_back_together_when_accept_commit_fails(self) -> None:
        self.start_modification()
        self.submit("slice-1", "exec-1", "candidate-1")
        release_error = ArchiveWorkspaceError(
            "WORKSPACE_STATE_COMMIT_FAILED",
            "模拟修改占用释放失败。",
        )
        with patch.object(
            archive_workspace_close,
            "_write_workspace_state",
            side_effect=release_error,
        ):
            with self.assertRaises(ArchiveWorkspaceError):
                archive_candidates.review_candidate(
                    self.root,
                    "reliable-delivery",
                    "slice-1",
                    "candidate-1",
                    "review-1",
                    "passed",
                    None,
                    None,
                    "该测试只验证事务回滚，独立负向测试不适用",
                )

        status = self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery"
        )
        self.assertEqual("candidate", status["delivery_view"]["conclusions"]["slices"]["slice-1"]["status"])
        self.assertIsNotNone(status["delivery_view"]["trusted_machine_facts"]["modification_lease"])
        accepted = self.run_cli(
            "review-slice", "--feature-id", "reliable-delivery",
            "--package-id", "slice-1", "--candidate-id", "candidate-1",
            "--review-execution-id", "review-1", "--result", "passed",
        )
        self.assertEqual("accepted", accepted["status"])
        self.assertIsNone(self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery"
        )["delivery_view"]["trusted_machine_facts"]["modification_lease"])

    def test_retry_requires_a_checkpoint_from_the_new_execution(self) -> None:
        self.start_modification()
        self.workspace.joinpath("slice-1.txt").write_text(
            "candidate-1", encoding="utf-8"
        )
        first_checkpoint = self.record_checkpoint(
            "exec-1", identifier="same-final"
        )
        first_candidate = write_candidate(
            self.artifact_path(
                "reliable-delivery", "05-implementation", "candidate-1.json"
            ),
            "candidate-1",
        )
        self.run_cli(
            "submit-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-1", "--status", "completed",
            "--candidate-file", first_candidate,
        )
        issues = write_issues(
            self.artifact_path(
                "reliable-delivery", "05-implementation", "issues.json"
            ),
            "需要返修",
        )
        self.run_cli(
            "review-slice", "--feature-id", "reliable-delivery",
            "--package-id", "slice-1", "--candidate-id", "candidate-1",
            "--review-execution-id", "review-1", "--result", "failed",
            "--issues-file", issues,
        )
        self.run_cli(
            "resolve-slice", "--feature-id", "reliable-delivery",
            "--action", "retry", "--package-id", "slice-1",
            "--execution-id", "exec-2",
        )
        second_candidate = write_candidate(
            self.artifact_path(
                "reliable-delivery", "05-implementation", "candidate-2.json"
            ),
            "candidate-2",
        )

        stale = self.run_cli(
            "submit-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-2", "--status", "completed",
            "--candidate-file", second_candidate, expected=1,
        )
        self.assertEqual("FINAL_CHECKPOINT_STALE", stale["code"])
        prepared = self.run_cli(
            "prepare-action-input", "--feature-id", "reliable-delivery",
            "--input-kind", "checkpoint", "--execution-id", "exec-2",
        )
        self.assertEqual(
            "exec-2-checkpoint-1-v1.json",
            Path(prepared["target"]).name,
        )

        second_checkpoint = self.record_checkpoint(
            "exec-2", identifier="same-final"
        )
        self.assertFalse(second_checkpoint["idempotent"])
        self.assertEqual(
            "exec-2-checkpoint-1",
            second_checkpoint["checkpoint"]["checkpoint_id"],
        )
        self.assertNotEqual(
            first_checkpoint["checkpoint"]["checkpoint_id"],
            second_checkpoint["checkpoint"]["checkpoint_id"],
        )
        submitted = self.run_cli(
            "submit-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-2", "--status", "completed",
            "--candidate-file", second_candidate,
        )
        self.assertEqual("candidate", submitted["status"])

    def test_interruption_commit_failure_rolls_back_workspace_and_state(self) -> None:
        self.start_modification()
        target = self.workspace.joinpath("slice-1.txt")
        target.write_text("temporary", encoding="utf-8")
        release_error = ArchiveWorkspaceError(
            "WORKSPACE_STATE_COMMIT_FAILED",
            "模拟修改占用释放失败。",
        )
        with patch.object(
            archive_workspace_close,
            "_write_workspace_state",
            side_effect=release_error,
        ):
            with self.assertRaises(ArchiveWorkspaceError):
                archive_candidates.submit_candidate(
                    self.root,
                    "reliable-delivery",
                    "exec-1",
                    "interrupted",
                    None,
                )

        self.assertEqual("temporary", target.read_text(encoding="utf-8"))
        status = self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery"
        )
        self.assertEqual("active", status["delivery_view"]["conclusions"]["slices"]["slice-1"]["status"])
        self.assertIsNotNone(status["delivery_view"]["trusted_machine_facts"]["modification_lease"])
        recovered = self.run_cli(
            "submit-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-1", "--status", "interrupted",
        )
        self.assertEqual("interrupted", recovered["status"])
        self.assertFalse(target.exists())

    def test_interruption_restores_preexisting_content_and_is_idempotent(self) -> None:
        target = self.workspace.joinpath("slice-1.txt")
        target.write_text("用户已有内容", encoding="utf-8")
        self.start_modification()
        target.write_text("本次执行内容", encoding="utf-8")

        first = self.run_cli(
            "submit-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-1", "--status", "interrupted",
        )
        second = self.run_cli(
            "submit-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-1", "--status", "interrupted",
        )

        self.assertEqual("用户已有内容", target.read_text(encoding="utf-8"))
        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        self.assertNotIn("friction_id", first)
        self.assertNotIn("friction_id", second)
        status = self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery"
        )
        self.assertEqual("interrupted", status["delivery_view"]["conclusions"]["slices"]["slice-1"]["status"])
        self.assertIsNone(status["delivery_view"]["trusted_machine_facts"]["modification_lease"])

    def test_interrupted_slice_can_reacquire_lease_for_repair(self) -> None:
        self.start_modification()
        target = self.workspace.joinpath("slice-1.txt")
        target.write_text("本次执行内容", encoding="utf-8")
        self.run_cli(
            "submit-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-1", "--status", "interrupted",
        )

        view = self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery"
        )["delivery_view"]
        contract = view["next_action_contract"]
        self.assertEqual("implementation-recovery", view["current_stage"])
        self.assertEqual("resolve-slice", contract["command"])
        self.assertEqual("slice-1-repair", contract["generated_inputs"]["execution_id"])

        retried = self.run_cli(
            "resolve-slice", "--feature-id", "reliable-delivery",
            "--action", "retry", "--package-id", "slice-1",
            "--execution-id", contract["generated_inputs"]["execution_id"],
        )

        self.assertEqual("active", retried["status"])
        self.assertFalse(target.exists())
        status = self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery"
        )
        self.assertEqual(
            "slice-1-repair",
            status["delivery_view"]["trusted_machine_facts"]["modification_lease"]["holder_execution_id"],
        )

    def test_interrupted_retry_commit_failure_releases_new_lease(self) -> None:
        self.start_modification()
        self.workspace.joinpath("slice-1.txt").write_text(
            "本次执行内容", encoding="utf-8"
        )
        self.run_cli(
            "submit-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-1", "--status", "interrupted",
        )
        commit_error = ArchiveWorkspaceError(
            "WORKSPACE_STATE_COMMIT_FAILED",
            "模拟返修租约提交失败。",
        )

        with patch.object(
            archive_workspace_close,
            "_write_workspace_state",
            side_effect=commit_error,
        ):
            with self.assertRaises(ArchiveWorkspaceError):
                archive_candidates.retry_slice(
                    self.root,
                    "reliable-delivery",
                    "slice-1",
                    "slice-1-repair",
                )

        status = self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery"
        )
        self.assertEqual("interrupted", status["delivery_view"]["conclusions"]["slices"]["slice-1"]["status"])
        self.assertIsNone(status["delivery_view"]["trusted_machine_facts"]["modification_lease"])
        retried = self.run_cli(
            "resolve-slice", "--feature-id", "reliable-delivery",
            "--action", "retry", "--package-id", "slice-1",
            "--execution-id", "slice-1-repair",
        )
        self.assertEqual("active", retried["status"])

    def test_interruption_does_not_change_an_accepted_candidate(self) -> None:
        self.start_modification("slice-1", "exec-1")
        self.submit("slice-1", "exec-1", "candidate-1")
        self.run_cli(
            "review-slice", "--feature-id", "reliable-delivery",
            "--package-id", "slice-1", "--candidate-id", "candidate-1",
            "--review-execution-id", "review-1", "--result", "passed",
        )
        accepted = self.workspace.joinpath("slice-1.txt")

        self.start_modification("slice-2", "exec-2")
        temporary = self.workspace.joinpath("slice-2.txt")
        temporary.write_text("本次执行内容", encoding="utf-8")
        self.run_cli(
            "submit-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-2", "--status", "interrupted",
        )

        self.assertEqual("candidate-1", accepted.read_text(encoding="utf-8"))
        self.assertFalse(temporary.exists())

    def test_interruption_rejects_outside_scope_drift_without_partial_close(self) -> None:
        self.start_modification()
        target = self.workspace.joinpath("slice-1.txt")
        target.write_text("本次执行内容", encoding="utf-8")
        self.workspace.joinpath("outside.txt").write_text("用户并发修改", encoding="utf-8")

        result = self.run_cli(
            "submit-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-1", "--status", "interrupted", expected=1,
        )

        self.assertEqual("WORKSPACE_SCOPE_VIOLATION", result["code"])
        self.assertEqual("本次执行内容", target.read_text(encoding="utf-8"))
        status = self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery"
        )
        self.assertEqual("active", status["delivery_view"]["conclusions"]["slices"]["slice-1"]["status"])
        self.assertIsNotNone(status["delivery_view"]["trusted_machine_facts"]["modification_lease"])

    def test_abandon_restores_authorized_content_and_is_idempotent(self) -> None:
        target = self.workspace.joinpath("slice-1.txt")
        target.write_text("执行前内容", encoding="utf-8")
        self.start_modification()
        target.write_text("失败执行内容", encoding="utf-8")
        self.run_cli(
            "submit-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-1", "--status", "failed",
        )

        first = self.run_cli(
            "resolve-slice", "--feature-id", "reliable-delivery",
            "--action", "release", "--package-id", "slice-1",
            "--workspace-decision", "restored",
        )
        second = self.run_cli(
            "resolve-slice", "--feature-id", "reliable-delivery",
            "--action", "release", "--package-id", "slice-1",
            "--workspace-decision", "restored",
        )

        self.assertEqual("执行前内容", target.read_text(encoding="utf-8"))
        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        self.assertIsNone(self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery"
        )["delivery_view"]["trusted_machine_facts"]["modification_lease"])

    def test_submit_rejects_workspace_changes_outside_authorized_scope(self) -> None:
        self.start_modification()
        self.workspace.joinpath("outside.txt").write_text("unexpected", encoding="utf-8")

        result = self.record_checkpoint("exec-1", identifier="outside-scope")

        self.assertEqual("circuit_open", result["status"])
        self.assertIn("write_scope_violation", result["checkpoint"]["boundary_rules"])

    def test_review_rejects_workspace_changes_after_candidate_submission(self) -> None:
        self.start_modification()
        self.submit("slice-1", "exec-1", "candidate-1")
        self.workspace.joinpath("outside.txt").write_text("late change", encoding="utf-8")

        recovery = self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery"
        )["delivery_view"]
        self.assertEqual("implementation-recovery", recovery["current_stage"])
        self.assertIsNone(recovery["next_action"])
        self.assertIsNone(recovery["next_action_contract"])
        self.assertTrue(recovery["requires_human"]["required"])
        self.assertIn("恢复固定候选", recovery["requires_human"]["decision"])

        result = self.run_cli(
            "review-slice", "--feature-id", "reliable-delivery",
            "--package-id", "slice-1", "--candidate-id", "candidate-1",
            "--review-execution-id", "review-1", "--result", "passed", expected=1,
        )

        self.assertEqual("CANDIDATE_CHANGED", result["code"])

        self.workspace.joinpath("outside.txt").unlink()
        restored = self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery"
        )["delivery_view"]
        self.assertEqual("candidate-review", restored["current_stage"])
        self.assertEqual("review-slice", restored["next_action_contract"]["command"])

    def test_orphaned_lease_can_only_be_released_after_workspace_is_restored(self) -> None:
        with patch.object(
            archive_workspace,
            "workspace_guard_snapshot",
            side_effect=_svn_guard_test_double,
        ):
            acquire_modification_lease(
                self.root,
                "reliable-delivery",
                "orphan-package",
                self.workspace,
                ["orphan.txt"],
                "now",
            )
        self.workspace.joinpath("orphan.txt").write_text("unfinished", encoding="utf-8")
        dirty = self.run_cli(
            "resolve-slice", "--feature-id", "reliable-delivery",
            "--action", "release-orphan", "--package-id", "orphan-package",
            "--workspace-decision", "restored", expected=1,
        )
        self.assertEqual("WORKSPACE_NOT_RESTORED", dirty["code"])

        self.workspace.joinpath("orphan.txt").unlink()
        released = self.run_cli(
            "resolve-slice", "--feature-id", "reliable-delivery",
            "--action", "release-orphan", "--package-id", "orphan-package",
            "--workspace-decision", "restored",
        )
        self.assertEqual("orphaned-lease", released["recovery"])

    def test_matching_slice_cannot_use_orphaned_lease_recovery(self) -> None:
        self.start_modification()
        result = self.run_cli(
            "resolve-slice", "--feature-id", "reliable-delivery",
            "--action", "release-orphan", "--package-id", "slice-1",
            "--workspace-decision", "restored", expected=1,
        )
        self.assertEqual("LEASE_NOT_ORPHANED", result["code"])

    def test_blocked_slice_requires_explicit_workspace_decision(self) -> None:
        self.start_modification()
        target = self.workspace.joinpath("slice-1.txt")
        target.write_text("temporary", encoding="utf-8")
        self.run_cli(
            "submit-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-1", "--status", "interrupted",
        )
        result = self.run_cli(
            "resolve-slice", "--feature-id", "reliable-delivery",
            "--action", "release", "--package-id", "slice-1", expected=1,
        )
        self.assertEqual("WORKSPACE_DECISION_REQUIRED", result["code"])
        kept = self.run_cli(
            "resolve-slice", "--feature-id", "reliable-delivery",
            "--action", "release", "--package-id", "slice-1",
            "--workspace-decision", "kept", expected=1,
        )
        self.assertEqual("UNREVIEWED_WORKSPACE_CHANGES", kept["code"])
        self.assertFalse(target.exists())
        released = self.run_cli(
            "resolve-slice", "--feature-id", "reliable-delivery",
            "--action", "release", "--package-id", "slice-1",
            "--workspace-decision", "restored",
        )
        self.assertEqual("abandoned", released["status"])

    def test_active_slice_cannot_be_released_as_abandoned(self) -> None:
        self.start_modification()
        result = self.run_cli(
            "resolve-slice", "--feature-id", "reliable-delivery",
            "--action", "release", "--package-id", "slice-1",
            "--workspace-decision", "restored", expected=1,
        )
        self.assertEqual("SLICE_NOT_RELEASABLE", result["code"])
        status = self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery"
        )
        self.assertEqual("active", status["delivery_view"]["conclusions"]["slices"]["slice-1"]["status"])
        self.assertIsNotNone(status["delivery_view"]["trusted_machine_facts"]["modification_lease"])

    def test_start_result_contains_complete_execution_handoff(self) -> None:
        result = self.start_modification()
        handoff = result["execution_handoff"]
        self.assertEqual("reliable-delivery", handoff["feature_id"])
        self.assertEqual("slice-1", handoff["package_id"])
        self.assertEqual("exec-1", handoff["execution_id"])
        self.assertEqual(str(self.workspace.resolve()), handoff["workspace_root"])
        self.assertEqual(["slice-1.txt"], handoff["allowed_write_scope"])
        self.assertTrue(handoff["task_package"]["acceptance_conditions"])
        self.assertTrue(handoff["task_package"]["validations"])

    def test_review_failure_has_minimal_immutable_repair_handoff(self) -> None:
        self.run_cli(
            "transition-lifecycle", "--feature-id", "reliable-delivery", "--to", "active",
        )
        self.start_modification()
        first = self.submit("slice-1", "exec-1", "candidate-1")
        issues = write_issues(
            self.artifact_path("reliable-delivery", "05-implementation", "issues.json"),
            "需要修正",
        )
        failed_review = self.run_cli(
            "review-slice", "--feature-id", "reliable-delivery",
            "--package-id", "slice-1", "--candidate-id", "candidate-1",
            "--review-execution-id", "review-1", "--result", "failed",
            "--issues-file", issues,
        )
        self.assertNotIn("friction_id", failed_review)
        retried = self.run_cli(
            "resolve-slice", "--feature-id", "reliable-delivery",
            "--action", "retry", "--package-id", "slice-1",
            "--execution-id", "exec-2",
        )
        handoff = retried["repair_handoff"]
        self.assertEqual(first["candidate_digest"], handoff["original_candidate_digest"])
        self.assertEqual("review-1", handoff["review_execution_id"])
        self.assertEqual(["需要修正"], handoff["issues"])
        self.assertEqual(
            handoff["acceptance_scope"]["digest"],
            handoff["acceptance_scope_digest"],
        )
        role_result = self.run_cli(
            "context-summary", "--feature-id", "reliable-delivery",
            "--action", "implementation", "--role", "implementation",
            "--execution-id", "exec-2",
        )
        self.assertIn("role_view", role_result, role_result)
        role = role_result["role_view"]
        projected_handoff = role["repair_handoff"]
        for field, value in handoff.items():
            self.assertEqual(value, projected_handoff[field])
        self.assertEqual("candidate-1", projected_handoff["original_candidate"]["candidate_id"])
        self.assertEqual(first["candidate_digest"], projected_handoff["original_candidate"]["candidate_digest"])
        self.assertEqual(first["changes"], projected_handoff["original_candidate"]["changes"])
        self.assertEqual(first["verification"], projected_handoff["original_candidate"]["verification"])
        self.assertEqual(
            [{
                "failed_scenario": "需要修正",
                "affected_validations": handoff["acceptance_scope"]["validations"],
            }],
            projected_handoff["retest_requirements"],
        )
        self.assertEqual(projected_handoff, handoff)
        state = json.loads(self.root.joinpath("reliable-delivery/workflow-state.json").read_text(encoding="utf-8"))
        stored = state["execution"]["slices"]["slice-1"]["repair_handoff"]
        self.assertNotIn("original_candidate", stored)
        self.assertNotIn("retest_requirements", stored)
        second = self.submit("slice-1", "exec-2", "candidate-2")
        self.assertEqual(first["candidate_digest"], second["repair_source"]["original_candidate_digest"])
        review_result = self.run_cli(
            "context-summary", "--feature-id", "reliable-delivery",
            "--action", "implementation", "--role", "review",
            "--execution-id", "review-2",
        )
        self.assertIn("role_view", review_result, review_result)
        review_role = review_result["role_view"]
        for field, value in second["repair_source"].items():
            self.assertEqual(value, review_role["repair_handoff"][field])
        self.assertEqual("candidate-1", review_role["repair_handoff"]["original_candidate"]["candidate_id"])
        self.assertEqual("candidate-2", review_role["review_handoff"]["candidate"]["candidate_id"])
        self.assertEqual(
            second["candidate_digest"],
            review_role["review_handoff"]["candidate"]["candidate_digest"],
        )
        self.run_cli(
            "review-slice", "--feature-id", "reliable-delivery",
            "--package-id", "slice-1", "--candidate-id", "candidate-2",
            "--review-execution-id", "review-2", "--result", "passed",
        )
        state = json.loads(
            self.root.joinpath("reliable-delivery", "workflow-state.json").read_text(encoding="utf-8")
        )["execution"]["slices"]["slice-1"]
        self.assertNotIn("attempt_history", state)
        self.assertNotIn("review_history", state)
        snapshots = state["review_snapshots"]
        self.assertEqual(2, len(snapshots))
        self.assertEqual(["failed", "passed"], [item["review"]["result"] for item in snapshots])
        self.assertEqual("candidate-1", snapshots[0]["candidate"]["candidate_id"])
        self.assertEqual("candidate-2", snapshots[1]["candidate"]["candidate_id"])
        self.assertEqual(
            snapshots[0]["snapshot_digest"],
            handoff["review_snapshot_digest"],
        )
