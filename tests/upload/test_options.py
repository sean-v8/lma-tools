"""Public upload options have distinct purposes and matching completion."""

import io
from contextlib import redirect_stderr, redirect_stdout
from unittest import TestCase
from unittest.mock import patch

from lma_tools.upload import cli


class OptionTests(TestCase):
    def test_removed_alias_and_csv_override_are_rejected(self):
        for arguments in (["--reset-review"], ["--collections", "map.csv"]):
            with self.subTest(args=arguments), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    cli.parser().parse_args(arguments)
                self.assertEqual(error.exception.code, 2)

    def test_fresh_metadata_and_supported_overrides_remain_available(self):
        args = cli.parser().parse_args(
            ["--fresh-metadata", "--collection", "Artist", "--retries", "2"]
        )
        self.assertTrue(args.fresh_metadata)
        self.assertEqual(args.collection, "Artist")
        self.assertEqual(args.retries, 2)

    def test_completion_omits_removed_options(self):
        output = io.StringIO()
        with (
            redirect_stdout(output),
            patch.object(cli, "load_config", return_value={"shows_root": ""}),
            patch.object(cli, "ArchiveSession") as archive,
        ):
            self.assertEqual(cli.main(["--complete=--"]), 0)
        archive.assert_not_called()
        options = output.getvalue().splitlines()
        self.assertIn("--fresh-metadata", options)
        self.assertNotIn("--reset-review", options)
        self.assertNotIn("--collections", options)
