"""用安装器生成的相邻版本安装态验证整体交替；不伪造作业或安装锁。"""

import json
import os
from pathlib import Path
import py_compile
import shutil
import subprocess
import sys
import tempfile
import unittest

from test_installation import POWERSHELL, REPOSITORY_ROOT, VERSION, CORE_CLI, LOCK
import test_installation
import test_git_installation

sys.path.insert(0, str(REPOSITORY_ROOT / "src/FeatureArchive/tests"))
import _feature_archive_support


@unittest.skipUnless(POWERSHELL and shutil.which("git"), "需要 PowerShell 与 Git")
class VersionHandoffTests(unittest.TestCase):
    git = test_git_installation.GitInstallationTests.git
    project = test_git_installation.GitInstallationTests.project
    _run = test_git_installation.GitInstallationTests._run
    _managed_snapshot = staticmethod(test_git_installation.GitInstallationTests._managed_snapshot)

    @classmethod
    def setUpClass(cls):
        cls.source_temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.source_temp.cleanup)
        cls.previous_source = Path(cls.source_temp.name)
        for name in ("src", "plugin"):
            shutil.copytree(REPOSITORY_ROOT / name, cls.previous_source / name,
                            ignore=shutil.ignore_patterns("__pycache__", "tests"))
        for name in ("VERSION", "release.json", "install.ps1"):
            shutil.copy2(REPOSITORY_ROOT / name, cls.previous_source / name)
        for path in cls.previous_source.rglob("*"):
            if path.is_file():
                data = path.read_bytes()
                updated = data.replace(VERSION.encode(), b"3.7.5")
                if updated != data:
                    path.write_bytes(updated)

    def cli(self, target, *args, expected=0, **options):
        result = subprocess.run(
            [sys.executable, str(target / CORE_CLI), *(str(arg) for arg in args)],
            capture_output=True, encoding="utf-8", env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        self.assertEqual(expected, result.returncode, result.stderr or result.stdout)
        return json.loads(result.stdout or result.stderr)

    def previous_project(self, base, *, unfinished=True, vcs="git", without_new_paths=False):
        source = self.previous_source
        if without_new_paths:
            source = base / "previous-source"
            shutil.copytree(self.previous_source, source)
            for relative in (
                "src/FeatureArchive/README.md",
                "plugin/dloop/skills/dloop/references/snapshots.md",
                "plugin/dloop/skills/dloop-ui/references/model-contract.md",
            ):
                (source / relative).unlink()
            (source / "plugin/dloop/skills/dloop-ui/references").rmdir()
        if vcs == "svn":
            target, _ = test_installation.InstallationTests()._svn_project(base)
        else:
            target = self.project(base)
        (target / "main.txt").write_bytes(b"original\n")
        if vcs == "svn":
            subprocess.run([test_installation.SVN, "add", str(target / "main.txt")], check=True, capture_output=True)
            subprocess.run([test_installation.SVN, "commit", str(target), "-m", "fixture"], check=True, capture_output=True)
        else:
            self.git(target, "add", ".gitignore", "main.txt")
            self.git(target, "commit", "-qm", "fixture")
        result = subprocess.run(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
             "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); & '" +
             str(source / "install.ps1").replace("'", "''") + "' -Target '" +
             str(target).replace("'", "''") + "'"],
            capture_output=True, encoding="utf-8",
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        result = self.cli(target, "init", "--feature-id", "delivery", "--title", "未完成交付")
        feature = Path(result["archive_location"]["feature_root"])
        if unfinished:
            helper = _feature_archive_support.FeatureArchiveCliTestCase()
            helper.root = feature.parent
            helper.workspace = target
            helper._project_root = target
            helper.run_cli = lambda *args, **kwargs: self.cli(target, *args, **kwargs)
            helper.approve_requirements("delivery")
            helper.prepare_execution_inputs("delivery")
            package = helper.write_package("slice", feature_id="delivery", scope=["main.txt"])
            self.cli(target, "start-slice", "--feature-id", "delivery", "--execution-id", "worker",
                     "--package-file", package, "--workspace-root", target)
            (target / "main.txt").write_bytes(b"unfinished implementation\n")
            lease = json.loads(
                (feature.parent / ".feature-archive-workspace-state.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertIsNotNone(lease["modification_lease"])
        return target, feature

    @staticmethod
    def files(root):
        return {path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*") if path.is_file()}

    def test_modified_old_installation_is_backed_up_before_upgrade(self):
        for vcs in ("git", "svn"):
            with self.subTest(vcs=vcs), tempfile.TemporaryDirectory() as temporary:
                target, _ = self.previous_project(Path(temporary), vcs=vcs)
                paths = (".agents/skills/dloop/references/execution.md", "Tools/FeatureArchive/archive_candidates.py")
                for relative in paths:
                    with (target / relative).open("ab") as output:
                        output.write(b"\n# local maintenance patch\n")
                original = self._managed_snapshot(target)
                runtime = self.files(target / ".scratch/dloop-v3")
                result = self._run(target)
                self.assertEqual(0, result.returncode, result.stderr)
                backups = list((target / ".scratch/dloop-install-backups").iterdir())
                self.assertEqual(1, len(backups))
                backup = backups[0]
                self.assertIn(str(backup.resolve()), result.stdout)
                for relative in paths:
                    self.assertIn(relative, result.stdout)
                for relative, content in original.items():
                    if content is not None and "__pycache__" not in relative and not relative.startswith(".scratch/"):
                        self.assertEqual(content, (backup / "payload" / relative).read_bytes())
                history = next((target / ".scratch/dloop-history").iterdir())
                self.assertEqual(runtime, self.files(history / "runtime"))
                self.assertEqual(b"unfinished implementation\n", (target / "main.txt").read_bytes())
                self.assertEqual(0, self._run(target, "-Verify").returncode)

    def test_modified_upgrade_failure_restores_patch_lock_and_old_jobs(self):
        for step in ("during-upgrade-backup", "after-upgrade-backup", "after-retirement", "after-lock"):
            with self.subTest(step=step), tempfile.TemporaryDirectory() as temporary:
                target, _ = self.previous_project(Path(temporary))
                patch = target / ".agents/skills/dloop/references/execution.md"
                patch.write_bytes(patch.read_bytes() + b"\nlocal maintenance patch\n")
                original = self._managed_snapshot(target)
                runtime = self.files(target / ".scratch/dloop-v3")
                result = self._run(target, fail_step=step)
                self.assertNotEqual(0, result.returncode)
                self.assertIn(step, result.stderr)
                self.assertEqual(original, self._managed_snapshot(target))
                self.assertEqual(runtime, self.files(target / ".scratch/dloop-v3"))
                self.assertEqual(b"unfinished implementation\n", (target / "main.txt").read_bytes())
                backup = next((target / ".scratch/dloop-install-backups").iterdir())
                if step != "during-upgrade-backup":
                    self.assertEqual(patch.read_bytes(), (backup / "payload/.agents/skills/dloop/references/execution.md").read_bytes())
                self.assertNotIn("CategoryInfo", result.stderr)
                self.assertNotIn("FullyQualifiedErrorId", result.stderr)

    @staticmethod
    def add_user_content(target):
        additions = {
            "Tools/FeatureArchive/README.md": b"user file at new file path\n",
            ".agents/skills/dloop/references/snapshots.md/nested/user.bin": bytes(range(256)),
            ".agents/skills/dloop-ui/references": b"user file at new directory path\n",
            "Tools/FeatureArchive/custom/notes.txt": b"keep tool notes\n",
            "Tools/FeatureArchive/__pycache__/nested/notes.txt": b"keep cache-directory notes\n",
            ".agents/skills/dloop/local [draft].md": "用户补充说明\n".encode(),
            "Packages/com.dloop.ui-capture/custom/settings.json": b'{"user": true}\n',
        }
        directories = (
            ".agents/skills/dloop/references/snapshots.md/empty",
            "Tools/FeatureArchive/custom/empty",
        )
        for relative, content in additions.items():
            path = target / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        for relative in directories:
            (target / relative).mkdir(parents=True)
        return additions, directories

    def test_upgrade_preserves_additions_and_backs_up_path_conflicts(self):
        for vcs in ("git", "svn"):
            with self.subTest(vcs=vcs), tempfile.TemporaryDirectory() as temporary:
                target, _ = self.previous_project(Path(temporary), vcs=vcs, without_new_paths=True)
                additions, directories = self.add_user_content(target)
                cache = Path(py_compile.compile(str(target / CORE_CLI), doraise=True))
                original_lock = (target / LOCK).read_bytes()
                runtime = self.files(target / ".scratch/dloop-v3")
                result = self._run(target)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertFalse(cache.exists())
                backup, = (target / ".scratch/dloop-install-backups").iterdir()
                manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8-sig"))
                self.assertEqual([], manifest["modifiedFiles"])
                self.assertEqual(set(additions), set(manifest["additionalFiles"]))
                self.assertEqual({
                    "Tools/FeatureArchive/README.md",
                    ".agents/skills/dloop/references/snapshots.md",
                    ".agents/skills/dloop-ui/references",
                }, set(manifest["relocatedPaths"]))
                for relative, content in additions.items():
                    self.assertEqual(content, (backup / "payload" / relative).read_bytes())
                    if not any(relative == path or relative.startswith(path + "/")
                               for path in manifest["relocatedPaths"]):
                        self.assertEqual(content, (target / relative).read_bytes())
                for relative in directories:
                    self.assertTrue((backup / "payload" / relative).is_dir())
                self.assertTrue((target / directories[1]).is_dir())
                self.assertEqual(original_lock, (backup / "payload" / LOCK).read_bytes())
                new_lock = json.loads((target / LOCK).read_text(encoding="utf-8-sig"))
                owned = {item["path"] for item in new_lock["files"]}
                self.assertNotIn("Tools/FeatureArchive/custom/notes.txt", owned)
                history, = (target / ".scratch/dloop-history").iterdir()
                self.assertEqual(runtime, self.files(history / "runtime"))
                self.assertEqual(b"unfinished implementation\n", (target / "main.txt").read_bytes())
                self.assertEqual(0, self._run(target, "-Verify").returncode)
                self.assertEqual(0, self._run(target).returncode)

    def test_addition_conflict_failure_restores_files_directories_and_jobs(self):
        for step in ("during-upgrade-backup", "after-upgrade-backup", "during-upgrade-relocation",
                     "after-retirement", "after-uninstall-payload", "after-payload", "after-lock"):
            with self.subTest(step=step), tempfile.TemporaryDirectory() as temporary:
                target, _ = self.previous_project(Path(temporary), without_new_paths=True)
                additions, directories = self.add_user_content(target)
                py_compile.compile(str(target / CORE_CLI), doraise=True)
                patch = target / ".agents/skills/dloop/references/execution.md"
                patch.write_bytes(patch.read_bytes() + b"\nlocal patch\n")
                original = self._managed_snapshot(target)
                result = self._run(target, fail_step=step)
                self.assertNotEqual(0, result.returncode)
                self.assertIn(step, result.stderr)
                self.assertEqual(original, self._managed_snapshot(target))
                self.assertEqual(b"unfinished implementation\n", (target / "main.txt").read_bytes())
                if step != "during-upgrade-backup":
                    backup, = (target / ".scratch/dloop-install-backups").iterdir()
                    for relative, content in additions.items():
                        self.assertEqual(content, (backup / "payload" / relative).read_bytes())
                    for relative in directories:
                        self.assertTrue((backup / "payload" / relative).is_dir())
                retry = self._run(target)
                self.assertEqual(0, retry.returncode, retry.stderr)

    def test_upgrade_with_only_nonconflicting_additions(self):
        for empty_only in (True, False):
            with self.subTest(empty_only=empty_only), tempfile.TemporaryDirectory() as temporary:
                target, _ = self.previous_project(Path(temporary), unfinished=False)
                directory = target / "Tools/FeatureArchive/user/empty"
                directory.mkdir(parents=True)
                if not empty_only:
                    (directory.parent / "notes.txt").write_bytes(b"user notes")
                original = self.files(directory.parent)
                result = self._run(target)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertTrue(directory.is_dir())
                self.assertEqual(original, self.files(directory.parent))
                backup, = (target / ".scratch/dloop-install-backups").iterdir()
                self.assertTrue((backup / "payload/Tools/FeatureArchive/user/empty").is_dir())
                self.assertEqual(original, self.files(backup / "payload/Tools/FeatureArchive/user"))
                manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8-sig"))
                self.assertEqual([], manifest["relocatedPaths"])
                self.assertEqual(0, self._run(target, "-Verify").returncode)

    def test_unfinished_job_retires_unlocks_and_new_job_is_independent(self):
        with tempfile.TemporaryDirectory() as temporary:
            target, old_feature = self.previous_project(Path(temporary))
            runtime = target / ".scratch/dloop-v3"
            original = self.files(runtime)
            index = (target / ".git/index").read_bytes()
            result = self._run(target)
            self.assertEqual(0, result.returncode, result.stderr)
            batches = list((target / ".scratch/dloop-history").iterdir())
            self.assertEqual(1, len(batches))
            batch = batches[0]
            self.assertEqual(original, self.files(batch / "runtime"))
            receipt = json.loads((batch / "retirement.json").read_text(encoding="utf-8-sig"))
            self.assertEqual("retired", receipt["status"])
            self.assertEqual("3.7.5", receipt["sourceVersion"])
            self.assertEqual(VERSION, receipt["replacementVersion"])
            self.assertFalse(old_feature.exists())
            status = self.cli(target, "workflow-status")
            self.assertIsNone(status["modification_lease"])
            self.cli(target, "workflow-status", "--feature-id", "delivery", expected=1)
            created = self.cli(target, "init", "--feature-id", "delivery", "--title", "新交付")
            new_feature = Path(created["archive_location"]["feature_root"])
            self.assertNotEqual(old_feature, new_feature)
            self.assertIn(f"/v{VERSION}/outputs/", new_feature.as_posix())
            after_init = self.files(runtime)
            self.assertEqual(0, self._run(target).returncode)
            self.assertEqual(after_init, self.files(runtime))
            self.assertEqual(original, self.files(batch / "runtime"))
            self.assertEqual(1, len(list(batch.parent.iterdir())))
            helper = _feature_archive_support.FeatureArchiveCliTestCase()
            helper.root = new_feature.parent
            helper.workspace = target
            helper._project_root = target
            helper.run_cli = lambda *args, **kwargs: self.cli(target, *args, **kwargs)
            helper.approve_requirements("delivery")
            helper.prepare_execution_inputs("delivery")
            package = helper.write_package("new-slice", feature_id="delivery", scope=["main.txt"])
            self.cli(target, "start-slice", "--feature-id", "delivery", "--execution-id", "new-worker",
                     "--package-file", package, "--workspace-root", target)
            lease = json.loads(
                (new_feature.parent / ".feature-archive-workspace-state.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual("new-slice", lease["modification_lease"]["package_id"])
            self.assertEqual(original, self.files(batch / "runtime"))
            self.assertEqual(b"unfinished implementation\n", (target / "main.txt").read_bytes())
            self.assertEqual(index, (target / ".git/index").read_bytes())
            removed = self._run(target, "-Uninstall")
            self.assertEqual(0, removed.returncode, removed.stderr)
            self.assertEqual(original, self.files(batch / "runtime"))

    def test_failed_handoff_restores_old_install_documents_and_lease(self):
        with tempfile.TemporaryDirectory() as temporary:
            target, _ = self.previous_project(Path(temporary))
            before = self._managed_snapshot(target)
            for step in ("after-retirement", "after-uninstall-payload", "after-payload", "after-lock"):
                with self.subTest(step=step):
                    result = self._run(target, fail_step=step)
                    self.assertNotEqual(0, result.returncode)
                    self.assertIn(step, result.stderr)
                    self.assertEqual(before, self._managed_snapshot(target))
                    self.assertEqual("3.7.5", json.loads((target / LOCK).read_text(encoding="utf-8-sig"))["workflowVersion"])
                    self.assertEqual(b"unfinished implementation\n", (target / "main.txt").read_bytes())

    def test_uninstalled_legacy_documents_are_isolated_without_parsing(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = self.project(Path(temporary))
            runtime = target / ".scratch/dloop-v3"
            feature = runtime / "outputs/old-job"
            feature.mkdir(parents=True)
            (feature / "requirements.md").write_bytes("旧版需求与附件".encode())
            (feature / "attachment.bin").write_bytes(bytes(range(256)))
            original = self.files(runtime)
            result = self._run(target)
            self.assertEqual(0, result.returncode, result.stderr)
            batch, = (target / ".scratch/dloop-history").iterdir()
            self.assertEqual(original, self.files(batch / "runtime"))
            receipt = json.loads((batch / "retirement.json").read_text(encoding="utf-8-sig"))
            self.assertEqual("uninstalled", receipt["sourceVersion"])
            self.assertFalse(runtime.exists())

    def test_executing_old_command_blocks_handoff_without_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            target, feature = self.previous_project(Path(temporary), unfinished=False)
            before = self._managed_snapshot(target)
            lock = feature.parent / ".feature-archive-workspace-state.lock"
            with lock.open("r+b"):
                result = self._run(target)
            self.assertNotEqual(0, result.returncode)
            self.assertEqual(before, self._managed_snapshot(target))

    def test_svn_unfinished_job_retires_without_reverting_product_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            target, feature = self.previous_project(Path(temporary), vcs="svn")
            original = self.files(target / ".scratch/dloop-v3")
            ignore_before = test_installation.InstallationTests._ignore(target)
            result = self._run(target)
            self.assertEqual(0, result.returncode, result.stderr)
            batch, = (target / ".scratch/dloop-history").iterdir()
            self.assertEqual(original, self.files(batch / "runtime"))
            self.assertFalse(feature.exists())
            self.assertIsNone(self.cli(target, "workflow-status")["modification_lease"])
            self.cli(target, "init", "--feature-id", "new-delivery", "--title", "新交付")
            self.assertEqual(b"unfinished implementation\n", (target / "main.txt").read_bytes())
            self.assertEqual(ignore_before, test_installation.InstallationTests._ignore(target))


if __name__ == "__main__":
    unittest.main()
