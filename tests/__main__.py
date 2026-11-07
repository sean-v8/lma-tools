"""Run buffered tests with a passed-test counter and failure details only."""

import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from tqdm import tqdm

ROOT = Path(__file__).resolve().parents[1]


class ProgressResult(unittest.TextTestResult):
    """Advance only successful tests; exclude skipped/expected failures."""

    def addSuccess(self, test):
        super().addSuccess(test)
        self.passed += 1
        self.progress.update(1)

    def _exclude(self, test):
        parent = getattr(test, "test_case", test)
        if id(parent) not in self.excluded:
            self.excluded.add(id(parent))
            self.progress.total -= 1
        details = []
        if self.skipped:
            details.append(f"{len(self.skipped)} skipped")
        if self.expectedFailures:
            details.append(f"{len(self.expectedFailures)} expected failures")
        self.progress.set_postfix_str(", ".join(details))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._exclude(test)

    def addExpectedFailure(self, test, error):
        super().addExpectedFailure(test, error)
        self._exclude(test)

    def _restoreStdout(self):
        # Tracebacks already contain captured output; never echo it twice.
        self._mirrorOutput = False
        super()._restoreStdout()


class ProgressRunner(unittest.TextTestRunner):
    resultclass = ProgressResult

    def run(self, suite):
        result = self._makeResult()
        result.buffer = True
        result.failfast = self.failfast
        result.passed = 0
        result.excluded = set()
        interactive = self.stream.isatty()
        result.progress = tqdm(
            total=suite.countTestCases(),
            desc="Tests passed",
            file=self.stream,
            dynamic_ncols=True,
            disable=not interactive,
            bar_format=(
                "{desc}: {n_fmt}/{total_fmt} |{bar}| [{elapsed}]{postfix}"
            ),
        )
        unittest.registerResult(result)
        try:
            result.startTestRun()
            suite(result)
        finally:
            result.stopTestRun()
            result.progress.close()
            unittest.removeResult(result)
        if not interactive:
            message = f"Tests passed: {result.passed}/{result.progress.total}"
            if postfix := getattr(result.progress, "postfix", ""):
                message += f" ({postfix})"
            self.stream.writeln(message)
        if not result.wasSuccessful():
            result.printErrors()
        return result


def main():
    # Discovery failures become ordinary failed tests with their traceback.
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        suite = unittest.defaultTestLoader.discover(
            str(ROOT / "tests"), top_level_dir=str(ROOT)
        )
    result = ProgressRunner(verbosity=0).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
