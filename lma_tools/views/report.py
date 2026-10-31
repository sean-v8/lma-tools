"""Pure normalization, numeric sorting, and recording summaries."""

import re
from dataclasses import dataclass
from datetime import date

from .fields import first, values


def parse_date(value):
    text = first(value)
    match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if match:
        try:
            return date.fromisoformat(match.group())
        except ValueError:
            pass
    return None


def runtime_seconds(value):
    text = first(value).strip()
    if not re.fullmatch(r"\d+:\d{2}(?::\d{2})?", text):
        return None
    parts = [int(part) for part in text.split(":")]
    if any(part >= 60 for part in parts[1:]):
        return None
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def duration(seconds):
    hours, remainder = divmod(int(seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


@dataclass
class Recording:
    metadata: dict
    views: int | None
    daily: float | None
    runtime: int | None
    date: date | None

    @property
    def identifier(self):
        return self.metadata["identifier"]


def recordings(docs, view_counts, today=None):
    today = today or date.today()
    result = []
    for doc in docs:
        identifier = doc["identifier"]
        if (
            identifier.startswith("fav-")
            or doc.get("mediatype") == "collection"
        ):
            continue
        views = view_counts.get(identifier)
        uploaded = parse_date(doc.get("publicdate"))
        daily = None
        if uploaded and uploaded <= today and views is not None:
            daily = views / max(1, (today - uploaded).days)
        result.append(
            Recording(
                doc,
                views,
                daily,
                runtime_seconds(doc.get("runtime")),
                parse_date(doc.get("date")) or parse_date(identifier),
            )
        )
    return result


def sorted_recordings(rows, field, reverse=False):
    def key(row):
        if field in ("views", "daily", "runtime", "date"):
            return getattr(row, field)
        if field == "year":
            text = first(row.metadata.get(field))
            return int(text) if text.isdigit() else None
        return first(row.metadata.get(field)).casefold() or None

    present = [row for row in rows if key(row) is not None]
    absent = [row for row in rows if key(row) is None]
    present.sort(key=lambda row: row.identifier)
    return sorted(present, key=key, reverse=reverse) + absent


def summarize(rows, exclude_zero_views=True):
    known = [row for row in rows if row.views is not None]
    averaged = [
        row for row in known if not exclude_zero_views or row.views > 0
    ]
    daily = [row.daily for row in averaged if row.daily is not None]
    runtimes = [row.runtime for row in rows if row.runtime is not None]
    dates, shows, performances = set(), set(), set()
    for row in rows:
        if row.date is None:
            continue
        dates.add(row.date)
        venues = tuple(str(v) for v in values(row.metadata.get("venue")))
        if not venues:
            continue
        shows.add((row.date, venues))
        collections = tuple(
            sorted(
                str(c)
                for c in values(row.metadata.get("collection"))
                if not str(c).startswith("fav-")
            )
        )
        if collections:
            performances.add((collections, row.date, venues))
    total_runtime = sum(runtimes)
    return {
        "tapes": len(rows),
        "performances": len(performances),
        "shows": len(shows),
        "dates": len(dates),
        "views": sum(row.views for row in known),
        "unknown_views": len(rows) - len(known),
        "zero_views": sum(row.views == 0 for row in known),
        "average_views": (
            sum(row.views for row in averaged) / len(averaged)
            if averaged
            else None
        ),
        "average_daily": sum(daily) / len(daily) if daily else None,
        "runtime_count": len(runtimes),
        "runtime_seconds": total_runtime,
        "average_runtime_seconds": (
            total_runtime / len(runtimes) if runtimes else None
        ),
        "runtime_days": total_runtime / 86400,
    }


def render(rows, summary, sort, timing=""):
    """Render the original script's rows, summary labels, and spacing."""
    width = max((len(row.identifier) for row in rows), default=0)
    lines = []
    for row in rows:
        if sort == "runtime":
            display = duration(row.runtime) if row.runtime is not None else "?"
        elif sort == "daily":
            display = f"{row.daily:.2f}" if row.daily is not None else "?"
        else:
            display = str(row.views) if row.views is not None else "?"
        lines.append(f"{row.identifier:<{width + 2}}{display:>4}")
    lines.extend(
        [
            "",
            f"         Tapes: {summary['tapes']}",
            f"  Performances: {summary['performances']}",
            f"         Shows: {summary['shows']}",
            f"         Dates: {summary['dates']}",
        ]
    )
    if summary["average_views"] is not None:
        daily = summary["average_daily"]
        daily_text = f"{daily:.2f}" if daily is not None else "?"
        lines.extend(
            [
                "",
                f"         Views: {summary['views']}",
                f"       Average: {summary['average_views']:.1f}",
                f"         Daily: {daily_text}",
            ]
        )
    if summary["runtime_seconds"]:
        total = duration(summary["runtime_seconds"])
        average = duration(summary["average_runtime_seconds"])
        lines.extend(
            [
                "",
                f"       Runtime: {total}",
                f"       Average: {average:>{len(total)}}",
                f"          Days: {summary['runtime_days']:>{len(total)}.2f}",
            ]
        )
    if timing:
        lines.extend(["", timing])
    lines.extend(["", "*" * (width + 6)])
    return "\n".join(lines)
