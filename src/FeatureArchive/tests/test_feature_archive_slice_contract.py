from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from archive_slice_contract import HARD_BREAKER_RULES

try:
    from _feature_archive_support import FeatureArchiveCliTestCase, write_candidate
except ModuleNotFoundError:
    from ._feature_archive_support import FeatureArchiveCliTestCase, write_candidate


class FeatureArchiveSliceContractTests(FeatureArchiveCliTestCase):
    real_snapshots = False

    def setUp(self) -> None:
        super().setUp()
        self.init_complex()
        self.approve_requirements()
        self.prepare_execution_inputs()

    def value(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def write_value(self, path: Path, value: dict) -> Path:
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def start(self, package: Path, execution_id: str = "exec-1"):
        return self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", execution_id, "--package-file", package,
            "--workspace-root", self.workspace,
        )

    def checkpoint(
        self,
        identifier: str,
        *,
        hard: list[dict] | None = None,
        validation_status: str = "failed",
        expected: int = 0,
        execution_id: str = "exec-1",
    ):
        value = {
            "contract_version": 1,
            "hypothesis": "当前基础假设仍成立",
            "change_summary": f"完成 {identifier} 对应的一轮实现和验证",
            "validation_results": [{
                "method": "执行聚焦验证",
                "status": validation_status,
                "evidence": ["聚焦场景验证结果已记录"],
            }],
            "discoveries": ["记录了新的边界事实"],
            "next_step": "按检查结论继续或停止实施",
            "observed_breakers": hard or [],
        }
        path = self.write_value(
            self.artifact_path(
                "reliable-delivery", "05-implementation", f"{identifier}.json"
            ),
            value,
        )
        return self.run_cli(
            "checkpoint-slice", "--feature-id", "reliable-delivery",
            "--execution-id", execution_id, "--checkpoint-file", path, expected=expected,
        )

    def hard_break(self, package: Path, execution_id: str = "exec-1") -> dict:
        self.start(package, execution_id)
        return self.checkpoint(
            "hard-1",
            hard=[{
                "rule": "strong_dependency_invalid",
                "failed_assumption": "既有依赖实际不可用",
                "evidence": "运行场景证实接口行为与契约不符",
            }],
            execution_id=execution_id,
        )

    def test_missing_contract_cannot_enter_implementation(self) -> None:
        package = self.write_package("slice-1")
        value = self.value(package)
        value.pop("slice_contract")
        value.pop("contract_check")
        self.write_value(package, value)

        result = self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-1", "--package-file", package,
            "--workspace-root", self.workspace, expected=1,
        )

        self.assertEqual("INVALID_TASK_PACKAGE", result["code"])
        status = self.run_cli("workflow-status", "--feature-id", "reliable-delivery")
        self.assertNotIn("slice-1", status["delivery_view"]["conclusions"]["slices"])

    def test_failed_contract_check_is_reviewable_and_blocks_start(self) -> None:
        package = self.write_package("slice-1")
        value = self.value(package)
        value["contract_check"]["regression_protection"] = {
            "passed": False,
            "evidence": "尚无既有行为回归保护",
        }
        self.write_value(package, value)

        checked = self.run_cli(
            "check-slice-contract", "--feature-id", "reliable-delivery",
            "--package-file", package, expected=1,
        )
        self.assertEqual("contract_failed", checked["status"])
        self.assertIn("supplement-contract", checked["contract_check"]["recommended_actions"])
        self.assertTrue(checked["contract_check"]["failure_reasons"])

        blocked = self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-1", "--package-file", package,
            "--workspace-root", self.workspace, expected=1,
        )
        self.assertEqual("contract_failed", blocked["status"])
        self.assertIsNone(self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery"
        )["delivery_view"]["trusted_machine_facts"]["modification_lease"])

    def test_start_with_approved_plan_persists_failed_contract_without_a_lease(self) -> None:
        package = self.write_package("slice-1")
        value = self.value(package)
        value["contract_check"]["regression_protection"] = {
            "passed": False,
            "evidence": "尚无既有行为回归保护",
        }
        self.write_value(package, value)

        blocked = self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-1", "--package-file", package,
            "--workspace-root", self.workspace, expected=1,
        )

        self.assertEqual("contract_failed", blocked["status"])
        status = self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery"
        )
        self.assertEqual("contract_failed", status["delivery_view"]["conclusions"]["slices"]["slice-1"]["status"])
        state = self.value(self.root / "reliable-delivery" / "workflow-state.json")
        self.assertEqual(1, len(state["execution"]["slices"]["slice-1"]["contract_history"]))
        self.assertIsNone(status["delivery_view"]["trusted_machine_facts"]["modification_lease"])

    def test_unstarted_slice_termination_preserves_another_active_slice(self) -> None:
        self.start(self.write_package("active-slice"))
        changed = self.workspace / "active-slice.txt"
        changed.write_text("正在实施的成果", encoding="utf-8")
        lease_path = self.root / ".feature-archive-workspace-state.json"
        lease_before = lease_path.read_bytes()
        for status in ("contract_draft", "contract_failed", "ready"):
            with self.subTest(status=status):
                package_id = f"unused-{status}"
                package = self.write_package(package_id)
                if status == "contract_failed":
                    value = self.value(package)
                    value["contract_check"]["independently_acceptable"] = {
                        "passed": False, "evidence": "需要重新划分独立验收范围",
                    }
                    self.write_value(package, value)
                checked = self.run_cli(
                    "prepare-slice-contract" if status == "contract_draft" else "check-slice-contract",
                    "--feature-id", "reliable-delivery", "--package-file", package,
                    expected=1 if status == "contract_failed" else 0,
                )
                self.assertEqual(status, checked["status"])
                arguments = (
                    "resolve-slice", "--feature-id", "reliable-delivery",
                    "--action", "release", "--package-id", package_id,
                    "--workspace-decision", "kept",
                )
                released = self.run_cli(*arguments)
                self.assertEqual("abandoned", released["status"])
                self.assertFalse(released["idempotent"])
                self.assertTrue(self.run_cli(*arguments)["idempotent"])
                current = self.run_cli("workflow-status", "--feature-id", "reliable-delivery")
                self.assertEqual("implementation", current["delivery_view"]["current_stage"])
                self.assertEqual(lease_before, lease_path.read_bytes())
                self.assertEqual("正在实施的成果", changed.read_text(encoding="utf-8"))

    def test_replacement_after_failed_contract_can_finish_delivery(self) -> None:
        feature_id = "reliable-delivery"
        self.run_cli("transition-lifecycle", "--feature-id", feature_id, "--to", "active")
        package = self.write_package("slice-1")
        value = self.value(package)
        value["contract_check"]["independently_acceptable"] = {
            "passed": False, "evidence": "需要重新划分独立验收范围",
        }
        self.write_value(package, value)
        checked = self.run_cli(
            "check-slice-contract", "--feature-id", feature_id,
            "--package-file", package, expected=1,
        )
        self.assertIn("new-slice", checked["contract_check"]["recommended_actions"])
        self.run_cli(
            "resolve-slice", "--feature-id", feature_id, "--action", "release",
            "--package-id", "slice-1", "--workspace-decision", "kept",
        )
        status = self.run_cli("workflow-status", "--feature-id", feature_id)
        self.assertEqual("implementation-required", status["delivery_view"]["current_stage"])
        self.assertIsNone(status["delivery_view"]["trusted_machine_facts"]["modification_lease"])
        state = self.value(self.root / feature_id / "workflow-state.json")
        original = state["execution"]["slices"]["slice-1"]
        self.assertIsNone(original["execution_id"])
        self.assertEqual("failed", original["contract_check"]["status"])
        self.assertEqual(1, len(original["contract_history"]))

        plan = self.write_value(self.artifact_path(feature_id, "04-plan", "replacement-plan.json"), {
            "schema_version": 1, "plan_id": feature_id, "version": 2,
            "slices": [{"slice_id": "slice-2", "prerequisites": [], "replacements": []}],
        })
        checked_plan = self.run_cli("check-slice-plan", "--feature-id", feature_id, "--plan-file", plan)
        self.run_cli(
            "approve-slice-plan", "--feature-id", feature_id, "--plan-file", plan,
            "--plan-digest", checked_plan["plan_digest"],
        )
        self.start(self.write_package("slice-2", approve_plan=False), "exec-2")
        self.workspace.joinpath("slice-2.txt").write_text("实现新切片", encoding="utf-8")
        self.checkpoint("replacement-done", execution_id="exec-2", validation_status="passed")
        candidate = write_candidate(
            self.artifact_path(feature_id, "05-implementation", "candidate-2.json"), "candidate-2",
        )
        self.run_cli(
            "submit-slice", "--feature-id", feature_id, "--execution-id", "exec-2",
            "--status", "completed", "--candidate-file", candidate,
        )
        self.run_cli(
            "review-slice", "--feature-id", feature_id, "--package-id", "slice-2",
            "--candidate-id", "candidate-2", "--review-execution-id", "review-2", "--result", "passed",
        )
        self.set_status(self.root / feature_id / "06-validation" / "README.md", "completed")
        self.run_cli("transition-lifecycle", "--feature-id", feature_id, "--to", "validating")
        final = self.approve_final_with_confirmation()
        self.assertEqual("approved", final["status"])
        self.run_cli("validate")

    def test_ordinary_implementation_failure_records_checkpoint_and_continues(self) -> None:
        self.start(self.write_package("slice-1"))

        result = self.checkpoint("ordinary-1")

        self.assertEqual("active", result["status"])
        self.assertEqual("continue", result["checkpoint"]["result"])
        status = self.run_cli("workflow-status", "--feature-id", "reliable-delivery")
        self.assertEqual(1, status["delivery_view"]["conclusions"]["slices"]["slice-1"]["checkpoint_count"])
        self.assertIsNone(status["delivery_view"]["conclusions"]["slices"]["slice-1"]["breaker_report"])

    def test_unverified_environment_result_blocks_candidate_without_product_breaker(self) -> None:
        self.start(self.write_package("slice-1"))
        self.workspace.joinpath("slice-1.txt").write_text("candidate", encoding="utf-8")

        first = self.checkpoint("environment-1", validation_status="unverified")
        second = self.checkpoint("environment-2", validation_status="unverified")
        candidate = write_candidate(
            self.artifact_path("reliable-delivery", "05-implementation", "candidate.json"),
            "candidate-1",
        )
        blocked = self.run_cli(
            "submit-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-1", "--status", "completed",
            "--candidate-file", candidate, expected=1,
        )

        self.assertEqual("continue", first["checkpoint"]["result"])
        self.assertEqual("continue", second["checkpoint"]["result"])
        self.assertEqual([], second["checkpoint"]["triggered_rules"])
        self.assertEqual("FINAL_CHECKPOINT_NOT_PASSED", blocked["code"])

    def test_task_package_is_single_source_for_validation_level_and_rationale(self) -> None:
        package = self.write_package(
            "slice-1",
            validation_level="module_full",
            validation_rationale="修改共享核心和持久状态",
        )
        started = self.start(package)
        checkpoint = self.checkpoint("module-full", validation_status="passed")

        task = started["execution_handoff"]["task_package"]
        self.assertEqual("module_full", task["slice_contract"]["validation_level"])
        self.assertEqual(
            "修改共享核心和持久状态",
            task["slice_contract"]["validation_rationale"],
        )
        self.assertNotIn("validation_level", started["execution_handoff"])
        self.assertNotIn("validation_rationale", started["execution_handoff"])
        self.assertNotIn("validation_level", checkpoint["checkpoint"])
        self.assertNotIn("validation_rationale", checkpoint["checkpoint"])

    def test_intermediate_checkpoint_can_record_only_executed_validations(self) -> None:
        package = self.write_package("slice-1")
        value = self.value(package)
        value["slice_contract"]["validation_methods"].append("执行模块回归")
        self.write_value(package, value)
        self.start(package)
        self.workspace.joinpath("slice-1.txt").write_text("candidate", encoding="utf-8")

        checkpoint = self.checkpoint("partial", validation_status="passed")
        candidate = write_candidate(
            self.artifact_path("reliable-delivery", "05-implementation", "candidate.json"),
            "candidate-1",
        )
        blocked = self.run_cli(
            "submit-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-1", "--status", "completed",
            "--candidate-file", candidate, expected=1,
        )

        self.assertEqual(["执行聚焦验证"], [
            item["method"] for item in checkpoint["checkpoint"]["validation_results"]
        ])
        self.assertEqual("FINAL_CHECKPOINT_STALE", blocked["code"])

    def test_identical_checkpoint_retry_is_idempotent_until_workspace_changes(self) -> None:
        self.start(self.write_package("slice-1"))

        first = self.checkpoint("retryable", validation_status="passed")
        second = self.checkpoint("retryable", validation_status="passed")

        self.assertTrue(second["idempotent"])
        self.assertEqual(first["checkpoint"], second["checkpoint"])
        self.workspace.joinpath("slice-1.txt").write_text("changed", encoding="utf-8")
        changed = self.checkpoint("retryable", validation_status="passed")
        self.assertFalse(changed["idempotent"])
        self.assertEqual("exec-1-checkpoint-2", changed["checkpoint"]["checkpoint_id"])

    def test_slice_contract_rejects_unknown_validation_level(self) -> None:
        package = self.write_package("slice-1")
        value = self.value(package)
        value["slice_contract"]["validation_level"] = "workflow_snapshot_full"
        invalid = self.write_value(
            self.artifact_path("reliable-delivery", "05-implementation", "invalid-level.json"),
            value,
        )

        result = self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-1", "--package-file", invalid,
            "--workspace-root", self.workspace, expected=1,
        )

        self.assertEqual("INVALID_SLICE_CONTRACT", result["code"])

    def test_slice_contract_accepts_all_distinct_observable_scenarios(self) -> None:
        package = self.write_package("slice-1")
        value = self.value(package)
        scenarios = [f"可观察验收场景 {index}" for index in range(1, 8)]
        value["slice_contract"]["acceptance_scenarios"] = scenarios
        self.write_value(package, value)

        result = self.start(package)

        self.assertEqual("active", result["status"])
        self.assertEqual(scenarios, result["task"]["acceptance_conditions"])

    def test_hard_rule_opens_circuit_and_requires_report(self) -> None:
        package = self.write_package("slice-1")
        self.start(package)
        hits = [{
            "rule": rule,
            "failed_assumption": f"{rule} 对应的契约假设失效",
            "evidence": "实施事实已由人工显式确认",
        } for rule in HARD_BREAKER_RULES if rule != "write_scope_violation"]
        result = self.checkpoint("hard-all", hard=hits)

        self.assertEqual("circuit_open", result["status"])
        self.assertEqual(
            set(HARD_BREAKER_RULES) - {"write_scope_violation"},
            {item["rule"] for item in result["breaker_report"]["triggered_rules"]},
        )
        self.assertEqual(
            ["retry", "terminate_restored"],
            [item["action"] for item in result["breaker_report"]["allowed_actions"]],
        )
        blocked = self.checkpoint("hard-2", expected=1)
        self.assertEqual("IMPLEMENTATION_HALTED", blocked["code"])
        view = self.run_cli(
            "context-summary", "--feature-id", "reliable-delivery",
            "--action", "implementation", "--role", "coordinator",
        )
        self.assertEqual("implementation-recovery", view["delivery_view"]["current_stage"])
        self.assertTrue(view["delivery_view"]["requires_human"]["required"])
        self.assertEqual(
            "resolve-slice",
            view["delivery_view"]["next_action_contract"]["command"],
        )

    def test_repeated_validation_failure_stays_with_implementation_ai(self) -> None:
        self.start(self.write_package("slice-1"))

        first = self.checkpoint("patch-1")
        second = self.checkpoint("patch-2")
        third = self.checkpoint("patch-3")

        self.assertEqual("active", first["status"])
        self.assertEqual("active", second["status"])
        self.assertEqual("active", third["status"])
        self.assertEqual([], third["checkpoint"]["triggered_rules"])

    def test_passed_validations_never_become_no_improvement_breaker(self) -> None:
        self.start(self.write_package("slice-1"))

        first = self.checkpoint("passed-1", validation_status="passed")
        second = self.checkpoint("passed-2", validation_status="passed")

        self.assertEqual("active", first["status"])
        self.assertEqual("active", second["status"])
        self.assertFalse(second["checkpoint"]["boundary_crossed"])

    def test_passed_contract_cannot_be_downgraded_by_same_version_prepare(self) -> None:
        package = self.write_package("slice-1")
        checked = self.run_cli(
            "check-slice-contract", "--feature-id", "reliable-delivery",
            "--package-file", package,
        )
        prepared = self.run_cli(
            "prepare-slice-contract", "--feature-id", "reliable-delivery",
            "--package-file", package,
        )

        self.assertEqual("ready", checked["status"])
        self.assertEqual("ready", prepared["status"])
        self.assertEqual("passed", prepared["contract_check"]["status"])

    def test_boundary_is_derived_and_old_self_reported_boundary_fields_are_rejected(self) -> None:
        self.start(self.write_package("slice-1"))
        value = {
            "contract_version": 1,
            "hypothesis": "边界仍成立",
            "change_summary": "没有合法熔断事实",
            "validation_results": [{
                "method": "执行聚焦验证", "status": "passed", "evidence": ["验证通过"],
            }],
            "discoveries": [],
            "next_step": "继续",
            "observed_breakers": [],
            "boundary_crossed": True,
            "boundary_rules": [],
        }
        path = self.write_value(
            self.artifact_path(
                "reliable-delivery", "05-implementation", "ambiguous-boundary.json"
            ),
            value,
        )

        result = self.run_cli(
            "checkpoint-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-1", "--checkpoint-file", path, expected=1,
        )

        self.assertEqual("INVALID_CHECKPOINT", result["code"])

    def test_candidate_requires_current_fully_passed_checkpoint(self) -> None:
        self.start(self.write_package("slice-1"))
        self.workspace.joinpath("slice-1.txt").write_text("candidate", encoding="utf-8")
        candidate = write_candidate(
            self.artifact_path(
                "reliable-delivery", "05-implementation", "no-checkpoint-candidate.json"
            ),
            "candidate-1",
        )

        result = self.run_cli(
            "submit-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-1", "--status", "completed", "--candidate-file", candidate,
            expected=1,
        )

        self.assertEqual("FINAL_CHECKPOINT_REQUIRED", result["code"])

    def test_tampered_checkpoint_cannot_forge_candidate_gate(self) -> None:
        self.start(self.write_package("slice-1"))
        self.workspace.joinpath("slice-1.txt").write_text("candidate", encoding="utf-8")
        self.checkpoint("trusted-final", validation_status="passed")
        state_path = self.root / "reliable-delivery" / "workflow-state.json"
        state = self.value(state_path)
        checkpoint = state["execution"]["slices"]["slice-1"]["checkpoints"][-1]
        checkpoint["validation_results"][0]["evidence"] = ["事后伪造的证据"]
        self.write_value(state_path, state)
        candidate = write_candidate(
            self.artifact_path(
                "reliable-delivery", "05-implementation", "tampered-candidate.json"
            ),
            "candidate-tampered",
        )

        result = self.run_cli(
            "submit-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-1", "--status", "completed", "--candidate-file", candidate,
            expected=1,
        )

        self.assertEqual("INVALID_WORKFLOW_STATE", result["code"])

    def test_system_detects_actual_write_scope_violation_without_self_report(self) -> None:
        self.start(self.write_package("slice-1"))
        self.workspace.joinpath("outside.txt").write_text("outside", encoding="utf-8")

        result = self.checkpoint("scope-violation", validation_status="passed")

        self.assertEqual("circuit_open", result["status"])
        self.assertIn("write_scope_violation", result["checkpoint"]["boundary_rules"])

    def test_circuit_open_retries_only_the_original_slice_contract(self) -> None:
        package = self.write_package("slice-1")
        self.hard_break(package)

        result = self.run_cli(
            "resolve-slice", "--feature-id", "reliable-delivery",
            "--action", "retry", "--package-id", "slice-1",
            "--execution-id", "exec-2",
        )

        self.assertEqual("active", result["status"])
        state = self.value(self.root / "reliable-delivery" / "workflow-state.json")
        record = state["execution"]["slices"]["slice-1"]
        self.assertEqual(1, record["package"]["slice_contract"]["version"])
        self.assertEqual("exec-2", record["execution_id"])
        self.assertTrue(record["breaker_reports"])

    def test_retry_does_not_consume_identity_until_outside_changes_are_resolved(self) -> None:
        self.start(self.write_package("slice-1"))
        outside = self.workspace / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        self.assertEqual("circuit_open", self.checkpoint("scope", validation_status="passed")["status"])
        state = self.root / "reliable-delivery/workflow-state.json"
        lease = self.root / ".feature-archive-workspace-state.json"
        before = (state.read_bytes(), lease.read_bytes())
        args = ("resolve-slice", "--feature-id", "reliable-delivery", "--action", "retry",
                "--package-id", "slice-1", "--execution-id", "exec-2")
        blocked = self.run_cli(*args, expected=1)
        self.assertEqual("RETRY_SCOPE_UNRESOLVED", blocked["code"])
        self.assertIn("outside.txt", blocked["message"])
        self.assertIn("第 1 版", blocked["message"])
        self.assertEqual(before, (state.read_bytes(), lease.read_bytes()))
        outside.unlink()
        self.assertEqual("active", self.run_cli(*args)["status"])

    def test_circuit_open_requires_manual_restore_before_termination(self) -> None:
        package = self.write_package("slice-1")
        self.hard_break(package)
        changed = self.workspace / "slice-1.txt"
        changed.write_text("unfinished", encoding="utf-8")

        blocked = self.run_cli(
            "resolve-slice", "--feature-id", "reliable-delivery",
            "--action", "release", "--package-id", "slice-1",
            "--workspace-decision", "restored", expected=1,
        )
        self.assertEqual("WORKSPACE_NOT_RESTORED", blocked["code"])

        changed.unlink()
        released = self.run_cli(
            "resolve-slice", "--feature-id", "reliable-delivery",
            "--action", "release", "--package-id", "slice-1",
            "--workspace-decision", "restored",
        )
        self.assertEqual("abandoned", released["status"])
        self.assertIsNone(
            self.run_cli("workflow-status", "--feature-id", "reliable-delivery")["delivery_view"]["trusted_machine_facts"]["modification_lease"]
        )
