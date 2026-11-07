"""Submit the final derive without mistaking a duplicate for upload failure."""

from requests import HTTPError

from .core import check_response


def queue_final_derive(session, identifier, journal):
    try:
        response = session.submit_task(
            identifier,
            "derive.php",
            comment="lma-tools: selected uploads complete",
            request_kwargs={"timeout": (30, 60)},
        )
    except HTTPError as error:
        response = error.response
        if response is None:
            raise
    try:
        try:
            result = response.json()
        except ValueError:
            result = None
        duplicate = (
            response.status_code in (200, 400, 409)
            and isinstance(result, dict)
            and result.get("success") is False
            and result.get("error") == "derive.php already queued/running"
        )
        if duplicate:
            journal.append("derive_already_pending")
            print("Uploads verified; derivation already queued/running.")
            return
        check_response(response)
        journal.append("derive_queued")
    finally:
        response.close()
