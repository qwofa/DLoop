from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from archive_slice_plan import digest as plan_digest, eligibility, normalize_plan

try:
    from _feature_archive_support import FeatureArchiveCliTestCase, write_candidate
except ModuleNotFoundError:
    from ._feature_archive_support import FeatureArchiveCliTestCase, write_candidate


class FeatureArchiveSlicePlanTests(FeatureArchiveCliTestCase):
    real_snapshots = False

    def setUp(self) -> None:
        super().setUp()
        self.archive = self.init_complex()
        self.approve_requirements()
        self.prepare_execution_inputs()

    def write_plan(
        self,
        version: int,
        declarations: list[dict],
        name: str = "plan",
    ) -> Path:
        path = self.artifact_path(
            "reliable-delivery", "04-plan", f"{name}-{version}.json"
        )
        path.write_text(json.dumps({
            "schema_version": 1,
            "plan_id": "reliable-delivery",
            "version": version,
            "slices": declarations,
        }, ensure_ascii=False), encoding="utf-8")
        return path

    @staticmethod
    def declarations(count: int) -> list[dict]:
        return [
            {"slice_id": f"slice-{index}", "prerequisites": [], "replacements": []}
            for index in range(1, count + 1)
        ]

    def check(self, path: Path, expected: int = 0):
        return self.run_cli(
            "check-slice-plan", "--feature-id", "reliable-delivery", "--plan-file", path,
            expected=expected,
        )

    def approve(self, path: Path, plan_digest: str, expected: int = 0):
        return self.run_cli(
            "approve-slice-plan", "--feature-id", "reliable-delivery", "--plan-file", path,
            "--plan-digest", plan_digest, expected=expected,
        )

    def state(self) -> dict:
        return json.loads(self.archive.joinpath("workflow-state.json").read_text(encoding="utf-8"))

    def test_every_plan_check_and_status_expose_derived_graph_and_eligibility(self) -> None:
        plan = self.write_plan(3, self.declarations(6))
        checked = self.check(plan)
        self.assertEqual("valid", checked["status"])
        self.assertEqual(6, checked["slice_count"])
        self.assertEqual(6, len(checked["graph"]["nodes"]))
        self.assertEqual([], checked["graph"]["edges"])
        self.assertEqual(6, len(checked["eligibility"]["can_start"]))

        approved = self.approve(plan, checked["plan_digest"])
        self.assertEqual(checked["graph"], approved["graph"])
        self.assertEqual(checked["eligibility"], approved["eligibility"])
        plan_state = self.state()["execution"]["slice_plan"]
        self.assertEqual({"current_version", "history"}, set(plan_state))
        serialized = json.dumps(plan_state, ensure_ascii=False)
        self.assertNotIn('"graph"', serialized)
        self.assertNotIn('"eligibility"', serialized)

        status = self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery",
        )["delivery_view"]["conclusions"]["slice_plan"]
        self.assertEqual(checked["graph"], status["graph"])
        self.assertEqual(checked["eligibility"], status["eligibility"])

    def test_cli_help_describes_plan_check_and_atomic_approval_inputs(self) -> None:
        root_help = self.run_help()
        self.assertIn("check-slice-plan", root_help)
        self.assertIn("approve-slice-plan", root_help)
        self.assertIn("--plan-file", self.run_help("check-slice-plan"))
        approval_help = self.run_help("approve-slice-plan")
        self.assertIn("--plan-file", approval_help)
        self.assertIn("--plan-digest", approval_help)

    def test_seven_slice_graph_has_typed_directional_edges_and_live_eligibility(self) -> None:
        declarations = self.declarations(7)
        declarations[1]["prerequisites"] = ["slice-1"]
        declarations[2]["replacements"] = ["slice-4"]
        plan = self.write_plan(1, declarations)
        checked = self.check(plan)

        self.assertEqual(7, checked["slice_count"])
        self.assertIn(
            {"from": "slice-1", "to": "slice-2", "type": "prerequisite"},
            checked["graph"]["edges"],
        )
        self.assertIn(
            {"from": "slice-4", "to": "slice-3", "type": "replacement"},
            checked["graph"]["edges"],
        )
        self.assertIn("slice-2", checked["eligibility"]["waiting_prerequisite_acceptance"])
        self.assertIn("slice-3", checked["eligibility"]["waiting_replacement_completion"])

        normalized = normalize_plan(json.loads(plan.read_text(encoding="utf-8")), "reliable-delivery")
        live = eligibility(normalized, {
            "slice-1": {"status": "accepted"},
            "slice-4": {"status": "accepted"},
        })
        self.assertIn("slice-2", live["can_start"])
        self.assertIn("slice-3", live["can_start"])

    def test_eligibility_preserves_both_direct_waiting_reasons(self) -> None:
        declarations = self.declarations(7)
        declarations[6]["prerequisites"] = ["slice-1", "slice-2"]
        declarations[6]["replacements"] = ["slice-3"]
        plan = normalize_plan({
            "schema_version": 1,
            "plan_id": "reliable-delivery",
            "version": 1,
            "slices": declarations,
        }, "reliable-delivery")
        result = eligibility(plan, {"slice-1": {"status": "accepted"}})
        item = next(value for value in result["items"] if value["slice_id"] == "slice-7")
        self.assertFalse(item["can_start"])
        self.assertEqual(["slice-2"], item["pending_prerequisites"])
        self.assertEqual(["slice-3"], item["pending_replacements"])
        self.assertIn("slice-7", result["waiting_prerequisite_acceptance"])
        self.assertIn("slice-7", result["waiting_replacement_completion"])

    def test_illegal_relation_classes_return_reproducible_chains_for_small_plans(self) -> None:
        cases = {}

        declarations = self.declarations(2)
        declarations[0]["prerequisites"] = ["slice-1"]
        cases["SLICE_PLAN_SELF_REFERENCE"] = declarations

        declarations = self.declarations(2)
        declarations[0]["prerequisites"] = ["missing"]
        cases["SLICE_PLAN_MISSING_REFERENCE"] = declarations

        declarations = self.declarations(2)
        declarations[0]["prerequisites"] = ["slice-2", "slice-2"]
        cases["SLICE_PLAN_DUPLICATE_RELATION"] = declarations

        declarations = self.declarations(2)
        declarations[0]["prerequisites"] = ["slice-2"]
        declarations[1]["prerequisites"] = ["slice-1"]
        cases["SLICE_PLAN_PREREQUISITE_CYCLE"] = declarations

        declarations = self.declarations(2)
        declarations[0]["replacements"] = ["slice-2"]
        declarations[1]["replacements"] = ["slice-1"]
        cases["SLICE_PLAN_REPLACEMENT_CYCLE"] = declarations

        declarations = self.declarations(2)
        declarations[0]["prerequisites"] = ["slice-2"]
        declarations[1]["replacements"] = ["slice-1"]
        cases["SLICE_PLAN_MIXED_CYCLE"] = declarations

        for index, (code, declarations) in enumerate(cases.items(), start=1):
            with self.subTest(code=code):
                plan = self.write_plan(index, declarations, code.lower())
                first = self.check(plan, expected=1)
                second = self.check(plan, expected=1)
                self.assertEqual(code, first["code"])
                self.assertTrue(first["relation_chain"])
                self.assertEqual(first["relation_chain"], second["relation_chain"])
                self.assertTrue(all(set(edge) == {"from", "to", "type"} for edge in first["relation_chain"]))

    def test_approval_history_is_immutable_idempotent_and_versioned(self) -> None:
        first_plan = self.write_plan(5, self.declarations(2), "first")
        first_check = self.check(first_plan)
        first = self.approve(first_plan, first_check["plan_digest"])
        self.assertFalse(first["idempotent"])
        replay = self.approve(first_plan, first_check["plan_digest"])
        self.assertTrue(replay["idempotent"])

        rewritten = self.declarations(2)
        rewritten[1]["prerequisites"] = ["slice-1"]
        rewritten_plan = self.write_plan(5, rewritten, "rewritten")
        rewritten_check = self.check(rewritten_plan)
        conflict = self.approve(rewritten_plan, rewritten_check["plan_digest"], expected=1)
        self.assertEqual("SLICE_PLAN_SILENT_REWRITE", conflict["code"])

        downgraded = self.write_plan(4, self.declarations(2), "downgrade")
        downgraded_check = self.check(downgraded)
        downgrade = self.approve(downgraded, downgraded_check["plan_digest"], expected=1)
        self.assertEqual("SLICE_PLAN_DOWNGRADE", downgrade["code"])

        next_plan = self.write_plan(6, rewritten, "next")
        next_check = self.check(next_plan)
        next_approval = self.approve(next_plan, next_check["plan_digest"])
        self.assertEqual(6, next_approval["version"])
        state = self.state()["execution"]["slice_plan"]
        self.assertEqual([5, 6], [item["version"] for item in state["history"]])
        self.assertEqual(6, state["current_version"])

    def test_digest_drift_does_not_modify_current_plan(self) -> None:
        plan = self.write_plan(1, self.declarations(2))
        self.check(plan)
        before = self.state()["execution"]["slice_plan"]
        result = self.approve(plan, "sha256:" + "0" * 64, expected=1)
        self.assertEqual("SLICE_PLAN_DIGEST_MISMATCH", result["code"])
        self.assertEqual(before, self.state()["execution"]["slice_plan"])

    def test_start_requires_membership_and_live_prerequisite_eligibility(self) -> None:
        declarations = self.declarations(2)
        declarations[1]["prerequisites"] = ["slice-1"]
        plan = self.write_plan(1, declarations)
        checked = self.check(plan)
        self.approve(plan, checked["plan_digest"])

        blocked = self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-2", "--package-file", self.write_package("slice-2"),
            "--workspace-root", self.workspace, expected=1,
        )
        self.assertEqual("SLICE_NOT_ELIGIBLE", blocked["code"])
        blocked_state = self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery",
        )
        self.assertEqual({}, blocked_state["delivery_view"]["conclusions"]["slices"])
        self.assertIsNone(blocked_state["delivery_view"]["trusted_machine_facts"]["modification_lease"])

        started = self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-1", "--package-file", self.write_package("slice-1"),
            "--workspace-root", self.workspace,
        )
        self.assertEqual(1, started["slice_plan_binding"]["plan_version"])

    def test_start_without_an_approved_plan_is_rejected_without_persisting_a_slice(self) -> None:
        package = self.write_package("slice-1", approve_plan=False)
        rejected = self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-no-plan", "--package-file", package,
            "--workspace-root", self.workspace, expected=1,
        )

        self.assertEqual("SLICE_PLAN_APPROVAL_REQUIRED", rejected["code"])
        state = self.state()["execution"]
        self.assertEqual([], state["slice_plan"]["history"])
        self.assertEqual({}, state["slices"])

    def test_start_without_plan_precedes_failed_contract_and_keeps_state_empty(self) -> None:
        package = self.write_package("slice-1", approve_plan=False)
        value = json.loads(package.read_text(encoding="utf-8"))
        value["slice_contract"]["dependency_assumptions"] = [{
            "name": "外部强依赖",
            "assumption": "实施前必须可用",
            "strength": "strong",
            "availability": "unavailable",
            "long_term": False,
            "responsibility_domain": "外部系统",
        }]
        value["contract_check"]["strong_dependencies_available"] = {
            "passed": False,
            "evidence": "外部强依赖当前不可用",
        }
        package.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

        rejected = self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-no-plan-contract-failed", "--package-file", package,
            "--workspace-root", self.workspace, expected=1,
        )

        self.assertEqual("SLICE_PLAN_APPROVAL_REQUIRED", rejected["code"])
        state = self.state()["execution"]
        self.assertEqual([], state["slice_plan"]["history"])
        self.assertEqual({}, state["slices"])
        self.assertIsNone(self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery",
        )["delivery_view"]["trusted_machine_facts"]["modification_lease"])

    def test_start_outside_current_plan_is_rejected_without_persisting_a_slice(self) -> None:
        plan = self.write_plan(1, self.declarations(1), "membership")
        checked = self.check(plan)
        self.approve(plan, checked["plan_digest"])
        package = self.write_package("slice-2", approve_plan=False)

        rejected = self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-outside-plan", "--package-file", package,
            "--workspace-root", self.workspace, expected=1,
        )

        self.assertEqual("SLICE_NOT_IN_CURRENT_PLAN", rejected["code"])
        state = self.state()["execution"]
        self.assertEqual([1], [item["version"] for item in state["slice_plan"]["history"]])
        self.assertEqual({}, state["slices"])
        self.assertIsNone(self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery",
        )["delivery_view"]["trusted_machine_facts"]["modification_lease"])

    def test_candidate_contains_fixed_complete_plan_binding_and_repointing_is_invalid(self) -> None:
        first_plan = self.write_plan(1, self.declarations(2), "candidate-first")
        checked = self.check(first_plan)
        self.approve(first_plan, checked["plan_digest"])
        self.run_cli(
            "transition-lifecycle", "--feature-id", "reliable-delivery", "--to", "active",
        )
        self.run_cli(
            "stage-action", "--feature-id", "reliable-delivery",
            "--stage", "architecture", "--decision", "approve",
        )
        started = self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-candidate", "--package-file", self.write_package("slice-1"),
            "--workspace-root", self.workspace,
        )
        self.workspace.joinpath("slice-1.txt").write_text("candidate\n", encoding="utf-8")
        self.record_checkpoint("exec-candidate", identifier="candidate-binding-final")
        submitted = self.run_cli(
            "submit-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-candidate", "--status", "completed",
            "--candidate-file", write_candidate(
                self.artifact_path(
                    "reliable-delivery", "05-implementation", "candidate-binding.json"
                ),
                "candidate-binding",
            ),
        )

        binding = submitted["slice_plan_binding"]
        self.assertEqual(
            started["slice_plan_binding"]["plan_version"],
            binding["plan_version"],
        )
        self.assertEqual(
            started["slice_plan_binding"]["plan_digest"],
            binding["plan_digest"],
        )
        self.assertEqual(
            {"plan_version", "plan_digest", "history_ref"},
            set(binding),
        )
        candidate_digest = submitted["candidate_digest"]

        second_plan = self.write_plan(2, self.declarations(3), "candidate-second")
        second_checked = self.check(second_plan)
        self.approve(second_plan, second_checked["plan_digest"])
        state = self.state()
        candidate = state["execution"]["slices"]["slice-1"]["candidate"]
        self.assertEqual(1, candidate["slice_plan_binding"]["plan_version"])
        self.assertEqual(candidate_digest, candidate["candidate_digest"])

        review_context = self.run_cli(
            "context-summary", "--feature-id", "reliable-delivery",
            "--action", "implementation", "--role", "review",
            "--execution-id", "review-candidate-binding",
        )
        self.assertIn("role_view", review_context, review_context)
        self.assertEqual(
            binding,
            review_context["role_view"]["source_rechecks"]["slice_plan_binding"],
        )

        second_history = state["execution"]["slice_plan"]["history"][-1]
        candidate["slice_plan_binding"] = {
            "plan_version": second_history["version"],
            "plan_digest": second_history["plan_digest"],
            "history_ref": "slice_plan.history",
        }
        self.archive.joinpath("workflow-state.json").write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8"
        )
        invalid = self.run_cli("validate", expected=1)
        self.assertEqual("INVALID_WORKFLOW_STATE", invalid["code"])

    def test_active_slice_relationship_cannot_be_changed_by_upgrade(self) -> None:
        initial = self.write_plan(1, self.declarations(2), "initial")
        checked = self.check(initial)
        self.approve(initial, checked["plan_digest"])
        self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-1", "--package-file", self.write_package("slice-1"),
            "--workspace-root", self.workspace,
        )

        changed = self.declarations(2)
        changed[0]["prerequisites"] = ["slice-2"]
        upgraded = self.write_plan(2, changed, "upgraded")
        upgraded_check = self.check(upgraded)
        rejected = self.approve(upgraded, upgraded_check["plan_digest"], expected=1)
        self.assertEqual("SLICE_PLAN_FACT_CONFLICT", rejected["code"])

    def test_validate_rejects_persisted_derived_graph(self) -> None:
        plan = self.write_plan(1, self.declarations(7))
        checked = self.check(plan)
        self.approve(plan, checked["plan_digest"])
        state = self.state()
        state["execution"]["slice_plan"]["graph"] = checked["graph"]
        self.archive.joinpath("workflow-state.json").write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8"
        )
        result = self.run_cli("validate", expected=1)
        self.assertEqual("INVALID_WORKFLOW_STATE", result["code"])

    def test_validate_rechecks_every_persisted_large_plan_graph_semantic(self) -> None:
        plan = self.write_plan(1, self.declarations(7), "valid-history")
        checked = self.check(plan)
        self.approve(plan, checked["plan_digest"])
        pristine = self.state()

        def self_reference(declarations: list[dict]) -> None:
            declarations[0]["prerequisites"] = ["slice-1"]

        def missing_reference(declarations: list[dict]) -> None:
            declarations[0]["prerequisites"] = ["missing"]

        def duplicate_relation(declarations: list[dict]) -> None:
            declarations[0]["prerequisites"] = ["slice-2", "slice-2"]

        def prerequisite_cycle(declarations: list[dict]) -> None:
            declarations[0]["prerequisites"] = ["slice-2"]
            declarations[1]["prerequisites"] = ["slice-1"]

        def replacement_cycle(declarations: list[dict]) -> None:
            declarations[0]["replacements"] = ["slice-2"]
            declarations[1]["replacements"] = ["slice-1"]

        def mixed_cycle(declarations: list[dict]) -> None:
            declarations[0]["prerequisites"] = ["slice-2"]
            declarations[1]["replacements"] = ["slice-1"]

        cases = {
            "self": self_reference,
            "missing": missing_reference,
            "duplicate": duplicate_relation,
            "prerequisite-cycle": prerequisite_cycle,
            "replacement-cycle": replacement_cycle,
            "mixed-cycle": mixed_cycle,
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                state = json.loads(json.dumps(pristine))
                history = state["execution"]["slice_plan"]["history"][0]
                mutate(history["plan"]["slices"])
                history["plan_digest"] = plan_digest(history["plan"])
                self.archive.joinpath("workflow-state.json").write_text(
                    json.dumps(state, ensure_ascii=False), encoding="utf-8"
                )
                invalid = self.run_cli("validate", expected=1)
                self.assertEqual("INVALID_WORKFLOW_STATE", invalid["code"])

        self.archive.joinpath("workflow-state.json").write_text(
            json.dumps(pristine, ensure_ascii=False), encoding="utf-8"
        )


if __name__ == "__main__":
    import unittest
    unittest.main()
