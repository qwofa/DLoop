"""Run an existing suite; missing prerequisites and unexpected skips fail CI."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[2]
SUITES = {
    "core": "src/FeatureArchive/tests",
    "distribution": "tests",
}
# These modules exercise real repository/host boundaries; all other core modules
# form the business layer. CI still runs both layers with the default "all".
INTEGRATION_MODULES = {
    "test_dloop_mcp.py",
    "test_feature_archive_cli.py",
    "test_feature_archive_dloop_ui.py",
    "test_feature_archive_git.py",
    "test_feature_archive_snapshots.py",
    "test_feature_archive_svn.py",
    "test_feature_archive_windows_acl.py",
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
}


class CIResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.passed = 0
        self.timings = []

    def startTest(self, test):
        self._started = time.perf_counter()
        super().startTest(test)

    def stopTest(self, test):
        self.timings.append({"test": test.id(), "seconds": time.perf_counter() - self._started})
        super().stopTest(test)

    def addSuccess(self, test):
        self.passed += 1
        super().addSuccess(test)


def load_suite(suite, pattern="test_*.py", layer="all", matches=None):
    directory = ROOT / SUITES[suite]
    loader = unittest.TestLoader()
    if matches:
        loader.testNamePatterns = [value if "*" in value else f"*{value}*" for value in matches]
    selected = unittest.TestSuite()
    for path in sorted(directory.glob(pattern)):
        if not path.is_file():
            continue
        integration = path.name in INTEGRATION_MODULES
        if layer == "business" and integration or layer == "integration" and not integration:
            continue
        selected.addTests(loader.discover(str(directory), pattern=path.name))
    return selected


def check_environment():
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


def summarize(suite, result, elapsed=None):
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
    if elapsed is not None:
        lines.extend(["", f"Suite wall time (including shared fixtures): {elapsed:.3f}s."])
    if result.timings:
        lines.extend(["", f"Test-case time (including per-test setup and cleanup): {sum(item['seconds'] for item in result.timings):.3f}s.",
                      "", "Slowest tests:"])
        for item in sorted(result.timings, key=lambda item: item["seconds"], reverse=True)[:10]:
            lines.append(f"- {item['seconds']:.3f}s: `{item['test']}`")
    return successful, "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=SUITES)
    parser.add_argument("--pattern", default="test_*.py", help="Test filename glob")
    parser.add_argument("--match", action="append", help="Test name substring or glob; repeat to select multiple names")
    parser.add_argument("--layer", choices=("all", "business", "integration"), default="all")
    parser.add_argument("--timings-json", type=Path, help="Write every test duration to this file")
    args = parser.parse_args()
    if args.suite != "core" and args.layer != "all":
        parser.error("--layer is only available for core")
    os.chdir(ROOT)
    print(f"Selection: suite={args.suite}; layer={args.layer}; pattern={args.pattern}; match={args.match or 'all'}", flush=True)
    check_environment()
    tests = load_suite(args.suite, args.pattern, args.layer, args.match)
    started = time.perf_counter()
    result = unittest.TextTestRunner(verbosity=2, resultclass=CIResult).run(tests)
    elapsed = time.perf_counter() - started
    successful, summary = summarize(args.suite, result, elapsed)
    print(summary, flush=True)
    if args.timings_json:
        args.timings_json.parent.mkdir(parents=True, exist_ok=True)
        args.timings_json.write_text(json.dumps({
            "suite": args.suite, "layer": args.layer, "pattern": args.pattern, "match": args.match,
            "successful": successful, "elapsed_seconds": elapsed,
            "tests_run": result.testsRun, "passed": result.passed,
            "failures": len(result.failures), "errors": len(result.errors),
            "skipped": [{"test": test.id(), "reason": reason} for test, reason in result.skipped],
            "timings": result.timings,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as output:
            output.write(summary)
    return 0 if successful else 1


if __name__ == "__main__":
    sys.exit(main())
