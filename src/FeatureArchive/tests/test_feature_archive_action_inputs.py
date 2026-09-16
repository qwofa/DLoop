from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from _feature_archive_support import FeatureArchiveCliTestCase
except ModuleNotFoundError:
    from ._feature_archive_support import FeatureArchiveCliTestCase


class FeatureArchiveActionInputTests(FeatureArchiveCliTestCase):
    real_snapshots = False

    def test_ui_requirements_recommends_internal_preparation(self) -> None:
        self.run_cli(
            "init",
            "--feature-id", "ui-delivery",
            "--title", "UI 交付",
            "--configuration", "dloop-ui-v1",
        )

        view = self.run_cli(
            "workflow-status", "--feature-id", "ui-delivery"
        )["delivery_view"]

        self.assertEqual(
            ["APPROVAL_DOCUMENT_NOT_READY"],
            [item["code"] for item in view["blockers"]],
        )
        self.assertEqual("write", view["next_action"]["kind"])
        self.assertEqual(("01-requirements", "README.md"), Path(view["next_action_contract"]["target"]).parts[-2:])
        self.assertFalse(view["requires_human"]["required"])

    def test_requirements_delivery_recommends_writing_before_approval(self) -> None:
        archive = self.init_complex()

        view = self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery"
        )["delivery_view"]

        self.assertEqual("requirements", view["current_stage"])
        self.assertEqual("write", view["next_action"]["kind"])
        self.assertTrue(
            Path(view["next_action_contract"]["target"]).samefile(
                archive / "01-requirements" / "README.md"
            )
        )
        self.assertNotIn("command", view["next_action_contract"])
        self.assertFalse(view["requires_human"]["required"])

    def test_delivery_recommends_each_unfinished_formal_document(self) -> None:
        archive = self.init_complex()
        self.approve_requirements()
        self.run_cli(
            "transition-lifecycle",
            "--feature-id", "reliable-delivery",
            "--to", "active",
        )

        for stage, relative_path, expected_status in (
            ("design", "03-design/README.md", "confirmed"),
            ("plan", "04-plan/README.md", "completed"),
        ):
            view = self.run_cli(
                "workflow-status", "--feature-id", "reliable-delivery"
            )["delivery_view"]
            contract = view["next_action_contract"]
            self.assertEqual(stage, view["current_stage"])
            self.assertEqual("write", contract["kind"])
            self.assertTrue(
                Path(contract["target"]).samefile(archive / relative_path)
            )
            self.assertEqual(expected_status, contract["expected_status"])
            self.assertFalse(view["requires_human"]["required"])
            self.set_status(archive / relative_path, expected_status)

        self.set_status(archive / "05-implementation" / "README.md", "completed")
        self.accept_test_implementation()
        self.run_cli(
            "transition-lifecycle",
            "--feature-id", "reliable-delivery",
            "--to", "validating",
        )
        view = self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery"
        )["delivery_view"]
        contract = view["next_action_contract"]
        self.assertEqual("validation", view["current_stage"])
        self.assertEqual("write", contract["kind"])
        self.assertTrue(
            Path(contract["target"]).samefile(
                archive / "06-validation" / "README.md"
            )
        )
        self.assertEqual("completed", contract["expected_status"])
        self.assertFalse(view["requires_human"]["required"])

    def test_task_package_template_is_exact_and_never_overwrites_edits(self) -> None:
        self.init_complex()

        prepared = self.run_cli(
            "prepare-action-input",
            "--feature-id", "reliable-delivery",
            "--input-kind", "task-package",
            "--package-id", "slice-input",
        )
        target = Path(prepared["target"])
        template = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual("prepared", prepared["status"])
        self.assertEqual(
            {
                "package_id",
                "write_scope",
                "generated_write_scope",
                "context_contract_version",
                "context_materials",
                "slice_contract",
                "contract_check",
            },
            set(template),
        )
        self.assertEqual("pending", template["slice_contract"]["approval_status"])
        self.assertEqual([], template["generated_write_scope"])
        self.assertIn("预检查", prepared["input_guidance"]["generated_write_scope"]["description"])
        target.write_text("{\"agent_edit\": true}\n", encoding="utf-8")

        repeated = self.run_cli(
            "prepare-action-input",
            "--feature-id", "reliable-delivery",
            "--input-kind", "task-package",
            "--package-id", "slice-input",
        )

        self.assertEqual("existing", repeated["status"])
        self.assertEqual({"agent_edit": True}, json.loads(target.read_text(encoding="utf-8")))

    def test_ui_task_package_template_includes_full_requirements_and_design(self) -> None:
        self.run_cli(
            "init",
            "--feature-id", "ui-delivery",
            "--title", "UI 交付",
            "--configuration", "dloop-ui-v1",
        )

        prepared = self.run_cli(
            "prepare-action-input",
            "--feature-id", "ui-delivery",
            "--input-kind", "task-package",
            "--package-id", "ui-slice",
        )
        template = json.loads(Path(prepared["target"]).read_text(encoding="utf-8"))
        self.assertEqual([], template["delivery_requirements"])
        self.assertIn("delivery_requirements", prepared["input_guidance"])

        self.assertEqual(
            {("ui-delivery.requirements.overview", "full"), ("ui-delivery.design.overview", "full")},
            {(item["source"], item["mode"]) for item in template["context_materials"]},
        )

    def test_generated_task_package_requires_business_inputs_before_ready(self) -> None:
        self.init_complex()
        self.approve_requirements()
        self.prepare_execution_inputs()
        prepared = self.run_cli(
            "prepare-action-input",
            "--feature-id", "reliable-delivery",
            "--input-kind", "task-package",
            "--package-id", "slice-input",
        )
        target = Path(prepared["target"])
        template = json.loads(target.read_text(encoding="utf-8"))
        contract = template["slice_contract"]
        self.assertTrue(contract["validation_level"].startswith("待填写："))
        self.assertNotEqual(["slice-input"], contract["expected_impact_areas"]["modules"])

        template["write_scope"] = ["slice-input.txt"]
        contract["approval_status"] = "approved"
        for evidence in template["contract_check"].values():
            evidence["passed"] = True
            evidence["evidence"] = "已确认当前事实"
        target.write_text(json.dumps(template, ensure_ascii=False), encoding="utf-8")

        rejected = self.run_cli(
            "prepare-slice-contract",
            "--feature-id", "reliable-delivery",
            "--package-file", target,
            expected=1,
        )
        self.assertEqual("INVALID_SLICE_CONTRACT", rejected["code"])
        self.assertIn("仍为待填写占位内容", rejected["message"])

        contract.update({
            "revision_summary": "初始契约",
            "business_outcome": "完成当前切片",
            "acceptance_scenarios": ["结果可观察"],
            "in_scope": {
                "business_behaviors": ["完成当前切片约定的业务行为"],
                "data_responsibilities": ["只处理当前切片数据责任"],
                "system_boundaries": ["当前测试责任域"],
                "lifecycle": ["从启动到独立验收"],
            },
            "expected_impact_areas": {
                "modules": ["slice-input"],
                "resources": [],
                "data_boundaries": [],
            },
            "invariants": ["现有正常切片行为保持不变"],
            "validation_level": "targeted",
            "validation_rationale": "当前变化只影响单一切片及其直接边界",
            "validation_methods": ["执行聚焦验证"],
            "rollback_point": "异常时保留现场并人工处理",
            "circuit_breaker_conditions": ["当前切片无法独立验收时熔断"],
            "decision_owner": "测试负责人",
        })
        guidance = prepared["input_guidance"]
        dependency = dict(guidance["slice_contract.dependency_assumptions"]["example"])
        dependency["availability"] = "available"
        unknown = dict(guidance["slice_contract.unknowns"]["example"])
        unknown["status"] = "resolved"
        contract["dependency_assumptions"] = [dependency]
        contract["unknowns"] = [unknown]
        template["context_materials"] = guidance["context_materials"]["examples"]
        target.write_text(json.dumps(template, ensure_ascii=False), encoding="utf-8")

        drafted = self.run_cli(
            "prepare-slice-contract",
            "--feature-id", "reliable-delivery",
            "--package-file", target,
        )
        self.assertEqual("contract_draft", drafted["status"])
        checked = self.run_cli(
            "check-slice-contract",
            "--feature-id", "reliable-delivery",
            "--package-id", "slice-input",
        )
        self.assertEqual("ready", checked["status"])

    def test_prepared_plan_checks_approves_and_preserves_edited_next_version(self) -> None:
        archive = self.init_complex()
        prepared = self.run_cli(
            "prepare-action-input", "--feature-id", "reliable-delivery", "--input-kind", "slice-plan",
        )
        target = Path(prepared["target"])
        self.assertTrue(target.samefile(archive / "04-plan" / "slice-plan-v1.json"))
        plan = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual([], plan["slices"])
        self.assertIsNone(json.loads((archive / "workflow-state.json").read_text(encoding="utf-8"))["execution"]["slice_plan"]["current_version"])
        plan["slices"] = prepared["input_guidance"]["slices"]["example"]
        target.write_text(json.dumps(plan), encoding="utf-8")

        def invoke(contract):
            arguments = [contract["command"]]
            for key, value in contract["arguments"].items():
                arguments.extend(["--" + key.replace("_", "-"), value])
            return self.run_cli(*arguments)

        checked = invoke(prepared["next_action"])
        self.assertIn("reward-ui", checked["eligibility"]["waiting_prerequisite_acceptance"])
        approved = invoke(checked["next_action"])
        self.assertEqual("approved", approved["status"])
        state_before = (archive / "workflow-state.json").read_bytes()
        revised = self.run_cli(
            "prepare-action-input", "--feature-id", "reliable-delivery", "--input-kind", "slice-plan",
        )
        revision = json.loads(Path(revised["target"]).read_text(encoding="utf-8"))
        self.assertEqual(2, revision["version"])
        self.assertEqual(plan["slices"], revision["slices"])
        revision["slices"].append({"slice_id": "reward-help", "prerequisites": ["reward-ui"], "replacements": []})
        edited = json.dumps(revision).encode("utf-8")
        Path(revised["target"]).write_bytes(edited)
        repeated = self.run_cli(
            "prepare-action-input", "--feature-id", "reliable-delivery", "--input-kind", "slice-plan",
        )
        self.assertEqual("existing", repeated["status"])
        self.assertEqual(revised["target"], repeated["target"])
        self.assertEqual(edited, Path(repeated["target"]).read_bytes())
        self.assertEqual(state_before, (archive / "workflow-state.json").read_bytes())
        self.assertEqual(3, invoke(repeated["next_action"])["slice_count"])

    def test_coordinator_receives_plan_preparation_only_when_plan_writing_is_open(self) -> None:
        self.init_complex()
        self.approve_requirements()
        self.prepare_execution_inputs()
        self.run_cli("transition-lifecycle", "--feature-id", "reliable-delivery", "--to", "active")
        view = self.run_cli("context-summary", "--feature-id", "reliable-delivery",
                            "--action", "plan", "--role", "coordinator")["role_view"]
        preparation = view["input_preparations"][0]
        self.assertEqual("slice-plan", preparation["arguments"]["input_kind"])
        self.assertEqual("prepare-action-input", preparation["command"])
        self.assertEqual("friction-note", view["friction_note"]["command"])

    def test_registered_package_and_generated_identity_make_actions_executable(self) -> None:
        self.init_complex()
        self.approve_requirements()
        self.prepare_execution_inputs()
        self.run_cli("transition-lifecycle", "--feature-id", "reliable-delivery", "--to", "active")
        package = self.write_package("slice-generated")

        drafted = self.run_cli(
            "prepare-slice-contract",
            "--feature-id", "reliable-delivery",
            "--package-file", package,
        )
        self.assertEqual("contract_draft", drafted["status"])
        contract = self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery"
        )["delivery_view"]
        self.assertTrue(contract["can_advance"])
        self.assertEqual([], contract["blockers"])
        self.assertFalse(contract["requires_human"]["required"])
        contract = contract["next_action_contract"]
        self.assertEqual("check-slice-contract", contract["command"])
        self.assertEqual([], contract["required_inputs"])
        checked = self.run_cli(
            "check-slice-contract",
            "--feature-id", contract["arguments"]["feature_id"],
            "--package-id", contract["arguments"]["package_id"],
        )
        self.assertEqual("ready", checked["status"])

        contract = self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery"
        )["delivery_view"]["next_action_contract"]
        self.assertEqual("start-slice", contract["command"])
        self.assertEqual([], contract["required_inputs"])
        execution_id = contract["arguments"]["execution_id"]
        workspace_root = Path(contract["arguments"]["workspace_root"])
        started = self.run_cli(
            "start-slice",
            "--feature-id", contract["arguments"]["feature_id"],
            "--package-id", contract["arguments"]["package_id"],
            "--execution-id", execution_id,
            "--workspace-root", contract["arguments"]["workspace_root"],
        )
        self.assertEqual("active", started["status"])
        self.assertEqual("slice-generated-implementation", execution_id)

        view = self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery"
        )["delivery_view"]
        contract = view["next_action_contract"]
        self.assertEqual("checkpoint-slice", contract["command"])
        self.assertEqual(["FINAL_CHECKPOINT_REQUIRED"], [
            item["code"] for item in view["blockers"]
        ])
        self.assertEqual(["checkpoint_file"], contract["required_inputs"])
        preparation = contract["input_preparation"]
        checkpoint_input = self.run_cli(
            preparation["command"],
            "--feature-id", preparation["arguments"]["feature_id"],
            "--input-kind", preparation["arguments"]["input_kind"],
            "--execution-id", preparation["arguments"]["execution_id"],
        )
        checkpoint_path = Path(checkpoint_input["target"])
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        self.assertNotIn("checkpoint_id", checkpoint)
        checkpoint["hypothesis"] = "当前契约事实仍成立"
        checkpoint["change_summary"] = "完成当前切片实现"
        checkpoint["next_step"] = "提交候选"
        for result in checkpoint["validation_results"]:
            result["status"] = "passed"
            result["evidence"] = ["验证已执行并通过"]
        checkpoint_path.write_text(
            json.dumps(checkpoint, ensure_ascii=False), encoding="utf-8"
        )
        workspace_root.joinpath("slice-generated.txt").write_text(
            "implemented\n", encoding="utf-8"
        )
        recorded = self.run_cli(
            "checkpoint-slice",
            "--feature-id", "reliable-delivery",
            "--execution-id", execution_id,
            "--checkpoint-file", checkpoint_path,
        )
        self.assertEqual(
            f"{execution_id}-checkpoint-1",
            recorded["checkpoint"]["checkpoint_id"],
        )

        contract = self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery"
        )["delivery_view"]["next_action_contract"]
        self.assertEqual("submit-slice", contract["command"])
        preparation = contract["input_preparation"]
        candidate_input = self.run_cli(
            preparation["command"],
            "--feature-id", preparation["arguments"]["feature_id"],
            "--input-kind", preparation["arguments"]["input_kind"],
            "--execution-id", preparation["arguments"]["execution_id"],
        )
        candidate = json.loads(Path(candidate_input["target"]).read_text(encoding="utf-8"))
        self.assertEqual(
            {"candidate_id", "verification", "unverified_boundaries"},
            set(candidate),
        )
        self.assertEqual("slice-generated-candidate-1", candidate["candidate_id"])
        candidate["verification"] = ["编译与静态核验通过"]
        candidate["unverified_boundaries"] = ["未运行真实模型预览和实际点击"]
        Path(candidate_input["target"]).write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")
        self.run_cli("submit-slice", "--feature-id", "reliable-delivery", "--execution-id", execution_id,
                     "--status", "completed", "--candidate-file", candidate_input["target"])
        view = self.run_cli("workflow-status", "--feature-id", "reliable-delivery")["delivery_view"]
        review_contract = view["next_action_contract"]
        review_id = review_contract["arguments"]["review_execution_id"]
        before_state = (self.root / "reliable-delivery/workflow-state.json").read_bytes()
        prepared_paths = {}
        for preparation in review_contract["input_preparations"]:
            prepared = self.run_cli(preparation["command"], "--feature-id", "reliable-delivery",
                                    "--input-kind", preparation["arguments"]["input_kind"], "--execution-id", review_id)
            field = next(iter(prepared["template"]))
            target = Path(prepared["target"])
            self.assertEqual("05-implementation" if field == "issues" else "06-validation", target.parent.name)
            prepared_paths[field] = target
            values = [] if field == "issues" else ["独立静态核对通过，运行边界保持"]
            target.write_text(json.dumps({field: values}, ensure_ascii=False), encoding="utf-8")
            repeated = self.run_cli(preparation["command"], "--feature-id", "reliable-delivery",
                                   "--input-kind", preparation["arguments"]["input_kind"], "--execution-id", review_id)
            self.assertEqual("existing", repeated["status"])
            self.assertEqual({field: values}, json.loads(target.read_text(encoding="utf-8")))
        self.assertEqual(before_state, (self.root / "reliable-delivery/workflow-state.json").read_bytes())
        rejected = self.run_cli("prepare-action-input", "--feature-id", "reliable-delivery",
                                "--input-kind", "review-issues", "--execution-id", execution_id, expected=1)
        self.assertEqual("REVIEW_NOT_INDEPENDENT", rejected["code"])
        handoff = review_contract["handoff_preparation"]
        handed = self.run_cli(handoff["command"], *[part for key, value in handoff["arguments"].items()
                                                   for part in ("--" + key.replace("_", "-"), value)], "--include-role-view")
        self.assertTrue(handed["handoff_ready"], handed)
        self.run_cli("review-slice", "--feature-id", "reliable-delivery", "--package-id", "slice-generated",
                     "--candidate-id", candidate["candidate_id"], "--review-execution-id", review_id,
                     "--result", "passed", "--issues-file", prepared_paths["issues"], "--verification-file", prepared_paths["verification"])
        archive = self.root / "reliable-delivery"
        self.set_status(archive / "05-implementation/README.md", "completed")
        self.set_status(archive / "06-validation/README.md", "completed")
        self.run_cli("transition-lifecycle", "--feature-id", "reliable-delivery", "--to", "validating")
        self.approve_final_with_confirmation()
        summary = self.run_cli("workflow-status", "--feature-id", "reliable-delivery")["delivery_view"]["delivery_summary"]
        self.assertEqual("approve", summary["acceptance"])
        self.assertEqual("validating", summary["archive_lifecycle"])
        self.assertFalse(summary["archive_read_only"])
        self.assertEqual("documented-with-boundaries", summary["verification_status"])
        self.assertEqual(candidate["unverified_boundaries"], summary["unverified_boundaries"])
        self.assertEqual("pending", json.loads((archive / "feature.json").read_text(encoding="utf-8"))["validation"]["conclusion"])
        shared = self.run_cli("export-share", "--feature-id", "reliable-delivery")
        status = json.loads((Path(shared["share_location"]["path"]) / "delivery-status.json").read_text(encoding="utf-8"))
        self.assertEqual("approve", status["delivery_status"]["delivery_summary"]["acceptance"])
        validation_path = archive / "06-validation/README.md"
        validation_path.write_text(validation_path.read_text(encoding="utf-8") + "\n补充待接入的正式资源。\n", encoding="utf-8")
        self.run_cli("confirm-change", "--document-id", "reliable-delivery.validation.overview", "--semantic-change", "true")
        updated = self.run_cli("workflow-status", "--feature-id", "reliable-delivery")["delivery_view"]["delivery_summary"]
        self.assertEqual("stale", updated["acceptance"])
        self.assertEqual(candidate["unverified_boundaries"], updated["unverified_boundaries"])

    def test_review_input_preparation_requires_a_candidate(self) -> None:
        self.init_complex()
        result = self.run_cli("prepare-action-input", "--feature-id", "reliable-delivery", "--input-kind", "review-verification",
                              "--execution-id", "independent-review", expected=1)
        self.assertEqual("FIXED_CANDIDATE_REQUIRED", result["code"])

    def test_external_rework_preserves_unknown_cost_and_linked_recovery(self) -> None:
        archive = self.init_complex()
        before = (archive / "workflow-state.json").read_bytes()
        note = self.run_cli("friction-note", "--feature-id", "reliable-delivery", "--summary", "生成绑定时编辑器重载",
                            "--extra-work", "恢复连接后仅补生成剩余窗口", "--evidence", "tool-output:editor-reload")
        self.run_cli("friction-note", "--feature-id", "reliable-delivery", "--incident-id", note["incident_id"],
                     "--recovery", "补生成完成，构建通过")
        summary = self.run_cli("friction-summary", "--feature-id", "reliable-delivery")
        self.assertEqual(1, len(summary["incidents"]))
        incident = summary["incidents"][0]
        self.assertEqual("unknown", incident["cost_status"])
        self.assertEqual("unknown", incident["repeat_status"])
        self.assertEqual("recorded", incident["recovery_status"])
        self.assertEqual(["恢复连接后仅补生成剩余窗口"], incident["extra_work"])
        self.assertEqual(before, (archive / "workflow-state.json").read_bytes())


if __name__ == "__main__":
    import unittest

    unittest.main()
