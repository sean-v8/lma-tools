"""CLI output and exit-status contracts with an offline client."""

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch

from lma_tools.views.cli import main
from lma_tools.views.client import ArchiveError
from lma_tools.views.config import Config


class CliTests(unittest.TestCase):
    def run_cli(self, arguments, failure=False):
        client = Mock()
        client.search.return_value = [{"identifier": "demo"}]
        if failure:
            client.view_counts.return_value = [
                ("demo", ArchiveError("unavailable"))
            ]
        else:
            client.view_counts.return_value = [("demo", 10)]
        output, errors = io.StringIO(), io.StringIO()
        with patch("lma_tools.views.cli.ArchiveClient", return_value=client):
            with patch(
                "lma_tools.views.cli.load_config", return_value=Config()
            ):
                with redirect_stdout(output), redirect_stderr(errors):
                    status = main(arguments)
        client.close.assert_called_once()
        return status, output.getvalue(), errors.getvalue(), client

    def test_json_clean_and_query_override(self):
        status, output, errors, client = self.run_cli(
            ["uploader:demo", "--json"]
        )
        self.assertEqual(status, 0)
        data = json.loads(output)
        self.assertEqual(data["recordings"][0]["views"], 10)
        self.assertEqual(errors, "")
        client.search.assert_called_once_with("(uploader:demo)")

    def test_partial_json_and_nonzero_status(self):
        status, output, errors, _ = self.run_cli(["--json"], failure=True)
        self.assertEqual(status, 1)
        self.assertIsNone(json.loads(output)["recordings"][0]["views"])
        self.assertIn("unavailable", errors)

    def test_plain_once(self):
        status, output, _, _ = self.run_cli(["--once"])
        self.assertEqual(status, 0)
        self.assertIn("demo", output)
        self.assertIn("Views", output)

    def test_bad_config_no_traceback(self):
        with patch(
            "lma_tools.views.cli.load_config", side_effect=ValueError("bad")
        ):
            with redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(main(["--once"]), 1)
        self.assertEqual(errors.getvalue(), "Error: bad\n")
