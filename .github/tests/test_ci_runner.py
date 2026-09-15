"""Verify CI cannot report empty, skipped or failed suites as passing."""

import io
from pathlib import Path
import sys
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
                run_tests.check_environment("core")
