from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


EVAL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EVAL_ROOT))

from dloop_eval.core import (  # noqa: E402
    EvalContractError,
    _grade_scenario,
    _read_is_allowed,
    _validate_trace,
    compare_reports,
    grade_run,
    load_scenarios,
    prepare_run,
    render_comparison,
    render_report,
)


class DLoopBehaviorEvalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.skill = self.base / "skill"
        self.skill.mkdir()
        self.skill.joinpath("SKILL.md").write_text(
            "---\nname: dloop\ndescription: 测试 skill v3.4.2\n---\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_scenario_matrix_has_public_and_holdout_cases(self) -> None:
        public = load_scenarios(EVAL_ROOT, include_holdout=False)
        full = load_scenarios(EVAL_ROOT, include_holdout=True)

        self.assertEqual(10, len(public))
        self.assertEqual(12, len(full))
        self.assertEqual("public", {item["visibility"] for item in public}.pop())
        self.assertEqual(2, sum(item["visibility"] == "holdout" for item in full))
        self.assertEqual(len(full), len({item["id"] for item in full}))

    def test_prepare_snapshots_skill_and_refuses_overwrite(self) -> None:
        run_dir = self.base / "run"

        manifest = prepare_run(
            EVAL_ROOT,
            self.skill,
            run_dir,
            variant="baseline-v3.4.2",
            include_holdout=False,
        )

        self.assertEqual(10, len(manifest["scenario_ids"]))
        self.assertGreater(manifest["skill_material"]["entry_bytes"], 0)
        self.assertTrue(run_dir.joinpath("skill-snapshot", "SKILL.md").is_file())
        self.assertTrue(run_dir.joinpath("trace-vocabulary.md").is_file())
        self.assertEqual(
            "not_run",
            json.loads(
                run_dir.joinpath("traces", "resume-existing-delivery.json").read_text(
                    encoding="utf-8"
                )
            )["execution"]["status"],
        )
        with self.assertRaises(EvalContractError):
            prepare_run(
                EVAL_ROOT,
                self.skill,
                run_dir,
                variant="candidate-v3.4.3",
                include_holdout=False,
            )

    def test_unrun_trace_is_reported_as_not_run(self) -> None:
        run_dir = self.base / "run"
        prepare_run(
            EVAL_ROOT,
            self.skill,
            run_dir,
            variant="baseline-v3.4.2",
            include_holdout=False,
        )

        report = grade_run(EVAL_ROOT, run_dir)

        self.assertEqual(0, report["summary"]["run"])
        self.assertEqual(10, report["summary"]["not_run"])
        self.assertIsNone(report["summary"]["compliance_score"])
        self.assertEqual(
            self.skill.joinpath("SKILL.md").stat().st_size,
            report["skill_material"]["entry_bytes"],
        )
        self.assertEqual("not_run", report["scenarios"][0]["trace"]["execution"]["status"])
        self.assertIn("未运行", render_report(report))

    def test_hard_gate_failure_overrides_compliance(self) -> None:
        run_dir = self.base / "run"
        prepare_run(
            EVAL_ROOT,
            self.skill,
            run_dir,
            variant="baseline-v3.4.2",
            include_holdout=False,
        )
        trace_path = run_dir / "traces" / "resume-existing-delivery.json"
        trace = json.loads(trace_path.read_text(encoding="utf-8"))
        trace["execution"].update(
            {
                "status": "completed",
                "executor": "test",
                "started_at": "2026-09-03T08:00:00Z",
                "completed_at": "2026-09-03T08:01:00Z",
            }
        )
        trace["events"] = [
            {"sequence": 1, "type": "decision", "name": "resume_existing_delivery"},
            {"sequence": 2, "type": "command", "name": "workflow-status"},
            {"sequence": 3, "type": "command", "name": "audit"},
            {"sequence": 4, "type": "command", "name": "init"},
        ]
        trace["final"] = {
            "state": "coordinator_ready",
            "stop_reason": "ready_for_next_action",
            "handoff_to": "coordinator",
            "summary": "已恢复唯一匹配交付项。",
            "blockers": [],
        }
        trace_path.write_text(
            json.dumps(trace, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        report = grade_run(EVAL_ROOT, run_dir)
        result = next(
            item for item in report["scenarios"]
            if item["scenario_id"] == "resume-existing-delivery"
        )

        self.assertEqual("failed", result["status"])
        self.assertEqual(["no_duplicate_init"], [item["id"] for item in result["hard_gate_failures"]])
        self.assertGreater(result["compliance"]["score"], 0)

    def test_trace_contract_rejects_missing_fields_and_false_attribution(self) -> None:
        valid = {
            "schema_version": 1,
            "scenario_id": "example",
            "variant": "candidate",
            "execution": {
                "status": "completed",
                "mode": "model-protocol-dry-run",
                "executor": "test",
                "started_at": "2026-09-03T08:00:00Z",
                "completed_at": "2026-09-03T08:01:00Z",
                "capture": "self-reported-semantic-trace",
            },
            "events": [
                {
                    "sequence": 1,
                    "type": "read",
                    "category": "role_view",
                    "target": "delivery_view",
                }
            ],
            "metrics": {"turns": None, "context_tokens": None, "elapsed_ms": None},
            "final": {
                "state": "coordinator_ready",
                "stop_reason": "ready",
                "handoff_to": "coordinator",
                "summary": "ready",
                "blockers": [],
            },
        }
        invalid_cases = []

        missing_target = json.loads(json.dumps(valid))
        missing_target["events"][0].pop("target")
        invalid_cases.append(missing_target)

        wrong_mode = json.loads(json.dumps(valid))
        wrong_mode["execution"]["mode"] = "actual-cli"
        invalid_cases.append(wrong_mode)

        wrong_capture = json.loads(json.dumps(valid))
        wrong_capture["execution"]["capture"] = "host-observed"
        invalid_cases.append(wrong_capture)

        missing_executor = json.loads(json.dumps(valid))
        missing_executor["execution"]["executor"] = None
        invalid_cases.append(missing_executor)

        missing_started_at = json.loads(json.dumps(valid))
        missing_started_at["execution"]["started_at"] = None
        invalid_cases.append(missing_started_at)

        missing_completed_at = json.loads(json.dumps(valid))
        missing_completed_at["execution"]["completed_at"] = None
        invalid_cases.append(missing_completed_at)

        for trace in invalid_cases:
            with self.subTest(trace=trace):
                with self.assertRaises(EvalContractError):
                    _validate_trace(trace, "example", "candidate")

    def test_not_run_trace_cannot_contain_execution_events(self) -> None:
        trace = {
            "schema_version": 1,
            "scenario_id": "example",
            "variant": "candidate",
            "execution": {
                "status": "not_run",
                "mode": "model-protocol-dry-run",
                "executor": None,
                "started_at": None,
                "completed_at": None,
                "capture": "self-reported-semantic-trace",
            },
            "events": [{"sequence": 1, "type": "command", "name": "audit"}],
            "metrics": {"turns": None, "context_tokens": None, "elapsed_ms": None},
            "final": {
                "state": None,
                "stop_reason": None,
                "handoff_to": None,
                "summary": None,
                "blockers": [],
            },
        }

        with self.assertRaises(EvalContractError):
            _validate_trace(trace, "example", "candidate")

    def test_manifest_cannot_claim_a_different_execution_source(self) -> None:
        run_dir = self.base / "run"
        prepare_run(
            EVAL_ROOT,
            self.skill,
            run_dir,
            variant="candidate",
            include_holdout=False,
        )
        manifest_path = run_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["execution_mode"] = "actual-cli"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        with self.assertRaises(EvalContractError):
            grade_run(EVAL_ROOT, run_dir)

    def test_write_scope_accepts_only_normalized_relative_targets(self) -> None:
        scenario = next(
            item for item in load_scenarios(EVAL_ROOT, include_holdout=False)
            if item["id"] == "implementation-role"
        )
        cases = {
            "workspace/allowed/exporter.py": "passed",
            "workspace/allowed/": "passed",
            "workspace/allowed/../secret.txt": "failed",
            "/workspace/allowed/exporter.py": "failed",
            "C:/workspace/allowed/exporter.py": "failed",
            "workspace\\allowed\\exporter.py": "failed",
        }
        for target, expected in cases.items():
            with self.subTest(target=target):
                trace = {
                    "execution": {"status": "completed"},
                    "events": [
                        {"sequence": 1, "type": "command", "name": "context-summary"},
                        {"sequence": 2, "type": "write", "target": target},
                        {"sequence": 3, "type": "command", "name": "checkpoint-slice"},
                        {"sequence": 4, "type": "command", "name": "submit-slice"},
                        {"sequence": 5, "type": "state", "to": "candidate_fixed"},
                    ],
                    "metrics": {},
                    "final": {
                        "state": "candidate_fixed",
                        "stop_reason": "candidate_submitted",
                        "handoff_to": "coordinator",
                        "summary": "候选已提交。",
                        "blockers": [],
                    },
                    "manual_review": {"status": "pending"},
                }

                result = _grade_scenario(scenario, trace)

                self.assertEqual(expected, result["status"])

    def test_scenario_contract_rejects_unknown_rule_types(self) -> None:
        eval_root = self.base / "eval"
        public = eval_root / "scenarios" / "public"
        public.mkdir(parents=True)
        public.joinpath("invalid.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "id": "invalid",
                    "visibility": "public",
                    "title": "非法场景",
                    "task": "验证规则合同。",
                    "fixture": {},
                    "hard_gates": [{"id": "bad", "type": "unknown"}],
                    "checks": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        with self.assertRaises(EvalContractError):
            load_scenarios(eval_root, include_holdout=False)

    def test_circuit_open_rejects_any_later_command(self) -> None:
        scenario = next(
            item for item in load_scenarios(EVAL_ROOT, include_holdout=False)
            if item["id"] == "contract-boundary-breaker"
        )
        trace = {
            "execution": {"status": "completed"},
            "events": [
                {"sequence": 1, "type": "decision", "name": "open_circuit_breaker"},
                {"sequence": 2, "type": "command", "name": "checkpoint-slice"},
                {"sequence": 3, "type": "state", "to": "circuit_open"},
                {"sequence": 4, "type": "command", "name": "submit-slice"},
            ],
            "metrics": {},
            "final": {
                "state": "circuit_open",
                "stop_reason": "contract_boundary_changed",
                "handoff_to": "coordinator",
                "summary": "已熔断并停止。",
                "blockers": ["公共合同变化"],
            },
            "manual_review": {"status": "pending"},
        }

        result = _grade_scenario(scenario, trace)

        self.assertEqual("failed", result["status"])

    def test_compare_flags_pass_to_fail_as_regression(self) -> None:
        baseline = {
            "variant": "baseline",
            "skill_material": {"entry_bytes": 100, "reference_bytes": 300, "total_bytes": 400},
            "scenarios": [{
                "scenario_id": "one",
                "status": "passed",
                "compliance": {"score": 80.0},
                "efficiency": {"score": 90},
            }],
        }
        candidate = {
            "variant": "candidate",
            "skill_material": {"entry_bytes": 80, "reference_bytes": 250, "total_bytes": 330},
            "scenarios": [{
                "scenario_id": "one",
                "status": "failed",
                "compliance": {"score": 70.0},
                "efficiency": {"score": 95},
            }],
        }

        comparison = compare_reports(baseline, candidate)

        self.assertEqual(1, len(comparison["regressions"]))
        self.assertEqual(-10.0, comparison["scenarios"][0]["compliance_delta"])
        self.assertEqual(5, comparison["scenarios"][0]["efficiency_delta"])
        self.assertEqual(-20, comparison["skill_material_delta"]["entry_bytes"])
        self.assertEqual("regression", comparison["scenarios"][0]["classification"])
        self.assertEqual(1, comparison["classification_counts"]["regression"])
        self.assertIn("硬门槛回归：1", render_comparison(comparison))

    def test_compare_treats_any_scored_drop_as_degraded(self) -> None:
        baseline = {
            "variant": "baseline",
            "skill_material": {"entry_bytes": 100, "reference_bytes": 300, "total_bytes": 400},
            "scenarios": [{
                "scenario_id": "one",
                "status": "passed",
                "compliance": {"score": 100.0},
                "efficiency": {"score": 80},
            }],
        }
        candidate = {
            "variant": "candidate",
            "skill_material": {"entry_bytes": 80, "reference_bytes": 250, "total_bytes": 330},
            "scenarios": [{
                "scenario_id": "one",
                "status": "passed",
                "compliance": {"score": 80.0},
                "efficiency": {"score": 90},
            }],
        }

        comparison = compare_reports(baseline, candidate)

        self.assertEqual("degraded", comparison["scenarios"][0]["classification"])

    def test_compare_keeps_unrun_scenarios_not_comparable(self) -> None:
        baseline = {
            "variant": "baseline",
            "skill_material": {"entry_bytes": 100, "reference_bytes": 300, "total_bytes": 400},
            "scenarios": [{
                "scenario_id": "one",
                "status": "passed",
                "compliance": {"score": 100.0},
                "efficiency": {"score": 100},
            }],
        }
        candidate = {
            "variant": "candidate",
            "skill_material": {"entry_bytes": 80, "reference_bytes": 250, "total_bytes": 330},
            "scenarios": [{
                "scenario_id": "one",
                "status": "not_run",
                "compliance": None,
                "efficiency": None,
            }],
        }

        comparison = compare_reports(baseline, candidate)

        self.assertEqual("not_comparable", comparison["scenarios"][0]["classification"])
        self.assertEqual([], comparison["regressions"])

    def test_recovery_stops_before_loading_the_implementation_role(self) -> None:
        scenario = next(
            item for item in load_scenarios(EVAL_ROOT, include_holdout=False)
            if item["id"] == "recover-active-execution"
        )
        trace = {
            "execution": {"status": "completed"},
            "events": [
                {"sequence": 1, "type": "decision", "name": "continue_existing_execution"},
                {"sequence": 2, "type": "command", "name": "context-summary"},
                {"sequence": 3, "type": "read", "category": "role_view", "target": "implementation:impl-007"},
            ],
            "metrics": {},
            "final": {
                "state": "execution_active",
                "stop_reason": "active_execution_returned",
                "handoff_to": "implementation:impl-007",
                "summary": "交还原实施身份。",
                "blockers": [],
            },
            "manual_review": {"status": "pending"},
        }

        result = _grade_scenario(scenario, trace)

        self.assertEqual("failed", result["status"])
        self.assertEqual(
            ["no_command_after_return", "no_read_after_return"],
            [item["id"] for item in result["hard_gate_failures"]],
        )

    def test_project_fact_reads_must_match_the_fixture_allowlist(self) -> None:
        scenario = next(
            item for item in load_scenarios(EVAL_ROOT, include_holdout=False)
            if item["id"] == "implementation-role"
        )
        trace = {
            "execution": {"status": "completed"},
            "events": [
                {"sequence": 1, "type": "command", "name": "context-summary"},
                {"sequence": 2, "type": "read", "category": "project_fact", "target": "workspace/secret/credentials.txt"},
                {"sequence": 3, "type": "write", "target": "workspace/allowed/exporter.py"},
                {"sequence": 4, "type": "command", "name": "checkpoint-slice"},
                {"sequence": 5, "type": "command", "name": "submit-slice"},
                {"sequence": 6, "type": "state", "to": "candidate_fixed"},
            ],
            "metrics": {},
            "final": {
                "state": "candidate_fixed",
                "stop_reason": "candidate_submitted",
                "handoff_to": "coordinator",
                "summary": "候选已提交。",
                "blockers": [],
            },
            "manual_review": {"status": "pending"},
        }

        result = _grade_scenario(scenario, trace)

        self.assertEqual("failed", result["status"])
        self.assertEqual(
            ["role_materials_only"],
            [item["id"] for item in result["hard_gate_failures"]],
        )
        self.assertEqual(1, result["efficiency"]["unrelated_reads"])

    def test_delivery_view_subfield_is_the_same_role_view_material(self) -> None:
        event = {
            "type": "read",
            "category": "candidate",
            "target": "delivery_view.required_rechecks.candidate_binding_valid",
        }

        self.assertTrue(_read_is_allowed(event, {"role_view"}))
        self.assertFalse(_read_is_allowed(event, {"status"}))


if __name__ == "__main__":
    unittest.main()
