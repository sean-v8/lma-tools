"""Unattended runs accept defaults without inventing replacement consent."""

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from lma_tools.upload import batch, cli, prepare, remote

from .helpers import ID, NOTES, metadata_fixture


class UnattendedTests(unittest.TestCase):
    def test_batch_yes_processes_all_without_a_selection_prompt(self):
        args = cli.parser().parse_args(["--yes", "-p"])
        outputs = [(Path("/root"), Path("/show"))] * 2
        with (
            patch("builtins.input", side_effect=AssertionError("prompt")),
            patch.object(cli, "main", return_value=0) as run,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                batch.review_outputs(["--yes", "-p"], args, {}, outputs, None),
                0,
            )
        self.assertEqual(run.call_count, 2)

    def test_full_yes_upload_skips_confirmation_and_forwards_review_choice(
        self,
    ):
        with metadata_fixture(self) as (_, item, _, args):
            with patch("builtins.input", side_effect=AssertionError("prompt")):
                self.assertEqual(cli.main([*args, "--yes"]), 0)
            self.assertEqual(item.upload_file.call_count, 3)
            self.assertTrue(cli.edit_review.call_args.kwargs["yes"])

    def test_metadata_yes_still_validates_generated_defaults(self):
        metadata = cli.parse_notes(NOTES, ID)
        metadata.update(collection="ArtistCollection", runtime="1:00:00")
        with (
            patch(
                "lma_tools.upload.metadata_review.snapshot", return_value={}
            ),
            patch(
                "lma_tools.upload.ui.edit_document", side_effect=AssertionError
            ),
            redirect_stdout(io.StringIO()),
        ):
            _, result = cli.edit_review(ID, metadata, history=False, yes=True)
            self.assertEqual(result["creator"], "Artist")
            metadata["collection"] = ""
            with self.assertRaisesRegex(ValueError, "collection"):
                cli.edit_review(ID, metadata, history=False, yes=True)

    def test_preparation_yes_validates_before_applying_without_editor(self):
        for valid in (True, False):
            with tempfile.TemporaryDirectory() as temporary:
                plan = {"tracks": [{}]}
                with (
                    self.subTest(valid=valid),
                    patch.object(prepare, "build_plan", return_value=plan),
                    patch.object(prepare, "review_text", return_value="plan"),
                    patch.object(
                        prepare,
                        "validate_plan",
                        return_value=plan,
                        side_effect=None if valid else ValueError("ambiguous"),
                    ) as validate,
                    patch.object(
                        prepare, "apply_plan", return_value=plan
                    ) as apply,
                    patch.object(
                        prepare.ui, "edit_document", side_effect=AssertionError
                    ),
                ):
                    if valid:
                        self.assertEqual(
                            prepare.prepare_show(temporary, {}, yes=True), plan
                        )
                        apply.assert_called_once()
                    else:
                        with self.assertRaisesRegex(ValueError, "ambiguous"):
                            prepare.prepare_show(temporary, {}, yes=True)
                        apply.assert_not_called()
                    validate.assert_called_once()

    def test_yes_never_selects_cleanup_or_replaces_without_force(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "track.flac"
            path.write_bytes(b"new")
            entries = [
                dict(name=name, source="original", md5="0" * 32, size="3")
                for name in (path.name, "obsolete.flac")
            ]
            item = Mock(files=entries)
            journal = Mock()
            journal.resume_state.return_value = None
            with (
                patch("builtins.input", side_effect=AssertionError("prompt")),
                redirect_stdout(io.StringIO()),
            ):
                with self.assertRaisesRegex(ValueError, "--force"):
                    remote.review_remote(
                        item, [path], journal, clean=True, unattended=True
                    )
                replacements, deletions = remote.review_remote(
                    item,
                    [path],
                    journal,
                    clean=True,
                    unattended=True,
                    force=True,
                )
            self.assertEqual(set(replacements), {path.name})
            self.assertEqual(deletions, [])

    def test_yes_clean_is_rejected_instead_of_silently_deleting(self):
        with self.assertRaisesRegex(ValueError, "interactive deletion"):
            cli.main(["--yes", "--clean"])
