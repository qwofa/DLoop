"""功能交付档案命令行黑盒测试。"""

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
    from _feature_archive_support import complete_test_implementation, invoke_feature_archive
except ModuleNotFoundError:
    from ._feature_archive_support import complete_test_implementation, invoke_feature_archive
CATEGORY_DIRECTORIES = (
    "01-requirements",
    "02-investigation",
    "03-design",
    "04-plan",
    "05-implementation",
    "06-validation",
)
DOCUMENT_ID_PATTERN = re.compile(
    r"^[a-z0-9]+(?:-[a-z0-9]+)*(?:\.[a-z0-9]+(?:-[a-z0-9]+)*)+$"
)


class FeatureArchiveCliTests(unittest.TestCase):
    def _run_command(
        self,
        command: str,
        root: Path,
        *arguments: str,
    ) -> subprocess.CompletedProcess[str]:
        return invoke_feature_archive(
            [command, *arguments],
            project_root=root.parents[3],
        )

    def _run_init(
        self,
        root: Path,
        feature_id: str = "building-interaction",
        title: str = "建筑交互",
    ) -> subprocess.CompletedProcess[str]:
        return invoke_feature_archive(
            [
                "init",
                "--feature-id",
                feature_id,
                "--title",
                title,
            ],
            project_root=root.parents[3],
        )

    @staticmethod
    def _front_matter(path: Path) -> dict[str, str]:
        lines = path.read_text(encoding="utf-8").splitlines()
        if not lines or lines[0] != "---":
            raise AssertionError(f"{path} 缺少元数据")
        fields: dict[str, str] = {}
        for line in lines[1:]:
            if line == "---":
                return fields
            if ":" in line:
                key, value = line.split(":", 1)
                fields[key] = value.strip()
        raise AssertionError(f"{path} 的元数据未闭合")

    @staticmethod
    def _snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
        return {
            str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in root.rglob("*")
            if path.is_file()
        }

    @staticmethod
    def _replace_metadata_list(path: Path, field: str, values: list[str]) -> None:
        content = path.read_text(encoding="utf-8")
        replacement = f"{field}: [{', '.join(values)}]"
        updated, count = re.subn(
            rf"(?m)^{re.escape(field)}:\s*\[.*\]\s*$",
            replacement,
            content,
        )
        if count != 1:
            raise AssertionError(f"{path} 中没有唯一的 {field} 列表")
        path.write_text(updated, encoding="utf-8", newline="\n")

    @staticmethod
    def _replace_metadata_scalar(path: Path, field: str, value: str) -> None:
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
    def _add_stale_from_status(path: Path, value: str = "confirmed") -> None:
        content = path.read_text(encoding="utf-8")
        updated, count = re.subn(
            r"(?m)^content_status: stale$",
            f"content_status: stale\nstale_from_status: {value}",
            content,
        )
        if count != 1:
            raise AssertionError(f"{path} 没有唯一的 stale 状态")
        path.write_text(updated, encoding="utf-8", newline="\n")

    def _confirm_change(
        self,
        root: Path,
        document_id: str,
        semantic_change: bool,
    ) -> subprocess.CompletedProcess[str]:
        return self._run_command(
            "confirm-change",
            root,
            "--document-id",
            document_id,
            "--semantic-change",
            "true" if semantic_change else "false",
        )

    def _transition(
        self,
        root: Path,
        target: str,
        *arguments: str,
        feature_id: str = "building-interaction",
    ) -> subprocess.CompletedProcess[str]:
        return self._run_command(
            "transition-lifecycle",
            root,
            "--feature-id",
            feature_id,
            "--to",
            target,
            *arguments,
        )

    def _set_category_statuses(
        self,
        archive: Path,
        status: str,
    ) -> None:
        for directory in CATEGORY_DIRECTORIES:
            self._replace_metadata_scalar(
                archive / directory / "README.md",
                "content_status",
                status,
            )
        terminology = archive / "01-requirements" / "terminology.md"
        if terminology.is_file():
            self._replace_metadata_scalar(
                terminology,
                "content_status",
                status,
            )

    def _approve_for_freeze(
        self,
        root: Path,
        feature_id: str = "building-interaction",
    ) -> None:
        requirements = self._run_command(
            "stage-action",
            root,
            "--feature-id",
            feature_id,
            "--stage",
            "requirements",
            "--decision",
            "approve",
        )
        self.assertEqual(0, requirements.returncode, requirements.stderr)

        def run_cli(*arguments: object) -> dict[str, object]:
            result = self._run_command(
                str(arguments[0]),
                root,
                *(str(argument) for argument in arguments[1:]),
            )
            self.assertEqual(0, result.returncode, result.stderr)
            return json.loads(result.stdout)

        confirmation = complete_test_implementation(
            run_cli,
            root,
            feature_id,
            root.parents[3].parent / f"{feature_id}-workspace",
        )
        final = self._run_command(
            "stage-action",
            root,
            "--feature-id",
            feature_id,
            "--stage",
            "final",
            "--decision",
            "approve",
            "--integration-confirmation",
            str(confirmation),
        )
        self.assertEqual(0, final.returncode, final.stderr)

    def _freeze_at(
        self,
        root: Path,
        frozen_at: str,
        feature_id: str = "building-interaction",
    ) -> None:
        archive = root / feature_id
        self.assertEqual(
            0,
            self._transition(
                root,
                "active",
                "--now",
                frozen_at,
                feature_id=feature_id,
            ).returncode,
        )
        self.assertEqual(
            0,
            self._transition(
                root,
                "validating",
                "--now",
                frozen_at,
                feature_id=feature_id,
            ).returncode,
        )
        self._set_category_statuses(archive, "completed")
        self._approve_for_freeze(root, feature_id)
        result = self._transition(
            root,
            "frozen",
            "--validation-conclusion",
            "passed",
            "--unverified-boundaries",
            "none",
            "--residual-risks",
            "none",
            "--now",
            frozen_at,
            feature_id=feature_id,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def _prepare_three_layer_chain(
        self,
        root: Path,
    ) -> tuple[Path, Path, Path]:
        self.assertEqual(0, self._run_init(root).returncode)
        archive = root / "building-interaction"
        requirements = archive / "01-requirements" / "README.md"
        design = archive / "03-design" / "README.md"
        plan = archive / "04-plan" / "README.md"
        self._replace_metadata_list(
            design,
            "dependencies",
            ["building-interaction.requirements.overview"],
        )
        self._replace_metadata_list(
            plan,
            "dependencies",
            ["building-interaction.design.overview"],
        )
        design_result = self._confirm_change(
            root,
            "building-interaction.design.overview",
            False,
        )
        self.assertEqual(0, design_result.returncode, design_result.stderr)
        plan_result = self._confirm_change(
            root,
            "building-interaction.plan.overview",
            False,
        )
        self.assertEqual(0, plan_result.returncode, plan_result.stderr)
        return requirements, design, plan

    def test_init_creates_strict_archive_with_unique_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "project" / ".scratch" / "dloop-v3" / "v3.9.0" / "outputs"
            result = self._run_init(root)

            self.assertEqual(0, result.returncode, result.stderr)
            response = json.loads(result.stdout)
            self.assertEqual("created", response["status"])
            archive = root / "building-interaction"
            self.assertTrue((archive / "README.md").is_file())
            self.assertTrue((archive / "feature.json").is_file())

            manifest = json.loads((archive / "feature.json").read_text(encoding="utf-8"))
            self.assertEqual("building-interaction", manifest["feature_id"])
            self.assertEqual("建筑交互", manifest["title"])
            self.assertEqual("draft", manifest["lifecycle"])
            self.assertEqual(list(CATEGORY_DIRECTORIES), [
                category["directory"] for category in manifest["categories"]
            ])

            generated_documents = [archive / "README.md"]
            for directory in CATEGORY_DIRECTORIES:
                category_path = archive / directory
                self.assertTrue(category_path.is_dir())
                expected_names = (
                    ["README.md", "terminology.md"]
                    if directory == "01-requirements"
                    else ["README.md"]
                )
                self.assertEqual(
                    expected_names,
                    sorted(path.name for path in category_path.iterdir()),
                )
                generated_documents.append(category_path / "README.md")
            generated_documents.append(
                archive / "01-requirements" / "terminology.md"
            )

            document_ids = []
            for document in generated_documents:
                metadata = self._front_matter(document)
                self.assertEqual("draft", metadata["content_status"])
                self.assertEqual("0.1.0", metadata["semantic_version"])
                if metadata["document_id"].endswith(".requirements.overview"):
                    self.assertEqual(
                        "[building-interaction.requirements.terminology]",
                        metadata["dependencies"],
                    )
                    self.assertIn(
                        "building-interaction.requirements.terminology",
                        metadata["dependency_versions"],
                    )
                else:
                    self.assertEqual("[]", metadata["dependencies"])
                    self.assertEqual("{}", metadata["dependency_versions"])
                self.assertTrue(metadata["content_fingerprint"].startswith("sha256:"))
                document_ids.append(metadata["document_id"])
                self.assertRegex(metadata["document_id"], DOCUMENT_ID_PATTERN)
                self.assertRegex(document.read_text(encoding="utf-8"), r"[\u4e00-\u9fff]")

            self.assertEqual(len(document_ids), len(set(document_ids)))

    def test_repeated_init_is_idempotent_and_preserves_user_body(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "project" / ".scratch" / "dloop-v3" / "v3.9.0" / "outputs"
            first_result = self._run_init(root)
            self.assertEqual(0, first_result.returncode, first_result.stderr)

            design_overview = (
                root / "building-interaction" / "03-design" / "README.md"
            )
            with design_overview.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write("\n## 用户正文\n\n保留这段已经填写的设计内容。\n")
            custom_note = root / "building-interaction" / "03-design" / "权衡记录.md"
            custom_note.write_text("# 用户专题\n\n不强制用户草稿立即补充元数据。\n", encoding="utf-8")
            before = self._snapshot(root)

            second_result = self._run_init(root)

            self.assertEqual(0, second_result.returncode, second_result.stderr)
            self.assertEqual("unchanged", json.loads(second_result.stdout)["status"])
            self.assertEqual(before, self._snapshot(root))
            self.assertIn("保留这段已经填写的设计内容", design_overview.read_text(encoding="utf-8"))

    def test_existing_partial_archive_is_rejected_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "project" / ".scratch" / "dloop-v3" / "v3.9.0" / "outputs"
            self.assertEqual(0, self._run_init(root, "seed", "根初始化").returncode)
            archive = root / "building-interaction"
            archive.mkdir(parents=True)
            note = archive / "用户说明.txt"
            note.write_text("不要覆盖", encoding="utf-8")

            result = self._run_init(root)

            self.assertEqual(1, result.returncode)
            self.assertEqual("INCOMPLETE_ARCHIVE", json.loads(result.stderr)["code"])
            self.assertEqual("不要覆盖", note.read_text(encoding="utf-8"))
            for directory in CATEGORY_DIRECTORIES:
                self.assertFalse((archive / directory).exists())

    def test_path_conflict_fails_before_writing_and_reports_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "project" / ".scratch" / "dloop-v3" / "v3.9.0" / "outputs"
            self.assertEqual(0, self._run_init(root, "seed", "根初始化").returncode)
            archive = root / "building-interaction"
            archive.mkdir(parents=True)
            conflict = archive / "03-design"
            conflict.write_text("这是冲突文件", encoding="utf-8")
            before = self._snapshot(root)

            result = self._run_init(root)

            self.assertEqual(1, result.returncode)
            diagnostic = json.loads(result.stderr)
            self.assertEqual("failed", diagnostic["status"])
            self.assertEqual("PATH_CONFLICT", diagnostic["code"])
            self.assertIn("请移动冲突文件后重试", diagnostic["message"])
            self.assertEqual(before, self._snapshot(root))
            self.assertFalse((archive / "README.md").exists())
            self.assertFalse((archive / "01-requirements").exists())

    def test_invalid_feature_id_does_not_create_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "project" / ".scratch" / "dloop-v3" / "v3.9.0" / "outputs"

            result = self._run_init(root, feature_id="../逃逸")

            self.assertEqual(1, result.returncode)
            diagnostic = json.loads(result.stderr)
            self.assertEqual("INVALID_FEATURE_ID", diagnostic["code"])
            self.assertFalse(root.exists())

    def test_validate_accepts_forward_and_skipped_layer_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "project" / ".scratch" / "dloop-v3" / "v3.9.0" / "outputs"
            self.assertEqual(0, self._run_init(root).returncode)
            archive = root / "building-interaction"
            investigation = archive / "02-investigation" / "README.md"
            implementation = archive / "05-implementation" / "README.md"
            self._replace_metadata_list(
                investigation,
                "dependencies",
                ["building-interaction.requirements.overview"],
            )
            self._replace_metadata_list(
                implementation,
                "dependencies",
                [
                    "building-interaction.requirements.overview",
                    "building-interaction.investigation.overview",
                ],
            )

            result = self._run_command("validate", root)

            self.assertEqual(0, result.returncode, result.stderr)
            response = json.loads(result.stdout)
            self.assertEqual("valid", response["status"])
            self.assertEqual(4, response["dependency_count"])
            self.assertFalse((root / "feature-archive-dependencies.json").exists())
            self.assertFalse((root / "feature-archives.md").exists())

    def test_validate_rejects_missing_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "project" / ".scratch" / "dloop-v3" / "v3.9.0" / "outputs"
            self.assertEqual(0, self._run_init(root).returncode)
            design = root / "building-interaction" / "03-design" / "README.md"
            self._replace_metadata_list(
                design,
                "dependencies",
                ["building-interaction.requirements.missing"],
            )

            result = self._run_command("validate", root)

            self.assertEqual(1, result.returncode)
            diagnostic = json.loads(result.stderr)
            self.assertEqual("MISSING_DEPENDENCY", diagnostic["code"])
            self.assertIn("不存在", diagnostic["message"])

    def test_validate_rejects_reverse_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "project" / ".scratch" / "dloop-v3" / "v3.9.0" / "outputs"
            self.assertEqual(0, self._run_init(root).returncode)
            requirements = root / "building-interaction" / "01-requirements" / "README.md"
            self._replace_metadata_list(
                requirements,
                "dependencies",
                [
                    "building-interaction.requirements.terminology",
                    "building-interaction.design.overview",
                ],
            )
            self._replace_metadata_scalar(
                requirements,
                "dependency_versions",
                '{"building-interaction.design.overview": "0.1.0", '
                '"building-interaction.requirements.terminology": "0.1.0"}',
            )

            result = self._run_command("validate", root)

            self.assertEqual(1, result.returncode)
            diagnostic = json.loads(result.stderr)
            self.assertEqual("INVALID_DEPENDENCY_DIRECTION", diagnostic["code"])
            self.assertIn("违反六层正向职责顺序", diagnostic["message"])

    def test_validate_rejects_cross_feature_hard_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "project" / ".scratch" / "dloop-v3" / "v3.9.0" / "outputs"
            self.assertEqual(0, self._run_init(root).returncode)
            self.assertEqual(
                0,
                self._run_init(
                    root,
                    feature_id="combat-report",
                    title="战报",
                ).returncode,
            )
            design = root / "building-interaction" / "03-design" / "README.md"
            self._replace_metadata_list(
                design,
                "dependencies",
                ["combat-report.requirements.overview"],
            )

            result = self._run_command("validate", root)

            self.assertEqual(1, result.returncode)
            diagnostic = json.loads(result.stderr)
            self.assertEqual("CROSS_FEATURE_DEPENDENCY", diagnostic["code"])
            self.assertIn("related_documents", diagnostic["message"])

    def test_validate_rejects_self_and_multi_document_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "project" / ".scratch" / "dloop-v3" / "v3.9.0" / "outputs"
            self.assertEqual(0, self._run_init(root).returncode)
            archive = root / "building-interaction"
            requirements = archive / "01-requirements" / "README.md"
            investigation = archive / "02-investigation" / "README.md"
            self._replace_metadata_list(
                requirements,
                "dependencies",
                [
                    "building-interaction.requirements.terminology",
                    "building-interaction.requirements.overview",
                ],
            )
            self._replace_metadata_scalar(
                requirements,
                "dependency_versions",
                '{"building-interaction.requirements.overview": "0.1.0", '
                '"building-interaction.requirements.terminology": "0.1.0"}',
            )

            self_result = self._run_command("validate", root)
            self.assertEqual(1, self_result.returncode)
            self.assertEqual("SELF_DEPENDENCY", json.loads(self_result.stderr)["code"])

            self._replace_metadata_list(
                requirements,
                "dependencies",
                [
                    "building-interaction.requirements.terminology",
                    "building-interaction.investigation.overview",
                ],
            )
            self._replace_metadata_scalar(
                requirements,
                "dependency_versions",
                '{"building-interaction.investigation.overview": "0.1.0", '
                '"building-interaction.requirements.terminology": "0.1.0"}',
            )
            self._replace_metadata_list(
                investigation,
                "dependencies",
                ["building-interaction.requirements.overview"],
            )

            cycle_result = self._run_command("validate", root)
            self.assertEqual(1, cycle_result.returncode)
            diagnostic = json.loads(cycle_result.stderr)
            self.assertEqual("CYCLIC_DEPENDENCY", diagnostic["code"])
            self.assertIn("循环依赖", diagnostic["message"])

    def test_validate_rejects_duplicate_document_identifier(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "project" / ".scratch" / "dloop-v3" / "v3.9.0" / "outputs"
            self.assertEqual(0, self._run_init(root).returncode)
            archive = root / "building-interaction"
            duplicate = archive / "03-design" / "重复设计.md"
            duplicate.write_text(
                (archive / "03-design" / "README.md").read_text(encoding="utf-8"),
                encoding="utf-8",
            )

            result = self._run_command("validate", root)

            self.assertEqual(1, result.returncode)
            diagnostic = json.loads(result.stderr)
            self.assertEqual("DUPLICATE_DOCUMENT_ID", diagnostic["code"])

    def test_rebuild_indexes_is_complete_non_destructive_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "project" / ".scratch" / "dloop-v3" / "v3.9.0" / "outputs"
            self.assertEqual(0, self._run_init(root).returncode)
            self.assertEqual(
                0,
                self._run_init(
                    root,
                    feature_id="combat-report",
                    title="战报",
                ).returncode,
            )
            archive = root / "building-interaction"
            building_manifest_path = archive / "feature.json"
            building_manifest = json.loads(
                building_manifest_path.read_text(encoding="utf-8")
            )
            building_manifest["lifecycle"] = "validating"
            building_manifest_path.write_text(
                json.dumps(building_manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            design = archive / "03-design" / "README.md"
            validation = archive / "06-validation" / "README.md"
            self._replace_metadata_list(
                design,
                "dependencies",
                ["building-interaction.requirements.overview"],
            )
            self._replace_metadata_list(
                design,
                "related_documents",
                [
                    "combat-report.design.overview",
                    "retired-feature.design.overview",
                ],
            )
            self._replace_metadata_scalar(validation, "content_status", "stale")
            self._add_stale_from_status(validation)
            combat_requirements = (
                root / "combat-report" / "01-requirements" / "README.md"
            )
            self._replace_metadata_scalar(
                combat_requirements,
                "content_status",
                "blocked",
            )
            combat_manifest_path = root / "combat-report" / "feature.json"
            combat_manifest = json.loads(
                combat_manifest_path.read_text(encoding="utf-8")
            )
            combat_manifest["lifecycle"] = "pending_cleanup"
            combat_manifest["frozen_at"] = "2026-06-01T00:00:00Z"
            combat_manifest_path.write_text(
                json.dumps(combat_manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            archive_before = {
                feature_id: self._snapshot(root / feature_id)
                for feature_id in ("building-interaction", "combat-report")
            }

            first_result = self._run_command("rebuild-indexes", root)

            self.assertEqual(0, first_result.returncode, first_result.stderr)
            first_response = json.loads(first_result.stdout)
            self.assertEqual("rebuilt", first_response["status"])
            machine_path = root / "feature-archive-dependencies.json"
            global_path = root / "feature-archives.md"
            machine_index = json.loads(machine_path.read_text(encoding="utf-8"))
            self.assertEqual(
                ["building-interaction.requirements.overview"],
                machine_index["documents"][
                    "building-interaction.design.overview"
                ]["dependencies"],
            )
            self.assertEqual(
                ["building-interaction.design.overview"],
                machine_index["documents"][
                    "building-interaction.requirements.overview"
                ]["dependents"],
            )
            related_edges = machine_index["related_edges"]
            self.assertIn(
                {
                    "from": "building-interaction.design.overview",
                    "to": "combat-report.design.overview",
                    "resolved": True,
                },
                related_edges,
            )
            self.assertIn(
                {
                    "from": "building-interaction.design.overview",
                    "to": "retired-feature.design.overview",
                    "resolved": False,
                },
                related_edges,
            )
            global_index = global_path.read_text(encoding="utf-8")
            self.assertIn("验证中", global_index)
            self.assertIn("待清理档案：`combat-report`", global_index)
            self.assertIn("building-interaction.validation.overview", global_index)
            self.assertIn("combat-report.requirements.overview", global_index)
            self.assertIn("2026-06-01T00:00:00Z", global_index)
            self.assertEqual(
                archive_before,
                {
                    feature_id: self._snapshot(root / feature_id)
                    for feature_id in ("building-interaction", "combat-report")
                },
            )
            first_index_snapshot = {
                path.name: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in (machine_path, global_path)
            }

            second_result = self._run_command("rebuild-indexes", root)

            self.assertEqual(0, second_result.returncode, second_result.stderr)
            self.assertEqual("unchanged", json.loads(second_result.stdout)["status"])
            self.assertEqual(
                first_index_snapshot,
                {
                    path.name: (path.read_bytes(), path.stat().st_mtime_ns)
                    for path in (machine_path, global_path)
                },
            )

            machine_path.unlink()
            rebuilt_result = self._run_command("rebuild-indexes", root)

            self.assertEqual(0, rebuilt_result.returncode, rebuilt_result.stderr)
            rebuilt_machine = json.loads(machine_path.read_text(encoding="utf-8"))
            self.assertEqual(machine_index, rebuilt_machine)

    def test_non_semantic_batch_updates_fingerprint_once_without_invalidation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "project" / ".scratch" / "dloop-v3" / "v3.9.0" / "outputs"
            requirements, design, _ = self._prepare_three_layer_chain(root)
            requirements.write_text(
                requirements.read_text(encoding="utf-8")
                + "\n第一次保存。\n",
                encoding="utf-8",
                newline="\n",
            )
            requirements.write_text(
                requirements.read_text(encoding="utf-8")
                + "第二次保存，仅修正文案。\n",
                encoding="utf-8",
                newline="\n",
            )

            result = self._confirm_change(
                root,
                "building-interaction.requirements.overview",
                False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            response = json.loads(result.stdout)
            self.assertEqual("committed", response["status"])
            self.assertFalse(response["semantic_change"])
            self.assertEqual([], response["invalidated_documents"])
            self.assertEqual([], response["refresh_queue"])
            self.assertEqual(
                "0.1.0",
                self._front_matter(requirements)["semantic_version"],
            )
            self.assertEqual("draft", self._front_matter(design)["content_status"])
            snapshot = self._snapshot(root)

            repeated = self._confirm_change(
                root,
                "building-interaction.requirements.overview",
                False,
            )

            self.assertEqual(0, repeated.returncode, repeated.stderr)
            self.assertEqual("unchanged", json.loads(repeated.stdout)["status"])
            self.assertEqual(snapshot, self._snapshot(root))

    def test_semantic_batch_invalidates_only_direct_dependents_and_blocks_input(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "project" / ".scratch" / "dloop-v3" / "v3.9.0" / "outputs"
            requirements, design, plan = self._prepare_three_layer_chain(root)
            with requirements.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write("\n新增业务规则。\n")

            result = self._confirm_change(
                root,
                "building-interaction.requirements.overview",
                True,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            response = json.loads(result.stdout)
            self.assertEqual(
                ["building-interaction.design.overview"],
                response["invalidated_documents"],
            )
            self.assertEqual(
                ["building-interaction.design.overview"],
                response["refresh_queue"],
            )
            self.assertEqual(
                "0.1.1",
                self._front_matter(requirements)["semantic_version"],
            )
            self.assertEqual("stale", self._front_matter(design)["content_status"])
            self.assertEqual("draft", self._front_matter(plan)["content_status"])
            queue_result = self._run_command("refresh-queue", root)
            self.assertEqual(0, queue_result.returncode, queue_result.stderr)
            self.assertEqual(
                ["building-interaction.design.overview"],
                json.loads(queue_result.stdout)["refresh_queue"],
            )

            stale_result = self._run_command(
                "assert-fresh",
                root,
                "--document-id",
                "building-interaction.design.overview",
            )
            self.assertEqual(1, stale_result.returncode)
            self.assertEqual(
                "STALE_DOCUMENT",
                json.loads(stale_result.stderr)["code"],
            )
            plan_result = self._run_command(
                "assert-fresh",
                root,
                "--document-id",
                "building-interaction.plan.overview",
            )
            self.assertEqual(0, plan_result.returncode, plan_result.stderr)

    def test_non_semantic_refresh_stops_propagation_at_middle_layer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "project" / ".scratch" / "dloop-v3" / "v3.9.0" / "outputs"
            requirements, design, plan = self._prepare_three_layer_chain(root)
            with requirements.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write("\n调整上游规则。\n")
            self.assertEqual(
                0,
                self._confirm_change(
                    root,
                    "building-interaction.requirements.overview",
                    True,
                ).returncode,
            )
            plan_before = plan.read_bytes()

            result = self._confirm_change(
                root,
                "building-interaction.design.overview",
                False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            response = json.loads(result.stdout)
            self.assertEqual([], response["invalidated_documents"])
            self.assertEqual([], response["refresh_queue"])
            design_metadata = self._front_matter(design)
            self.assertEqual("0.1.0", design_metadata["semantic_version"])
            self.assertEqual("draft", design_metadata["content_status"])
            self.assertEqual(
                {"building-interaction.requirements.overview": "0.1.1"},
                json.loads(design_metadata["dependency_versions"]),
            )
            self.assertEqual(plan_before, plan.read_bytes())

    def test_semantic_refresh_continues_to_next_direct_layer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "project" / ".scratch" / "dloop-v3" / "v3.9.0" / "outputs"
            requirements, design, plan = self._prepare_three_layer_chain(root)
            with requirements.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write("\n调整上游规则。\n")
            self.assertEqual(
                0,
                self._confirm_change(
                    root,
                    "building-interaction.requirements.overview",
                    True,
                ).returncode,
            )
            with design.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write("\n设计语义随上游继续变化。\n")

            result = self._confirm_change(
                root,
                "building-interaction.design.overview",
                True,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            response = json.loads(result.stdout)
            self.assertEqual(
                ["building-interaction.plan.overview"],
                response["invalidated_documents"],
            )
            self.assertEqual(
                ["building-interaction.plan.overview"],
                response["refresh_queue"],
            )
            self.assertEqual(
                "0.1.1",
                self._front_matter(design)["semantic_version"],
            )
            self.assertEqual("stale", self._front_matter(plan)["content_status"])

    def test_legal_lifecycle_transitions_update_entry_manifest_and_indexes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "project" / ".scratch" / "dloop-v3" / "v3.9.0" / "outputs"
            self.assertEqual(0, self._run_init(root).returncode)
            archive = root / "building-interaction"
            requirements = archive / "01-requirements" / "README.md"

            active = self._transition(root, "active")
            self.assertEqual(0, active.returncode, active.stderr)
            self.assertEqual("transitioned", json.loads(active.stdout)["status"])
            self.assertEqual(
                "draft",
                self._front_matter(requirements)["content_status"],
            )
            self.assertIn(
                "- 当前生命周期：活跃",
                (archive / "README.md").read_text(encoding="utf-8"),
            )

            validating = self._transition(root, "validating")
            self.assertEqual(0, validating.returncode, validating.stderr)
            resumed = self._transition(root, "active")
            self.assertEqual(0, resumed.returncode, resumed.stderr)
            self.assertEqual(0, self._transition(root, "validating").returncode)

            self._set_category_statuses(archive, "completed")
            self._approve_for_freeze(root)
            frozen = self._transition(
                root,
                "frozen",
                "--validation-conclusion",
                "passed",
                "--unverified-boundaries",
                "documented",
                "--residual-risks",
                "none",
            )

            self.assertEqual(0, frozen.returncode, frozen.stderr)
            response = json.loads(frozen.stdout)
            self.assertEqual("frozen", response["lifecycle"])
            self.assertEqual("rebuilt", response["indexes"])
            manifest = json.loads(
                (archive / "feature.json").read_text(encoding="utf-8")
            )
            self.assertEqual("frozen", manifest["lifecycle"])
            self.assertIsNotNone(manifest["frozen_at"])
            self.assertEqual(
                {
                    "conclusion": "passed",
                    "unverified_boundaries": "documented",
                    "residual_risks": "none",
                },
                manifest["validation"],
            )
            self.assertIn(
                "- 当前生命周期：冻结",
                (archive / "README.md").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "冻结",
                (root / "feature-archives.md").read_text(encoding="utf-8"),
            )
            machine_index = json.loads(
                (root / "feature-archive-dependencies.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                "frozen",
                machine_index["features"]["building-interaction"]["lifecycle"],
            )

    def test_validating_gate_lists_stale_and_blocked_upstream_documents(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "project" / ".scratch" / "dloop-v3" / "v3.9.0" / "outputs"
            self.assertEqual(0, self._run_init(root).returncode)
            self.assertEqual(0, self._transition(root, "active").returncode)
            archive = root / "building-interaction"
            requirements = archive / "01-requirements" / "README.md"
            design = archive / "03-design" / "README.md"
            self._replace_metadata_scalar(
                requirements,
                "content_status",
                "blocked",
            )
            self._replace_metadata_scalar(design, "content_status", "stale")
            self._add_stale_from_status(design)
            snapshot = self._snapshot(root)

            result = self._transition(root, "validating")

            self.assertEqual(1, result.returncode)
            response = json.loads(result.stderr)
            self.assertEqual("LIFECYCLE_GATE_BLOCKED", response["code"])
            blockers = {
                blocker["document_id"]: blocker["reason"]
                for blocker in response["blockers"]
            }
            self.assertEqual(
                {
                    "building-interaction.requirements.overview",
                    "building-interaction.design.overview",
                },
                set(blockers),
            )
            self.assertIn("blocked", blockers[
                "building-interaction.requirements.overview"
            ])
            self.assertIn(
                "stale",
                blockers["building-interaction.design.overview"],
            )
            self.assertEqual(snapshot, self._snapshot(root))

    def test_frozen_gate_checks_all_six_categories_and_reports_documents(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "project" / ".scratch" / "dloop-v3" / "v3.9.0" / "outputs"
            self.assertEqual(0, self._run_init(root).returncode)
            self.assertEqual(0, self._transition(root, "active").returncode)
            self.assertEqual(0, self._transition(root, "validating").returncode)
            archive = root / "building-interaction"
            self._set_category_statuses(archive, "completed")
            self._approve_for_freeze(root)
            investigation = archive / "02-investigation" / "README.md"
            implementation = archive / "05-implementation" / "README.md"
            validation = archive / "06-validation" / "README.md"
            self._replace_metadata_scalar(
                investigation,
                "content_status",
                "draft",
            )
            self._replace_metadata_scalar(
                implementation,
                "content_status",
                "blocked",
            )
            self._replace_metadata_scalar(
                validation,
                "content_status",
                "stale",
            )
            self._add_stale_from_status(validation)

            result = self._transition(
                root,
                "frozen",
                "--validation-conclusion",
                "passed",
                "--unverified-boundaries",
                "none",
                "--residual-risks",
                "documented",
            )

            self.assertEqual(1, result.returncode)
            response = json.loads(result.stderr)
            blocker_ids = {
                blocker["document_id"] for blocker in response["blockers"]
            }
            self.assertEqual(
                {
                    "building-interaction.investigation.overview",
                    "building-interaction.implementation.overview",
                    "building-interaction.validation.overview",
                    "building-interaction.workflow-state",
                },
                blocker_ids,
            )
            self.assertEqual(
                "validating",
                json.loads(
                    (archive / "feature.json").read_text(encoding="utf-8")
                )["lifecycle"],
            )

    def test_frozen_gate_requires_explicit_success_and_validation_boundaries(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "project" / ".scratch" / "dloop-v3" / "v3.9.0" / "outputs"
            self.assertEqual(0, self._run_init(root).returncode)
            self.assertEqual(0, self._transition(root, "active").returncode)
            self.assertEqual(0, self._transition(root, "validating").returncode)
            archive = root / "building-interaction"
            self._set_category_statuses(archive, "completed")
            self._approve_for_freeze(root)

            pending = self._transition(root, "frozen")

            self.assertEqual(1, pending.returncode)
            pending_response = json.loads(pending.stderr)
            self.assertEqual(3, len(pending_response["blockers"]))
            self.assertTrue(
                all(
                    blocker["document_id"]
                    == "building-interaction.validation.overview"
                    for blocker in pending_response["blockers"]
                )
            )

            failed = self._transition(
                root,
                "frozen",
                "--validation-conclusion",
                "failed",
                "--unverified-boundaries",
                "none",
                "--residual-risks",
                "none",
            )
            self.assertEqual(1, failed.returncode)
            failed_response = json.loads(failed.stderr)
            self.assertEqual(1, len(failed_response["blockers"]))
            self.assertIn("failed", failed_response["blockers"][0]["reason"])

    def test_frozen_archive_rejects_changes_reactivation_and_index_mutation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "project" / ".scratch" / "dloop-v3" / "v3.9.0" / "outputs"
            self.assertEqual(0, self._run_init(root).returncode)
            self.assertEqual(0, self._transition(root, "active").returncode)
            self.assertEqual(0, self._transition(root, "validating").returncode)
            archive = root / "building-interaction"
            self._set_category_statuses(archive, "completed")
            self._approve_for_freeze(root)
            self.assertEqual(
                0,
                self._transition(
                    root,
                    "frozen",
                    "--validation-conclusion",
                    "passed",
                    "--unverified-boundaries",
                    "none",
                    "--residual-risks",
                    "none",
                ).returncode,
            )
            archive_snapshot = self._snapshot(archive)

            rebuilt = self._run_command("rebuild-indexes", root)
            self.assertEqual(0, rebuilt.returncode, rebuilt.stderr)
            self.assertEqual(archive_snapshot, self._snapshot(archive))

            requirements = archive / "01-requirements" / "README.md"
            with requirements.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write("\n冻结后的非法人工修改。\n")
            changed_snapshot = self._snapshot(archive)
            change = self._confirm_change(
                root,
                "building-interaction.requirements.overview",
                True,
            )
            self.assertEqual(1, change.returncode)
            self.assertEqual(
                "READ_ONLY_ARCHIVE",
                json.loads(change.stderr)["code"],
            )
            self.assertEqual(changed_snapshot, self._snapshot(archive))

            reactivate = self._transition(root, "active")
            self.assertEqual(1, reactivate.returncode)
            self.assertEqual(
                "READ_ONLY_ARCHIVE",
                json.loads(reactivate.stderr)["code"],
            )
            self.assertEqual(changed_snapshot, self._snapshot(archive))

    def test_init_rejects_an_incomplete_frozen_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "project" / ".scratch" / "dloop-v3" / "v3.9.0" / "outputs"
            self.assertEqual(0, self._run_init(root).returncode)
            self.assertEqual(0, self._transition(root, "active").returncode)
            self.assertEqual(0, self._transition(root, "validating").returncode)
            archive = root / "building-interaction"
            self._set_category_statuses(archive, "completed")
            self._approve_for_freeze(root)
            self.assertEqual(
                0,
                self._transition(
                    root,
                    "frozen",
                    "--validation-conclusion",
                    "passed",
                    "--unverified-boundaries",
                    "none",
                    "--residual-risks",
                    "none",
                ).returncode,
            )
            missing_path = archive / "02-investigation" / "README.md"
            missing_path.unlink()
            snapshot = self._snapshot(root)

            result = self._run_init(root)

            self.assertEqual(1, result.returncode)
            self.assertEqual(
                "INCOMPLETE_ARCHIVE",
                json.loads(result.stderr)["code"],
            )
            self.assertFalse(missing_path.exists())
            self.assertEqual(snapshot, self._snapshot(root))

    def test_illegal_lifecycle_skip_is_rejected_without_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "project" / ".scratch" / "dloop-v3" / "v3.9.0" / "outputs"
            self.assertEqual(0, self._run_init(root).returncode)
            snapshot = self._snapshot(root)

            result = self._transition(root, "validating")

            self.assertEqual(1, result.returncode)
            self.assertEqual(
                "INVALID_LIFECYCLE_TRANSITION",
                json.loads(result.stderr)["code"],
            )
            self.assertEqual(snapshot, self._snapshot(root))

    def test_cleanup_detection_obeys_thirty_day_boundary_with_controlled_time(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "project" / ".scratch" / "dloop-v3" / "v3.9.0" / "outputs"
            self.assertEqual(0, self._run_init(root).returncode)
            self._freeze_at(root, "2026-06-01T00:00:00Z")
            archive = root / "building-interaction"
            frozen_snapshot = self._snapshot(archive)

            before = self._run_command(
                "detect-cleanup",
                root,
                "--now",
                "2026-06-30T23:59:59Z",
            )

            self.assertEqual(0, before.returncode, before.stderr)
            before_response = json.loads(before.stdout)
            self.assertEqual("unchanged", before_response["status"])
            self.assertEqual([], before_response["marked"])
            self.assertFalse(before_response["evaluations"][0]["eligible"])
            self.assertEqual(frozen_snapshot, self._snapshot(archive))

            boundary = self._run_command(
                "detect-cleanup",
                root,
                "--now",
                "2026-07-01T00:00:00Z",
            )

            self.assertEqual(0, boundary.returncode, boundary.stderr)
            boundary_response = json.loads(boundary.stdout)
            self.assertEqual("marked", boundary_response["status"])
            self.assertEqual(["building-interaction"], boundary_response["marked"])
            self.assertEqual(
                "2026-07-01T00:00:00Z",
                boundary_response["evaluations"][0]["eligible_at"],
            )
            manifest = json.loads(
                (archive / "feature.json").read_text(encoding="utf-8")
            )
            self.assertEqual("pending_cleanup", manifest["lifecycle"])
            self.assertEqual("2026-06-01T00:00:00Z", manifest["frozen_at"])
            self.assertEqual(30, manifest["retention_days"])
            self.assertTrue(archive.is_dir())
            self.assertIn(
                "- 当前生命周期：待清理",
                (archive / "README.md").read_text(encoding="utf-8"),
            )

    def test_indexes_and_purge_preview_never_delete_eligible_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "project" / ".scratch" / "dloop-v3" / "v3.9.0" / "outputs"
            self.assertEqual(0, self._run_init(root).returncode)
            self._freeze_at(root, "2026-06-01T00:00:00Z")
            self.assertEqual(
                0,
                self._run_command(
                    "detect-cleanup",
                    root,
                    "--now",
                    "2026-07-01T00:00:00Z",
                ).returncode,
            )
            archive = root / "building-interaction"
            archive_snapshot = self._snapshot(archive)

            rebuilt = self._run_command("rebuild-indexes", root)
            preview = self._run_command(
                "purge",
                root,
                "--feature-id",
                "building-interaction",
                "--now",
                "2026-07-01T00:00:00Z",
            )

            self.assertEqual(0, rebuilt.returncode, rebuilt.stderr)
            self.assertEqual(0, preview.returncode, preview.stderr)
            preview_response = json.loads(preview.stdout)
            self.assertTrue(preview_response["can_execute"])
            self.assertEqual("delete", preview_response["targets"][0]["action"])
            self.assertEqual(archive_snapshot, self._snapshot(archive))
            self.assertIn(
                "待清理档案：`building-interaction`",
                (root / "feature-archives.md").read_text(encoding="utf-8"),
            )

    def test_purge_refuses_retain_reason_blockers_and_ineligible_lifecycle(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "project" / ".scratch" / "dloop-v3" / "v3.9.0" / "outputs"
            self.assertEqual(0, self._run_init(root).returncode)
            self.assertEqual(
                0,
                self._run_init(root, "combat-report", "战报").returncode,
            )
            self._freeze_at(root, "2026-06-01T00:00:00Z")
            self._freeze_at(root, "2026-06-01T00:00:00Z", "combat-report")
            self.assertEqual(
                0,
                self._run_command(
                    "detect-cleanup",
                    root,
                    "--feature-id",
                    "building-interaction",
                    "--now",
                    "2026-07-01T00:00:00Z",
                ).returncode,
            )
            archive = root / "building-interaction"
            manifest_path = archive / "feature.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["retain_reason"] = "等待回归争议关闭"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            self._replace_metadata_scalar(
                archive / "05-implementation" / "README.md",
                "content_status",
                "blocked",
            )
            root_snapshot = self._snapshot(root)

            preview = self._run_command(
                "purge",
                root,
                "--feature-id",
                "building-interaction",
                "--feature-id",
                "combat-report",
                "--now",
                "2026-07-01T00:00:00Z",
            )

            self.assertEqual(0, preview.returncode, preview.stderr)
            response = json.loads(preview.stdout)
            self.assertFalse(response["can_execute"])
            blockers = {
                blocker["code"]
                for target in response["targets"]
                for blocker in target["blockers"]
            }
            self.assertIn("RETAIN_REASON", blockers)
            self.assertIn("DOCUMENT_BLOCKER", blockers)
            self.assertIn("INELIGIBLE_LIFECYCLE", blockers)

            execute = self._run_command(
                "purge",
                root,
                "--feature-id",
                "building-interaction",
                "--feature-id",
                "combat-report",
                "--now",
                "2026-07-01T00:00:00Z",
                "--execute",
            )
            self.assertEqual(1, execute.returncode)
            self.assertEqual("PURGE_BLOCKED", json.loads(execute.stderr)["code"])
            self.assertEqual(root_snapshot, self._snapshot(root))

    def test_purge_preview_reports_invalid_reference_and_execute_refuses(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "project" / ".scratch" / "dloop-v3" / "v3.9.0" / "outputs"
            self.assertEqual(0, self._run_init(root).returncode)
            self.assertEqual(
                0,
                self._run_init(root, "combat-report", "战报").returncode,
            )
            self._freeze_at(root, "2026-06-01T00:00:00Z")
            self.assertEqual(
                0,
                self._run_command(
                    "detect-cleanup",
                    root,
                    "--now",
                    "2026-07-01T00:00:00Z",
                    "--feature-id",
                    "building-interaction",
                ).returncode,
            )
            design = (
                root
                / "combat-report"
                / "03-design"
                / "README.md"
            )
            self._replace_metadata_list(
                design,
                "dependencies",
                ["building-interaction.requirements.overview"],
            )
            self._replace_metadata_scalar(
                design,
                "dependency_versions",
                '{"building-interaction.requirements.overview": "0.1.0"}',
            )
            snapshot = self._snapshot(root)

            preview = self._run_command(
                "purge",
                root,
                "--feature-id",
                "building-interaction",
                "--now",
                "2026-07-01T00:00:00Z",
            )

            self.assertEqual(0, preview.returncode, preview.stderr)
            response = json.loads(preview.stdout)
            self.assertFalse(response["can_execute"])
            self.assertEqual(
                "INVALID_ARCHIVE_REFERENCE",
                response["targets"][0]["blockers"][0]["code"],
            )

            execute = self._run_command(
                "purge",
                root,
                "--feature-id",
                "building-interaction",
                "--now",
                "2026-07-01T00:00:00Z",
                "--execute",
            )
            self.assertEqual(1, execute.returncode)
            self.assertEqual("PURGE_BLOCKED", json.loads(execute.stderr)["code"])
            self.assertEqual(snapshot, self._snapshot(root))

    def test_successful_purge_deletes_only_selected_archive_and_rebuilds_indexes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "project" / ".scratch" / "dloop-v3" / "v3.9.0" / "outputs"
            self.assertEqual(0, self._run_init(root).returncode)
            self.assertEqual(
                0,
                self._run_init(root, "combat-report", "战报").returncode,
            )
            self._freeze_at(root, "2026-06-01T00:00:00Z")
            self._freeze_at(root, "2026-06-01T00:00:00Z", "combat-report")
            detected = self._run_command(
                "detect-cleanup",
                root,
                "--now",
                "2026-07-01T00:00:00Z",
            )
            self.assertEqual(0, detected.returncode, detected.stderr)

            result = self._run_command(
                "purge",
                root,
                "--feature-id",
                "building-interaction",
                "--now",
                "2026-07-01T00:00:00Z",
                "--execute",
            )

            self.assertEqual(0, result.returncode, result.stderr)
            response = json.loads(result.stdout)
            self.assertEqual("purged", response["status"])
            self.assertEqual(["building-interaction"], response["purged"])
            self.assertFalse((root / "building-interaction").exists())
            self.assertTrue((root / "combat-report").is_dir())
            machine_index = json.loads(
                (root / "feature-archive-dependencies.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertNotIn("building-interaction", machine_index["features"])
            self.assertIn("combat-report", machine_index["features"])
            global_index = (root / "feature-archives.md").read_text(
                encoding="utf-8"
            )
            self.assertNotIn("building-interaction", global_index)
            self.assertIn("combat-report", global_index)

    def test_complete_archive_workflow_from_init_to_safe_purge(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "project" / ".scratch" / "dloop-v3" / "v3.9.0" / "outputs"
            self.assertEqual(0, self._run_init(root).returncode)
            self.assertEqual(
                0,
                self._transition(
                    root,
                    "active",
                    "--now",
                    "2026-06-01T00:00:00Z",
                ).returncode,
            )
            archive = root / "building-interaction"
            requirements = archive / "01-requirements" / "README.md"
            design = archive / "03-design" / "README.md"
            self._replace_metadata_list(
                design,
                "dependencies",
                ["building-interaction.requirements.overview"],
            )
            self.assertEqual(
                0,
                self._confirm_change(
                    root,
                    "building-interaction.design.overview",
                    False,
                ).returncode,
            )

            with requirements.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write("\n## 已确认补充\n\n新增验收边界。\n")
            semantic_change = self._confirm_change(
                root,
                "building-interaction.requirements.overview",
                True,
            )
            self.assertEqual(0, semantic_change.returncode, semantic_change.stderr)
            queue = self._run_command(
                "refresh-queue",
                root,
                "--feature-id",
                "building-interaction",
            )
            self.assertEqual(
                ["building-interaction.design.overview"],
                json.loads(queue.stdout)["refresh_queue"],
            )
            refreshed = self._confirm_change(
                root,
                "building-interaction.design.overview",
                False,
            )
            self.assertEqual(0, refreshed.returncode, refreshed.stderr)
            self.assertEqual(
                0,
                self._run_command(
                    "assert-fresh",
                    root,
                    "--document-id",
                    "building-interaction.design.overview",
                ).returncode,
            )

            self._set_category_statuses(archive, "completed")
            self._approve_for_freeze(root)
            self.assertEqual(
                0,
                self._transition(
                    root,
                    "validating",
                    "--now",
                    "2026-06-01T00:00:00Z",
                ).returncode,
            )
            frozen = self._transition(
                root,
                "frozen",
                "--validation-conclusion",
                "passed",
                "--unverified-boundaries",
                "documented",
                "--residual-risks",
                "none",
                "--now",
                "2026-06-01T00:00:00Z",
            )
            self.assertEqual(0, frozen.returncode, frozen.stderr)

            detected = self._run_command(
                "detect-cleanup",
                root,
                "--now",
                "2026-07-01T00:00:00Z",
            )
            self.assertEqual(0, detected.returncode, detected.stderr)
            preview = self._run_command(
                "purge",
                root,
                "--feature-id",
                "building-interaction",
                "--now",
                "2026-07-01T00:00:00Z",
            )
            self.assertTrue(json.loads(preview.stdout)["can_execute"])
            history = root.parent / "snapshots/building-interaction.git"
            self.assertTrue(history.is_dir())
            self.assertTrue(json.loads(preview.stdout)["targets"][0]["snapshot_history_will_be_deleted"])
            purged = self._run_command(
                "purge",
                root,
                "--feature-id",
                "building-interaction",
                "--now",
                "2026-07-01T00:00:00Z",
                "--execute",
            )
            self.assertEqual(0, purged.returncode, purged.stderr)
            self.assertFalse(archive.exists())
            self.assertFalse(history.exists())
            machine_index = json.loads(
                (root / "feature-archive-dependencies.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual({}, machine_index["features"])


if __name__ == "__main__":
    unittest.main()
