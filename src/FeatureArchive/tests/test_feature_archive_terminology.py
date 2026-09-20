"""功能交付档案业务术语与 Markdown 引用黑盒测试。"""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


TOOL_PATH = Path(__file__).resolve().parents[1] / "feature_archive.py"
sys.path.insert(0, str(TOOL_PATH.parent))

try:
    from _feature_archive_support import invoke_feature_archive
except ModuleNotFoundError:
    from ._feature_archive_support import invoke_feature_archive


class FeatureArchiveTerminologyTests(unittest.TestCase):
    def _run(
        self,
        command: str,
        root: Path,
        *arguments: str,
    ) -> subprocess.CompletedProcess[str]:
        return invoke_feature_archive(
            [command, *arguments],
            project_root=root.parents[3],
        )

    def _init(
        self,
        root: Path,
        feature_id: str = "war-center",
        title: str = "战争中心",
    ) -> Path:
        result = self._run(
            "init",
            root,
            "--feature-id",
            feature_id,
            "--title",
            title,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return root / feature_id

    @staticmethod
    def _replace_front_matter(
        path: Path,
        field: str,
        value: str,
    ) -> None:
        content = path.read_text(encoding="utf-8")
        updated, count = re.subn(
            rf"(?m)^{re.escape(field)}:\s*.*$",
            f"{field}: {value}",
            content,
        )
        if count != 1:
            raise AssertionError(f"{path} 中没有唯一的 {field} 字段")
        path.write_text(updated, encoding="utf-8", newline="\n")

    @staticmethod
    def _add_confirmed_term(
        terminology: Path,
        name: str = "NEW功能",
        definition: str = "用于向用户提示存在新内容的功能概念。",
    ) -> None:
        content = terminology.read_text(encoding="utf-8")
        marker = "## 候选术语"
        block = (
            f"### {name}\n\n"
            "- 状态：已确认\n"
            f"- 一句话定义：{definition}\n"
            "- 相邻概念边界：不负责描述具体展示条件和清除时机。\n"
            "- 别名：New、新内容提示。\n\n"
        )
        terminology.write_text(
            content.replace(marker, block + marker),
            encoding="utf-8",
            newline="\n",
        )

    def test_init_creates_required_terminology_and_requirement_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "project" / ".scratch" / "dloop-v3" / "v3.9.1" / "outputs"
            archive = self._init(root)

            terminology = archive / "01-requirements" / "terminology.md"
            requirements = archive / "01-requirements" / "README.md"
            self.assertTrue(terminology.is_file())
            self.assertIn(
                "document_id: war-center.requirements.terminology",
                terminology.read_text(encoding="utf-8"),
            )
            requirements_text = requirements.read_text(encoding="utf-8")
            self.assertIn(
                "dependencies: [war-center.requirements.terminology]",
                requirements_text,
            )
            self.assertIn(
                '"war-center.requirements.terminology": "0.1.0"',
                requirements_text,
            )
            manifest = json.loads(
                (archive / "feature.json").read_text(encoding="utf-8")
            )
            self.assertEqual(2, manifest["terminology_schema_version"])

            validated = self._run("validate", root)
            self.assertEqual(0, validated.returncode, validated.stderr)

    def test_missing_terminology_contract_is_rejected_without_retrofit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "project" / ".scratch" / "dloop-v3" / "v3.9.1" / "outputs"
            archive = self._init(root)
            terminology = archive / "01-requirements" / "terminology.md"
            terminology.unlink()

            manifest_path = archive / "feature.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            del manifest["terminology_schema_version"]
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )

            entry = archive / "README.md"
            entry.write_text(
                entry.read_text(encoding="utf-8").replace(
                    "2. [业务术语表](01-requirements/terminology.md)\n",
                    "",
                ),
                encoding="utf-8",
                newline="\n",
            )
            requirements = archive / "01-requirements" / "README.md"
            self._replace_front_matter(requirements, "dependencies", "[]")
            self._replace_front_matter(
                requirements,
                "dependency_versions",
                "{}",
            )
            requirements.write_text(
                requirements.read_text(encoding="utf-8").replace(
                    "- [业务术语表](terminology.md)",
                    "当前没有专题文档。仅在内容复杂度确有需要时新增。",
                ),
                encoding="utf-8",
                newline="\n",
            )
            validation = self._run("validate", root)
            self.assertEqual(1, validation.returncode)
            self.assertEqual("INVALID_MANIFEST", json.loads(validation.stderr)["code"])

            result = self._run(
                "init",
                root,
                "--feature-id",
                "war-center",
                "--title",
                "战争中心",
            )

            self.assertEqual(1, result.returncode)
            self.assertEqual(
                "INCOMPATIBLE_TERMINOLOGY_VERSION",
                json.loads(result.stderr)["code"],
            )
            self.assertFalse(terminology.exists())

    def test_only_fixed_terminology_same_category_dependency_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "project" / ".scratch" / "dloop-v3" / "v3.9.1" / "outputs"
            archive = self._init(root)
            other = archive / "01-requirements" / "other.md"
            other.write_text(
                (archive / "01-requirements" / "terminology.md")
                .read_text(encoding="utf-8")
                .replace(
                    "war-center.requirements.terminology",
                    "war-center.requirements.other",
                ),
                encoding="utf-8",
                newline="\n",
            )
            requirements = archive / "01-requirements" / "README.md"
            self._replace_front_matter(
                requirements,
                "dependencies",
                "[war-center.requirements.terminology, "
                "war-center.requirements.other]",
            )
            self._replace_front_matter(
                requirements,
                "dependency_versions",
                '{"war-center.requirements.other": "0.1.0", '
                '"war-center.requirements.terminology": "0.1.0"}',
            )

            result = self._run("validate", root)

            self.assertEqual(1, result.returncode)
            self.assertEqual(
                "INVALID_DEPENDENCY_DIRECTION",
                json.loads(result.stderr)["code"],
            )

    def test_confirmed_bold_term_link_and_normal_document_link_validate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "project" / ".scratch" / "dloop-v3" / "v3.9.1" / "outputs"
            archive = self._init(root)
            terminology = archive / "01-requirements" / "terminology.md"
            self._add_confirmed_term(terminology)
            design = archive / "03-design" / "README.md"
            with design.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(
                    "\n## NEW设计\n\n"
                    "设计实现**[NEW功能]"
                    "(../01-requirements/terminology.md#new功能)**，"
                    "并遵守[需求边界](../01-requirements/README.md#当前摘要)。\n"
                )

            result = self._run("validate", root)

            self.assertEqual(0, result.returncode, result.stderr)

    def test_candidate_term_cannot_be_formally_referenced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "project" / ".scratch" / "dloop-v3" / "v3.9.1" / "outputs"
            archive = self._init(root)
            terminology = archive / "01-requirements" / "terminology.md"
            content = terminology.read_text(encoding="utf-8")
            terminology.write_text(
                content
                + "\n### 临时候选\n\n"
                "- 状态：候选\n"
                "- 一句话定义：尚未获得用户确认的业务概念。\n",
                encoding="utf-8",
                newline="\n",
            )
            design = archive / "03-design" / "README.md"
            with design.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(
                    "\n正式使用**[临时候选]"
                    "(../01-requirements/terminology.md#临时候选)**。\n"
                )

            result = self._run("validate", root)

            self.assertEqual(1, result.returncode)
            self.assertEqual(
                "INVALID_TERM_REFERENCE",
                json.loads(result.stderr)["code"],
            )

    def test_broken_document_anchor_is_rejected_but_code_example_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "project" / ".scratch" / "dloop-v3" / "v3.9.1" / "outputs"
            archive = self._init(root)
            design = archive / "03-design" / "README.md"
            with design.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(
                    "\n```markdown\n"
                    "[示例](../01-requirements/README.md#不存在)\n"
                    "```\n"
                    "\n真实[失效引用](../01-requirements/README.md#不存在)。\n"
                )

            result = self._run("validate", root)

            self.assertEqual(1, result.returncode)
            self.assertEqual(
                "BROKEN_DOCUMENT_ANCHOR",
                json.loads(result.stderr)["code"],
            )

    def test_rename_term_updates_definition_references_and_alias_without_version_bump(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "project" / ".scratch" / "dloop-v3" / "v3.9.1" / "outputs"
            archive = self._init(root)
            terminology = archive / "01-requirements" / "terminology.md"
            self._add_confirmed_term(terminology)
            requirements = archive / "01-requirements" / "README.md"
            with requirements.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(
                    "\n使用**[NEW功能](./terminology.md#new功能)**。\n"
                )
            before_version = re.search(
                r"(?m)^semantic_version:\s*(\S+)$",
                terminology.read_text(encoding="utf-8"),
            ).group(1)

            result = self._run(
                "rename-term",
                root,
                "--feature-id",
                "war-center",
                "--from",
                "NEW功能",
                "--to",
                "新内容提示",
            )

            self.assertEqual(0, result.returncode, result.stderr)
            response = json.loads(result.stdout)
            self.assertEqual("renamed", response["status"])
            terminology_text = terminology.read_text(encoding="utf-8")
            self.assertIn("### 新内容提示", terminology_text)
            self.assertIn("NEW功能", terminology_text)
            self.assertNotIn("### NEW功能", terminology_text)
            self.assertIn(
                "**[新内容提示](./terminology.md#新内容提示)**",
                requirements.read_text(encoding="utf-8"),
            )
            after_version = re.search(
                r"(?m)^semantic_version:\s*(\S+)$",
                terminology_text,
            ).group(1)
            self.assertEqual(before_version, after_version)
            self.assertEqual(0, self._run("validate", root).returncode)

    def test_rename_term_rejects_frozen_archive_without_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "project" / ".scratch" / "dloop-v3" / "v3.9.1" / "outputs"
            archive = self._init(root)
            terminology = archive / "01-requirements" / "terminology.md"
            self._add_confirmed_term(terminology)
            manifest = archive / "feature.json"
            value = json.loads(manifest.read_text(encoding="utf-8"))
            value["lifecycle"] = "frozen"
            value["frozen_at"] = "2026-07-29T00:00:00Z"
            manifest.write_text(
                json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            before = {
                path.relative_to(archive).as_posix(): path.read_bytes()
                for path in archive.rglob("*")
                if path.is_file()
            }

            result = self._run(
                "rename-term",
                root,
                "--feature-id",
                "war-center",
                "--from",
                "NEW功能",
                "--to",
                "新内容提示",
            )

            self.assertEqual(1, result.returncode)
            self.assertEqual("READ_ONLY_ARCHIVE", json.loads(result.stderr)["code"])
            after = {
                path.relative_to(archive).as_posix(): path.read_bytes()
                for path in archive.rglob("*")
                if path.is_file()
            }
            self.assertEqual(before, after)

    def test_semantic_definition_change_invalidates_requirements(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "project" / ".scratch" / "dloop-v3" / "v3.9.1" / "outputs"
            archive = self._init(root)
            terminology = archive / "01-requirements" / "terminology.md"
            self._add_confirmed_term(terminology)

            changed = self._run(
                "confirm-change",
                root,
                "--document-id",
                "war-center.requirements.terminology",
                "--semantic-change",
                "true",
            )

            self.assertEqual(0, changed.returncode, changed.stderr)
            requirements = archive / "01-requirements" / "README.md"
            self.assertIn(
                "content_status: stale",
                requirements.read_text(encoding="utf-8"),
            )
            queue = self._run(
                "refresh-queue",
                root,
                "--feature-id",
                "war-center",
            )
            self.assertEqual(0, queue.returncode, queue.stderr)
            self.assertEqual(
                ["war-center.requirements.overview"],
                json.loads(queue.stdout)["refresh_queue"],
            )

    def test_analyze_impact_combines_term_references_and_dependency_graph(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "project" / ".scratch" / "dloop-v3" / "v3.9.1" / "outputs"
            archive = self._init(root)
            terminology = archive / "01-requirements" / "terminology.md"
            self._add_confirmed_term(terminology)
            requirements = archive / "01-requirements" / "README.md"
            design = archive / "03-design" / "README.md"
            with design.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(
                    "\n## NEW职责\n\n"
                    "设计**[NEW功能]"
                    "(../01-requirements/terminology.md#new功能)**。\n"
                )
            self._replace_front_matter(
                design,
                "dependencies",
                "[war-center.requirements.overview]",
            )
            self._replace_front_matter(
                design,
                "dependency_versions",
                '{"war-center.requirements.overview": "0.1.0"}',
            )
            plan = archive / "04-plan" / "README.md"
            self._replace_front_matter(
                plan,
                "dependencies",
                "[war-center.design.overview]",
            )
            self._replace_front_matter(
                plan,
                "dependency_versions",
                '{"war-center.design.overview": "0.1.0"}',
            )

            result = self._run(
                "analyze-impact",
                root,
                "--feature-id",
                "war-center",
                "--term",
                "NEW功能",
            )

            self.assertEqual(0, result.returncode, result.stderr)
            response = json.loads(result.stdout)
            self.assertEqual("NEW功能", response["term"])
            self.assertEqual(
                ["war-center.requirements.overview"],
                response["direct_dependents"],
            )
            self.assertEqual(
                [
                    "war-center.requirements.overview",
                    "war-center.design.overview",
                    "war-center.plan.overview",
                ],
                response["transitive_dependents"],
            )
            self.assertEqual(
                "war-center/03-design/README.md",
                response["references"][0]["path"],
            )
            self.assertEqual("NEW职责", response["references"][0]["section"])
            self.assertEqual("0.1.0", re.search(
                r"(?m)^semantic_version:\s*(\S+)$",
                requirements.read_text(encoding="utf-8"),
            ).group(1))

if __name__ == "__main__":
    unittest.main()
