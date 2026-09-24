"""真实临时 Git 项目的安装生命周期，不依赖 SVN。"""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import test_installation
from test_installation import POWERSHELL, VERSION, CORE_CLI, LOCK


@unittest.skipUnless(POWERSHELL and shutil.which("git"), "需要 PowerShell 与 Git")
class GitInstallationTests(unittest.TestCase):
    _run = test_installation.InstallationTests._run
    _managed_snapshot = staticmethod(test_installation.InstallationTests._managed_snapshot)

    def git(self, target, *arguments):
        result = subprocess.run(
            ["git", "-C", str(target), "-c", "user.name=DLoop test", "-c", "user.email=dloop@example.invalid", *arguments],
            capture_output=True, encoding="utf-8",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout

    def project(self, base, *, ignored=True):
        target = base / "中文 Git project - copy (draft) [1] & user's"
        target.mkdir()
        self.git(target, "init", "-q")
        if ignored:
            (target / ".gitignore").write_bytes(b".scratch/\n")
        return target

    def test_install_verify_reinstall_uninstall_in_ordinary_nested_and_linked_worktrees(self):
        for kind in ("ordinary", "nested", "worktree"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                repository = self.project(base)
                target = repository
                if kind == "nested":
                    target = repository / "nested project"
                    target.mkdir()
                elif kind == "worktree":
                    self.git(repository, "add", ".gitignore")
                    self.git(repository, "commit", "-qm", "fixture")
                    target = base / "linked worktree"
                    self.git(repository, "worktree", "add", "-q", "-b", "linked", str(target))
                marker = target / "existing.txt"
                marker.write_bytes(b"staged content")
                self.git(target, "add", "existing.txt")
                marker.write_bytes(b"working content")
                index_path = Path(self.git(target, "rev-parse", "--path-format=absolute", "--git-path", "index").strip())
                index_before = index_path.read_bytes()
                ignore_before = (repository / ".gitignore").read_bytes()
                archive = target / ".scratch/dloop-v3/v4.0.1/outputs/retained.txt"
                archive.parent.mkdir(parents=True)
                archive.write_bytes(b"keep archive")
                history = target / ".scratch/dloop-v3/v4.0.1/snapshots/retained.git/objects/retained"
                history.parent.mkdir(parents=True)
                history.write_bytes(b"persistent snapshot")
                result = self._run(target, "-Version", f"v{VERSION}")
                self.assertEqual(0, result.returncode, result.stderr)
                retired = list((target / ".scratch/dloop-history").glob("*/runtime"))
                self.assertEqual(1, len(retired))
                archive = retired[0] / f"v{VERSION}/outputs/retained.txt"
                history = retired[0] / f"v{VERSION}/snapshots/retained.git/objects/retained"
                for args in ((), ("-Verify",), (), ("-Uninstall",)):
                    result = self._run(target, "-Version", f"v{VERSION}", *args)
                    self.assertEqual(0, result.returncode, result.stderr)
                    self.assertEqual(index_before, index_path.read_bytes())
                    self.assertEqual(ignore_before, (repository / ".gitignore").read_bytes())
                    self.assertEqual(b"working content", marker.read_bytes())
                    self.assertEqual(b"keep archive", archive.read_bytes())
                    self.assertEqual(b"persistent snapshot", history.read_bytes())
                self.assertFalse((target / CORE_CLI).exists())
                self.assertFalse((target / LOCK).exists())

    def test_missing_ignore_refuses_before_writing_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = self.project(Path(temporary), ignored=False)
            result = self._run(target, "-Version", f"v{VERSION}")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("预先", result.stderr)
            self.assertEqual({}, self._managed_snapshot(target))

    def test_failed_git_install_restores_payload_and_keeps_ignore_and_index(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = self.project(Path(temporary))
            self.git(target, "add", ".gitignore")
            index_before = (target / ".git/index").read_bytes()
            result = self._run(target, "-Version", f"v{VERSION}", fail_step="after-lock")
            self.assertNotEqual(0, result.returncode)
            self.assertEqual({}, self._managed_snapshot(target))
            self.assertEqual(b".scratch/\n", (target / ".gitignore").read_bytes())
            self.assertEqual(index_before, (target / ".git/index").read_bytes())


if __name__ == "__main__":
    unittest.main()
