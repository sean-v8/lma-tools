"""Optional in-memory diagnostics for the interactive debug display."""

from contextlib import contextmanager
from time import perf_counter

from urllib3.exceptions import MaxRetryError
from urllib3.util.retry import Retry


class TimingRecorder:
    def __init__(self, enabled=False):
        self.enabled = enabled
        self.events = []
        self.request_id = 0

    def clear(self):
        self.events.clear()
        self.request_id = 0

    def write(self, event, **fields):
        if self.enabled:
            self.events.append({"event": event, **fields})

    @contextmanager
    def span(self, event, **fields):
        if not self.enabled:
            yield fields
            return
        started = perf_counter()
        try:
            yield fields
        except BaseException as exc:
            fields["outcome"] = type(exc).__name__
            raise
        else:
            fields["outcome"] = "ok"
        finally:
            self.write(event, seconds=perf_counter() - started, **fields)

    def render(self):
        if not self.enabled:
            return ""
        requests = [e for e in self.events if e["event"] == "request"]
        retries = [e for e in self.events if e["event"] == "retry_scheduled"]
        cache = next(
            (e for e in self.events if e["event"] == "views_cache"), {}
        )
        lines = []
        for kind, label in (
            ("search", "Search"),
            ("views", "Views"),
            ("metadata", "Metadata"),
        ):
            group = [e for e in requests if e["kind"] == kind]
            if kind == "metadata" and not group:
                continue
            ids = {e["request"] for e in group}
            count = len(group) + sum(e["request"] in ids for e in retries)
            seconds = sum(e["seconds"] for e in group)
            detail = f"{count} request" + ("" if count == 1 else "s")
            if kind == "views":
                detail += f", {cache.get('pending', 0)} items"
                if cache.get("cached"):
                    detail += f", {cache['cached']} cached"
            elif kind == "search" and not group:
                detail = "cached"
            lines.append(f"{label:>14}: {seconds:.3f}s ({detail})")
        causes = [
            f"HTTP {e['status']}"
            if e.get("status")
            else e.get("cause", "error")
            for e in retries
        ]
        details = f" ({', '.join(sorted(set(causes)))})" if causes else ""
        lines.append(f"       Retries: {len(retries)}{details}")
        splits = [e for e in self.events if e["event"] == "batch_reduced"]
        details = ""
        if splits:
            statuses = ", ".join(
                str(e) for e in sorted({event["status"] for event in splits})
            )
            details = f" (HTTP {statuses}; batch size {splits[-1]['size']})"
        lines.append(f"        Splits: {len(splits)}{details}")
        report = next((e for e in self.events if e["event"] == "report"), {})
        lines.append(f"         Total: {report.get('seconds', 0):.3f}s")
        return "\n".join(lines)


class TimingRetry(Retry):
    """Observe urllib3 retries without changing its retry policy."""

    def __init__(self, *args, timing=None, **kwargs):
        self.timing = timing if timing is not None else TimingRecorder()
        super().__init__(*args, **kwargs)

    def new(self, **kwargs):
        return super().new(timing=self.timing, **kwargs)

    def increment(
        self,
        method=None,
        url=None,
        response=None,
        error=None,
        _pool=None,
        _stacktrace=None,
    ):
        fields = {
            "request": self.timing.request_id,
            "retry": len(self.history) + 1,
            "status": response.status if response is not None else None,
            "cause": type(error).__name__ if error is not None else "HTTP",
        }
        try:
            result = super().increment(
                method, url, response, error, _pool, _stacktrace
            )
        except MaxRetryError:
            self.timing.write("retry_exhausted", **fields)
            raise
        self.timing.write("retry_scheduled", **fields)
        return result

    def sleep(self, response=None):
        with self.timing.span("retry_wait", request=self.timing.request_id):
            super().sleep(response)
