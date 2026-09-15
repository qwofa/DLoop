from __future__ import annotations

import hashlib
from pathlib import Path
import shutil

try:
    from _feature_archive_support import FeatureArchiveCliTestCase, invoke_feature_archive
except ModuleNotFoundError:
    from ._feature_archive_support import FeatureArchiveCliTestCase, invoke_feature_archive


class FeatureArchiveWorkflowRulesTests(FeatureArchiveCliTestCase):
    real_snapshots = False

    def setUp(self) -> None:
        super().setUp()
        repository_root = Path(__file__).resolve().parents[3]
        self._project_root = self.base / "installed-project"
        source_skill = (
            repository_root / "plugin" / "dloop" / "skills" / "dloop"
        )
        shutil.copytree(
            source_skill,
            self._project_root / ".agents" / "skills" / "dloop",
        )
        tool_directory = self._project_root / "Tools" / "FeatureArchive"
        tool_directory.mkdir(parents=True)
        shutil.copy2(
            (
                repository_root / "src" / "FeatureArchive" / "README.md"
                if (repository_root / "src" / "FeatureArchive").is_dir()
                else repository_root / "Tools" / "FeatureArchive" / "README.md"
            ),
            tool_directory / "README.md",
        )

    @property
    def project_root(self) -> Path:
        return self._project_root

    def _rules(self, *actions: str):
        arguments: list[object] = ["workflow-rules"]
        for action in actions:
            arguments.extend(("--action", action))
        return self.run_cli(*arguments)

    def test_requirement_review_returns_complete_closed_bundle(self) -> None:
        result = self._rules("requirements-review")

        self.assertEqual(2, result["schema_version"])
        self.assertEqual("ready", result["status"])
        self.assertEqual(["requirements-review"], result["requested_actions"])
        self.assertNotIn("archive_location", result)
        self.assertEqual(
            [
                ".agents/skills/dloop/references/requirements-and-terminology.md",
                ".agents/skills/dloop/references/reviews-and-approvals.md",
                ".agents/skills/dloop/references/snapshots.md",
            ],
            [item["path"] for item in result["references"]],
        )

        for item in result["references"]:
            path = self.project_root / item["path"]
            payload = path.read_bytes()
            self.assertEqual(payload.decode("utf-8"), item["content"])
            self.assertEqual(
                "sha256:" + hashlib.sha256(payload).hexdigest(),
                item["sha256"],
            )
            self.assertEqual({"path", "sha256", "content"}, set(item))

    def test_rule_command_is_profile_independent_and_documented(self) -> None:
        help_text = self.run_help("workflow-rules")
        self.assertNotIn("--project-root", help_text)
        self.assertIn("--action", help_text)
        self.assertNotIn("--feature-id", help_text)
        self.assertNotIn("--profile", help_text)

        readme = (
            self.project_root / "Tools" / "FeatureArchive" / "README.md"
        ).read_text(encoding="utf-8")
        self.assertIn("## 工作流规则投递", readme)
        self.assertIn("唯一严格配置", readme)
        self.assertIn("--entry-question", readme)
        self.assertIn("--allowed-material", readme)
        self.assertIn("--integration-confirmation", readme)
        self.assertIn("输出契约版本保持为 `2`", readme)
        self.assertIn("task_message_fields", readme)
        self.assertIn("只有在当前动作为调查时才获得并行事实取证权限", readme)
        self.assertIn("其他动作明确禁止派生并行分支", readme)
        self.assertIn("具体材料与停止条件由对应阶段规则投递", readme)
        self.assertIn(
            "必须显式声明 `--role {coordinator,cold-read,design-review,implementation,review,final-review}`",
            readme,
        )
        self.assertIn("显式全局校验、索引和清理仍严格检查全部档案", readme)

        references = (
            self.project_root
            / ".agents"
            / "skills"
            / "dloop"
            / "references"
        )
        validation = (references / "validation-and-cleanup.md").read_text(
            encoding="utf-8"
        )
        validation_policy = (references / "validation-policy.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("## 冻结", validation)
        self.assertIn("## 保留期与清理", validation)
        self.assertIn("## 只读分享", validation)
        self.assertIn("export-share", validation)
        self.assertIn("不能作为正式工作流输入", validation)
        self.assertIn("验证强度唯一策略", validation_policy)
        self.assertIn("安装生命周期变化", validation_policy)

    def test_non_review_action_keeps_single_responsibility_module(self) -> None:
        result = self._rules("design")

        self.assertEqual(
            [
                ".agents/skills/dloop/references/documents-and-dependencies.md",
                ".agents/skills/dloop/references/snapshots.md",
            ],
            [item["path"] for item in result["references"]],
        )
        self.assertNotIn("## 独立冷读", result["references"][0]["content"])

        investigation = self._rules("investigation")["references"][0]["content"]
        self.assertIn("## 调查并行边界", investigation)
        self.assertIn("拆分与汇总成本不低于预计节省时间时保持单线", investigation)

    def test_role_rules_keep_parallelism_read_only_and_execution_single_line(self) -> None:
        result = self._rules("cold-read", "slice-start", "candidate-review")
        contents = {
            item["path"].rsplit("/", 1)[-1]: item["content"]
            for item in result["references"]
        }
        reviews = contents["reviews-and-approvals.md"]
        execution = contents["execution.md"]
        validation_policy = contents["validation-policy.md"]

        self.assertIn("冷读者不读取常驻入口", reviews)
        self.assertIn("入口问题", reviews)
        self.assertIn("允许材料", reviews)
        self.assertIn("尚未闭环", reviews)
        self.assertIn("只使用原生子 Agent", reviews)
        self.assertIn("主协调者等待其最终返回", reviews)
        self.assertIn("不得通过 Windows 命令行", reviews)
        self.assertIn("原生交接失败", reviews)
        self.assertIn("完整流程合同", reviews)
        self.assertIn("全有或全无", reviews)
        self.assertIn("直接说明、只能推断、没有说明、存在歧义", reviews)
        self.assertIn("独立设计评审", reviews)
        self.assertIn("存在明显简化空间时再比较更简单替代方案", reviews)
        self.assertIn("项目核对路径和更简单替代方案按实际需要记录", reviews)
        self.assertIn("不强制固定标题", reviews)
        self.assertIn("一次取得", reviews)
        self.assertIn("不修改档案或实现", reviews)
        self.assertIn("不判断模块设计选择是否合理", reviews)
        self.assertIn("实施完成后不增加冷读", reviews)
        self.assertIn("实施者和评审者不读取常驻入口", execution)
        self.assertIn("单线角色合同", execution)
        self.assertIn("不在本阶段发起并行调查或并行实现", execution)
        self.assertIn("必需执行身份", execution)
        self.assertIn("评审身份必须不同于实施身份", execution)
        self.assertIn("不得使用实施对话", execution)
        self.assertIn("任务包引用的批准设计", execution)
        self.assertIn("未批准的公共接口、跨模块职责或状态归属、外部配置合同和恢复协议变化", execution)
        self.assertIn("内部整理不因缺少单独架构批准而退回", execution)
        self.assertIn("验证强度唯一策略", validation_policy)
        self.assertIn("不触发机械重复全量测试", validation_policy)

    def test_all_actions_return_exact_closed_rule_sets(self) -> None:
        prefix = ".agents/skills/dloop/references/"
        requirements = prefix + "requirements-and-terminology.md"
        documents = prefix + "documents-and-dependencies.md"
        reviews = prefix + "reviews-and-approvals.md"
        execution = prefix + "execution.md"
        validation_policy = prefix + "validation-policy.md"
        validation = prefix + "validation-and-cleanup.md"
        expected = {
            "create": [requirements],
            "resume": [requirements],
            "requirements": [requirements],
            "terminology": [requirements],
            "requirements-review": [requirements, reviews],
            "investigation": [documents],
            "design": [documents],
            "plan": [documents, validation_policy],
            "documentation": [documents],
            "diagram": [documents],
            "dependency-refresh": [documents],
            "architecture-review": [documents, reviews],
            "cold-read": [reviews],
            "slice-plan": [validation_policy, execution],
            "slice-start": [validation_policy, execution],
            "slice-submit": [validation_policy, execution],
            "candidate-review": [validation_policy, execution],
            "slice-rework": [validation_policy, execution],
            "slice-release": [execution],
            "share": [validation],
            "validation": [validation_policy, validation],
            "final-review": [validation_policy, validation, reviews],
            "freeze": [validation_policy, validation],
            "retention": [validation],
            "cleanup": [validation],
        }

        expected["snapshot"] = [prefix + "snapshots.md"]
        for action, expected_paths in expected.items():
            if action not in {"terminology", "diagram", "cold-read", "slice-plan", "share", "retention", "snapshot"}:
                expected_paths = [*expected_paths, prefix + "snapshots.md"]
            with self.subTest(action=action):
                result = self._rules(action)
                self.assertEqual(
                    expected_paths,
                    [item["path"] for item in result["references"]],
                )

    def test_multiple_actions_preserve_requests_and_deduplicate_references(self) -> None:
        result = self._rules(
            "requirements-review",
            "final-review",
            "requirements-review",
        )

        self.assertEqual(
            ["requirements-review", "final-review", "requirements-review"],
            result["requested_actions"],
        )
        self.assertEqual(
            [
                ".agents/skills/dloop/references/requirements-and-terminology.md",
                ".agents/skills/dloop/references/reviews-and-approvals.md",
                ".agents/skills/dloop/references/validation-policy.md",
                ".agents/skills/dloop/references/validation-and-cleanup.md",
                ".agents/skills/dloop/references/snapshots.md",
            ],
            [item["path"] for item in result["references"]],
        )

    def test_repeated_request_is_byte_stable_and_read_only(self) -> None:
        tracked = [
            self.project_root
            / ".agents"
            / "skills"
            / "dloop"
            / "SKILL.md",
            *(
                self.project_root
                / ".agents"
                / "skills"
                / "dloop"
                / "references"
            ).glob("*.md"),
        ]
        before = {
            path: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in tracked
        }

        first = self._rules("requirements-review", "final-review")
        second = self._rules("requirements-review", "final-review")

        self.assertEqual(first, second)
        self.assertEqual(
            before,
            {
                path: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in tracked
            },
        )
        self.assertEqual(
            {
                "schema_version",
                "status",
                "requested_actions",
                "references",
            },
            set(first),
        )

    def test_unknown_action_fails_without_partial_references(self) -> None:
        result = self.run_cli(
            "workflow-rules",
            "--action",
            "unknown-action",
            expected=1,
        )

        self.assertEqual("INVALID_WORKFLOW_RULE_ACTION", result["code"])
        self.assertNotIn("archive_location", result)
        self.assertNotIn("references", result)

    def test_missing_fixed_rule_file_fails_without_partial_references(self) -> None:
        project_root = self.base / "project"
        skill_directory = (
            project_root / ".agents" / "skills" / "dloop"
        )
        skill_directory.mkdir(parents=True)
        (skill_directory / "SKILL.md").write_text("# 唯一入口\n", encoding="utf-8")

        result = self.run_cli(
            "workflow-rules",
            "--action",
            "requirements-review",
            expected=1,
            project_root=project_root,
        )

        self.assertEqual("WORKFLOW_RULES_UNAVAILABLE", result["code"])
        self.assertNotIn("references", result)

    def test_unavailable_roots_entry_and_utf8_files_fail_read_only(self) -> None:
        missing_root = self.base / "missing-root"
        result = self.run_cli(
            "workflow-rules",
            "--action",
            "requirements",
            expected=1,
            project_root=missing_root,
        )
        self.assertEqual("WORKFLOW_RULES_UNAVAILABLE", result["code"])
        self.assertFalse(missing_root.exists())

        missing_entry = self.base / "missing-entry"
        missing_entry.mkdir()
        result = self.run_cli(
            "workflow-rules",
            "--action",
            "requirements",
            expected=1,
            project_root=missing_entry,
        )
        self.assertEqual("WORKFLOW_RULES_UNAVAILABLE", result["code"])
        self.assertEqual([], list(missing_entry.iterdir()))

        directory_entry = self.base / "directory-entry"
        directory_skill = (
            directory_entry / ".agents" / "skills" / "dloop"
        )
        (directory_skill / "SKILL.md").mkdir(parents=True)
        result = self.run_cli(
            "workflow-rules",
            "--action",
            "requirements",
            expected=1,
            project_root=directory_entry,
        )
        self.assertEqual("WORKFLOW_RULES_UNAVAILABLE", result["code"])
        self.assertNotIn("references", result)

        invalid_entry = self.base / "invalid-entry"
        invalid_skill = (
            invalid_entry / ".agents" / "skills" / "dloop"
        )
        invalid_skill.mkdir(parents=True)
        (invalid_skill / "SKILL.md").write_bytes(b"\xff")
        result = self.run_cli(
            "workflow-rules",
            "--action",
            "requirements",
            expected=1,
            project_root=invalid_entry,
        )
        self.assertEqual("WORKFLOW_RULES_UNAVAILABLE", result["code"])
        self.assertNotIn("references", result)

        invalid_project = self.base / "invalid-utf8"
        skill = (
            invalid_project / ".agents" / "skills" / "dloop"
        )
        references = skill / "references"
        references.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# 唯一入口\n", encoding="utf-8")
        invalid_reference = references / "requirements-and-terminology.md"
        invalid_reference.write_bytes(b"\xff")
        before = {
            path.relative_to(invalid_project).as_posix(): path.read_bytes()
            for path in invalid_project.rglob("*")
            if path.is_file()
        }
        result = self.run_cli(
            "workflow-rules",
            "--action",
            "requirements",
            expected=1,
            project_root=invalid_project,
        )
        self.assertEqual("WORKFLOW_RULES_UNAVAILABLE", result["code"])
        self.assertNotIn("references", result)
        self.assertEqual(
            before,
            {
                path.relative_to(invalid_project).as_posix(): path.read_bytes()
                for path in invalid_project.rglob("*")
                if path.is_file()
            },
        )

    def test_directory_rule_and_missing_action_are_rejected(self) -> None:
        invalid_project = self.base / "directory-rule"
        skill = (
            invalid_project / ".agents" / "skills" / "dloop"
        )
        references = skill / "references"
        references.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# 唯一入口\n", encoding="utf-8")
        (references / "requirements-and-terminology.md").mkdir()
        result = self.run_cli(
            "workflow-rules",
            "--action",
            "requirements",
            expected=1,
            project_root=invalid_project,
        )
        self.assertEqual("WORKFLOW_RULES_UNAVAILABLE", result["code"])
        self.assertNotIn("references", result)

        completed = invoke_feature_archive(
            ["workflow-rules"],
            project_root=self.project_root,
        )
        self.assertEqual(2, completed.returncode)
        self.assertEqual("", completed.stdout)
