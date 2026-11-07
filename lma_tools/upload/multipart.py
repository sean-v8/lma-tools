"""Stream resumable IAS3 parts directly from the original file."""

import base64
import hashlib
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit

from .progress import UploadCancelled, check_cancelled
from .recovery import remote_state, retryable, unchanged

MIB = 1024 * 1024


@dataclass(frozen=True)
class MultipartOptions:
    enabled: bool = True
    part_size_mib: int = 16

    @property
    def part_bytes(self):
        return self.part_size_mib * MIB


def load_options(data):
    if not isinstance(data, dict):
        raise ValueError("upload.resume must be a TOML table")
    if set(data) - {"enabled", "part_size_mib"}:
        raise ValueError("Unknown upload.resume settings")
    options = MultipartOptions(**data)
    if type(options.enabled) is not bool:
        raise ValueError("resume.enabled must be true or false")
    if (
        type(options.part_size_mib) is not int
        or not 5 <= options.part_size_mib <= 1024
    ):
        raise ValueError("resume.part_size_mib must be 5 through 1024")
    return options


class MultipartError(ValueError):
    def __init__(self, code, message, http_status=None):
        self.code = code
        self.http_status = http_status
        super().__init__(f"Multipart {code}: {message}")


class AssemblyPending(ValueError):
    """Completion may have succeeded; never restart the file blindly."""


def xml_text(root, tag):
    node = root.find("{*}" + tag)
    return node.text if node is not None and node.text else ""


def query_string(params):
    query = urlencode({k: v for k, v in params.items() if k != "uploads"})
    if "uploads" in params:
        return "uploads" + ("&" + query if query else "")
    return query


class PartReader:
    """A bounded, rewindable view; no part is copied to disk or RAM."""

    _lma_multipart = True

    def __init__(self, path, offset, size, progress, cancel):
        self.handle = path.open("rb")
        self.offset, self.size = offset, size
        self.progress, self.cancel = progress, cancel
        self.on_sent = None
        self.ack_status = "awaiting ack"
        self.sent = False
        self.seek(0)

    def __len__(self):
        return self.size

    def tell(self):
        return self.handle.tell() - self.offset

    def seek(self, offset, whence=0):
        position = (
            offset
            if whence == 0
            else self.tell() + offset
            if whence == 1
            else self.size + offset
        )
        if not 0 <= position <= self.size:
            raise ValueError("Seek outside multipart boundary")
        self.handle.seek(self.offset + position)
        return position

    def restart(self):
        self.seek(0)
        self.sent = False
        self.progress.resume(self.offset)

    def read(self, size=-1):
        check_cancelled(self.cancel)
        remaining = self.size - self.tell()
        count = remaining if size < 0 else min(size, remaining)
        data = self.handle.read(count)
        if not data and remaining:
            raise ValueError("File truncated during multipart transfer")
        self.progress.advance(len(data))
        if not self.sent and self.tell() == self.size:
            self.sent = True
            self.progress.status(self.ack_status)
            if self.on_sent is not None:
                self.on_sent()
        return data

    def close(self):
        self.handle.close()


class MultipartUpload:
    def __init__(
        self,
        session,
        identifier,
        path,
        digest,
        size,
        stamp,
        journal,
        options,
        policy,
        progress,
        cancel=None,
        queue_derive=True,
    ):
        self.queue_derive = queue_derive
        self.session, self.identifier, self.path = session, identifier, path
        self.digest, self.size, self.stamp = digest, size, stamp
        self.options, self.policy = options, policy
        self.progress, self.cancel = progress, cancel
        self.url = (
            "https://s3.us.archive.org/"
            + quote(identifier, safe="")
            + "/"
            + quote(path.name, safe="")
        )
        if not journal.enabled:
            raise ValueError("File resume requires upload logging.")
        self.journal = journal
        self.signature = {
            "identifier": identifier,
            "name": path.name,
            "md5": digest,
            "size": size,
        }
        self.state = None

    def save(self):
        self.journal.save_resume(self.path.name, self.state)

    def wait(self, attempt):
        delay = min(
            self.policy.max_delay, self.policy.delay * 2 ** min(attempt, 20)
        )
        if self.cancel is None:
            time.sleep(delay)
        elif self.cancel.wait(delay):
            raise UploadCancelled("Multipart wait cancelled")
        check_cancelled(self.cancel)

    def request(self, method, params, data=None, headers=None):
        """Retry parts under the same ID; keep credentials on trusted hosts."""
        operation = (
            "initiate"
            if "uploads" in params
            else f"part {params['partNumber']}"
            if "partNumber" in params
            else "list parts"
            if method == "GET"
            else "complete"
        )
        for attempt in range(self.policy.retries + 1):
            check_cancelled(self.cancel)
            unchanged(self.path, self.size, self.stamp)
            url = self.url + "?" + query_string(params)
            try:
                for _ in range(6):
                    if isinstance(data, PartReader):
                        data.restart()
                    response = self.session.request(
                        method,
                        url,
                        data=data,
                        headers={
                            "Authorization": (
                                f"LOW {self.session.access_key}:"
                                f"{self.session.secret_key}"
                            ),
                            "x-archive-queue-derive": "0",
                            **(headers or {}),
                        },
                        allow_redirects=False,
                        # urllib3 uses connect timeout while writing bodies.
                        timeout=(300 if method == "PUT" else 30, 300),
                    )
                    try:
                        if method == "GET" and response.status_code in (
                            405,
                            501,
                        ):
                            raise MultipartError(
                                "NotImplemented", "ListParts unavailable"
                            )
                        if response.status_code in (307, 308):
                            target = urljoin(url, response.headers["Location"])
                            parsed = urlsplit(target)
                            if (
                                parsed.scheme != "https"
                                or not (parsed.hostname or "").endswith(
                                    ".archive.org"
                                )
                                or parsed.port not in (None, 443)
                                or parsed.username
                                or parsed.password
                            ):
                                raise MultipartError(
                                    "Redirect", "Untrusted upload redirect"
                                )
                            query = dict(parse_qsl(parsed.query))
                            query.update(params)
                            url = parsed._replace(
                                query=query_string(query), fragment=""
                            ).geturl()
                            continue
                        root = None
                        if response.content.lstrip().startswith(b"<"):
                            try:
                                root = ET.fromstring(response.content)
                            except ET.ParseError:
                                pass
                        if (
                            root is not None
                            and root.tag.split("}")[-1] == "Error"
                        ):
                            code = xml_text(root, "Code") or "Unknown"
                            if (
                                response.status_code >= 500
                                and code != "NotImplemented"
                            ):
                                response.raise_for_status()
                            raise MultipartError(
                                code,
                                xml_text(root, "Message"),
                                response.status_code,
                            )
                        response.raise_for_status()
                        if not 200 <= response.status_code < 300:
                            raise MultipartError(
                                "HTTP",
                                f"Unexpected status {response.status_code}",
                            )
                        return root, dict(response.headers)
                    finally:
                        response.close()
                raise MultipartError("Redirect", "Too many upload redirects")
            except Exception as exc:
                transient = retryable(exc) or (
                    isinstance(exc, MultipartError)
                    and (
                        exc.code
                        in {"SlowDown", "RequestTimeout", "InternalError"}
                        or (
                            operation == "initiate"
                            and exc.code == "NoSuchBucket"
                        )
                    )
                )
                status = getattr(
                    getattr(exc, "response", None), "status_code", None
                )
                if status is None:
                    status = getattr(exc, "http_status", None)
                detail = type(exc).__name__
                message = " ".join(str(exc).splitlines())
                for secret in (
                    self.session.access_key,
                    self.session.secret_key,
                ):
                    if secret:
                        message = message.replace(secret, "<redacted>")
                if status is not None:
                    detail += f" HTTP {status}"
                if isinstance(exc, MultipartError):
                    detail += f" ({exc.code})"
                self.journal.append(
                    "resume_request_failed",
                    name=self.path.name,
                    operation=operation,
                    attempt=attempt + 1,
                    error=type(exc).__name__,
                    http_status=status,
                    detail=message[:2000],
                )
                if not transient or attempt == self.policy.retries:
                    raise
                self.progress.status(
                    "waiting for item"
                    if isinstance(exc, MultipartError)
                    and exc.code == "NoSuchBucket"
                    else "retrying part"
                )
                self.progress.message(
                    f"Multipart {operation}: {detail}; "
                    f"retry {attempt + 1}/{self.policy.retries}. "
                    + message[:300],
                    transient=True,
                )
                self.wait(attempt)

    def initiate(self):
        root, _ = self.request("POST", {"uploads": ""})
        upload_id = xml_text(root, "UploadId") if root is not None else ""
        if not upload_id:
            raise MultipartError("Initiate", "Missing UploadId")
        self.state = {
            "signature": self.signature,
            "upload_id": upload_id,
            "part_bytes": self.options.part_bytes,
            "parts": {},
            "phase": "uploading",
            "version": 1,
        }
        self.save()

    def load(self):
        state = self.journal.resume_state(self.path.name)
        if state is None:
            self.initiate()
            return True
        try:
            valid = (
                state["version"] == 1
                and state["signature"] == self.signature
                and isinstance(state["upload_id"], str)
                and state["upload_id"]
                and type(state["part_bytes"]) is int
                and 5 * MIB <= state["part_bytes"] <= 1024 * MIB
                and isinstance(state["parts"], dict)
                and state["phase"] in {"uploading", "completing", "assembling"}
            )
        except (OSError, ValueError, KeyError, TypeError):
            valid = False
        if not valid:
            raise ValueError(
                "Multipart checkpoint does not match this file/target or is "
                f"invalid; inspect {self.journal.path}. Nothing was discarded."
            )
        self.state = state
        return False

    def server_parts(self):
        """Reconcile when supported; completion still validates every MD5."""
        parts = {}
        marker = 0
        for _ in range(11):
            try:
                root, _ = self.request(
                    "GET",
                    {
                        "uploadId": self.state["upload_id"],
                        "part-number-marker": str(marker),
                        "max-parts": "1000",
                    },
                )
            except MultipartError as exc:
                if exc.code in {"NotImplemented", "MethodNotAllowed"}:
                    return None
                if exc.code == "NoSuchUpload":
                    self.progress.message(
                        "Multipart ID expired; starting a new upload."
                    )
                    self.initiate()
                    return {}
                raise
            if root is None or root.tag.split("}")[-1] != "ListPartsResult":
                raise MultipartError("ListParts", "Invalid part listing")
            for part in root.findall("{*}Part"):
                number = int(xml_text(part, "PartNumber"))
                if number in parts or not 1 <= number <= 10000:
                    raise MultipartError("ListParts", "Invalid part number")
                parts[number] = (
                    xml_text(part, "ETag").strip('"').lower(),
                    int(xml_text(part, "Size")),
                )
            if xml_text(root, "IsTruncated").lower() != "true":
                return parts
            next_marker = int(xml_text(root, "NextPartNumberMarker"))
            if next_marker <= marker:
                raise MultipartError("ListParts", "Invalid pagination")
            marker = next_marker
        raise MultipartError("ListParts", "Part listing exceeded 10000 parts")

    def part_digest(self, offset, size):
        digest = hashlib.md5()
        with self.path.open("rb") as handle:
            handle.seek(offset)
            while size:
                check_cancelled(self.cancel)
                chunk = handle.read(min(size, 128 * 1024))
                if not chunk:
                    raise ValueError("File truncated during multipart hashing")
                digest.update(chunk)
                size -= len(chunk)
        return digest

    def verify(self):
        self.progress.status("assembling")
        for check in range((self.policy.retries + 1) * self.policy.checks):
            check_cancelled(self.cancel)
            unchanged(self.path, self.size, self.stamp)
            # Assembly may temporarily expose a placeholder or old version.
            state, source = remote_state(
                self.session,
                self.identifier,
                self.path.name,
                self.digest,
                self.size,
                force=True,
            )
            if state == "match":
                self.journal.clear_resume(
                    self.path.name, self.state["upload_id"]
                )
                return {"verified_by": source, "multipart": True}
            if check + 1 < (self.policy.retries + 1) * self.policy.checks:
                self.wait(check)
        raise AssemblyPending(
            f"Assembly of {self.path.name} is not yet checksum-confirmed. "
            "Checkpoint retained; rerun to check without resending the file."
        )

    def run(self, *, defer_assembly=False):
        """Return None when deferring assembly; success still needs verify."""
        created = self.load()
        if self.state["phase"] == "assembling":
            self.progress.resume(self.size)
            self.progress.status("assembling")
            return None if defer_assembly else self.verify()
        if self.state["phase"] == "uploading":
            if not created:
                self.progress.status("resuming")
            server = {} if created else self.server_parts()
            part_bytes = self.state["part_bytes"]
            count = (self.size + part_bytes - 1) // part_bytes
            if count > 10000:
                raise ValueError(
                    "Increase multipart part_size_mib for this file"
                )
            for number in range(1, count + 1):
                unchanged(self.path, self.size, self.stamp)
                offset = (number - 1) * part_bytes
                size = min(part_bytes, self.size - offset)
                digest = self.part_digest(offset, size)
                expected = digest.hexdigest()
                known = self.state["parts"].get(str(number))
                present = (
                    server.get(number) == (expected, size)
                    if server is not None
                    else known == expected
                )
                if not present:
                    self.progress.status(f"part {number}/{count}")
                    body = PartReader(
                        self.path, offset, size, self.progress, self.cancel
                    )
                    body.ack_status = f"awaiting ack {number}/{count}"
                    body.on_sent = lambda: self.journal.append(
                        "resume_part_sent",
                        name=self.path.name,
                        number=number,
                        size=size,
                    )
                    try:
                        _, headers = self.request(
                            "PUT",
                            {
                                "uploadId": self.state["upload_id"],
                                "partNumber": str(number),
                            },
                            data=body,
                            headers={
                                "Content-MD5": base64.b64encode(
                                    digest.digest()
                                ).decode(),
                                "Content-Type": "application/octet-stream",
                            },
                        )
                    finally:
                        body.close()
                    etag = next(
                        (v for k, v in headers.items() if k.lower() == "etag"),
                        "",
                    )
                    if etag and etag.strip('"').lower() != expected:
                        raise MultipartError("Checksum", "Part ETag mismatch")
                self.state["parts"][str(number)] = expected
                self.save()
                self.progress.resume(offset + size)
            unchanged(self.path, self.size, self.stamp)
            self.state["phase"] = "completing"
            self.save()
        root = ET.Element("CompleteMultipartUpload")
        count = (self.size + self.state["part_bytes"] - 1) // self.state[
            "part_bytes"
        ]
        for number in range(1, count + 1):
            part = ET.SubElement(root, "Part")
            ET.SubElement(part, "PartNumber").text = str(number)
            ET.SubElement(part, "ETag").text = (
                '"' + self.state["parts"][str(number)] + '"'
            )
        self.progress.resume(self.size)
        self.progress.status("completing")
        try:
            self.request(
                "POST",
                {"uploadId": self.state["upload_id"]},
                data=ET.tostring(root),
                headers={
                    "Content-Type": "application/xml",
                    "x-archive-queue-derive": "1"
                    if self.queue_derive
                    else "0",
                },
            )
        except Exception as exc:
            if isinstance(exc, MultipartError) and exc.code in {
                "InvalidPart",
                "InvalidPartOrder",
                "EntityTooSmall",
            }:
                # A definite rejection permits reconciliation, unlike a lost
                # reply which may already have started asynchronous assembly.
                self.state.update(phase="uploading", parts={})
                self.save()
                self.progress.message(
                    "Completion refused; rerun to reconcile stored parts."
                )
                raise
            if not retryable(exc) and not (
                isinstance(exc, MultipartError) and exc.code == "NoSuchUpload"
            ):
                raise
            # A lost completion acknowledgement must not start a fresh ID.
            self.progress.message("Completion uncertain; verifying assembly.")
        self.state["phase"] = "assembling"
        self.save()
        self.progress.status("assembling")
        return None if defer_assembly else self.verify()
