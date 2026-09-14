from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


TOOL_DIRECTORY = Path(__file__).resolve().parents[1]
if str(TOOL_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(TOOL_DIRECTORY))

from archive_context import (
    ArchiveContextError,
    _design_review_content_blockers,
    _material,
)
from archive_failure_attribution import friction_log_path, record_friction
from archive_slice_contract import digest
from archive_validation import DocumentRecord

try:
    from _feature_archive_support import (
        FeatureArchiveCliTestCase,
        invoke_feature_archive,
        write_candidate,
    )
except ModuleNotFoundError:
    from ._feature_archive_support import (
        FeatureArchiveCliTestCase,
        invoke_feature_archive,
        write_candidate,
    )


class FeatureArchiveContextTests(FeatureArchiveCliTestCase):
    @staticmethod
    def _compact_bytes(value: object) -> bytes:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def _ready_archive(self) -> Path:
        archive = self.init_complex()
        self.approve_requirements()
        self.run_cli(
            "transition-lifecycle", "--feature-id", "reliable-delivery", "--to", "active",
        )
        self.prepare_execution_inputs()
        return archive

    def test_new_task_package_keeps_structured_materials(self) -> None:
        self._ready_archive()
        package = self.write_package("slice-1")
        result = self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-1", "--package-file", package,
            "--workspace-root", self.workspace,
        )
        self.assertNotIn("migration_warnings", result)
        self.assertNotIn("migration_warnings", result["task"])
        material = result["task"]["context_materials"][0]
        self.assertEqual("sections", material["mode"])
        self.assertEqual(["当前摘要"], material["sections"])

    def test_unreadable_role_material_fails_without_widening_the_read(self) -> None:
        document = DocumentRecord(
            document_id="reliable-delivery.requirements.overview",
            feature_id="reliable-delivery",
            path=self.base / "missing-material.md",
            category="requirements",
            content_status="confirmed",
            semantic_version="1.0.0",
            content_fingerprint="sha256:" + "0" * 64,
            dependencies=(),
            dependency_versions={},
            related_documents=(),
            stale_from_status=None,
        )

        with self.assertRaises(ArchiveContextError) as caught:
            _material(document, "读取需求范围")

        self.assertEqual("CONTEXT_MATERIAL_UNREADABLE", caught.exception.code)

    def test_missing_contract_version_is_rejected(self) -> None:
        self._ready_archive()
        package = self.write_package("legacy", legacy_context=True)
        result = self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-legacy", "--package-file", package,
            "--workspace-root", self.workspace, expected=1,
        )
        self.assertEqual("INVALID_TASK_PACKAGE", result["code"])

    def test_old_contract_version_is_rejected(self) -> None:
        self._ready_archive()
        package = self.write_package("old-version")
        value = json.loads(package.read_text(encoding="utf-8"))
        value["context_contract_version"] = 1
        package.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        result = self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-old-version", "--package-file", package,
            "--workspace-root", self.workspace, expected=1,
        )
        self.assertEqual("INVALID_TASK_PACKAGE", result["code"])

    def test_new_contract_rejects_string_material(self) -> None:
        self._ready_archive()
        package = self.write_package("invalid")
        value = json.loads(package.read_text(encoding="utf-8"))
        value["context_materials"] = ["需求总览"]
        package.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        result = self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-invalid", "--package-file", package,
            "--workspace-root", self.workspace, expected=1,
        )
        self.assertEqual("INVALID_TASK_PACKAGE", result["code"])

    def test_material_modes_are_mutually_exclusive(self) -> None:
        self._ready_archive()
        package = self.write_package("invalid-mode")
        value = json.loads(package.read_text(encoding="utf-8"))
        value["context_materials"][0] = {
            "source": "requirements.overview",
            "purpose": "读取需求",
            "mode": "full",
            "sections": ["当前摘要"],
        }
        package.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        result = self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-invalid-mode", "--package-file", package,
            "--workspace-root", self.workspace, expected=1,
        )
        self.assertEqual("INVALID_TASK_PACKAGE", result["code"])

    def test_missing_or_duplicate_section_fails_without_full_read_fallback(self) -> None:
        self._ready_archive()
        missing = self.write_package("missing-section")
        value = json.loads(missing.read_text(encoding="utf-8"))
        value["context_materials"][0]["sections"] = ["不存在的章节"]
        missing.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        result = self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-missing", "--package-file", missing,
            "--workspace-root", self.workspace, expected=1,
        )
        self.assertEqual("CONTEXT_SECTION_MISSING", result["code"])

        duplicate_source = self.workspace / "duplicate.md"
        duplicate_source.write_text("## 重复\n一\n## 重复\n二\n", encoding="utf-8")
        duplicate = self.write_package("duplicate-section")
        value = json.loads(duplicate.read_text(encoding="utf-8"))
        value["context_materials"] = [{
            "source": "duplicate.md",
            "purpose": "验证重复标题失败",
            "mode": "sections",
            "sections": ["重复"],
        }]
        duplicate.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        result = self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-duplicate", "--package-file", duplicate,
            "--workspace-root", self.workspace, expected=1,
        )
        self.assertEqual("CONTEXT_SECTION_AMBIGUOUS", result["code"])

    def test_context_summary_is_compact_and_action_scoped(self) -> None:
        self._ready_archive()
        result = self.run_cli(
            "context-summary", "--feature-id", "reliable-delivery",
            "--action", "implementation", "--role", "coordinator",
        )
        self.assertEqual(2, result["schema_version"])
        self.assertIn("implementation", result["allowed_actions"])
        self.assertEqual([], result["blockers"])
        encoded = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("workspace_snapshot", encoded)
        self.assertNotIn("used_execution_ids", encoded)
        self.assertNotIn("content_fingerprint", encoded)

    def test_context_summary_requires_an_explicit_role(self) -> None:
        self._ready_archive()
        completed = invoke_feature_archive(
            [
                "context-summary",
                "--feature-id",
                "reliable-delivery",
                "--action",
                "implementation",
            ],
            project_root=self.project_root,
        )
        self.assertEqual(2, completed.returncode)
        self.assertIn("--role", completed.stderr)

    def test_explicit_coordinator_role_adds_complete_contract(self) -> None:
        self._ready_archive()
        selected = self.run_cli(
            "context-summary", "--feature-id", "reliable-delivery",
            "--action", "implementation", "--role", "coordinator",
        )

        self.assertEqual("coordinator", selected["role_view"]["role"])
        self.assertEqual("implementation", selected["role_view"]["baton"])
        self.assertIn("goal", selected["role_view"])
        self.assertIn("completion_conditions", selected["role_view"])
        self.assertEqual([], selected["role_view"]["required_materials"])
        self.assertEqual(
            ["业务决定或下一角色所需的最小交接标识"],
            selected["role_view"]["expected_outputs"],
        )
        self.assertNotIn(
            "并行发起互不依赖的事实取证",
            selected["role_view"]["allowed_operations"],
        )
        self.assertIn(
            "在非调查动作中发起并行分支",
            selected["role_view"]["forbidden_operations"],
        )

        investigation = self.run_cli(
            "context-summary", "--feature-id", "reliable-delivery",
            "--action", "investigation", "--role", "coordinator",
        )
        self.assertIn(
            "并行发起互不依赖的事实取证",
            investigation["role_view"]["allowed_operations"],
        )
        self.assertNotIn(
            "在非调查动作中发起并行分支",
            investigation["role_view"]["forbidden_operations"],
        )

    def test_cold_read_role_is_self_contained_and_only_references_business_materials(self) -> None:
        self.init_complex()
        result = self.run_cli(
            "context-summary", "--feature-id", "reliable-delivery",
            "--action", "requirements", "--role", "cold-read",
            "--entry-question", "独立复述目标、边界与验收条件",
            "--allowed-material", "reliable-delivery.requirements.terminology",
            "--allowed-material", "reliable-delivery.requirements.overview",
        )

        view = result["role_view"]
        self.assertEqual("cold-read", view["role"])
        self.assertNotIn("task_message_fields", view)
        self.assertEqual(
            {
                "role", "baton", "goal", "allowed_operations", "forbidden_operations",
                "preconditions", "required_materials", "expected_outputs",
                "completion_conditions", "source_rechecks",
                "write_targets", "friction_note",
            },
            set(view),
        )
        self.assertEqual(
            [
                "reliable-delivery.requirements.terminology",
                "reliable-delivery.requirements.overview",
            ],
            [material["source"] for material in view["required_materials"]],
        )
        self.assertEqual(
            ["full", "full"],
            [material["mode"] for material in view["required_materials"]],
        )
        self.assertTrue(all(material["purpose"] for material in view["required_materials"]))
        self.assertNotIn("content", json.dumps(view, ensure_ascii=False))
        self.assertEqual("独立复述目标、边界与验收条件", view["goal"])
        self.assertIn("判断模块设计或实现质量", view["forbidden_operations"])

    def test_cold_read_role_rejects_missing_temporary_handoff_inputs(self) -> None:
        self.init_complex()
        result = self.run_cli(
            "context-summary", "--feature-id", "reliable-delivery",
            "--action", "requirements", "--role", "cold-read",
        )

        self.assertNotIn("role_view", result)
        self.assertEqual([], result["suggested_materials"])
        self.assertEqual(
            ["COLD_READ_QUESTION_REQUIRED", "COLD_READ_MATERIALS_REQUIRED"],
            [item["code"] for item in result["role_blocker"]["reasons"]],
        )

    def test_cold_read_role_rejects_an_unclosed_execution_baton(self) -> None:
        self._ready_archive()
        package = self.write_package("slice-cold-read-conflict")
        self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "active-implementer", "--package-file", package,
            "--workspace-root", self.workspace,
        )
        result = self.run_cli(
            "context-summary", "--feature-id", "reliable-delivery",
            "--action", "requirements", "--role", "cold-read",
            "--entry-question", "复述当前需求",
            "--allowed-material", "reliable-delivery.requirements.terminology",
            "--allowed-material", "reliable-delivery.requirements.overview",
        )

        self.assertNotIn("role_view", result)
        self.assertIn(
            "ACTIVE_BATON_CONFLICT",
            [item["code"] for item in result["role_blocker"]["reasons"]],
        )

    def test_design_review_role_projects_formal_materials_without_manual_fact_copy(self) -> None:
        archive = self._ready_archive()
        design = archive / "03-design" / "README.md"
        content = design.read_text(encoding="utf-8").replace(
            "尚待补充。",
            "已选定统一角色交接，评审需要判断接手角色取得的信息是否足以直接工作。",
            1,
        )
        design.write_text(content, encoding="utf-8")
        self.run_cli(
            "confirm-change", "--document-id", "reliable-delivery.design.overview",
            "--semantic-change", "true",
        )

        result = self.run_cli(
            "context-summary", "--feature-id", "reliable-delivery", "--action", "design",
            "--role", "design-review",
        )

        view = result["role_view"]
        self.assertEqual("design-review", view["role"])
        self.assertEqual("architecture-review", view["baton"])
        self.assertNotIn("task_message_fields", view)
        self.assertEqual(
            [
                "reliable-delivery.requirements.overview",
                "reliable-delivery.design.overview",
            ],
            [material["source"] for material in view["required_materials"]],
        )
        self.assertTrue(all(material["path"] for material in view["required_materials"]))
        self.assertIn("从设计材料出发，只读核对能改变当前判断的调用链、已有实现和项目惯例；引用清单不是只读调查白名单", view["allowed_operations"])
        self.assertNotIn("更简单替代方案比较", view["expected_outputs"])
        self.assertEqual("approve", view["source_rechecks"]["requirements_approval"])
        self.assertEqual("pending", view["source_rechecks"]["architecture_approval"])

        self.run_cli(
            "stage-action", "--feature-id", "reliable-delivery", "--stage", "architecture",
            "--decision", "approve",
        )
        blocked = self.run_cli(
            "context-summary", "--feature-id", "reliable-delivery", "--action", "design",
            "--role", "design-review",
        )
        self.assertNotIn("role_view", blocked)
        self.assertEqual(
            ["DESIGN_REVIEW_ALREADY_APPROVED"],
            [item["code"] for item in blocked["role_blocker"]["reasons"]],
        )

    def test_design_review_ignores_stale_downstream_plan(self) -> None:
        archive = self._ready_archive()
        plan = archive / "04-plan" / "README.md"
        plan_content = plan.read_text(encoding="utf-8")
        plan_content = plan_content.replace(
            "dependencies: []",
            "dependencies: [reliable-delivery.design.overview]",
            1,
        ).replace(
            "dependency_versions: {}",
            'dependency_versions: {"reliable-delivery.design.overview": "0.1.0"}',
            1,
        )
        plan.write_text(plan_content, encoding="utf-8")
        design = archive / "03-design" / "README.md"
        design.write_text(
            design.read_text(encoding="utf-8").replace(
                "尚待补充。",
                "采用统一角色交接，由设计评审先确认方案，再刷新下游计划。",
                1,
            ),
            encoding="utf-8",
        )
        self.run_cli(
            "confirm-change", "--document-id", "reliable-delivery.design.overview",
            "--semantic-change", "true",
        )

        result = self.run_cli(
            "context-summary", "--feature-id", "reliable-delivery", "--action", "design",
            "--role", "design-review",
        )

        self.assertEqual(
            ["reliable-delivery.plan.overview"],
            result["stale_documents"],
        )
        self.assertIn("role_view", result)
        self.assertNotIn("role_blocker", result)
        self.assertEqual(
            ["reliable-delivery.plan.overview"],
            result["role_view"]["source_rechecks"]["stale_documents"],
        )

    def test_design_review_role_blocks_placeholder_design_inputs(self) -> None:
        archive = self._ready_archive()

        result = self.run_cli(
            "context-summary", "--feature-id", "reliable-delivery", "--action", "design",
            "--role", "design-review",
        )

        self.assertNotIn("role_view", result)
        self.assertEqual([], result["suggested_materials"])
        reason = result["role_blocker"]["reasons"][0]
        self.assertEqual("DESIGN_REVIEW_INPUTS_INCOMPLETE", reason["code"])
        self.assertEqual(["设计内容"], reason["missing"])

        design = archive / "03-design" / "README.md"
        design.write_text(
            design.read_text(encoding="utf-8").replace("尚待补充。", ""),
            encoding="utf-8",
        )
        self.run_cli(
            "confirm-change", "--document-id", "reliable-delivery.design.overview",
            "--semantic-change", "true",
        )
        empty = self.run_cli(
            "context-summary", "--feature-id", "reliable-delivery", "--action", "design",
            "--role", "design-review",
        )
        self.assertIn(
            "DESIGN_REVIEW_INPUTS_INCOMPLETE",
            [item["code"] for item in empty["role_blocker"]["reasons"]],
        )

    def test_design_review_role_blocks_unconfirmed_design_body(self) -> None:
        archive = self._ready_archive()
        design = archive / "03-design" / "README.md"
        design.write_text(
            design.read_text(encoding="utf-8").replace(
                "尚待补充。",
                "这段设计尚未经过正式确认。",
                1,
            ),
            encoding="utf-8",
        )

        result = self.run_cli(
            "context-summary", "--feature-id", "reliable-delivery", "--action", "design",
            "--role", "design-review",
        )

        self.assertNotIn("role_view", result)
        self.assertEqual([], result["suggested_materials"])
        self.assertEqual(
            ["UNCONFIRMED_EDIT"],
            [item["code"] for item in result["role_blocker"]["reasons"]],
        )

    def test_design_review_distinguishes_placeholders_from_quoted_content(self) -> None:
        path = self.base / "design.md"
        document = DocumentRecord(
            document_id="reliable-delivery.design.overview",
            feature_id="reliable-delivery",
            path=path,
            category="design",
            content_status="confirmed",
            semantic_version="0.1.0",
            content_fingerprint="sha256:" + "0" * 64,
            dependencies=(),
            dependency_versions={},
            related_documents=(),
            stale_from_status=None,
        )
        template = (
            "---\ndocument_id: reliable-delivery.design.overview\n---\n\n"
            "# 设计总览\n\n"
            "> 记录方案、权衡、接口边界和设计决策。\n\n"
            "## 当前摘要\n\n{content}\n\n"
            "## 专题文档\n\n"
            "当前没有专题文档。仅在内容复杂度确有需要时新增。\n"
        )
        for placeholder in (
            "TODO", "TBD", "待补充", "> TODO",
            "TODO: 补充接口边界", "TBD - 决定缓存策略", "待补充：失败恢复方案",
        ):
            with self.subTest(placeholder=placeholder):
                path.write_text(
                    template.format(content=placeholder),
                    encoding="utf-8",
                )
                self.assertTrue(_design_review_content_blockers(document))

        path.write_text(
            template.format(content="> 采用统一动态角色交接。"),
            encoding="utf-8",
        )

        self.assertEqual((), _design_review_content_blockers(document))

    def test_non_coordinator_role_is_all_or_nothing_when_handoff_is_blocked(self) -> None:
        self.init_complex()
        result = self.run_cli(
            "context-summary", "--feature-id", "reliable-delivery",
            "--action", "implementation", "--role", "cold-read",
        )

        self.assertNotIn("role_view", result)
        self.assertEqual([], result["suggested_materials"])
        self.assertEqual("ROLE_VIEW_BLOCKED", result["role_blocker"]["code"])
        self.assertEqual("cold-read", result["role_blocker"]["role"])
        self.assertEqual(
            [
                "REQUIREMENTS_APPROVAL_REQUIRED",
                "ACTION_NOT_AVAILABLE",
                "COLD_READ_QUESTION_REQUIRED",
                "COLD_READ_MATERIALS_REQUIRED",
            ],
            [item["code"] for item in result["role_blocker"]["reasons"]],
        )
        self.assertEqual(
            "write",
            result["role_blocker"]["next_action"]["kind"],
        )

    def test_non_coordinator_role_wraps_invalid_formal_sources_as_a_stable_blocker(self) -> None:
        archive = self.init_complex()
        requirements = archive / "01-requirements" / "README.md"
        requirements.write_text(
            requirements.read_text(encoding="utf-8")
            + "\n[冲突来源](../../missing-source.md)\n",
            encoding="utf-8",
        )
        result = self.run_cli(
            "context-summary", "--feature-id", "reliable-delivery",
            "--action", "requirements", "--role", "cold-read",
            "--entry-question", "复述当前需求",
            "--allowed-material", "reliable-delivery.requirements.terminology",
            "--allowed-material", "reliable-delivery.requirements.overview",
            expected=1,
        )

        self.assertNotIn("role_view", result)
        self.assertEqual("cold-read", result["role_blocker"]["role"])
        self.assertIn(
            result["role_blocker"]["reasons"][0]["code"],
            {"CONTENT_FINGERPRINT_MISMATCH", "BROKEN_DOCUMENT_LINK"},
        )
        self.assertEqual(
            {"action": "return-to-coordinator"},
            result["role_blocker"]["next_action"],
        )

    def test_implementation_role_is_bound_to_the_active_execution(self) -> None:
        self._ready_archive()
        package = self.write_package("slice-role")
        self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "implementer-role", "--package-file", package,
            "--workspace-root", self.workspace,
        )
        result = self.run_cli(
            "context-summary", "--feature-id", "reliable-delivery",
            "--action", "implementation", "--role", "implementation",
            "--execution-id", "implementer-role",
        )

        view = result["role_view"]
        self.assertEqual("implementation", view["role"])
        self.assertNotIn("task_message_fields", view)
        self.assertEqual("implementer-role", view["execution_id"])
        self.assertEqual("slice-role", view["baton"])
        self.assertEqual(
            "task-package:slice-role",
            view["required_materials"][0]["source"],
        )
        self.assertIn("submit-slice", view["completion_conditions"][0])
        self.assertEqual([], view["write_targets"])

        blocked = self.run_cli(
            "context-summary", "--feature-id", "reliable-delivery",
            "--action", "implementation", "--role", "implementation",
            "--execution-id", "different-implementer",
        )
        self.assertNotIn("role_view", blocked)
        self.assertEqual(
            ["EXECUTION_ID_MISMATCH"],
            [item["code"] for item in blocked["role_blocker"]["reasons"]],
        )

    def test_implementation_role_rejects_conflicting_action(self) -> None:
        self._ready_archive()
        package = self.write_package("slice-action")
        self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "implementer-action", "--package-file", package,
            "--workspace-root", self.workspace,
        )
        result = self.run_cli(
            "context-summary", "--feature-id", "reliable-delivery",
            "--action", "plan", "--role", "implementation",
            "--execution-id", "implementer-action",
        )

        self.assertEqual("plan", result["requested_action"])
        self.assertNotIn("role_view", result)
        self.assertEqual(
            ["ROLE_ACTION_MISMATCH"],
            [item["code"] for item in result["role_blocker"]["reasons"]],
        )

    def test_context_summary_requires_explicit_action(self) -> None:
        completed = invoke_feature_archive(
            [
                "context-summary",
                "--feature-id", "reliable-delivery",
                "--role", "coordinator",
            ],
            project_root=self.project_root,
        )

        self.assertEqual(2, completed.returncode)
        self.assertIn("--action", completed.stderr)

    def test_implementation_role_rejects_an_invalid_architecture_approval(self) -> None:
        self._ready_archive()
        self.run_cli(
            "stage-action", "--feature-id", "reliable-delivery",
            "--stage", "architecture", "--decision", "reject",
        )
        result = self.run_cli(
            "context-summary", "--feature-id", "reliable-delivery",
            "--action", "implementation", "--role", "implementation",
            "--execution-id", "implementer-blocked",
        )

        self.assertNotIn("role_view", result)
        self.assertEqual(
            ["ARCHITECTURE_APPROVAL_BLOCKED"],
            [item["code"] for item in result["role_blocker"]["reasons"]],
        )

    def test_review_role_uses_a_different_identity_and_fixed_candidate_references(self) -> None:
        self._ready_archive()
        package = self.write_package("slice-review")
        package_value = json.loads(package.read_text(encoding="utf-8"))
        package_value["context_materials"].append({
            "source": "reliable-delivery.design.overview",
            "purpose": "读取批准设计",
            "mode": "sections",
            "sections": ["当前摘要"],
        })
        package.write_text(json.dumps(package_value, ensure_ascii=False), encoding="utf-8")
        self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "implementer-review", "--package-file", package,
            "--workspace-root", self.workspace,
        )
        self.workspace.joinpath("slice-review.txt").write_text("candidate\n", encoding="utf-8")
        self.record_checkpoint("implementer-review", identifier="review-final")
        candidate_file = self.artifact_path(
            "reliable-delivery", "05-implementation", "candidate-review.json"
        )
        candidate_file.write_text(json.dumps({
            "candidate_id": "candidate-review",
            "verification": ["聚焦验证通过"],
            "unverified_boundaries": [],
        }, ensure_ascii=False), encoding="utf-8")
        submitted = self.run_cli(
            "submit-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "implementer-review", "--status", "completed",
            "--candidate-file", candidate_file,
        )

        self_review = self.run_cli(
            "context-summary", "--feature-id", "reliable-delivery",
            "--action", "implementation", "--role", "review",
            "--execution-id", "implementer-review",
        )
        self.assertNotIn("role_view", self_review)
        self.assertEqual(
            ["SELF_REVIEW_FORBIDDEN"],
            [item["code"] for item in self_review["role_blocker"]["reasons"]],
        )

        result = self.run_cli(
            "context-summary", "--feature-id", "reliable-delivery",
            "--action", "implementation", "--role", "review",
            "--execution-id", "reviewer-role",
        )

        view = result["role_view"]
        self.assertEqual("review", view["role"])
        self.assertNotIn("task_message_fields", view)
        self.assertEqual("reviewer-role", view["execution_id"])
        self.assertEqual("slice-review", view["baton"])
        self.assertEqual(
            [
                "task-package:slice-review",
                "candidate:candidate-review",
                "reliable-delivery.requirements.overview",
                "reliable-delivery.design.overview",
            ],
            [material["source"] for material in view["required_materials"]],
        )
        self.assertIn("不得使用实施者对话", view["forbidden_operations"])
        self.assertIn("批准设计", view["goal"])
        self.assertIn("沿实际变化只读核对相关调用链、现有实现惯例与验证证据，按风险在隔离位置补充负向或边界检查", view["allowed_operations"])
        handoff = view["review_handoff"]
        self.assertEqual("reliable-delivery", handoff["feature_id"])
        self.assertEqual("slice-review", handoff["package_id"])
        self.assertEqual("reviewer-role", handoff["review_execution_id"])
        task_package = handoff["task_package"]
        self.assertEqual(package_value["slice_contract"], task_package["slice_contract"])
        self.assertEqual(package_value["write_scope"], task_package["write_scope"])
        self.assertEqual(
            package_value["slice_contract"]["business_outcome"],
            task_package["goal"],
        )
        self.assertEqual("candidate-review", handoff["candidate"]["candidate_id"])
        self.assertEqual(submitted["candidate_digest"], handoff["candidate"]["candidate_digest"])
        self.assertEqual(["聚焦验证通过"], handoff["candidate"]["verification"])
        self.assertTrue(handoff["candidate"]["changes"])
        self.assertEqual(
            [
                "通过、失败或未验证结论",
                "失败或未验证时的问题列表",
                "可选的独立负向或边界测试证据或不适用理由",
            ],
            view["expected_outputs"],
        )
        self.assertEqual(
            {"plan_version", "plan_digest", "history_ref"},
            set(view["source_rechecks"]["slice_plan_binding"]),
        )

    def test_final_review_role_projects_complete_accepted_delivery(self) -> None:
        archive = self._ready_archive()
        package = self.write_package("slice-final-review")
        self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "final-implementer", "--package-file", package,
            "--workspace-root", self.workspace,
        )
        self.workspace.joinpath("slice-final-review.txt").write_text("candidate\n", encoding="utf-8")
        self.record_checkpoint("final-implementer", identifier="final-review-checkpoint")
        submitted = self.run_cli(
            "submit-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "final-implementer", "--status", "completed",
            "--candidate-file", write_candidate(
                self.artifact_path(
                    "reliable-delivery",
                    "05-implementation",
                    "candidate-final-review.json",
                ),
                "candidate-final-review",
            ),
        )
        self.run_cli(
            "review-slice", "--feature-id", "reliable-delivery",
            "--package-id", "slice-final-review", "--candidate-id", "candidate-final-review",
            "--review-execution-id", "final-reviewer", "--result", "passed",
        )
        self.set_status(archive / "06-validation" / "README.md", "completed")
        self.run_cli(
            "transition-lifecycle", "--feature-id", "reliable-delivery", "--to", "validating",
        )

        missing = self.run_cli(
            "context-summary", "--feature-id", "reliable-delivery", "--action", "validation",
            "--role", "final-review",
        )
        self.assertNotIn("role_view", missing)
        self.assertNotIn("conclusions", missing["delivery_view"])
        self.assertEqual(
            ["INTEGRATION_CONFIRMATION_REQUIRED"],
            [item["code"] for item in missing["role_blocker"]["reasons"]],
        )

        canonical_confirmation = self.write_integration_confirmation()
        outside_confirmation = self.base / "outside-integration-confirmation.json"
        outside_confirmation.write_bytes(canonical_confirmation.read_bytes())
        outside = self.run_cli(
            "context-summary", "--feature-id", "reliable-delivery", "--action", "validation",
            "--role", "final-review", "--integration-confirmation", outside_confirmation,
        )
        self.assertNotIn("role_view", outside)
        self.assertEqual(
            ["INVALID_INTEGRATION_CONFIRMATION"],
            [item["code"] for item in outside["role_blocker"]["reasons"]],
        )

        mismatched_confirmation = self.write_integration_confirmation(
            candidate_summary_digest="sha256:" + "0" * 64,
        )
        mismatched = self.run_cli(
            "context-summary", "--feature-id", "reliable-delivery", "--action", "validation",
            "--role", "final-review", "--integration-confirmation", mismatched_confirmation,
        )
        self.assertNotIn("role_view", mismatched)
        self.assertEqual(
            ["INTEGRATION_CONFIRMATION_CANDIDATE_MISMATCH"],
            [item["code"] for item in mismatched["role_blocker"]["reasons"]],
        )

        confirmation = self.write_integration_confirmation()
        result = self.run_cli(
            "context-summary", "--feature-id", "reliable-delivery", "--action", "validation",
            "--role", "final-review", "--integration-confirmation", confirmation,
        )

        view = result["role_view"]
        self.assertEqual("final-review", view["role"])
        self.assertNotIn("task_message_fields", view)
        handoff = view["final_review_handoff"]
        self.assertNotIn("conclusions", result["delivery_view"])
        self.assertEqual(
            1,
            json.dumps(result, ensure_ascii=False).count(
                '"accepted_candidate_summary"'
            ),
        )
        self.assertEqual(
            submitted["candidate_digest"],
            handoff["accepted_candidate_summary"]["items"][0]["candidate_digest"],
        )
        self.assertEqual(1, handoff["accepted_candidate_summary"]["total_items"])
        self.assertEqual("slice-final-review", handoff["accepted_candidates"][0]["package_id"])
        self.assertEqual(
            "candidate-final-review",
            handoff["accepted_candidates"][0]["candidate"]["candidate_id"],
        )
        self.assertEqual("passed", handoff["accepted_candidates"][0]["review"]["result"])
        self.assertEqual(
            "06-validation/integration-confirmation.json",
            handoff["integration_confirmation"]["confirmation_path"],
        )
        self.assertEqual("integration-confirmation", view["required_materials"][-1]["source"])
        self.assertNotIn(
            "reliable-delivery.implementation.overview",
            [material["source"] for material in view["required_materials"]],
        )
        self.assertEqual([], result["suggested_materials"])
        self.assertEqual(
            ["candidate_workspace_drift", "final_validation_candidate_binding", "passed_review_binding"],
            [item["code"] for item in view["source_rechecks"]],
        )
        self.assertNotIn("page", handoff)

    def test_final_review_role_returns_complete_multi_slice_delivery_once(self) -> None:
        archive = self._ready_archive()
        package_ids = [f"slice-final-page-{index}" for index in range(3)]
        self.ensure_approved_slice_plan(*package_ids)
        for index, package_id in enumerate(package_ids):
            package = self.write_package(package_id, approve_plan=False)
            execution_id = f"final-page-implementer-{index}"
            candidate_id = f"candidate-final-page-{index}"
            self.run_cli(
                "start-slice", "--feature-id", "reliable-delivery",
                "--execution-id", execution_id, "--package-file", package,
                "--workspace-root", self.workspace,
            )
            self.workspace.joinpath(f"{package_id}.txt").write_text(
                f"candidate {index}\n",
                encoding="utf-8",
            )
            self.record_checkpoint(execution_id, identifier=f"final-page-checkpoint-{index}")
            self.run_cli(
                "submit-slice", "--feature-id", "reliable-delivery",
                "--execution-id", execution_id, "--status", "completed",
                "--candidate-file", write_candidate(
                    self.artifact_path(
                        "reliable-delivery", "05-implementation", f"{candidate_id}.json"
                    ),
                    candidate_id,
                ),
            )
            self.run_cli(
                "review-slice", "--feature-id", "reliable-delivery",
                "--package-id", package_id, "--candidate-id", candidate_id,
                "--review-execution-id", f"final-page-reviewer-{index}",
                "--result", "passed",
            )
        self.set_status(archive / "06-validation" / "README.md", "completed")
        self.run_cli(
            "transition-lifecycle", "--feature-id", "reliable-delivery", "--to", "validating",
        )
        confirmation = self.write_integration_confirmation()

        result = self.run_cli(
            "context-summary", "--feature-id", "reliable-delivery", "--action", "validation",
            "--role", "final-review", "--integration-confirmation", confirmation,
        )
        handoff = result["role_view"]["final_review_handoff"]
        self.assertEqual(3, handoff["accepted_candidate_summary"]["total_items"])
        self.assertEqual(
            package_ids,
            [item["package_id"] for item in handoff["accepted_candidates"]],
        )
        self.assertTrue(all(item["review"]["result"] == "passed" for item in handoff["accepted_candidates"]))
        self.assertNotIn("page", handoff)

    def test_final_review_role_blocks_candidates_from_multiple_workspaces(self) -> None:
        archive = self._ready_archive()
        package_ids = ("slice-workspace-a", "slice-workspace-b")
        self.ensure_approved_slice_plan(*package_ids)
        second_workspace = self.base / "workspace-second"
        second_workspace.mkdir()
        for index, package_id in enumerate(package_ids):
            execution_id = f"workspace-implementer-{index}"
            candidate_id = f"candidate-workspace-{index}"
            package = self.write_package(package_id, approve_plan=False)
            self.run_cli(
                "start-slice", "--feature-id", "reliable-delivery",
                "--execution-id", execution_id, "--package-file", package,
                "--workspace-root", self.workspace,
            )
            self.workspace.joinpath(f"{package_id}.txt").write_text("candidate\n", encoding="utf-8")
            self.record_checkpoint(execution_id, identifier=f"workspace-checkpoint-{index}")
            self.run_cli(
                "submit-slice", "--feature-id", "reliable-delivery",
                "--execution-id", execution_id, "--status", "completed",
                "--candidate-file", write_candidate(
                    self.artifact_path(
                        "reliable-delivery", "05-implementation", f"{candidate_id}.json"
                    ),
                    candidate_id,
                ),
            )
            self.run_cli(
                "review-slice", "--feature-id", "reliable-delivery",
                "--package-id", package_id, "--candidate-id", candidate_id,
                "--review-execution-id", f"workspace-reviewer-{index}", "--result", "passed",
            )
        self.set_status(archive / "06-validation" / "README.md", "completed")
        self.run_cli(
            "transition-lifecycle", "--feature-id", "reliable-delivery", "--to", "validating",
        )
        state_path = archive / "workflow-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        snapshot = state["execution"]["slices"][package_ids[0]]["review_snapshots"][-1]
        snapshot["candidate"]["workspace_root"] = str(second_workspace.resolve())
        snapshot["candidate"]["candidate_digest"] = digest({
            key: value
            for key, value in snapshot["candidate"].items()
            if key != "candidate_digest"
        })
        snapshot["review"]["candidate_digest"] = snapshot["candidate"]["candidate_digest"]
        snapshot["snapshot_digest"] = digest({
            key: value for key, value in snapshot.items() if key != "snapshot_digest"
        })
        state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        result = self.run_cli(
            "context-summary", "--feature-id", "reliable-delivery", "--action", "validation",
            "--role", "final-review",
            "--integration-confirmation", self.write_integration_confirmation(),
        )

        self.assertNotIn("role_view", result)
        self.assertEqual(
            ["WORKSPACE_ROOT_CONFLICT"],
            [item["code"] for item in result["role_blocker"]["reasons"]],
        )

    def test_first_implementation_keeps_coordinator_document_write_targets(self) -> None:
        archive = self._ready_archive()
        for action, directory in (("plan", "04-plan"), ("implementation", "05-implementation")):
            with self.subTest(action=action):
                result = self.run_cli(
                    "context-summary", "--feature-id", "reliable-delivery",
                    "--action", action, "--role", "coordinator",
                )
                self.assertEqual("implementation-required", result["delivery_view"]["current_stage"])
                self.assertEqual([], result["blockers"])
                targets = result["role_view"]["write_targets"]
                self.assertEqual(1, len(targets))
                self.assertEqual((archive / directory / "README.md").resolve(), Path(targets[0]["path"]))

        validation = self.run_cli(
            "context-summary", "--feature-id", "reliable-delivery",
            "--action", "validation", "--role", "coordinator",
        )
        self.assertEqual([], validation["role_view"]["write_targets"])

    def test_final_review_role_rejects_zero_slice_delivery(self) -> None:
        archive = self._ready_archive()
        active_view = self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery",
        )["delivery_view"]
        self.assertEqual("implementation-required", active_view["current_stage"])
        self.assertIsNone(active_view["next_action"])
        self.assertEqual(
            ["ACCEPTED_IMPLEMENTATION_REQUIRED"],
            [item["code"] for item in active_view["blockers"]],
        )

        self.set_status(archive / "06-validation" / "README.md", "completed")
        self.run_cli(
            "transition-lifecycle", "--feature-id", "reliable-delivery", "--to", "validating",
        )

        result = self.run_cli(
            "context-summary", "--feature-id", "reliable-delivery", "--action", "validation",
            "--role", "final-review",
        )

        self.assertNotIn("role_view", result)
        self.assertEqual(
            ["DELIVERY_STAGE_MISMATCH"],
            [item["code"] for item in result["role_blocker"]["reasons"]],
        )
        self.assertEqual(
            "implementation-required",
            result["delivery_view"]["current_stage"],
        )
        self.assertEqual(
            ["ACCEPTED_IMPLEMENTATION_REQUIRED"],
            [item["code"] for item in result["delivery_view"]["blockers"]],
        )

    def test_existing_status_entries_share_one_delivery_view_contract(self) -> None:
        self._ready_archive()
        package = self.write_package("slice-1")
        self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "exec-1", "--package-file", package,
            "--workspace-root", self.workspace,
        )

        workflow = self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery",
        )
        context = self.run_cli(
            "context-summary", "--feature-id", "reliable-delivery",
            "--action", "implementation", "--role", "coordinator",
        )

        self.assertEqual(workflow["delivery_view"], context["delivery_view"])
        view = workflow["delivery_view"]
        self.assertEqual("implementation", view["current_stage"])
        self.assertIsInstance(view["can_advance"], bool)
        self.assertIsInstance(view["blockers"], list)
        self.assertEqual(
            {"command": "checkpoint-slice", "execution_id": "exec-1"},
            view["next_action"],
        )
        self.assertEqual(
            ["FINAL_CHECKPOINT_REQUIRED"],
            [item["code"] for item in view["blockers"]],
        )
        self.assertEqual({"required": False, "decision": None}, view["requires_human"])
        self.assertEqual(
            {"requirements", "architecture", "final"},
            set(view["conclusions"]["approvals"]),
        )
        self.assertEqual("active", view["conclusions"]["slices"]["slice-1"]["status"])
        self.assertIn("current_candidate", view["conclusions"])
        self.assertIn("lifecycle", view["trusted_machine_facts"])
        self.assertEqual(
            [
                "candidate_workspace_drift",
                "final_validation_candidate_binding",
                "passed_review_binding",
            ],
            [item["code"] for item in view["required_rechecks"]],
        )

    def test_context_summary_blocks_action_before_requirement_approval(self) -> None:
        self.init_complex()
        result = self.run_cli(
            "context-summary", "--feature-id", "reliable-delivery",
            "--action", "design", "--role", "coordinator",
        )
        codes = {item["code"] for item in result["blockers"]}
        self.assertIn("REQUIREMENTS_APPROVAL_REQUIRED", codes)
        self.assertIn("ACTION_NOT_AVAILABLE", codes)

    def test_selected_feature_operations_ignore_unrelated_damage_but_global_commands_stay_strict(self) -> None:
        self._ready_archive()
        self.run_cli(
            "init", "--feature-id", "unrelated-history",
            "--title", "无关历史档案",
        )
        unrelated = self.root / "unrelated-history" / "05-implementation" / "README.md"
        unrelated.write_text(
            unrelated.read_text(encoding="utf-8")
            + "\n[失效历史链接](../../missing-skill/SKILL.md)\n",
            encoding="utf-8",
        )

        status = self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery",
        )
        summary = self.run_cli(
            "context-summary", "--feature-id", "reliable-delivery", "--action", "implementation",
            "--role", "coordinator",
        )
        self.assertEqual("active", status["delivery_view"]["trusted_machine_facts"]["lifecycle"])
        self.assertEqual("reliable-delivery", summary["feature_id"])

        for command in ("validate", "rebuild-indexes", "detect-cleanup"):
            result = self.run_cli(command, expected=1)
            self.assertIn(result["code"], {"CONTENT_FINGERPRINT_MISMATCH", "BROKEN_DOCUMENT_LINK"})
        targeted_cleanup = self.run_cli(
            "detect-cleanup", "--feature-id", "reliable-delivery", expected=1,
        )
        self.assertIn(
            targeted_cleanup["code"],
            {"CONTENT_FINGERPRINT_MISMATCH", "BROKEN_DOCUMENT_LINK"},
        )

        current = self.root / "reliable-delivery" / "05-implementation" / "README.md"
        current.write_text(
            current.read_text(encoding="utf-8")
            + "\n[当前功能失效链接](../../missing-current/SKILL.md)\n",
            encoding="utf-8",
        )
        current_result = self.run_cli(
            "workflow-status", "--feature-id", "reliable-delivery", expected=1,
        )
        self.assertIn(
            current_result["code"],
            {"CONTENT_FINGERPRINT_MISMATCH", "BROKEN_DOCUMENT_LINK"},
        )

    def test_selected_feature_slice_and_lifecycle_ignore_unrelated_damage(self) -> None:
        self._ready_archive()
        self.run_cli(
            "init", "--feature-id", "unrelated-history",
            "--title", "无关历史档案",
        )
        unrelated = self.root / "unrelated-history" / "05-implementation" / "README.md"
        unrelated.write_text(
            unrelated.read_text(encoding="utf-8")
            + "\n[失效历史链接](../../missing-skill/SKILL.md)\n",
            encoding="utf-8",
        )
        package = self.write_package("isolated-slice")
        started = self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "isolated-implementer", "--package-file", package,
            "--workspace-root", self.workspace,
        )
        self.workspace.joinpath("isolated-slice.txt").write_text("isolated\n", encoding="utf-8")
        self.record_checkpoint("isolated-implementer", identifier="isolated-final")
        candidate_file = self.artifact_path(
            "reliable-delivery", "05-implementation", "isolated-candidate.json"
        )
        candidate_file.write_text(json.dumps({
            "candidate_id": "isolated-candidate",
            "verification": ["隔离验证通过"],
            "unverified_boundaries": [],
        }, ensure_ascii=False), encoding="utf-8")
        submitted = self.run_cli(
            "submit-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "isolated-implementer", "--status", "completed",
            "--candidate-file", candidate_file,
        )
        reviewed = self.run_cli(
            "review-slice", "--feature-id", "reliable-delivery",
            "--package-id", "isolated-slice", "--candidate-id", "isolated-candidate",
            "--review-execution-id", "isolated-reviewer", "--result", "passed",
        )
        transitioned = self.run_cli(
            "transition-lifecycle", "--feature-id", "reliable-delivery", "--to", "validating",
        )

        self.assertEqual("active", started["status"])
        self.assertEqual("candidate", submitted["status"])
        self.assertEqual("accepted", reviewed["status"])
        self.assertEqual("validating", transitioned["lifecycle"])
        self.assertEqual("deferred", transitioned["indexes"])

    def test_selected_feature_freshness_operations_ignore_unrelated_damage(self) -> None:
        self._ready_archive()
        self.run_cli(
            "init", "--feature-id", "unrelated-history",
            "--title", "无关历史档案",
        )
        unrelated = self.root / "unrelated-history" / "05-implementation" / "README.md"
        unrelated.write_text(
            unrelated.read_text(encoding="utf-8")
            + "\n[失效历史链接](../../missing-skill/SKILL.md)\n",
            encoding="utf-8",
        )

        queue = self.run_cli(
            "refresh-queue", "--feature-id", "reliable-delivery",
        )
        fresh = self.run_cli(
            "assert-fresh", "--document-id", "reliable-delivery.requirements.overview",
        )
        confirmed = self.run_cli(
            "confirm-change", "--document-id", "reliable-delivery.plan.overview",
            "--semantic-change", "false",
        )

        self.assertEqual("reliable-delivery", queue["feature_id"])
        self.assertEqual("fresh", fresh["status"])
        self.assertEqual("unchanged", confirmed["status"])
        self.run_cli("refresh-queue", expected=1)

    def test_skill_entry_and_routing_contract_stay_small(self) -> None:
        repository_root = Path(__file__).resolve().parents[3]
        skill_root = (
            repository_root / "plugin" / "dloop" / "skills" / "dloop"
        )
        skill = skill_root / "SKILL.md"
        content = skill.read_text(encoding="utf-8")
        self.assertLessEqual(len(content.encode("utf-8")), 8 * 1024)
        self.assertIn("$dloop", content)
        self.assertIn("$dloop-ui", content)
        self.assertNotIn("--profile", content)
        for command in ("workflow-rules", "workflow-status", "audit", "context-summary"):
            self.assertIn(command, content)
        self.assertNotIn("references/", content)
        self.assertNotIn("## 六环节默认工作集", content)
        for action in (
            "requirements-review",
            "architecture-review",
            "candidate-review",
            "final-review",
        ):
            self.assertIn(f"`{action}`", content)
        self.assertTrue(
            (skill.parent / "references" / "reviews-and-approvals.md").is_file()
        )
        documents = skill_root.joinpath(
            "references", "documents-and-dependencies.md"
        ).read_text(encoding="utf-8")
        self.assertIn("## 调查并行边界", documents)

    def test_representative_complex_handoff_artifacts_stay_within_byte_budgets(self) -> None:
        archive = self._ready_archive()
        package_value = {
            "package_id": "context-budget-slice",
            "write_scope": ["src/context_guard.py", "docs/context_guard.md"],
            "context_contract_version": 2,
            "context_materials": [
                {
                    "source": "reliable-delivery.requirements.overview",
                    "purpose": "读取范围与验收条件",
                    "mode": "sections",
                    "sections": ["当前摘要"],
                },
                {
                    "source": "reliable-delivery.design.overview",
                    "purpose": "读取已确认实现边界",
                    "mode": "sections",
                    "sections": ["当前摘要"],
                },
            ],
            "slice_contract": {
                "schema_version": 3,
                "version": 1,
                "revision_summary": "初始契约",
                "business_outcome": "交付跨 Agent 的上下文预算护栏",
                "acceptance_scenarios": [
                    "上下文摘要包含当前阶段和下一动作",
                    "候选记录包含实际变更和验证结果",
                    "独立评审记录包含身份、结论和问题列表",
                ],
                "in_scope": {
                    "business_behaviors": ["约束上下文交接体积"],
                    "data_responsibilities": ["保存任务、候选和评审证据"],
                    "system_boundaries": ["DLoop 上下文投影"],
                    "lifecycle": ["实施到独立评审"],
                },
                "out_of_scope": ["不修改摘要算法", "不新增运行时遥测"],
                "dependency_assumptions": [],
                "expected_impact_areas": {
                    "modules": ["上下文投影"],
                    "resources": ["任务包和执行记录"],
                    "data_boundaries": [],
                },
                "invariants": ["角色视图保持全有或全无"],
                "validation_level": "targeted",
                "validation_rationale": "当前任务只改变上下文投影及其直接边界",
                "validation_methods": [
                    "python -m unittest Tools.FeatureArchive.tests.test_feature_archive_context -v",
                    "python -m unittest discover -s Tools/FeatureArchive/tests -v",
                ],
                "unknowns": [],
                "rollback_point": "删除本切片创建的两个文件并恢复切片前状态",
                "circuit_breaker_conditions": ["任务包无法维持紧凑交接"],
                "decision_owner": "测试负责人",
                "approval_status": "approved",
            },
            "contract_check": {
                criterion: {"passed": True, "evidence": "代表性夹具已提供证据"}
                for criterion in (
                    "independently_acceptable",
                    "strong_dependencies_available",
                    "structural_unknowns_resolved",
                    "impact_closed_loop",
                    "observable_acceptance_covered",
                    "regression_protection",
                    "safe_rollback",
                )
            },
        }
        package = self.artifact_path(
            "reliable-delivery", "05-implementation", "context-budget-slice.json"
        )
        package.write_bytes(self._compact_bytes(package_value))
        self.ensure_approved_slice_plan("context-budget-slice")
        started = self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "implementer-budget", "--package-file", package,
            "--workspace-root", self.workspace,
        )
        self.assertEqual("strict-v1", json.loads(
            archive.joinpath("feature.json").read_text(encoding="utf-8")
        )["workflow_profile"])
        self.assertEqual("active", started["status"])
        task = started["task"]
        self.assertEqual(2, task["context_contract_version"])
        self.assertEqual("context-budget-slice", task["package_id"])
        self.assertGreaterEqual(len(task["write_scope"]), 2)
        self.assertGreaterEqual(len(task["acceptance_conditions"]), 3)
        self.assertGreaterEqual(len(task["validations"]), 2)
        self.assertTrue(task["rollback"])
        self.assertTrue(all(item["purpose"] for item in task["context_materials"]))
        self.assertTrue(all(item["sections"] for item in task["context_materials"]))
        self.assertLessEqual(len(package.read_bytes()), 5 * 1024)

        summary = self.run_cli(
            "context-summary", "--feature-id", "reliable-delivery",
            "--action", "implementation", "--role", "coordinator",
        )
        self.assertEqual("active", summary["lifecycle"])
        self.assertEqual("implementation", summary["requested_action"])
        self.assertIn("implementation", summary["allowed_actions"])
        self.assertIn("blockers", summary)
        self.assertNotIn("并行发起互不依赖的事实取证", summary["role_view"]["allowed_operations"])
        self.assertIn("在非调查动作中发起并行分支", summary["role_view"]["forbidden_operations"])
        self.assertIn("并行推进决策、写入、实施、评审或验证决策", summary["role_view"]["forbidden_operations"])
        self.assertEqual(
            "task-package:context-budget-slice",
            summary["suggested_materials"][0]["source"],
        )
        self.assertLessEqual(len(self._compact_bytes(summary)), 6 * 1024)

        for relative_path in package_value["write_scope"]:
            target = self.workspace / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"代表性变更：{relative_path}\n", encoding="utf-8")
        self.record_checkpoint("implementer-budget", identifier="budget-final")
        candidate_file = self.artifact_path(
            "reliable-delivery", "05-implementation", "candidate-budget.json"
        )
        candidate_file.write_bytes(self._compact_bytes({
            "candidate_id": "candidate-budget",
            "verification": ["聚焦测试通过", "完整工具测试通过"],
            "unverified_boundaries": ["宿主级多 Agent 生命周期另行验收"],
        }))
        submitted = self.run_cli(
            "submit-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "implementer-budget", "--status", "completed",
            "--candidate-file", candidate_file,
        )
        self.assertEqual("candidate", submitted["status"])
        state = json.loads(archive.joinpath("workflow-state.json").read_text(encoding="utf-8"))
        record = state["execution"]["slices"]["context-budget-slice"]
        candidate = record["candidate"]
        self.assertEqual("candidate-budget", candidate["candidate_id"])
        self.assertEqual(
            sorted(package_value["write_scope"]),
            sorted(item["path"] for item in candidate["changes"]),
        )
        self.assertEqual(2, len(candidate["verification"]))
        self.assertEqual(1, len(candidate["unverified_boundaries"]))
        for field in ("workspace_digest", "workspace_root", "workspace_guard_digest"):
            self.assertTrue(candidate[field])
        # 两个明确产物各保留正文摘要，候选仍限制在 1.25 KiB 内。
        self.assertLessEqual(len(self._compact_bytes(candidate)), 1280)

        reviewed = self.run_cli(
            "review-slice", "--feature-id", "reliable-delivery",
            "--package-id", "context-budget-slice", "--candidate-id", "candidate-budget",
            "--review-execution-id", "reviewer-budget", "--result", "passed",
        )
        self.assertEqual("accepted", reviewed["status"])
        state = json.loads(archive.joinpath("workflow-state.json").read_text(encoding="utf-8"))
        review = state["execution"]["slices"]["context-budget-slice"]["review_snapshots"][-1]["review"]
        self.assertEqual("reviewer-budget", review["review_execution_id"])
        self.assertEqual("passed", review["result"])
        self.assertEqual([], review["issues"])
        self.assertLessEqual(len(self._compact_bytes(review)), 1024)

    def test_cold_read_identifiers_and_paths_share_dependency_closure(self) -> None:
        archive = self.init_complex()
        overview = archive / "01-requirements" / "README.md"
        variants = (
            "reliable-delivery.requirements.overview",
            "reliable-delivery/01-requirements/README.md",
            "01-requirements/README.md",
            str(overview.resolve()),
        )
        projections = []
        for source in variants:
            result = self.run_cli(
                "context-summary", "--feature-id", "reliable-delivery",
                "--action", "requirements", "--role", "cold-read",
                "--entry-question", "复述需求范围",
                "--allowed-material", source,
            )
            projections.append([
                (item["source"], item["path"])
                for item in result["role_view"]["required_materials"]
            ])
        self.assertTrue(all(item == projections[0] for item in projections))
        self.assertEqual(
            [
                (
                    "reliable-delivery.requirements.terminology",
                    "reliable-delivery/01-requirements/terminology.md",
                ),
                (
                    "reliable-delivery.requirements.overview",
                    "reliable-delivery/01-requirements/README.md",
                ),
            ],
            projections[0],
        )

    def test_cold_read_material_errors_preserve_input_and_recovery(self) -> None:
        self.init_complex()
        outside = self.base / "outside.md"
        outside.write_text("非档案材料\n", encoding="utf-8")
        inputs = [
            "reliable-delivery.requirements.missing",
            "reliable-delivery/feature.json",
            str(outside),
            "reliable-delivery.requirements.overview",
            "01-requirements/README.md",
        ]
        arguments = [
            "context-summary", "--feature-id", "reliable-delivery",
            "--action", "requirements", "--role", "cold-read",
            "--entry-question", "复述需求范围",
        ]
        for source in inputs:
            arguments.extend(("--allowed-material", source))
        result = self.run_cli(*arguments)
        reasons = result["role_blocker"]["reasons"]
        self.assertEqual(
            [
                "COLD_READ_MATERIAL_UNKNOWN",
                "COLD_READ_MATERIAL_NOT_FORMAL",
                "COLD_READ_MATERIAL_OUT_OF_SCOPE",
                "COLD_READ_MATERIAL_DUPLICATE",
            ],
            [item["code"] for item in reasons],
        )
        self.assertEqual(
            [inputs[0], inputs[1], inputs[2], inputs[4]],
            [item["input"] for item in reasons],
        )
        self.assertTrue(all(item["recovery"] for item in reasons))
        self.assertNotIn("friction_id", result)

    def test_delivery_action_contract_is_immediately_executable(self) -> None:
        self.init_complex()
        self.approve_requirements()
        result = self.run_cli(
            "context-summary", "--feature-id", "reliable-delivery",
            "--action", "requirements", "--role", "coordinator",
        )
        view = result["delivery_view"]
        self.assertEqual("activation", view["current_stage"])
        contract = view["next_action_contract"]
        self.assertEqual([], contract["required_inputs"])
        activated = self.run_cli(
            contract["command"],
            "--feature-id", contract["arguments"]["feature_id"],
            "--to", contract["arguments"]["to"],
        )
        self.assertEqual("active", activated["lifecycle"])

    def test_role_projection_refuses_slice_without_matching_lease(self) -> None:
        self._ready_archive()
        package = self.write_package("slice-lease-mismatch")
        self.run_cli(
            "start-slice", "--feature-id", "reliable-delivery",
            "--execution-id", "implementer-lease",
            "--package-file", package,
            "--workspace-root", self.workspace,
        )
        workspace_state = self.root / ".feature-archive-workspace-state.json"
        state = json.loads(workspace_state.read_text(encoding="utf-8"))
        state["modification_lease"] = None
        workspace_state.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        record_friction(
            self.root,
            "既有上下文生成异常。",
            feature_id="reliable-delivery",
            command="context-summary",
            code="CONTEXT_RENDER_FAILED",
            target={"execution_id": "implementer-lease"},
        )
        result = self.run_cli(
            "context-summary", "--feature-id", "reliable-delivery",
            "--action", "implementation", "--role", "implementation",
            "--execution-id", "implementer-lease",
        )
        self.assertEqual(
            ["MODIFICATION_LEASE_MISMATCH"],
            [item["code"] for item in result["delivery_view"]["blockers"]],
        )
        self.assertEqual(
            ["DELIVERY_STAGE_MISMATCH"],
            [item["code"] for item in result["role_blocker"]["reasons"]],
        )
        entries = [
            json.loads(line)
            for line in friction_log_path(self.root).read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(["failure", "guard"], [entry["event"] for entry in entries])
        self.assertNotIn("resolved_friction_ids", result)
