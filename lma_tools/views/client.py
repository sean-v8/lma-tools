"""Archive HTTP boundary: pagination, retries, and schema checks."""

from dataclasses import dataclass
from urllib.parse import quote, urlsplit

import requests
from requests.adapters import HTTPAdapter

from .fields import FIELDS
from .timing import TimingRecorder, TimingRetry


class ArchiveError(Exception):
    """A request failed or returned an unexpected response."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


@dataclass
class Recovery:
    remaining: int = 24
    failed_singles: int = 0
    stopped: ArchiveError | None = None


class ArchiveClient:
    def __init__(
        self, timeout=30.0, session=None, view_batch_size=100, timing=None
    ):
        self.timing = timing if timing is not None else TimingRecorder()
        self.timeout = timeout
        self.session = session if session is not None else requests.Session()
        self.session.headers.update({"User-Agent": "lma-views"})
        retry = TimingRetry(
            timing=self.timing,
            total=3,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET",),
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        # Split oversized/failing view batches instead of retrying the same
        # URL four times. Connection establishment gets one retry only.
        view_retry = TimingRetry(
            timing=self.timing,
            total=1,
            connect=1,
            read=0,
            status=0,
            raise_on_status=False,
        )
        self.session.mount(
            "https://be-api.us.archive.org/views/",
            HTTPAdapter(max_retries=view_retry),
        )
        self.view_batch_size = view_batch_size

    def close(self):
        self.session.close()

    def _get(self, url, **kwargs):
        self.timing.request_id += 1
        path = urlsplit(url).path
        kind = (
            "views"
            if "/views/" in path
            else "search"
            if "advancedsearch" in path
            else "metadata"
        )
        details = {"request": self.timing.request_id, "kind": kind}
        if kind == "views":
            details["items"] = path.rsplit("/", 1)[1].count(",") + 1
        if kind == "search":
            details["page"] = kwargs.get("params", {}).get("page")
        with self.timing.span("request", **details) as fields:
            try:
                return self._request(url, fields, **kwargs)
            except ArchiveError as exc:
                fields["status"] = exc.status
                raise

    def _request(self, url, fields, **kwargs):
        try:
            response = self.session.get(url, timeout=self.timeout, **kwargs)
            fields["status"] = response.status_code
            response.raise_for_status()
            data = response.json()
        except requests.HTTPError as exc:
            status = (
                exc.response.status_code if exc.response is not None else None
            )
            raise ArchiveError(
                f"HTTP {status or 'error'} from {urlsplit(url).netloc}", status
            ) from exc
        except requests.Timeout as exc:
            raise ArchiveError(
                f"Timed out contacting {urlsplit(url).netloc}"
            ) from exc
        except requests.RequestException as exc:
            raise ArchiveError(
                f"Request failed contacting {urlsplit(url).netloc}"
            ) from exc
        except ValueError as exc:
            raise ArchiveError(
                f"Invalid JSON from {urlsplit(url).netloc}"
            ) from exc
        if not isinstance(data, dict):
            raise ArchiveError("Expected a JSON object from Archive")
        return data

    def search(self, query):
        if not query.strip():
            raise ValueError("Provide a query before searching")
        page = 1
        seen = set()
        while True:
            data = self._get(
                "https://archive.org/advancedsearch.php",
                params={
                    "q": query,
                    "fl[]": FIELDS,
                    "rows": 500,
                    "page": page,
                    "output": "json",
                    "sort[]": "identifier asc",
                },
            )
            try:
                result = data["response"]
                docs = result["docs"]
                count = result["numFound"]
                if not isinstance(docs, list) or type(count) is not int:
                    raise TypeError
                if count < 0:
                    raise ValueError
                for doc in docs:
                    if not isinstance(doc, dict):
                        raise TypeError
                    identifier = doc["identifier"]
                    if not isinstance(identifier, str) or not identifier:
                        raise TypeError
                    if identifier not in seen:
                        seen.add(identifier)
                        yield doc
            except (KeyError, TypeError, ValueError) as exc:
                raise ArchiveError("Invalid Archive search response") from exc
            if not docs and (page - 1) * 500 < count:
                raise ArchiveError("Search ended before all pages arrived")
            if page * 500 >= count:
                return
            page += 1

    def metadata(self, identifier):
        data = self._get(
            "https://archive.org/metadata/" + quote(identifier, safe="")
        )
        metadata = data.get("metadata")
        if not isinstance(metadata, dict):
            raise ArchiveError(f"Invalid metadata for {identifier}")
        return metadata

    def view_counts(self, identifiers):
        """Fetch small batches with bounded recovery and outage detection."""
        state = Recovery()
        batch = []
        length = 0
        for identifier in dict.fromkeys(identifiers):
            encoded = quote(identifier, safe="")
            if batch and (
                len(batch) >= self.view_batch_size
                or length + len(encoded) + 1 > 1500
            ):
                yield from self._view_batch(batch, state)
                batch, length = [], 0
            batch.append((identifier, encoded))
            length += len(encoded) + 1
        if batch:
            yield from self._view_batch(batch, state)

    def _view_batch(self, batch, state):
        if state.stopped is not None:
            for identifier, _ in batch:
                yield identifier, state.stopped
            return
        url = "https://be-api.us.archive.org/views/v1/short/"
        try:
            data = self._get(url + ",".join(encoded for _, encoded in batch))
        except ArchiveError as exc:
            if (
                exc.status in (414, 502, 503, 504)
                and len(batch) > 1
                and state.remaining > 0
            ):
                middle = len(batch) // 2
                self.timing.write(
                    "batch_reduced",
                    items=len(batch),
                    size=middle,
                    status=exc.status,
                )
                self.view_batch_size = min(self.view_batch_size, middle)
                position = 0
                while position < len(batch):
                    if state.remaining == 0:
                        self.timing.write("recovery_budget_exhausted")
                        for identifier, _ in batch[position:]:
                            yield identifier, exc
                        return
                    size = self.view_batch_size
                    state.remaining -= 1
                    yield from self._view_batch(
                        batch[position : position + size], state
                    )
                    position += size
                return
            if len(batch) == 1:
                state.failed_singles += 1
            if state.failed_singles >= 3:
                state.stopped = ArchiveError(
                    "View service unavailable; stopped after three failed "
                    "single-item requests"
                )
            elif exc.status is None or exc.status in (401, 403, 429):
                state.stopped = exc
            if state.stopped is not None:
                self.timing.write("views_stopped", status=exc.status)
            for identifier, _ in batch:
                yield identifier, exc
            return
        state.failed_singles = 0
        for identifier, _ in batch:
            try:
                count = data[identifier]["all_time"]
                if type(count) is not int or count < 0:
                    raise ValueError
                yield identifier, count
            except (KeyError, TypeError, ValueError):
                yield (
                    identifier,
                    ArchiveError("Missing or invalid view-count entry"),
                )
