from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _feature_archive_support import FeatureArchiveCliTestCase, write_candidate, write_issues


class FeatureArchiveHandoffTests(FeatureArchiveCliTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.init_complex()
        self.approve_requirements()
        self.run_cli("transition-lifecycle", "--feature-id", "reliable-delivery", "--to", "active")
        self.prepare_execution_inputs()
        self.feature = self.root / "reliable-delivery"

    def workflow_bytes(self, feature_id="reliable-delivery"):
        lease = self.root / ".feature-archive-workspace-state.json"
        return (
            (self.root / feature_id / "workflow-state.json").read_bytes(),
            lease.read_bytes() if lease.exists() else None,
        )

    def prepare(self, role, action="implementation", *extra):
        return self.run_cli(
            "prepare-handoff", "--feature-id", "reliable-delivery",
            "--action", action, "--role", role, "--include-role-view", *extra,
        )

    def read_prepared(self, prepared):
        command = prepared["context_command"]
        result = self.run_cli(command["command"], *command["arguments"])
        self.assertEqual({"current_stage", "blockers"}, set(result["delivery_view"]))
        self.assertNotIn("allowed_actions", result)
        self.assertNotIn("task_message_fields", result["role_view"])
        self.assertNotIn("inputs", result["role_view"]["friction_note"])
        return result

    def start(self, package, expected=0):
        return self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-1", "--package-file", package,
            "--workspace-root", self.workspace, expected=expected,
        )

    def candidate(self, expected=0):
        candidate = write_candidate(self.feature / "05-implementation" / "candidate.json", "candidate-1")
        return self.run_cli(
            "submit-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-1", "--status", "completed",
            "--candidate-file", candidate, expected=expected,
        )

    def test_compact_handoff_keeps_checks_and_downstream_materials(self):
        self.start(self.write_package("slice-1"))
        before = self.workflow_bytes()
        compact = self.run_cli(
            "prepare-handoff", "--feature-id", "reliable-delivery",
            "--action", "implementation", "--role", "implementation",
        )
        expanded = self.prepare("implementation")
        self.assertNotIn("role_view", compact)
        self.assertEqual(compact, {key: value for key, value in expanded.items() if key != "role_view"})
        self.assertEqual(expanded["role_view"], self.read_prepared(compact)["role_view"])
        self.assertEqual(before, self.workflow_bytes())
        self.assertLess(len(json.dumps(compact)), len(json.dumps(expanded)) / 2)
        arguments = (
            "prepare-handoff", "--feature-id", "reliable-delivery",
            "--action", "implementation", "--role", "implementation", "--execution-id", "wrong-id",
        )
        blocked = self.run_cli(*arguments)
        self.assertFalse(blocked["handoff_ready"])
        self.assertEqual(blocked, self.run_cli(*arguments, "--include-role-view"))

    def test_missing_requirement_reference_reaches_implementation_review_and_repair(self):
        requirements = self.feature / "01-requirements" / "README.md"
        outcomes = (
            "入口图标打开对象详情页。\n界面包含主要、补充和关联三个页签。\n"
            "本人固定信息区显示统计值，列表条目显示指标值。\n"
            "分类入口和列表项都显示新获得提示；打开检查全部分类，切类及默认选中不清除，主动查看后同步更新，重开不重复。\n"
            "展示时限到达请求新状态，后端确认参与条件后进入主界面。"
        )
        requirements.write_text(
            requirements.read_text(encoding="utf-8").replace("尚待补充。", outcomes),
            encoding="utf-8",
        )
        self.run_cli(
            "confirm-change", "--document-id", "reliable-delivery.requirements.overview",
            "--semantic-change", "true",
        )
        self.approve_requirements()
        package = self.write_package("slice-1")
        value = json.loads(package.read_text(encoding="utf-8"))
        value["context_materials"] = [{
            "source": "reliable-delivery.design.overview",
            "purpose": "核对设计", "mode": "full",
        }]
        package.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        original_package = package.read_bytes()
        original_requirements = requirements.read_bytes()

        def check_materials(prepared):
            before = self.workflow_bytes()
            view = self.read_prepared(prepared)["role_view"]
            sources = [item for item in view["required_materials"]
                       if item["source"] == "reliable-delivery.requirements.overview"]
            self.assertEqual(1, len(sources))
            self.assertEqual("full", sources[0]["mode"])
            self.assertEqual(requirements.resolve(), Path(sources[0]["path"]))
            self.assertIn(outcomes, Path(sources[0]["path"]).read_text(encoding="utf-8"))
            self.assertIn(outcomes, Path(sources[0]["path"]).read_text(encoding="utf-8"))
            self.assertEqual(before, self.workflow_bytes())

        self.start(package)
        check_materials(self.prepare("implementation"))
        self.workspace.joinpath("slice-1.txt").write_text("candidate\n", encoding="utf-8")
        self.record_checkpoint("exec-1")
        self.candidate()
        check_materials(self.prepare("review"))
        self.run_cli(
            "review-slice", "--feature-id", "reliable-delivery", "--package-id", "slice-1",
            "--candidate-id", "candidate-1", "--review-execution-id", "review-1",
            "--result", "failed", "--issues-file",
            write_issues(self.feature / "05-implementation" / "issues.json", "需要补齐场景结果"),
        )
        self.run_cli(
            "resolve-slice", "--feature-id", "reliable-delivery", "--action", "retry",
            "--package-id", "slice-1", "--execution-id", "exec-2",
        )
        check_materials(self.prepare("implementation"))
        self.assertEqual(original_package, package.read_bytes())
        self.assertEqual(original_requirements, requirements.read_bytes())

    def test_explicit_requirement_sections_are_kept_without_duplicate_full_read(self):
        package = self.write_package("slice-1")
        requested = json.loads(package.read_text(encoding="utf-8"))["context_materials"][0]
        self.start(package)
        view = self.read_prepared(self.prepare("implementation"))["role_view"]
        materials = [item for item in view["required_materials"]
                     if item["source"] == requested["source"]]
        self.assertEqual(1, len(materials))
        self.assertEqual(requested, {key: value for key, value in materials[0].items() if key != "path"})

    def test_start_collects_missing_materials_before_taking_identity_or_lease(self):
        package = self.write_package("slice-1")
        value = json.loads(package.read_text(encoding="utf-8"))
        value["context_materials"].extend([
            {"source": "guide.md", "purpose": "实施约定", "mode": "sections", "sections": ["输入", "结果"]},
            {"source": "reference.txt", "purpose": "相关事实", "mode": "full"},
        ])
        package.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        self.workspace.joinpath("guide.md").write_text("# 说明\n", encoding="utf-8")
        before = self.workflow_bytes()
        blocked = self.start(package, expected=1)
        self.assertFalse(blocked["handoff_ready"])
        self.assertEqual(3, len(blocked["blockers"]))
        self.assertEqual({"guide.md", "reference.txt"}, {item["source"] for item in blocked["blockers"]})
        self.assertFalse(blocked["next_action"]["start_downstream"])
        self.assertEqual(before, self.workflow_bytes())

        self.workspace.joinpath("guide.md").write_text("# 输入\n输入事实\n# 结果\n输出事实\n", encoding="utf-8")
        self.workspace.joinpath("reference.txt").write_text("相关事实\n", encoding="utf-8")
        started = self.start(package)
        self.assertTrue(started["handoff_ready"])
        self.assertTrue(all(Path(item["path"]).is_file() for item in started["required_materials"]))
        self.assertIn("reliable-delivery.design.overview", [item["source"] for item in started["required_materials"]])
        before = self.workflow_bytes()
        prepared = self.prepare("implementation")
        self.assertTrue(prepared["handoff_ready"])
        self.assertEqual("exec-1", prepared["task_message"]["execution_id"])
        self.assertEqual(started["required_materials"], prepared["role_view"]["required_materials"][1:])
        downstream = self.read_prepared(prepared)["role_view"]
        self.assertEqual(prepared["role_view"], downstream)
        outputs = "\n".join(downstream["expected_outputs"])
        for requirement in (
            "已批准需求的场景名称和材料定位", "不按控件或 Prefab 内部状态另拆场景",
            "真实结果证据与已有体验入口", "明确未验证部分", "不编造入口",
            "接线、遮挡、输入与状态清理",
        ):
            self.assertIn(requirement, outputs)
        self.assertEqual(before, self.workflow_bytes())

    def test_current_task_stays_complete_without_exposing_future_plan(self):
        self.start(self.write_package("slice-1"))
        self.run_cli(
            "prepare-slice-contract", "--feature-id", "reliable-delivery",
            "--package-file", self.write_package("future-slice"),
        )
        before = self.workflow_bytes()
        prepared = self.prepare("implementation")
        downstream = self.read_prepared(prepared)
        handoff = downstream["role_view"]["execution_handoff"]
        self.assertEqual(["slice-1.txt"], handoff["task_package"]["write_scope"])
        self.assertIn("slice_contract", handoff["task_package"])
        self.assertNotIn("allowed_write_scope", handoff)
        self.assertNotIn("future-slice", json.dumps(downstream))
        coordinator = self.run_cli(
            "context-summary", "--feature-id", "reliable-delivery",
            "--action", "implementation", "--role", "coordinator",
        )
        self.assertIn("future-slice", json.dumps(coordinator["delivery_view"]))
        self.assertIn("next_action_contract", coordinator["delivery_view"])
        self.assertEqual(before, self.workflow_bytes())

    def test_friction_guidance_is_available_only_when_needed(self):
        shutil.copytree(
            Path(__file__).resolve().parents[3] / "plugin" / "dloop" / "skills" / "dloop",
            self.project_root / ".agents" / "skills" / "dloop",
        )
        self.start(self.write_package("slice-1"))
        downstream = self.read_prepared(self.prepare("implementation"))
        note = downstream["role_view"]["friction_note"]
        self.assertIn("workflow-rules --action friction", note["input_guidance"])
        before = self.workflow_bytes()
        rules = self.run_cli("workflow-rules", "--action", "friction")
        self.assertIn("--extra-work", json.dumps(rules, ensure_ascii=False))
        arguments = ["--" + name.replace("_", "-") for name in note["arguments"]]
        values = list(note["arguments"].values())
        invocation = [item for pair in zip(arguments, values) for item in pair]
        result = self.run_cli(
            note["command"], *invocation, "--summary", "用户指出材料重复查找",
            "--extra-work", "重复定位已知材料", "--source", "user-feedback",
        )
        self.assertEqual("recorded", result["status"])
        self.assertEqual(before, self.workflow_bytes())

    def test_candidate_missing_material_stays_with_original_implementation_until_fixed(self):
        package = self.write_package("slice-1", scope=["guide.md"])
        value = json.loads(package.read_text(encoding="utf-8"))
        value["context_materials"].append({
            "source": "guide.md", "purpose": "实施与评审共享规则",
            "mode": "sections", "sections": ["使用说明"],
        })
        package.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        guide = self.workspace / "guide.md"
        guide.write_text("# 使用说明\n原有用法\n", encoding="utf-8")
        self.start(package)
        # 正常实施改写自己的授权文件时遗漏了任务包引用的章节。
        guide.write_text("# 新功能\n新用法\n", encoding="utf-8")
        self.record_checkpoint("exec-1")
        before = self.workflow_bytes()
        blocked = self.candidate(expected=1)
        self.assertEqual("CONTEXT_SECTION_MISSING", blocked["code"])
        self.assertEqual(before, self.workflow_bytes())
        self.assertEqual("# 新功能\n新用法\n", guide.read_text(encoding="utf-8"))

        guide.write_text("# 使用说明\n新用法\n", encoding="utf-8")
        self.record_checkpoint("exec-1", identifier="fixed")
        submitted = self.candidate()
        self.assertTrue(submitted["handoff_ready"])
        before = self.workflow_bytes()
        prepared = self.prepare("review")
        self.assertTrue(prepared["handoff_ready"])
        self.assertNotEqual("exec-1", prepared["task_message"]["execution_id"])
        self.assertEqual(before, self.workflow_bytes())
        self.assertEqual(prepared["task_message"], self.prepare("review")["task_message"])
        self.assertEqual(submitted["required_materials"], prepared["role_view"]["required_materials"][2:])
        downstream = self.read_prepared(prepared)
        self.assertEqual(prepared["role_view"], downstream["role_view"])
        self.assertNotIn("verification", prepared["task_message"])
        self.assertFalse(self.prepare("review", "implementation", "--execution-id", "exec-1")["handoff_ready"])

    def test_repair_is_assembled_before_switch_and_not_persisted_twice(self):
        self.start(self.write_package("slice-1"))
        self.workspace.joinpath("slice-1.txt").write_text("candidate\n", encoding="utf-8")
        self.record_checkpoint("exec-1")
        submitted = self.candidate()
        self.run_cli(
            "review-slice", "--feature-id", "reliable-delivery", "--package-id", "slice-1",
            "--candidate-id", "candidate-1", "--review-execution-id", "review-1", "--result", "failed",
            "--issues-file", write_issues(self.feature / "05-implementation" / "issues.json", "需要修正"),
        )
        repaired = self.run_cli(
            "resolve-slice", "--feature-id", "reliable-delivery", "--action", "retry",
            "--package-id", "slice-1", "--execution-id", "exec-2",
        )
        self.assertTrue(repaired["handoff_ready"])
        self.assertEqual(submitted["candidate_digest"], repaired["repair_handoff"]["original_candidate"]["candidate_digest"])
        prepared = self.prepare("implementation")
        self.assertEqual(repaired["repair_handoff"], prepared["role_view"]["repair_handoff"])
        self.assertEqual(prepared["role_view"], self.read_prepared(prepared)["role_view"])
        state = json.loads(self.workflow_bytes()[0])
        stored = state["execution"]["slices"]["slice-1"]["repair_handoff"]
        self.assertNotIn("original_candidate", stored)
        self.assertNotIn("retest_requirements", stored)

    def test_cold_read_collects_missing_inputs_without_returning_partial_contract(self):
        self.init_complex("cold-read-feature")

        def prepare_cold(*extra):
            return self.run_cli(
                "prepare-handoff", "--feature-id", "cold-read-feature",
                "--action", "requirements", "--role", "cold-read", "--include-role-view", *extra,
            )
        before = self.workflow_bytes("cold-read-feature")
        blocked = prepare_cold()
        self.assertFalse(blocked["handoff_ready"])
        self.assertEqual(
            {"COLD_READ_QUESTION_REQUIRED", "COLD_READ_MATERIALS_REQUIRED"},
            {item["code"] for item in blocked["blockers"]},
        )
        self.assertNotIn("role_view", blocked)
        self.assertNotIn("task_message", blocked)
        self.assertEqual(before, self.workflow_bytes("cold-read-feature"))
        prepared = prepare_cold(
            "--entry-question", "交付要实现什么？",
            "--allowed-material", "cold-read-feature.requirements.overview",
            "--allowed-material", "cold-read-feature.requirements.terminology",
        )
        self.assertTrue(prepared["handoff_ready"])
        self.assertEqual(prepared["role_view"], self.read_prepared(prepared)["role_view"])
        self.assertEqual(
            {"feature_id", "role", "entry_question", "allowed_materials"},
            set(prepared["task_message"]),
        )
        self.assertEqual(before, self.workflow_bytes("cold-read-feature"))

    def test_retry_keeps_previous_identity_when_formal_inputs_need_refresh(self):
        self.start(self.write_package("slice-1"))
        self.workspace.joinpath("slice-1.txt").write_text("candidate\n", encoding="utf-8")
        self.record_checkpoint("exec-1")
        self.candidate()
        self.run_cli(
            "review-slice", "--feature-id", "reliable-delivery", "--package-id", "slice-1",
            "--candidate-id", "candidate-1", "--review-execution-id", "review-1", "--result", "failed",
            "--issues-file", write_issues(self.feature / "05-implementation" / "issues.json", "需要修正"),
        )
        requirements = self.feature / "01-requirements" / "README.md"
        requirements.write_text(requirements.read_text(encoding="utf-8") + "\n补充需求的可观察验收边界。\n", encoding="utf-8")
        self.run_cli("confirm-change", "--document-id", "reliable-delivery.requirements.overview", "--semantic-change", "true")
        before = self.workflow_bytes()
        blocked = self.run_cli(
            "resolve-slice", "--feature-id", "reliable-delivery", "--action", "retry",
            "--package-id", "slice-1", "--execution-id", "exec-2", expected=1,
        )
        self.assertEqual("REQUIREMENTS_APPROVAL_REQUIRED", blocked["code"])
        self.assertEqual(before, self.workflow_bytes())
        self.assertNotIn("exec-2", json.loads(before[0])["execution"]["used_execution_ids"])

    def test_design_placeholder_is_returned_to_author_before_review(self):
        before = self.workflow_bytes()
        blocked = self.prepare("design-review", "design")
        self.assertFalse(blocked["handoff_ready"])
        self.assertEqual("DESIGN_REVIEW_INPUTS_INCOMPLETE", blocked["blockers"][0]["code"])
        self.assertEqual(before, self.workflow_bytes())
        self.assertNotIn("task_message", blocked)
        design = self.feature / "03-design" / "README.md"
        body = design.read_text(encoding="utf-8").replace("尚待补充。", "在当前模块内完成需求规定的数据处理，结果由现有界面显示。")
        design.write_text(body, encoding="utf-8")
        self.run_cli("confirm-change", "--document-id", "reliable-delivery.design.overview", "--semantic-change", "true")
        prepared = self.prepare("design-review", "design")
        self.assertTrue(prepared["handoff_ready"], prepared)
        self.assertEqual(prepared["role_view"], self.read_prepared(prepared)["role_view"])
        self.assertEqual({"feature_id", "role"}, set(prepared["task_message"]))

    def test_final_review_requires_confirmation_and_rechecks_current_candidate(self):
        confirmation = self.accept_test_implementation()
        self.set_status(self.feature / "06-validation" / "README.md", "completed")
        self.run_cli("transition-lifecycle", "--feature-id", "reliable-delivery", "--to", "validating")
        before = self.workflow_bytes()
        blocked = self.prepare("final-review", "validation")
        self.assertFalse(blocked["handoff_ready"])
        self.assertEqual("INTEGRATION_CONFIRMATION_REQUIRED", blocked["blockers"][0]["code"])
        self.assertEqual(before, self.workflow_bytes())
        prepared = self.prepare("final-review", "validation", "--integration-confirmation", confirmation)
        self.assertTrue(prepared["handoff_ready"], prepared)
        self.assertEqual(before, self.workflow_bytes())
        downstream = self.read_prepared(prepared)
        self.assertEqual(prepared["role_view"], downstream["role_view"])
        self.assertEqual(
            {"feature_id", "role", "integration_confirmation"}, set(prepared["task_message"]),
        )
        operations = "\n".join(downstream["role_view"]["allowed_operations"])
        for requirement in (
            "已批准需求的场景名称和正文定位", "逐个核对实际结果与证据",
            "真实体验入口、初始配置和操作", "不强制截图、录像或试玩数量",
        ):
            self.assertIn(requirement, operations)
        outputs = "\n".join(downstream["role_view"]["expected_outputs"])
        for requirement in ("实际偏差及影响", "未验证部分", "仍需人判断", "不复制或改写需求"):
            self.assertIn(requirement, outputs)
        self.assertIn(
            "用示意、规划截图或原型冒充最终运行结果，或用测试通过代替人的体验判断和业务批准",
            downstream["role_view"]["forbidden_operations"],
        )
