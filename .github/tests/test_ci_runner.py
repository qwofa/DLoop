"""Verify CI cannot report empty, skipped or failed suites as passing."""

import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_tests


class CIRunnerTests(unittest.TestCase):
    def result(self, action):
        test = unittest.FunctionTestCase(action)
        return unittest.TextTestRunner(stream=io.StringIO(), resultclass=run_tests.CIResult).run(test)

    def test_passing_test_passes(self):
        passed, _ = run_tests.summarize("core", self.result(lambda: None))
        self.assertTrue(passed)

    def test_empty_suite_fails(self):
        result = unittest.TextTestRunner(stream=io.StringIO(), resultclass=run_tests.CIResult).run(unittest.TestSuite())
        passed, summary = run_tests.summarize("core", result)
        self.assertFalse(passed)
        self.assertIn("No tests were discovered", summary)

    def test_unexpected_skip_fails(self):
        def skip():
            raise unittest.SkipTest("missing tool")
        passed, summary = run_tests.summarize("core", self.result(skip))
        self.assertFalse(passed)
        self.assertIn("UNEXPECTED SKIP", summary)

    def test_documented_skip_is_visible_and_allowed(self):
        def skip():
            raise unittest.SkipTest("no editor")
        result = self.result(skip)
        result.skipped[0][0].id = lambda: next(iter(run_tests.ALLOWED_SKIPS["distribution"]))
        passed, summary = run_tests.summarize("distribution", result)
        self.assertTrue(passed)
        self.assertIn("skipped: 1", summary)
        self.assertIn("separately configured editor", summary)

    def test_assertion_failure_fails(self):
        def fail():
            raise AssertionError("regression")
        passed, _ = run_tests.summarize("core", self.result(fail))
        self.assertFalse(passed)

    def test_multiple_failed_subtests_do_not_subtract_multiple_passes(self):
        class Subtests(unittest.TestCase):
            def runTest(self):
                for value in (1, 2):
                    with self.subTest(value=value):
                        self.fail("regression")
        result = unittest.TextTestRunner(stream=io.StringIO(), resultclass=run_tests.CIResult).run(Subtests())
        passed, summary = run_tests.summarize("core", result)
        self.assertFalse(passed)
        self.assertIn("Run: 1; passed: 0; failures: 2", summary)

    def test_missing_dependency_fails_before_discovery(self):
        with mock.patch.object(sys, "platform", "win32"), \
             mock.patch.object(run_tests.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "Required executable is missing: git"):
                run_tests.check_environment()

    def test_timings_cover_success_and_failure_without_leaking_between_runs(self):
        def fail():
            self.fail("regression")
        with mock.patch.object(run_tests, "time") as clock:
            clock.perf_counter.side_effect = [10, 10.25, 20, 22]
            first = self.result(lambda: None)
            second = self.result(fail)
        self.assertEqual([0.25], [item["seconds"] for item in first.timings])
        self.assertEqual([2], [item["seconds"] for item in second.timings])
        self.assertEqual(1, first.passed)
        self.assertEqual(0, second.passed)
        passed, summary = run_tests.summarize("core", second)
        self.assertFalse(passed)
        self.assertIn("2.000s", summary)

    def test_layer_and_name_selection_partition_the_suite_without_duplicates(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            modules = ("test_runner_fixture_business", "test_runner_fixture_repository")
            source = "import unittest\nclass Cases(unittest.TestCase):\n    def test_first(self): pass\n    def test_second(self): pass\n"
            for name in modules:
                (directory / f"{name}.py").write_text(source, encoding="utf-8")
                self.addCleanup(sys.modules.pop, name, None)
            with mock.patch.object(run_tests, "ROOT", directory), \
                 mock.patch.dict(run_tests.SUITES, {"core": "."}), \
                 mock.patch.object(run_tests, "INTEGRATION_MODULES", {modules[1] + ".py"}):
                self.assertEqual(4, run_tests.load_suite("core").countTestCases())
                for layer in ("business", "integration"):
                    self.assertEqual(2, run_tests.load_suite("core", layer=layer).countTestCases())
                selected = run_tests.load_suite("core", layer="integration", matches=["first", "*first"])
                self.assertEqual(1, selected.countTestCases())
                self.assertEqual(2, run_tests.load_suite("core", pattern=modules[0] + ".py").countTestCases())
                empty = run_tests.load_suite("core", matches=["missing_case"])
                result = unittest.TextTestRunner(stream=io.StringIO(), resultclass=run_tests.CIResult).run(empty)
                self.assertFalse(run_tests.summarize("core", result)[0])

    def test_selected_import_failure_is_reported_as_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "test_runner_fixture_broken.py").write_text("raise RuntimeError('broken import')", encoding="utf-8")
            with mock.patch.object(run_tests, "ROOT", directory), \
                 mock.patch.dict(run_tests.SUITES, {"core": "."}):
                suite = run_tests.load_suite("core")
                result = unittest.TextTestRunner(stream=io.StringIO(), resultclass=run_tests.CIResult).run(suite)
            self.assertEqual(1, len(result.errors))
            self.assertFalse(run_tests.summarize("core", result)[0])
