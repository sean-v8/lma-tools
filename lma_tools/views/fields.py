"""Search fields and original interactive shorthand."""

ALIASES = {
    "i": "identifier",
    "a": "collection",
    "c": "coverage",
    "d": "date",
    "n": "description",
    "r": "runtime",
    "t": "taper",
    "v": "venue",
    "y": "year",
    "u": "uploader",
}
FIELDS = (
    "identifier",
    "mediatype",
    "collection",
    "coverage",
    "creator",
    "date",
    "description",
    "lineage",
    "runtime",
    "scanner",
    "source",
    "subject",
    "taper",
    "title",
    "transferer",
    "type",
    "venue",
    "year",
    "uploader",
    "publicdate",
    "addeddate",
)
SORT_FIELDS = (*FIELDS, "views", "daily")


def field_name(value):
    name = ALIASES.get(value, value)
    if name not in SORT_FIELDS:
        raise ValueError(f"Unknown field: {value}")
    return name


def values(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def first(value):
    return str(next(iter(values(value)), ""))


def query_text(clauses):
    return " AND ".join(f"({clause})" for clause in clauses if clause.strip())


def quoted_clause(field, value):
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'{field}:"{escaped}"'
