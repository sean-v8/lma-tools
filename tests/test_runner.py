"""Quiet test output, reliable exit status and failure diagnostics."""

import io
import sys
import unittest

from tests.__main__ import ProgressRunner


class QuietRunnerTests(unittest.TestCase):
    def run_cases(self, cases):
        output = io.StringIO()
        result = ProgressRunner(stream=output, verbosity=0).run(
            unittest.TestSuite(cases)
        )
        return result, output.getvalue()

    def test_success_hides_stdout_and_stderr(self):
        def noisy():
            print("hidden stdout")
            print("hidden stderr", file=sys.stderr)

        result, text = self.run_cases([unittest.FunctionTestCase(noisy)])
        self.assertTrue(result.wasSuccessful())
        self.assertIn("Tests passed: 1/1", text)
        self.assertNotIn("hidden", text)

    def test_failure_includes_captured_output_only_once(self):
        def fail():
            print("necessary diagnostic")
            raise AssertionError("specific failure")

        result, text = self.run_cases([unittest.FunctionTestCase(fail)])
        self.assertFalse(result.wasSuccessful())
        self.assertIn("Tests passed: 0/1", text)
        self.assertIn("specific failure", text)
        self.assertEqual(text.count("necessary diagnostic"), 1)

    def test_skip_is_reported_without_inflating_passed_count(self):
        def skip():
            raise unittest.SkipTest("optional tool absent")

        result, text = self.run_cases(
            [
                unittest.FunctionTestCase(lambda: None),
                unittest.FunctionTestCase(skip),
            ]
        )
        self.assertTrue(result.wasSuccessful())
        self.assertIn("Tests passed: 1/1 (1 skipped)", text)

    def test_subtest_failure_is_not_counted_as_a_pass(self):
        class Sample(unittest.TestCase):
            def runTest(self):
                with self.subTest(track=5):
                    self.fail("ambiguous source")

        result, text = self.run_cases([Sample()])
        self.assertFalse(result.wasSuccessful())
        self.assertIn("Tests passed: 0/1", text)
        self.assertIn("track=5", text)

    def test_expected_failure_is_not_counted_as_a_pass(self):
        class Sample(unittest.TestCase):
            @unittest.expectedFailure
            def runTest(self):
                self.fail("known issue")

        result, text = self.run_cases([Sample()])
        self.assertTrue(result.wasSuccessful())
        self.assertEqual(result.passed, 0)
        self.assertIn("1 expected failures", text)

    def test_skipped_subtest_is_not_counted_as_a_pass(self):
        class Sample(unittest.TestCase):
            def runTest(self):
                with self.subTest(track=1):
                    self.skipTest("optional")
                with self.subTest(track=2):
                    self.assertTrue(True)

        result, text = self.run_cases([Sample()])
        self.assertTrue(result.wasSuccessful())
        self.assertIn("Tests passed: 0/0 (1 skipped)", text)

    def test_unexpected_success_is_a_failure(self):
        class Sample(unittest.TestCase):
            @unittest.expectedFailure
            def runTest(self):
                pass

        result, text = self.run_cases([Sample()])
        self.assertFalse(result.wasSuccessful())
        self.assertIn("UNEXPECTED SUCCESS", text)

    def test_error_retains_traceback_and_diagnostics(self):
        def error():
            print("error context", file=sys.stderr)
            raise RuntimeError("conversion broke")

        result, text = self.run_cases([unittest.FunctionTestCase(error)])
        self.assertFalse(result.wasSuccessful())
        self.assertIn("ERROR:", text)
        self.assertIn("RuntimeError: conversion broke", text)
        self.assertEqual(text.count("error context"), 1)
