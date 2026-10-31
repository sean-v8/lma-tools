"""Search state and per-session metadata/view caches."""

from collections import OrderedDict

from .client import ArchiveError
from .fields import first, query_text, values
from .report import recordings, sorted_recordings, summarize
from .timing import TimingRecorder


class SearchService:
    def __init__(self, client, timing=None):
        inherited = getattr(client, "timing", None)
        self.timing = (
            timing
            if timing is not None
            else (
                inherited
                if isinstance(inherited, TimingRecorder)
                else TimingRecorder()
            )
        )
        self.client = client
        self.search_cache = OrderedDict()
        self.view_cache = {}
        self.metadata_cache = {}
        self.warnings = []

    def search(self, clauses, refresh=False, reuse=True):
        cached = (
            not refresh and reuse and query_text(clauses) in self.search_cache
        )
        with self.timing.span("search", cached=cached) as fields:
            docs = self._search(clauses, refresh, reuse)
            fields["records"] = len(docs)
            return docs

    def _search(self, clauses, refresh=False, reuse=True):
        query = query_text(clauses)
        if refresh:
            self.search_cache.clear()
            self.view_cache.clear()
            self.metadata_cache.clear()
        if not reuse or query not in self.search_cache:
            self.search_cache[query] = list(self.client.search(query))
            if len(self.search_cache) > 8:
                self.search_cache.popitem(last=False)
        self.search_cache.move_to_end(query)
        return self.search_cache[query]

    def metadata_value(self, doc, field):
        value = doc.get(field)
        if value is None and field == "uploader":
            identifier = doc["identifier"]
            if identifier not in self.metadata_cache:
                self.metadata_cache[identifier] = self.client.metadata(
                    identifier
                )
            value = self.metadata_cache[identifier].get(field)
            if value is not None:
                doc[field] = value
        return value

    def list_values(self, clauses, field, first_only=False):
        result = set()
        for doc in self.search(clauses):
            entries = values(self.metadata_value(doc, field))
            if first_only:
                entries = entries[:1]
            result.update(
                str(value).split("T")[0] if field == "date" else str(value)
                for value in entries
            )
        return sorted(result)

    def favorites(self, clauses, sort="identifier"):
        # A full cached search already contains collection memberships.
        # Only ask Archive for the favorites subset when it is not cached.
        if query_text(clauses) in self.search_cache:
            docs = self.search(clauses)
        else:
            query = (
                clauses
                if "collection:fav*" in clauses
                else [*clauses, "collection:fav*"]
            )
            docs = self.search(query)
        result = []
        for doc in sorted(docs, key=lambda item: first(item.get(sort))):
            favorites = [
                str(value)[4:]
                for value in values(doc.get("collection"))
                if str(value).startswith("fav-")
            ]
            if favorites:
                result.append((doc["identifier"], favorites))
        return result

    def clear(self):
        self.search_cache.clear()
        self.view_cache.clear()
        self.metadata_cache.clear()
        self.warnings = []

    def report(self, *args, **kwargs):
        self.timing.clear()
        with self.timing.span("report") as fields:
            rows, summary = self._report(*args, **kwargs)
            fields["records"] = len(rows)
            fields["unknown_views"] = summary["unknown_views"]
            return rows, summary

    def _report(
        self,
        clauses,
        config,
        sort,
        reverse,
        refresh=False,
        on_progress=None,
        on_start=None,
        reuse_search=True,
    ):
        docs = self.search(clauses, refresh, reuse_search)
        self.warnings = []
        included = [
            doc
            for doc in docs
            if not doc["identifier"].startswith("fav-")
            and first(doc.get("mediatype")) != "collection"
        ]
        included.sort(key=lambda doc: first(doc.get("publicdate")))
        if on_start is not None:
            on_start(included)
        completed = 0
        if on_progress is not None:
            for doc in included:
                identifier = doc["identifier"]
                if identifier in self.view_cache:
                    completed += 1
                    on_progress(
                        identifier,
                        self.view_cache[identifier],
                        completed,
                        len(included),
                    )
        missing = [
            doc["identifier"]
            for doc in included
            if doc["identifier"] not in self.view_cache
        ]
        self.timing.write(
            "views_cache",
            cached=len(included) - len(missing),
            pending=len(missing),
        )
        counts = self.client.view_counts(missing) if missing else ()
        failures = {}
        for identifier, value in counts:
            completed += 1
            if on_progress is not None:
                on_progress(identifier, value, completed, len(included))
            if isinstance(value, ArchiveError):
                failures.setdefault(str(value), []).append(identifier)
            else:
                self.view_cache[identifier] = value
        for reason, identifiers in failures.items():
            examples = ", ".join(identifiers[:2])
            if len(identifiers) > 2:
                examples += ", ..."
            self.warnings.append(
                f"{len(identifiers)} recording(s): {reason} ({examples})"
            )
        if sort == "uploader":
            for doc in included:
                self.metadata_value(doc, sort)
        rows = recordings(included, self.view_cache)
        return (
            sorted_recordings(rows, sort, reverse),
            summarize(rows, config.exclude_zero_views),
        )
