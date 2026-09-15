"""功能交付流发行元数据一致性测试。"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TOOL_ROOT = REPOSITORY_ROOT / "src" / "FeatureArchive"
sys.path.insert(0, str(TOOL_ROOT))

import archive_terminology  # noqa: E402
import archive_initialization  # noqa: E402


class ReleaseMetadataTests(unittest.TestCase):
    def test_removed_observer_and_extension_payloads_are_absent(self) -> None:
        for relative in (
            "archive_extensions.py",
            "extensions/dloop_observer.py",
            "extensions/dloop_observer_guard.py",
            "extensions/dloop_observer.extension.json",
        ):
            self.assertFalse(TOOL_ROOT.joinpath(relative).exists(), relative)

    def test_required_configuration_release_files_are_tracked(self) -> None:
        if not REPOSITORY_ROOT.joinpath(".git").exists():
            self.skipTest("发行导出目录没有 Git 元数据")
        required = (
            "plugin/dloop/payload.json",
            "src/FeatureArchive/archive_configuration.py",
            "src/FeatureArchive/tests/test_feature_archive_configuration.py",
        )

        completed = subprocess.run(
            ["git", "-C", str(REPOSITORY_ROOT), "ls-files", "--error-unmatch", *required],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_single_plugin_packages_both_explicit_skills_at_the_release_version(self) -> None:
        version = (REPOSITORY_ROOT / "VERSION").read_text(encoding="utf-8").strip()
        plugin_root = REPOSITORY_ROOT / "plugin" / "dloop"
        manifest = json.loads(
            plugin_root.joinpath(".codex-plugin", "plugin.json").read_text(encoding="utf-8")
        )

        self.assertEqual("3.7.4", version)
        self.assertEqual("dloop", manifest["name"])
        self.assertEqual(version, manifest["version"])
        self.assertEqual("./skills/", manifest["skills"])
        self.assertTrue(plugin_root.joinpath("skills", "dloop", "SKILL.md").is_file())
        self.assertTrue(plugin_root.joinpath("skills", "dloop-ui", "SKILL.md").is_file())
        ui_entry = plugin_root.joinpath("skills", "dloop-ui", "scripts", "dloop_ui.py").read_text(
            encoding="utf-8"
        )
        self.assertLess(len(ui_entry.encode("utf-8")), 1024)
        self.assertIn("runpy.run_path", ui_entry)
        self.assertNotIn("def validate_model", ui_entry)
        self.assertFalse((REPOSITORY_ROOT / "skill").exists())

    def test_release_metadata_matches_tool_schema_versions(self) -> None:
        version = (REPOSITORY_ROOT / "VERSION").read_text(
            encoding="utf-8"
        ).strip()
        release = json.loads(
            (REPOSITORY_ROOT / "release.json").read_text(encoding="utf-8")
        )

        self.assertRegex(version, r"^\d+\.\d+\.\d+$")
        self.assertEqual(version, release["workflowVersion"])
        self.assertEqual(f"v{version}", release["sourceTag"])
        self.assertEqual("frozen", release["releaseStatus"])
        self.assertEqual(
            archive_initialization.SCHEMA_VERSION,
            release["archiveSchemaVersion"],
        )
        self.assertEqual(
            archive_terminology.TERMINOLOGY_SCHEMA_VERSION,
            release["terminologySchemaVersion"],
        )

    def test_release_declares_current_version_only_support(self) -> None:
        release = json.loads(
            (REPOSITORY_ROOT / "release.json").read_text(encoding="utf-8")
        )

        self.assertEqual(
            "current-version-only",
            release["versionSupport"]["mode"],
        )
        self.assertEqual(
            {
                "absent": "install-current",
                "sameVersion": (
                    "verify-ownership-and-reinstall-idempotently"
                ),
                "otherVersion": "reject-before-read-or-write",
            },
            release["versionSupport"]["behaviors"],
        )

    def test_plugin_payload_manifest_owns_current_modules(self) -> None:
        plugin_root = REPOSITORY_ROOT / "plugin" / "dloop"
        payload = json.loads(plugin_root.joinpath("payload.json").read_text(encoding="utf-8"))

        self.assertEqual(1, payload["schema_version"])
        self.assertEqual(
            {
                ("dloop-skill", "skill", "skills/dloop", ".agents/skills/dloop"),
                (
                    "dloop-ui-skill",
                    "skill",
                    "skills/dloop-ui",
                    ".agents/skills/dloop-ui",
                ),
                (
                    "dloop-ui-runtime",
                    "runtime",
                    "runtime",
                    "Tools/FeatureArchive",
                ),
                (
                    "dloop-ui-unity-editor",
                    "unity-editor",
                    "unity-editor",
                    "Packages/com.dloop.ui-capture",
                ),
            },
            {
                (module["id"], module["kind"], module["source"], module["destination"])
                for module in payload["modules"]
            },
        )
        self.assertFalse(
            plugin_root.joinpath("skills", "dloop-ui", "assets", "ui-model.template.json").exists()
        )
        self.assertTrue(
            plugin_root.joinpath("unity-editor", "Editor", "DloopUiCapture.cs").is_file()
        )

    def test_repository_does_not_own_project_delivery_archives(self) -> None:
        self.assertFalse((REPOSITORY_ROOT / ".scratch").exists())

    def test_current_materials_describe_current_scope(self) -> None:
        version = (REPOSITORY_ROOT / "VERSION").read_text(encoding="utf-8").strip()
        readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
        installation = (REPOSITORY_ROOT / "docs" / "installation.md").read_text(encoding="utf-8")
        changelog = (REPOSITORY_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

        self.assertEqual("3.7.4", version)
        self.assertIn("v3.7.4", readme)
        self.assertIn("-Version v3.7.4", installation)
        self.assertIn("-Uninstall", installation)
        self.assertIn("install.py", readme)
        self.assertIn("$dloop", readme)
        self.assertIn("$dloop-ui", readme)
        self.assertIn("已有 Prefab", readme)
        self.assertIn("真实截图", readme)
        self.assertNotIn("## 3.1.0 能力边界", readme)
        self.assertNotIn("## 3.0.0 能力边界", readme)
        self.assertIn("## 3.7.4 - 2026-09-15", changelog)
        self.assertIn("安装器只管理当前版本", changelog)

    def test_current_source_is_presented_as_frozen(self) -> None:
        release = json.loads(
            (REPOSITORY_ROOT / "release.json").read_text(encoding="utf-8")
        )
        release_notes = (REPOSITORY_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

        self.assertEqual(
            {
                "workflowVersion": "3.7.4",
                "archiveSchemaVersion": 3,
                "terminologySchemaVersion": 2,
                "sourceTag": "v3.7.4",
                "releaseStatus": "frozen",
                "versionSupport": release["versionSupport"],
            },
            release,
        )
        self.assertIn("frozen", release_notes)
        self.assertIn("由 `v3.7.4` 标签固定", release_notes)
        self.assertIn("持续集成", release_notes)
        self.assertIn("## 3.7.4 - 2026-09-15", release_notes)

    def test_skill_describes_the_current_runtime_contract(self) -> None:
        entry = (REPOSITORY_ROOT / "plugin" / "dloop" / "skills" / "dloop" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        references = REPOSITORY_ROOT / "plugin" / "dloop" / "skills" / "dloop" / "references"
        execution = references.joinpath("execution.md").read_text(encoding="utf-8")
        reviews = references.joinpath("reviews-and-approvals.md").read_text(
            encoding="utf-8"
        )
        validation_policy = references.joinpath("validation-policy.md").read_text(
            encoding="utf-8"
        )
        validation = references.joinpath("validation-and-cleanup.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("DLoop v3.7.4", entry)
        self.assertIn("## 默认路径", entry)
        self.assertIn("## 动作路由", entry)
        self.assertIn("## 关键不变量", entry)
        self.assertIn("## 完成与停止", entry)
        self.assertIn("workflow-rules", entry)
        self.assertIn("`share`", entry)
        self.assertNotIn("常用命令：", entry)
        self.assertNotIn("交付状态：", entry)
        self.assertNotIn("六项交接提示", entry)
        self.assertNotIn("当前模板", entry + execution)
        self.assertNotIn("批准、启动、提交或返修前先刷新状态", execution)

        self.assertIn("## 单线角色合同", execution)
        self.assertIn("## 任务包与切片契约", execution)
        self.assertIn("## 实施检查点与熔断", execution)
        self.assertIn("## 执行事务", execution)
        self.assertIn("缺业务决定时报告具体缺项", execution)
        self.assertIn("不读取历史示例或手写字段集合", execution)
        self.assertIn("输入没有变化时不重试", execution)
        self.assertNotIn("返修来源不适用", execution)
        self.assertNotIn("本参考文件适用于 DLoop", execution)
        self.assertIn("正常批准、启动、提交和返修由执行操作在事务内核对资格", execution)
        self.assertIn("--role design-review", reviews)
        self.assertIn("任务消息只交付当前交付项和设计评审角色", reviews)
        self.assertIn("主协调者等待其最终返回", reviews)
        self.assertNotIn("60 秒", reviews)
        self.assertIn("## 最终验收输入合同", reviews)
        self.assertIn("--role final-review", reviews)
        self.assertIn("不强制固定标题", reviews)
        self.assertIn("项目核对路径和更简单替代方案按实际需要记录", reviews)
        self.assertIn("一次取得", reviews)
        self.assertNotIn("续页标识", reviews)
        self.assertIn("最终验收角色视图从正式材料和不可变评审快照一次投影", validation)
        self.assertIn("export-share", validation)
        self.assertIn("不能作为正式工作流输入", validation)

        self.assertLessEqual(len(entry.encode("utf-8")), 8192)
        for command in ("check-slice-plan", "approve-slice-plan", "start-slice"):
            self.assertIn(command, execution)
        self.assertIn("中间检查点允许验证子集", execution)
        self.assertIn("证据和不适用理由都是可选补充", execution)
        self.assertIn("验证强度唯一策略", validation_policy)
        self.assertIn("安装生命周期变化", validation_policy)
        self.assertIn("真实宿主集成", validation_policy)
        self.assertIn("不触发机械重复全量测试", validation_policy)
        duplicated_validation_rules = entry + execution + validation
        self.assertNotIn("定向测试", duplicated_validation_rules)
        self.assertNotIn("模块全量", duplicated_validation_rules)
        self.assertNotIn("项目全量", duplicated_validation_rules)
        self.assertIn("确认文件本身不证明测试命令已经执行", execution)
        self.assertIn("系统不根据自由文本自动分类", validation)
        self.assertIn("不是通过结论的格式门槛", reviews)
        self.assertNotIn("观察模块", entry + execution + validation + reviews)

if __name__ == "__main__":
    unittest.main()
