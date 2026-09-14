"""当前 Windows SVN 项目的安装、校验与卸载测试。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPOSITORY_ROOT / "install.ps1"
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")
SVN = shutil.which("svn") or (
    r"C:\Program Files\SlikSvn\bin\svn.exe"
    if Path(r"C:\Program Files\SlikSvn\bin\svn.exe").is_file()
    else None
)
SVNADMIN = shutil.which("svnadmin") or (
    r"C:\Program Files\SlikSvn\bin\svnadmin.exe"
    if Path(r"C:\Program Files\SlikSvn\bin\svnadmin.exe").is_file()
    else None
)
VERSION = (REPOSITORY_ROOT / "VERSION").read_text(encoding="utf-8").strip()
CORE_CLI = Path("Tools") / "FeatureArchive" / "feature_archive.py"
FRICTION_INBOX = Path(".scratch") / "dloop-v3" / "friction-inbox"
LOCK = Path(".agents") / "feature-archive-workflow.lock.json"
UNITY_PACKAGE = Path("Packages") / "com.dloop.ui-capture"
REQUIRED_CONFIGURATION_PAYLOADS = (
    Path("Tools") / "FeatureArchive" / "archive_configuration.py",
    Path("Tools") / "FeatureArchive" / "archive_delivery_materials.py",
)


@unittest.skipUnless(
    POWERSHELL and SVN and SVNADMIN,
    "当前环境没有 PowerShell、SVN 或 svnadmin。",
)
class InstallationTests(unittest.TestCase):
    def _run(
        self,
        target: Path,
        *arguments: str,
        fail_step: str | None = None,
        installer: Path = INSTALLER,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        if fail_step is None:
            environment.pop("FEATURE_ARCHIVE_INSTALL_FAIL_STEP", None)
        else:
            environment["FEATURE_ARCHIVE_INSTALL_FAIL_STEP"] = fail_step
        return subprocess.run(
            [
                str(POWERSHELL),
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(installer),
                "-Target",
                str(target),
                *arguments,
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
        )

    def _svn_project(
        self,
        parent: Path,
        *,
        ignored: bool = True,
    ) -> tuple[Path, str]:
        repository = parent / "repository"
        working_copy = parent / "working-copy"
        subprocess.run([str(SVNADMIN), "create", str(repository)], check=True)
        repository_url = repository.resolve().as_uri()
        subprocess.run(
            [
                str(SVN),
                "mkdir",
                "--parents",
                f"{repository_url}/branches/Dev",
                "-m",
                "init",
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [str(SVN), "checkout", "-q", repository_url, str(working_copy)],
            check=True,
        )
        target = working_copy / "branches" / "Dev"
        if ignored:
            subprocess.run(
                [
                    str(SVN),
                    "propset",
                    "svn:ignore",
                    ".scratch\n",
                    str(target),
                ],
                check=True,
                capture_output=True,
            )
        return target, self._ignore(target) if ignored else ""

    @staticmethod
    def _ignore(target: Path) -> str:
        completed = subprocess.run(
            [str(SVN), "propget", "svn:ignore", "--strict", str(target)],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return completed.stdout

    @staticmethod
    def _json(path: Path) -> dict[str, object]:
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _unsupported_install(target: Path, version: str) -> None:
        marker = target / "Tools" / "FeatureArchive" / "unsupported-old.txt"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"unsupported {version}\n", encoding="utf-8")
        lock_path = target / LOCK
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(
            json.dumps(
                {
                    "workflowVersion": version,
                    "files": "must-not-be-read",
                }
            ),
            encoding="utf-8",
        )

    @staticmethod
    def _managed_snapshot(target: Path) -> dict[str, bytes | None]:
        roots = [
            target / ".agents",
            target / ".codex",
            target / "Tools",
            target / UNITY_PACKAGE,
            target / ".scratch" / "dloop-v3",
        ]
        snapshot: dict[str, bytes | None] = {}
        for root in roots:
            if not root.exists():
                continue
            for path in (root, *root.rglob("*")):
                relative_path = path.relative_to(target).as_posix()
                if path.is_file():
                    snapshot[relative_path] = path.read_bytes()
                elif path.is_dir():
                    snapshot[relative_path + "/"] = None
        return snapshot

    def test_fresh_install_verify_and_ignore_are_current_project_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target, ignore_before = self._svn_project(Path(directory))

            installed = self._run(target, "-Version", f"v{VERSION}")
            verified = self._run(
                target,
                "-Version",
                f"v{VERSION}",
                "-Verify",
            )

            self.assertEqual(0, installed.returncode, installed.stderr)
            self.assertEqual(0, verified.returncode, verified.stderr)
            self.assertTrue((target / CORE_CLI).is_file())
            self.assertFalse((target / "Tools" / "FeatureArchive" / "CodexAdapter").exists())
            self.assertFalse((target / ".codex" / "hooks.json").exists())
            self.assertTrue(
                (target / ".agents" / "skills" / "dloop" / "SKILL.md").is_file()
            )
            self.assertTrue(
                (target / ".agents" / "skills" / "dloop-ui" / "SKILL.md").is_file()
            )
            self.assertTrue(
                (target / "Tools" / "FeatureArchive" / "dloop_ui.py").is_file()
            )
            ui_help = subprocess.run(
                [
                    sys.executable,
                    str(target / ".agents/skills/dloop-ui/scripts/dloop_ui.py"),
                    "--help",
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(0, ui_help.returncode, ui_help.stderr)
            self.assertIn("ui-investigate", ui_help.stdout)
            self.assertIn("ui-publish", ui_help.stdout)
            self.assertNotIn("register-prefabs", ui_help.stdout)
            self.assertNotIn("prepare-capture", ui_help.stdout)
            self.assertNotIn("import-capture", ui_help.stdout)
            self.assertNotIn("scan-prefabs", ui_help.stdout)
            self.assertTrue(
                (target / UNITY_PACKAGE / "Editor" / "DloopUiCapture.cs").is_file()
            )
            lock = self._json(target / LOCK)
            locked_paths = {item["path"] for item in lock["files"]}
            self.assertIn(".agents/skills/dloop/SKILL.md", locked_paths)
            self.assertIn(".agents/skills/dloop-ui/SKILL.md", locked_paths)
            self.assertIn("Tools/FeatureArchive/dloop_ui.py", locked_paths)
            template = Path("Tools/FeatureArchive/templates/ui-delivery.html")
            self.assertIn(template.as_posix(), locked_paths)
            self.assertEqual(
                (REPOSITORY_ROOT / "plugin/dloop/runtime/templates/ui-delivery.html").read_bytes(),
                (target / template).read_bytes(),
            )
            self.assertIn(
                "Packages/com.dloop.ui-capture/Editor/DloopUiCapture.cs",
                locked_paths,
            )
            for payload in REQUIRED_CONFIGURATION_PAYLOADS:
                self.assertTrue((target / payload).is_file())
                self.assertIn(payload.as_posix(), locked_paths)
            self.assertNotIn("codexHook", lock)
            self.assertNotIn("vcsExclude", lock)
            self.assertEqual(ignore_before, self._ignore(target))

    def test_missing_required_ignore_refuses_without_partial_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target, _ = self._svn_project(Path(directory), ignored=False)

            result = self._run(target, "-Version", f"v{VERSION}")

            self.assertNotEqual(0, result.returncode)
            self.assertIn("预先", result.stderr)
            self.assertEqual({}, self._managed_snapshot(target))

    def test_existing_codex_configuration_is_not_modified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target, _ = self._svn_project(Path(directory))
            config = target / ".codex" / "config.toml"
            config.parent.mkdir(parents=True)
            hooks = target / ".codex" / "hooks.json"
            config_content = b'\xef\xbb\xbfmodel = "gpt-test"\r\n'
            hooks_content = b'{"hooks":{"Stop":[{"command":"third-party"}]}}\r\n'
            config.write_bytes(config_content)
            hooks.write_bytes(hooks_content)

            installed = self._run(target, "-Version", f"v{VERSION}")
            verified = self._run(
                target,
                "-Version",
                f"v{VERSION}",
                "-Verify",
            )
            removed = self._run(
                target,
                "-Version",
                f"v{VERSION}",
                "-Uninstall",
            )

            for result in (installed, verified, removed):
                self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(config_content, config.read_bytes())
            self.assertEqual(hooks_content, hooks.read_bytes())

    def test_unmanaged_payload_conflict_and_managed_drift_are_never_overwritten(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target, _ = self._svn_project(Path(directory))
            conflict = target / CORE_CLI
            conflict.parent.mkdir(parents=True)
            conflict.write_text("user file", encoding="utf-8")

            unmanaged = self._run(target, "-Version", f"v{VERSION}")

            self.assertNotEqual(0, unmanaged.returncode)
            self.assertEqual("user file", conflict.read_text(encoding="utf-8"))

        with tempfile.TemporaryDirectory() as directory:
            target, _ = self._svn_project(Path(directory))
            self.assertEqual(
                0,
                self._run(target, "-Version", f"v{VERSION}").returncode,
            )
            conflict = target / CORE_CLI
            conflict.write_text("local drift", encoding="utf-8")

            drifted = self._run(target, "-Version", f"v{VERSION}")

            self.assertNotEqual(0, drifted.returncode)
            self.assertIn("本地漂移", drifted.stderr)
            self.assertEqual("local drift", conflict.read_text(encoding="utf-8"))

    def test_install_failure_rolls_back_payload_and_lock(self) -> None:
        for fail_step in ("after-payload", "after-lock"):
            with self.subTest(fail_step=fail_step):
                with tempfile.TemporaryDirectory() as directory:
                    target, ignore_before = self._svn_project(Path(directory))
                    before = self._managed_snapshot(target)

                    result = self._run(
                        target,
                        "-Version",
                        f"v{VERSION}",
                        fail_step=fail_step,
                    )

                    self.assertNotEqual(0, result.returncode)
                    self.assertEqual(before, self._managed_snapshot(target))
                    self.assertEqual(ignore_before, self._ignore(target))

    def test_friction_inbox_survives_install_and_uninstall(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            target, _ = self._svn_project(parent)
            installed = self._run(target, "-Version", f"v{VERSION}")
            self.assertEqual(0, installed.returncode, installed.stderr)
            marker = target / FRICTION_INBOX / "fr-aaaaaaaaaaaaaaaaaaaaaaaa.jsonl"
            marker.parent.mkdir(parents=True)
            marker.write_text('{"kept":true}\n', encoding="utf-8")

            reinstalled = self._run(target, "-Version", f"v{VERSION}")
            self.assertEqual(0, reinstalled.returncode, reinstalled.stderr)
            self.assertEqual('{"kept":true}\n', marker.read_text(encoding="utf-8"))

            removed = self._run(
                target,
                "-Version",
                f"v{VERSION}",
                "-Uninstall",
            )
            self.assertEqual(0, removed.returncode, removed.stderr)
            self.assertEqual('{"kept":true}\n', marker.read_text(encoding="utf-8"))

    def test_complete_uninstall_is_safe_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target, ignore_before = self._svn_project(Path(directory))
            installed = self._run(target, "-Version", f"v{VERSION}")
            self.assertEqual(0, installed.returncode, installed.stderr)
            archive = target / ".scratch" / "dloop-v3" / "outputs" / "kept.json"
            archive.parent.mkdir(parents=True)
            archive.write_text('{"kept": true}\n', encoding="utf-8")
            history = target / ".scratch/dloop-v3/snapshots/kept.git/objects/retained"
            history.parent.mkdir(parents=True)
            history.write_bytes(b"persistent snapshot")

            removed = self._run(
                target,
                "-Version",
                f"v{VERSION}",
                "-Uninstall",
            )
            repeated = self._run(
                target,
                "-Version",
                f"v{VERSION}",
                "-Uninstall",
            )

            self.assertEqual(0, removed.returncode, removed.stderr)
            self.assertEqual(0, repeated.returncode, repeated.stderr)
            self.assertIn("未安装", repeated.stdout)
            self.assertFalse((target / LOCK).exists())
            self.assertFalse((target / CORE_CLI).exists())
            for payload in REQUIRED_CONFIGURATION_PAYLOADS:
                self.assertFalse((target / payload).exists())
            self.assertFalse((target / ".agents" / "skills" / "dloop").exists())
            self.assertFalse((target / UNITY_PACKAGE).exists())
            self.assertEqual('{"kept": true}\n', archive.read_text(encoding="utf-8"))
            self.assertEqual(b"persistent snapshot", history.read_bytes())
            self.assertEqual(ignore_before, self._ignore(target))

    def test_installed_runtime_avoids_bytecode_cache_before_uninstall(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target, _ = self._svn_project(Path(directory))
            installed = self._run(target, "-Version", f"v{VERSION}")
            self.assertEqual(0, installed.returncode, installed.stderr)

            environment = os.environ.copy()
            environment.pop("PYTHONDONTWRITEBYTECODE", None)
            executed = subprocess.run(
                [sys.executable, str(target / CORE_CLI), "--help"],
                cwd=target,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
            )
            self.assertEqual(0, executed.returncode, executed.stderr)

            def run_installed(*arguments: str) -> dict:
                result = subprocess.run(
                    [sys.executable, str(target / CORE_CLI), *arguments],
                    cwd=target,
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=environment,
                )
                self.assertEqual(0, result.returncode, result.stderr)
                return json.loads(result.stdout)

            run_installed("init", "--feature-id", "input-preparation", "--title", "输入准备")
            prepared = run_installed(
                "prepare-action-input", "--feature-id", "input-preparation",
                "--input-kind", "slice-plan",
            )
            self.assertEqual("slice-plan-v1.json", Path(prepared["target"]).name)
            self.assertTrue(Path(prepared["target"]).is_file())
            self.assertEqual("check-slice-plan", prepared["next_action"]["command"])
            note = run_installed(
                "friction-note", "--feature-id", "input-preparation",
                "--summary", "用户重复解释输入位置", "--extra-work", "重新定位输入文件",
                "--source", "user-feedback", "--recovery", "沿返回入口完成准备",
            )
            summary = run_installed("friction-summary", "--feature-id", "input-preparation")
            self.assertEqual(note["incident_id"], summary["incidents"][0]["incident_id"])
            self.assertEqual("recorded", summary["incidents"][0]["recovery_status"])
            self.assertTrue(
                (target / ".agents" / "skills" / "dloop" / "references" / "friction-records.md").is_file()
            )
            friction = target / ".scratch" / "dloop-v3" / "friction.jsonl"
            preserved_friction = friction.read_bytes()

            self.assertEqual(
                [],
                list((target / "Tools" / "FeatureArchive").rglob("*.pyc")),
            )

            verified = self._run(
                target,
                "-Version",
                f"v{VERSION}",
                "-Verify",
            )
            removed = self._run(
                target,
                "-Version",
                f"v{VERSION}",
                "-Uninstall",
            )

            self.assertEqual(0, verified.returncode, verified.stderr)
            self.assertEqual(0, removed.returncode, removed.stderr)
            self.assertEqual(preserved_friction, friction.read_bytes())
            self.assertTrue(Path(prepared["target"]).is_file())

    def test_uninstall_transactionally_removes_recognized_runtime_cache(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target, _ = self._svn_project(Path(directory))
            installed = self._run(target, "-Version", f"v{VERSION}")
            self.assertEqual(0, installed.returncode, installed.stderr)
            cache_files = (
                target
                / "Tools"
                / "FeatureArchive"
                / "__pycache__"
                / "archive_context.cpython-310.pyc",
            )
            for cache_file in cache_files:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_bytes(b"runtime-cache")
            before = self._managed_snapshot(target)

            failed = self._run(
                target,
                "-Version",
                f"v{VERSION}",
                "-Uninstall",
                fail_step="after-uninstall-runtime-cache",
            )

            self.assertNotEqual(0, failed.returncode)
            self.assertEqual(before, self._managed_snapshot(target))

            removed = self._run(
                target,
                "-Version",
                f"v{VERSION}",
                "-Uninstall",
            )
            self.assertEqual(0, removed.returncode, removed.stderr)
            for cache_file in cache_files:
                self.assertFalse(cache_file.exists())

    def test_other_versions_are_rejected_before_structure_read_or_changes(self) -> None:
        for installed_version in ("2.0.0", "3.3.3", "3.8.0"):
            for arguments in ((), ("-Verify",), ("-Uninstall",)):
                with self.subTest(version=installed_version, arguments=arguments):
                    with tempfile.TemporaryDirectory() as directory:
                        target, ignore_before = self._svn_project(Path(directory))
                        self._unsupported_install(target, installed_version)
                        before = self._managed_snapshot(target)

                        rejected = self._run(
                            target,
                            "-Version",
                            f"v{VERSION}",
                            *arguments,
                        )

                        self.assertNotEqual(0, rejected.returncode)
                        self.assertIn(installed_version, rejected.stderr)
                        self.assertIn(VERSION, rejected.stderr)
                        self.assertEqual(before, self._managed_snapshot(target))
                        self.assertEqual(ignore_before, self._ignore(target))

    def test_requested_other_release_refuses_before_target_changes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            target, ignore_before = self._svn_project(parent)
            before = self._managed_snapshot(target)

            rejected = self._run(
                target,
                "-Version",
                "v3.8.0",
            )

            self.assertNotEqual(0, rejected.returncode)
            self.assertIn("3.8.0", rejected.stderr)
            self.assertIn(VERSION, rejected.stderr)
            self.assertEqual(before, self._managed_snapshot(target))
            self.assertEqual(ignore_before, self._ignore(target))

    def test_uninstall_refuses_unowned_or_drifted_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target, _ = self._svn_project(Path(directory))
            installed = self._run(target, "-Version", f"v{VERSION}")
            self.assertEqual(0, installed.returncode, installed.stderr)
            (target / ".agents" / "skills" / "dloop" / "SKILL.md").write_text(
                "local drift",
                encoding="utf-8",
            )
            before = self._managed_snapshot(target)

            uninstall = self._run(
                target,
                "-Version",
                f"v{VERSION}",
                "-Uninstall",
            )

            self.assertNotEqual(0, uninstall.returncode)
            self.assertEqual(before, self._managed_snapshot(target))

        with tempfile.TemporaryDirectory() as directory:
            target, _ = self._svn_project(Path(directory))
            residual = target / CORE_CLI
            residual.parent.mkdir(parents=True)
            residual.write_text("unowned", encoding="utf-8")
            before = self._managed_snapshot(target)

            install = self._run(target, "-Version", f"v{VERSION}")
            uninstall = self._run(
                target,
                "-Version",
                f"v{VERSION}",
                "-Uninstall",
            )

            self.assertNotEqual(0, install.returncode)
            self.assertNotEqual(0, uninstall.returncode)
            self.assertEqual(before, self._managed_snapshot(target))

    def test_uninstall_rejects_install_lock_unregistered_files(
        self,
    ) -> None:
        unregistered_paths = (
            Path(".agents/skills/dloop/unregistered.txt"),
            Path("Tools/FeatureArchive/unregistered.txt"),
            Path(".agents/skills/dloop/__pycache__/entry.cpython-310.pyc"),
            Path("Tools/FeatureArchive/loose.pyc"),
            Path("Tools/FeatureArchive/__pycache__/notes.txt"),
            Path(
                "Tools/FeatureArchive/__pycache__/nested/"
                "archive_context.cpython-310.pyc"
            ),
        )
        for unregistered_path in unregistered_paths:
            with self.subTest(path=unregistered_path.as_posix()):
                with tempfile.TemporaryDirectory() as directory:
                    target, _ = self._svn_project(Path(directory))
                    installed = self._run(target, "-Version", f"v{VERSION}")
                    self.assertEqual(0, installed.returncode, installed.stderr)
                    unregistered = target / unregistered_path
                    unregistered.parent.mkdir(parents=True, exist_ok=True)
                    unregistered.write_text("unregistered\n", encoding="utf-8")
                    before = self._managed_snapshot(target)
                    uninstall = self._run(
                        target,
                        "-Version",
                        f"v{VERSION}",
                        "-Uninstall",
                    )

                    self.assertNotEqual(0, uninstall.returncode)
                    self.assertIn("安装锁未登记", uninstall.stderr)
                    self.assertEqual(before, self._managed_snapshot(target))

        with tempfile.TemporaryDirectory() as directory:
            target, _ = self._svn_project(Path(directory))
            installed = self._run(target, "-Version", f"v{VERSION}")
            self.assertEqual(0, installed.returncode, installed.stderr)
            nested = (
                target
                / "Tools"
                / "FeatureArchive"
                / "__pycache__"
                / "nested"
            )
            nested.mkdir(parents=True)
            before = self._managed_snapshot(target)

            uninstall = self._run(
                target,
                "-Version",
                f"v{VERSION}",
                "-Uninstall",
            )

            self.assertNotEqual(0, uninstall.returncode)
            self.assertIn("未知子目录", uninstall.stderr)
            self.assertEqual(before, self._managed_snapshot(target))

if __name__ == "__main__":
    unittest.main()
