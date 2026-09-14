"""Run an existing suite; missing prerequisites and unexpected skips fail CI."""

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[2]
SUITES = {
    "core": "src/FeatureArchive/tests",
    "distribution": "tests",
    "evals": "evals/dloop-behavior/tests",
}
ALLOWED_SKIPS = {
    "core": {
        "test_feature_archive_workflow.FeatureArchiveWorkflowTests."
        "test_installed_version_and_integrity_lock_match_managed_files":
            "Source checkout: installed ownership is covered by installation tests.",
    },
    "distribution": {
        "test_unity_capture.UnityCaptureContractTests."
        "test_editor_package_compiles_and_captures_without_changing_prefab":
            "Real Unity capture requires a separately configured editor.",
    },
    "evals": {},
}


class CIResult(unittest.TextTestResult):
    passed = 0

    def addSuccess(self, test):
        self.passed += 1
        super().addSuccess(test)


def check_environment(suite):
    if suite == "evals":
        return
    if sys.platform != "win32":
        raise RuntimeError("Core and installation CI require Windows.")
    commands = (
        ["git", "--version"],
        ["svn", "--version", "--quiet"],
        ["svnadmin", "--version", "--quiet"],
        ["powershell", "-NoProfile", "-Command", "$PSVersionTable.PSVersion.ToString()"],
    )
    for command in commands:
        if not shutil.which(command[0]):
            raise RuntimeError(f"Required executable is missing: {command[0]}")
        result = subprocess.run(command, check=True, capture_output=True, timeout=30,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        print(f"{command[0]}: {result.stdout.decode('utf-8', errors='replace').strip()}", flush=True)
    # A working --version is not sufficient on Windows with a legacy code page.
    with tempfile.TemporaryDirectory(prefix="dloop-ci-svn-") as directory:
        base = Path(directory)
        repository, project = base / "repository", base / "project"
        commands = [
            ["svnadmin", "create", str(repository)],
            ["svn", "checkout", repository.as_uri(), str(project)],
        ]
        for command in commands:
            subprocess.run(command, check=True, capture_output=True, timeout=30,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        target = project / "中文.txt"
        target.write_text("CI fixture\n", encoding="utf-8")
        group = "CI 中文分组"
        for command in (["svn", "add", str(target)], ["svn", "changelist", group, str(target)]):
            subprocess.run(command, check=True, capture_output=True, timeout=30,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        status = subprocess.run(["svn", "status", "--xml", str(project)],
                                check=True, capture_output=True, timeout=30,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        document = ET.fromstring(status.stdout)
        entry = document.find(f"changelist[@name='{group}']/entry")
        if entry is None or Path(entry.attrib["path"]).name != target.name:
            raise RuntimeError("SVN cannot round-trip Chinese filenames and changelist names.")
    print("SVN Chinese filename and changelist round-trip: PASS", flush=True)


def summarize(suite, result):
    skipped = [(test.id(), reason) for test, reason in result.skipped]
    unexpected = [name for name, _ in skipped if name not in ALLOWED_SKIPS[suite]]
    successful = result.wasSuccessful() and result.testsRun > 0 and not unexpected
    lines = [f"### {suite}: {'PASS' if successful else 'FAIL'}", "",
             f"Run: {result.testsRun}; passed: {result.passed}; failures: {len(result.failures)}; "
             f"errors: {len(result.errors)}; skipped: {len(skipped)}; "
             f"expected failures: {len(result.expectedFailures)}; "
             f"unexpected successes: {len(result.unexpectedSuccesses)}."]
    for name, reason in skipped:
        explanation = ALLOWED_SKIPS[suite].get(name, "UNEXPECTED SKIP: CI fails.")
        lines.extend(["", f"- `{name}`: {reason}. {explanation}"])
    if result.testsRun == 0:
        lines.extend(["", "No tests were discovered: CI fails."])
    return successful, "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=SUITES)
    args = parser.parse_args()
    os.chdir(ROOT)
    check_environment(args.suite)
    tests = unittest.defaultTestLoader.discover(str(ROOT / SUITES[args.suite]))
    result = unittest.TextTestRunner(verbosity=2, resultclass=CIResult).run(tests)
    successful, summary = summarize(args.suite, result)
    print(summary, flush=True)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as output:
            output.write(summary)
    return 0 if successful else 1


if __name__ == "__main__":
    sys.exit(main())
