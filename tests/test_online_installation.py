"""Exercise the one-line entry point against real Git and SVN projects."""

import os
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile

import test_installation


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "win32", "Windows installer")
class OnlineInstallationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="dloop-bootstrap-test-")
        cls.archive = Path(cls.temporary.name) / "release.zip"
        paths = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=ROOT, check=True, capture_output=True,
        ).stdout.decode("utf-8").split("\0")
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        with zipfile.ZipFile(cls.archive, "w", zipfile.ZIP_DEFLATED) as archive:
            for relative in sorted(set(paths)):
                if relative and (ROOT / relative).is_file():
                    archive.write(ROOT / relative, f"DLoop-{version}/{relative}")

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def install(self, target, *, failure=False, stdin=False, archive=None):
        environment = os.environ.copy()
        environment.pop("FEATURE_ARCHIVE_INSTALL_FAIL_STEP", None)
        if failure:
            environment["FEATURE_ARCHIVE_INSTALL_FAIL_STEP"] = "after-lock"
        return subprocess.run(
            [sys.executable, "-" if stdin else str(ROOT / "install.py"),
             "--archive", str(archive or self.archive)],
            input=(ROOT / "install.py").read_bytes() if stdin else None,
            cwd=target, capture_output=True, env=environment,
        )

    def git_project(self, directory):
        target = Path(directory) / "中文 project"
        target.mkdir()
        subprocess.run(["git", "init", "-q", str(target)], check=True)
        return target

    def test_stdin_install_reinstall_preserves_git_index_and_existing_rules(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = self.git_project(temporary)
            original = "# 原有规则\r\nLibrary/".encode("utf-8")
            ignore = target / ".gitignore"
            ignore.write_bytes(original)
            subprocess.run(["git", "-C", str(target), "add", ".gitignore"], check=True)
            index = target / ".git/index"
            original_index = index.read_bytes()
            for _ in range(2):
                result = self.install(target, stdin=True)
                self.assertEqual(0, result.returncode, result.stderr.decode(errors="replace"))
                self.assertEqual(original + b"\r\n/.scratch/\r\n", ignore.read_bytes())
                self.assertEqual(original_index, index.read_bytes())
                self.assertTrue((target / test_installation.LOCK).is_file())
                self.assertTrue((target / ".agents/skills/dloop/SKILL.md").is_file())
                self.assertTrue((target / ".agents/skills/dloop-ui/SKILL.md").is_file())

    def test_failed_install_restores_git_rule_and_payload(self):
        for original in (None, b"Library/\n"):
            with self.subTest(original=original), tempfile.TemporaryDirectory() as temporary:
                target = self.git_project(temporary)
                ignore = target / ".gitignore"
                if original is not None:
                    ignore.write_bytes(original)
                result = self.install(target, failure=True)
                self.assertNotEqual(0, result.returncode)
                self.assertEqual(original, ignore.read_bytes() if ignore.exists() else None)
                self.assertFalse((target / test_installation.LOCK).exists())
                self.assertFalse((target / test_installation.CORE_CLI).exists())

    @unittest.skipUnless(test_installation.SVN and test_installation.SVNADMIN, "SVN tools")
    def test_svn_preparation_and_failure_restore(self):
        for failure in (False, True):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                helper = test_installation.InstallationTests()
                target, _ = helper._svn_project(Path(temporary), ignored=False)
                subprocess.run([test_installation.SVN, "propset", "svn:ignore", "Library\n", str(target)],
                               check=True, capture_output=True)
                original = subprocess.run(
                    [test_installation.SVN, "propget", "--strict", "svn:ignore", str(target)],
                    check=True, capture_output=True,
                ).stdout
                result = self.install(target, failure=failure)
                self.assertEqual(failure, result.returncode != 0, result.stderr.decode(errors="replace"))
                value = subprocess.run(
                    [test_installation.SVN, "propget", "--strict", "svn:ignore", str(target)],
                    check=True, capture_output=True,
                ).stdout
                self.assertEqual(original if failure else original + b".scratch\r\n", value)
                self.assertEqual(not failure, (target / test_installation.LOCK).exists())

    def test_one_line_powershell_download_install(self):
        class QuietHandler(SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

        with tempfile.TemporaryDirectory() as temporary:
            target = self.git_project(temporary)
            serving = Path(temporary) / "http"
            serving.mkdir()
            (serving / "release.zip").write_bytes(self.archive.read_bytes())
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0), partial(QuietHandler, directory=str(serving)),
            )
            address = f"http://127.0.0.1:{server.server_port}"
            source = (ROOT / "install.py").read_text(encoding="utf-8").replace(
                'ARCHIVE_URL = f"https://codeload.github.com/qwofa/DLoop/zip/refs/tags/v{VERSION}"',
                f'ARCHIVE_URL = "{address}/release.zip"',
            )
            (serving / "install.py").write_text(source, encoding="utf-8")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                python = sys.executable.replace("'", "''")
                result = subprocess.run(
                    [test_installation.POWERSHELL, "-NoProfile", "-Command",
                     f"irm '{address}/install.py' -ErrorAction Stop | & '{python}' -; exit $LASTEXITCODE"],
                    cwd=target, capture_output=True,
                )
                self.assertEqual(0, result.returncode, result.stderr.decode(errors="replace"))
                self.assertIn(b"DLoop installed and verified.", result.stdout)
                self.assertTrue((target / test_installation.LOCK).is_file())
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_download_failure_changes_no_project_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = self.git_project(temporary)
            # Exercise the real download branch, forcing an ordinary network failure.
            source = (ROOT / "install.py").read_text(encoding="utf-8")
            source = source.replace(
                'urllib.request.urlretrieve(ARCHIVE_URL, archive)',
                'urllib.request.urlretrieve("http://127.0.0.1:1/unavailable", archive)',
            )
            result = subprocess.run([sys.executable, "-"], input=source.encode(),
                                    cwd=target, capture_output=True)
            self.assertNotEqual(0, result.returncode)
            self.assertEqual([".git"], [path.name for path in target.iterdir()])


if __name__ == "__main__":
    unittest.main()
