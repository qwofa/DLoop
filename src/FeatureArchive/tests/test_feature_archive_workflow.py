"""功能交付流发行源与项目安装结构测试。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest

TEST_PATH = Path(__file__).resolve()
SOURCE_REPOSITORY_ROOT = TEST_PATH.parents[3]
IS_SOURCE_REPOSITORY = TEST_PATH.parents[2].name == "src"
if IS_SOURCE_REPOSITORY:
    PROJECT_ROOT = SOURCE_REPOSITORY_ROOT
    TOOL_PATH = (
        PROJECT_ROOT / "src" / "FeatureArchive" / "feature_archive.py"
    )
    SKILL_PATH = (
        PROJECT_ROOT
        / "plugin"
        / "dloop"
        / "skills"
        / "dloop"
        / "SKILL.md"
    )
else:
    PROJECT_ROOT = TEST_PATH.parents[3]
    TOOL_PATH = (
        PROJECT_ROOT / "Tools" / "FeatureArchive" / "feature_archive.py"
    )
    SKILL_PATH = (
        PROJECT_ROOT
        / ".agents"
        / "skills"
        / "dloop"
        / "SKILL.md"
    )
CATEGORY_DIRECTORIES = (
    "01-requirements",
    "02-investigation",
    "03-design",
    "04-plan",
    "05-implementation",
    "06-validation",
)
COMMANDS = (
    "workflow-rules",
    "init",
    "validate",
    "rebuild-indexes",
    "confirm-change",
    "refresh-queue",
    "assert-fresh",
    "rename-term",
    "analyze-impact",
    "transition-lifecycle",
    "detect-cleanup",
    "purge",
)


class FeatureArchiveWorkflowTests(unittest.TestCase):
    def test_unattended_permissions_are_project_scoped_and_fail_fast(
        self,
    ) -> None:
        skill = SKILL_PATH.read_text(encoding="utf-8")
        execution_reference = (
            SKILL_PATH.parent / "references" / "execution.md"
        ).read_text(encoding="utf-8")

        for command in ("workflow-status", "audit"):
            self.assertIn(command, skill)
        self.assertIn("活动执行身份", skill)
        self.assertIn("不接管", skill)
        self.assertIn("事务内核对资格", execution_reference)
        self.assertIn("修改租约", execution_reference)
        for contract in (
            "首次出现沙箱辅助程序、访问控制、运行时缺失或等价权限基础设施失败",
            "立即停止并走既有中断与租约恢复路径",
            "不得轮换探针",
            "拆分等价命令",
            "扩大批准前缀",
            "重复请求用户批准",
            "不新增持久化权限状态、失败计数器、自动提权或后台监控",
        ):
            self.assertIn(contract, execution_reference)

    def test_project_instructions_use_one_archive_root_and_six_categories(
        self,
    ) -> None:
        skill = SKILL_PATH.read_text(encoding="utf-8")

        self.assertIn(".scratch/dloop-v3/v3.8.0/outputs/", skill)
        if IS_SOURCE_REPOSITORY:
            initialization = (
                PROJECT_ROOT
                / "src"
                / "FeatureArchive"
                / "archive_initialization.py"
            ).read_text(encoding="utf-8")
            for directory in CATEGORY_DIRECTORIES:
                self.assertIn(f'(\"{directory}\",', initialization)
            self.assertFalse((PROJECT_ROOT / ".scratch").exists())
            return

    def test_skill_matches_cli_delivery_contract_and_responsibility_boundaries(
        self,
    ) -> None:
        skill = SKILL_PATH.read_text(encoding="utf-8")
        requirements_reference = (
            SKILL_PATH.parent / "references" / "requirements-and-terminology.md"
        ).read_text(encoding="utf-8")
        documents_reference = (
            SKILL_PATH.parent / "references" / "documents-and-dependencies.md"
        ).read_text(encoding="utf-8")
        reviews_reference = (
            SKILL_PATH.parent / "references" / "reviews-and-approvals.md"
        ).read_text(encoding="utf-8")
        help_result = subprocess.run(
            [sys.executable, str(TOOL_PATH), "--help"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

        self.assertEqual(0, help_result.returncode, help_result.stderr)
        for command in COMMANDS:
            self.assertIn(command, help_result.stdout)

        self.assertIn("delivery_view", skill)
        self.assertIn("唯一下一动作", skill)
        self.assertIn("按当次合同使用", skill)
        self.assertIn("机器阻断原样返回", skill)

        self.assertIn("只有用户在当前任务中明确指定目标并要求更新", documents_reference)
        self.assertIn("AI 只判断语义是否变化", skill)
        self.assertIn("由工具维护", skill)
        self.assertIn("用户本轮明确授权", skill)
        self.assertIn("01-requirements/terminology.md", requirements_reference)
        self.assertIn("结构必需文件", requirements_reference)
        self.assertIn("正文按需维护", requirements_reference)
        self.assertIn("不为填充结构制造无歧义术语", requirements_reference)
        self.assertIn("静默发现候选", requirements_reference)
        self.assertIn("粗体 Markdown 链接", requirements_reference)
        self.assertIn("rename-term", requirements_reference)
        self.assertIn("analyze-impact", requirements_reference)

        for visualization_contract in (
            "结构化图示",
            "出现以下任一关系复杂度时优先考虑图示",
            "完整关系分散在两个以上章节",
            "流程图",
            "状态图",
            "流向图",
            "依赖图",
            "不得增加上游材料和正文没有确认的新节点",
            "保持同一主体层级和事实类型",
            "删除图后几乎不影响理解时",
            "应删除或简化重复、装饰性图示",
            "图和正文冲突",
        ):
            self.assertIn(visualization_contract, documents_reference)
        self.assertIn("选定图示可以作为材料", reviews_reference)
        self.assertNotIn("## 独立冷读", documents_reference)
        for anti_overdesign_contract in (
            "不设置固定图数量",
            "不为所有文档预建空图章节",
            "只有一个节点且没有关系时不加图",
            "严格串行任务使用编号列表",
        ):
            self.assertIn(anti_overdesign_contract, documents_reference)

        self.assertIn("只读取能改变判断或产出的最小工作集", skill)
        self.assertIn("不能从沉默、测试通过或旧批准推断", reviews_reference)
        self.assertNotIn("## 人工确认", documents_reference)

    def test_skill_metadata_and_local_only_isolation_are_complete(self) -> None:
        skill = SKILL_PATH.read_text(encoding="utf-8")
        openai_yaml = (
            SKILL_PATH.parent / "agents" / "openai.yaml"
        ).read_text(encoding="utf-8")

        self.assertTrue(skill.startswith("---\nname: dloop\n"))
        self.assertIn("description:", skill)
        self.assertIn('display_name: "DLoop"', openai_yaml)
        self.assertIn("$dloop", openai_yaml)
        self.assertIn("allow_implicit_invocation: false", openai_yaml)
        if IS_SOURCE_REPOSITORY:
            release = (PROJECT_ROOT / "release.json").read_text(
                encoding="utf-8"
            )
            workflow_version = (PROJECT_ROOT / "VERSION").read_text(
                encoding="utf-8"
            ).strip()
            self.assertIn(
                f'"workflowVersion": "{workflow_version}"',
                release,
            )
            return

    def test_installed_version_and_integrity_lock_match_managed_files(self) -> None:
        if IS_SOURCE_REPOSITORY:
            self.skipTest("发行源由发布清单验证")
        lock = json.loads(
            (PROJECT_ROOT / ".agents" / "feature-archive-workflow.lock.json").read_text(
                encoding="utf-8"
            )
        )
        version = (PROJECT_ROOT / "Tools" / "FeatureArchive" / "VERSION").read_text(
            encoding="utf-8"
        ).strip()
        self.assertEqual("3.8.0", version)
        self.assertEqual(version, lock["workflowVersion"])
        self.assertEqual("v" + version, lock["sourceTag"])
        self.assertEqual(3, lock["archiveSchemaVersion"])
        self.assertEqual(2, lock["terminologySchemaVersion"])
        for record in lock["files"]:
            path = PROJECT_ROOT / record["path"]
            self.assertTrue(path.is_file(), record["path"])
            self.assertEqual(
                record["sha256"],
                hashlib.sha256(path.read_bytes()).hexdigest(),
                record["path"],
            )

    def test_execution_handoff_uses_one_dynamic_fact_source_across_all_rules(self) -> None:
        skill = SKILL_PATH.read_text(encoding="utf-8")
        execution = (SKILL_PATH.parent / "references" / "execution.md").read_text(
            encoding="utf-8"
        )
        reviews = (
            SKILL_PATH.parent / "references" / "reviews-and-approvals.md"
        ).read_text(encoding="utf-8")

        self.assertIn("context-summary", skill)
        self.assertIn("不读取本入口或前序对话", skill)
        self.assertIn("主协调者只传递当前交付项、角色和必需执行身份", execution)
        self.assertIn("从正式材料一次投影", execution)
        self.assertIn("不得使用实施对话", execution)
        for role in ("design-review", "final-review"):
            self.assertIn(role, reviews)
        self.assertIn("## 独立冷读", reviews)
        self.assertIn("任务消息只交付当前交付项、最终验收角色和集成确认文件定位", reviews)
        self.assertIn("不复制正文或验收事实", reviews)


if __name__ == "__main__":
    unittest.main()
