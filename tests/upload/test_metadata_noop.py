"""A metadata no-op must not bypass unrelated response failures."""

import unittest
from unittest.mock import patch

from requests import HTTPError, Response

from lma_tools.upload import cli
from lma_tools.upload.core import check_response
from tests.upload.helpers import metadata_fixture


class MetadataNoopTests(unittest.TestCase):
    def response(self, status, body):
        response = Response()
        response.status_code = status
        response._content = body.encode()
        return response

    def test_exact_noop_is_accepted_only_for_metadata(self):
        response = self.response(
            400, '{"success":false,"error":"no changes to _meta.xml"}'
        )
        check_response(response, metadata_noop=True)
        with self.assertRaises(HTTPError):
            check_response(response)

    def test_other_failures_still_stop(self):
        for status, body in (
            (400, '{"success":false,"error":"permission denied"}'),
            (403, '{"success":false,"error":"no changes to _meta.xml"}'),
            (400, "invalid response"),
            (400, '{"success":true,"error":"no changes to _meta.xml"}'),
        ):
            with self.subTest(status=status, body=body):
                with self.assertRaises(HTTPError):
                    check_response(
                        self.response(status, body), metadata_noop=True
                    )
        with self.assertRaises(ValueError):
            check_response(
                self.response(200, '{"success":false,"error":"failed"}'),
                metadata_noop=True,
            )

    def test_noop_continues_through_audit_and_derive(self):
        with metadata_fixture(self) as (_, item, session, args):
            item.modify_metadata.return_value = self.response(
                400, '{"success":false,"error":"no changes to _meta.xml"}'
            )
            with patch.object(cli, "upload_files"):
                self.assertEqual(cli.main(args), 0)
            cli.audit_upload.assert_called_once()
            session.submit_task.assert_called_once()
            self.assertEqual(
                session.submit_task.call_args.args[1], "derive.php"
            )
