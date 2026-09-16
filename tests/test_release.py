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

        self.assertEqual("3.8.1", version)
        self.assertEqual("dloop", manifest["name"])
        self.assertEqual(version, manifest["version"])
        self.assertEqual("./skills/", manifest["skills"])
        self.assertTrue(plugin_root.joinpath("skills", "dloop", "SKILL.md").is_file())
        self.assertTrue(plugin_root.joinpath("skills", "dloop-ui", "SKILL.md").is_file())

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
            "archive-on-upgrade",
            release["versionSupport"]["mode"],
        )
        self.assertEqual(
            {
                "absent": "install-current",
                "sameVersion": (
                    "verify-ownership-and-reinstall-idempotently"
                ),
                "olderVersion": "archive-retire-and-install",
                "newerVersion": "reject-before-read-or-write",
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

        self.assertEqual("3.8.1", version)
        self.assertIn("v3.8.1", readme)
        self.assertIn("-Version v3.8.1", installation)
        self.assertIn("-Uninstall", installation)
        self.assertIn("install.py", readme)
        self.assertIn("$dloop", readme)
        self.assertIn("$dloop-ui", readme)
        self.assertIn("已有 Prefab", readme)
        self.assertIn("真实截图", readme)
        self.assertIn("安装器只管理当前版本", changelog)

    def test_current_source_is_presented_as_frozen(self) -> None:
        release = json.loads(
            (REPOSITORY_ROOT / "release.json").read_text(encoding="utf-8")
        )
        release_notes = (REPOSITORY_ROOT / "docs" / "v3.8.1-friction-fixes.md").read_text(encoding="utf-8")

        self.assertEqual(
            {
                "workflowVersion": "3.8.1",
                "archiveSchemaVersion": 3,
                "terminologySchemaVersion": 2,
                "sourceTag": "v3.8.1",
                "releaseStatus": "frozen",
                "versionSupport": release["versionSupport"],
            },
            release,
        )
        self.assertIn("frozen", release_notes)
        self.assertIn("基于冻结版 v3.8.0", release_notes)
        self.assertIn("## 验证范围", release_notes)

    def test_skill_describes_the_current_runtime_contract(self) -> None:
        version = (REPOSITORY_ROOT / "VERSION").read_text(encoding="utf-8").strip()
        skills = REPOSITORY_ROOT / "plugin" / "dloop" / "skills"
        for name in ("dloop", "dloop-ui"):
            with self.subTest(skill=name):
                entry = (skills / name / "SKILL.md").read_text(encoding="utf-8")
                self.assertIn(f"v{version}", entry.split("---", 2)[1])
        self.assertLessEqual(len((skills / "dloop" / "SKILL.md").read_bytes()), 8192)

if __name__ == "__main__":
    unittest.main()
