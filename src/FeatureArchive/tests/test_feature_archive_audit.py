"""交付档案只读审计命令测试。"""

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


class FeatureArchiveAuditTests(unittest.TestCase):
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

    def _init(self, root: Path, feature_id: str) -> None:
        result = self._run(
            "init",
            root,
            "--feature-id",
            feature_id,
            "--title",
            feature_id,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def _audit(
        self,
        root: Path,
        feature_id: str,
    ) -> subprocess.CompletedProcess[str]:
        return self._run(
            "audit",
            root,
            "--feature-id",
            feature_id,
        )

    @staticmethod
    def _append_body(path: Path, text: str = "\n正文改动。\n") -> None:
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(text)

    @staticmethod
    def _replace_metadata(path: Path, field: str, value: str) -> None:
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
    def _snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
        return {
            path.relative_to(root).as_posix(): (
                path.read_bytes(),
                path.stat().st_mtime_ns,
            )
            for path in root.rglob("*")
            if path.is_file()
        }

    def test_audit_allows_valid_current_feature_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project" / ".scratch" / "dloop-v3" / "v3.8.1" / "outputs"
            self._init(root, "current-feature")
            before = self._snapshot(root)

            first = self._audit(root, "current-feature")
            second = self._audit(root, "current-feature")

            self.assertEqual(0, first.returncode, first.stderr)
            self.assertEqual(first.stdout, second.stdout)
            self.assertEqual("", first.stderr)
            response = json.loads(first.stdout)
            self.assertEqual("allowed", response["status"])
            self.assertEqual("current-feature", response["feature_id"])
            self.assertEqual([], response["blockers"])
            self.assertEqual("canonical", response["archive_location"]["kind"])
            self.assertTrue(
                Path(response["archive_location"]["archive_root"]).samefile(root)
            )
            self.assertEqual(before, self._snapshot(root))

    def test_audit_blocks_all_unconfirmed_edits_with_minimal_facts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project" / ".scratch" / "dloop-v3" / "v3.8.1" / "outputs"
            self._init(root, "current-feature")
            plan = root / "current-feature" / "04-plan" / "README.md"
            design = root / "current-feature" / "03-design" / "README.md"
            self._append_body(plan)
            self._append_body(design)

            result = self._audit(root, "current-feature")

            self.assertEqual(1, result.returncode)
            self.assertEqual("", result.stderr)
            response = json.loads(result.stdout)
            self.assertEqual(
                {"status", "feature_id", "blockers", "archive_location"},
                set(response),
            )
            self.assertEqual("blocked", response["status"])
            self.assertEqual("current-feature", response["feature_id"])
            self.assertEqual(
                [
                    "current-feature.design.overview",
                    "current-feature.plan.overview",
                ],
                [blocker["document_id"] for blocker in response["blockers"]],
            )
            for blocker in response["blockers"]:
                self.assertEqual(
                    {"code", "document_id", "location", "message"},
                    set(blocker),
                )
                self.assertEqual("UNCONFIRMED_EDIT", blocker["code"])

    def test_audit_allows_after_confirm_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project" / ".scratch" / "dloop-v3" / "v3.8.1" / "outputs"
            self._init(root, "current-feature")
            plan = root / "current-feature" / "04-plan" / "README.md"
            self._append_body(plan)
            confirmed = self._run(
                "confirm-change",
                root,
                "--document-id",
                "current-feature.plan.overview",
                "--semantic-change",
                "false",
            )
            self.assertEqual(0, confirmed.returncode, confirmed.stderr)

            result = self._audit(root, "current-feature")

            self.assertEqual(0, result.returncode, result.stdout)
            self.assertEqual("allowed", json.loads(result.stdout)["status"])

    def test_audit_blocks_missing_current_dependency_at_declaring_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project" / ".scratch" / "dloop-v3" / "v3.8.1" / "outputs"
            self._init(root, "current-feature")
            plan = root / "current-feature" / "04-plan" / "README.md"
            self._replace_metadata(
                plan,
                "dependencies",
                "[current-feature.design.missing]",
            )

            result = self._audit(root, "current-feature")

            self.assertEqual(1, result.returncode)
            response = json.loads(result.stdout)
            blocker = response["blockers"][0]
            self.assertEqual("MISSING_DEPENDENCY", blocker["code"])
            self.assertEqual(
                "current-feature/04-plan/README.md",
                blocker["location"],
            )

    def test_audit_ignores_unrelated_invalid_and_unconfirmed_feature(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project" / ".scratch" / "dloop-v3" / "v3.8.1" / "outputs"
            self._init(root, "current-feature")
            self._init(root, "unrelated-feature")
            unrelated = root / "unrelated-feature"
            self._append_body(unrelated / "04-plan" / "README.md")
            self._replace_metadata(
                unrelated / "03-design" / "README.md",
                "content_status",
                "stale",
            )
            (unrelated / "06-validation" / "README.md").unlink()

            result = self._audit(root, "current-feature")

            self.assertEqual(0, result.returncode, result.stdout)
            self.assertEqual("allowed", json.loads(result.stdout)["status"])

    def test_audit_blocks_unreadable_utf8_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project" / ".scratch" / "dloop-v3" / "v3.8.1" / "outputs"
            self._init(root, "current-feature")
            plan = root / "current-feature" / "04-plan" / "README.md"
            plan.write_bytes(b"\xff\xfe")

            result = self._audit(root, "current-feature")

            self.assertEqual(1, result.returncode)
            response = json.loads(result.stdout)
            blocker = response["blockers"][0]
            self.assertEqual("UNREADABLE_DOCUMENT", blocker["code"])
            self.assertEqual(
                "current-feature/04-plan/README.md",
                blocker["location"],
            )

    def test_audit_blocks_invalid_current_structure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project" / ".scratch" / "dloop-v3" / "v3.8.1" / "outputs"
            self._init(root, "current-feature")
            missing = root / "current-feature" / "06-validation" / "README.md"
            missing.unlink()

            result = self._audit(root, "current-feature")

            self.assertEqual(1, result.returncode)
            response = json.loads(result.stdout)
            blocker = response["blockers"][0]
            self.assertEqual("INVALID_ARCHIVE_STRUCTURE", blocker["code"])
            self.assertEqual(
                "current-feature/06-validation/README.md",
                blocker["location"],
            )


if __name__ == "__main__":
    unittest.main()
