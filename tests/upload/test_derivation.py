"""Duplicate derives are harmless; other server failures stay visible."""

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock

from requests import HTTPError

from lma_tools.upload.derivation import queue_final_derive
from tests.upload.helpers import response


class DerivationTests(unittest.TestCase):
    def test_duplicate_responses_are_accepted_including_sdk_http_errors(self):
        for status in (200, 400, 409):
            with self.subTest(status=status), redirect_stdout(io.StringIO()):
                result = response(
                    status,
                    content=(
                        b'{"success":false,"error":'
                        b'"derive.php already queued/running"}'
                    ),
                )
                session, journal = Mock(), Mock()
                if status == 200:
                    session.submit_task.return_value = result
                else:
                    session.submit_task.side_effect = HTTPError(
                        response=result
                    )
                queue_final_derive(session, "example", journal)
                journal.append.assert_called_once_with(
                    "derive_already_pending"
                )

    def test_authentication_and_unknown_application_errors_are_not_ignored(
        self,
    ):
        for status, message in (
            (403, "derive.php already queued/running"),
            (200, "Permission denied"),
            (400, "Cannot submit derive task"),
        ):
            with self.subTest(status=status, message=message):
                result = response(status)
                result._content_consumed = True
                result.json = lambda: {"success": False, "error": message}
                session, journal = Mock(), Mock()
                session.submit_task.return_value = result
                with self.assertRaises((HTTPError, ValueError)):
                    queue_final_derive(session, "example", journal)
                journal.append.assert_not_called()
