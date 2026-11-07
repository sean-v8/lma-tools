"""Infer compact recording summaries without changing the original show notes.

Policy is descriptor-based, not a list of microphone models. Unknown sources
remain visible; only explicit instrument/direct-feed labels are summarized.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ROOM_HINTS = re.compile(
    r"\b(?:room|audience|ambient|ambience|overhead|nos|ortf|xy|din|blumlein|"
    r"spaced|split|stage\s+lip|cent(?:er|re)\s+stage)\b",
    re.I,
)
SPOT_HINTS = re.compile(
    r"\b(?:di|direct|kick|snare|toms?|drums?|guitars?|bass|keyboards?|keys|"
    r"vocals?|amps?|close[ -]?mic)\b",
    re.I,
)
PROCESS_HINTS = re.compile(
    r"\b(?:wav|rf64|flac|aiff|audacity|ardour|reaper|daw)\b", re.I
)
CAPTURE_WAV = re.compile(r"^WAV(?:\s*\([^)]*\))?$", re.I)
NUMBERED_DEVICE = re.compile(r"^(.*?)\s*#\s*(\d+)$")


@dataclass
class CaptureBranch:
    sources: list[str]
    chain: list[str]


def normalize_source(line):
    """Turn 'equipment - placement' into 'equipment (placement)'."""
    parts = re.split(r"\s+[-\u2013\u2014]\s+", line.strip(), maxsplit=1)
    if len(parts) == 2 and parts[0] and parts[1]:
        return f"{parts[0]} ({parts[1]})"
    return line.strip()


def chain_tokens(lines):
    return [
        part.strip()
        for line in lines
        for part in line.split(">")
        if part.strip()
    ]


def source_parts(lines, collapse_spots):
    parts = []
    hidden = False
    for line in lines:
        # Explicit multitrack/console descriptions are useful source
        # identities.
        multitrack = bool(re.search(r"\bmultitrack\b", line, re.I))
        room = bool(ROOM_HINTS.search(line))
        spot = bool(SPOT_HINTS.search(line))
        if collapse_spots and spot and not (room or multitrack):
            hidden = True
        else:
            parts.append(normalize_source(line))
    if hidden and "Multitrack" not in parts:
        parts.append("Multitrack")
    return parts


def parse_branches(groups):
    branches = []
    pending = []
    for group in groups:
        for line in group:
            if line.startswith(">"):
                tokens = chain_tokens([line])
                if pending:
                    branches.append(CaptureBranch(pending, tokens))
                    pending = []
                elif branches:
                    branches[-1].chain.extend(tokens)
                else:
                    raise ValueError(
                        "Recorder chain appears before any recording source."
                    )
            elif ">" in line:
                source, route = line.split(">", 1)
                pending.append(source.strip())
                branches.append(CaptureBranch(pending, chain_tokens([route])))
                pending = []
            else:
                pending.append(line)
    if pending:
        branches.append(CaptureBranch(pending, []))
    return branches


def common_route(branches):
    """Combine matching routes and count distinct recorder labels."""
    if len(branches) < 2 or any(not branch.chain for branch in branches):
        return None
    normalized = []
    labels = set()
    numbered = []
    for branch in branches:
        tokens = list(branch.chain)
        match = NUMBERED_DEVICE.fullmatch(tokens[0])
        numbered.append(match is not None)
        if match:
            tokens[0] = match[1].strip()
            labels.add(int(match[2]))
        normalized.append(tokens)
    if any(numbered) and not all(numbered):
        return None
    if any(tokens != normalized[0] for tokens in normalized[1:]):
        return None
    route = list(normalized[0])
    if len(labels) > 1:
        route[0] += f" x{len(labels)}"
    return route


def render_branch(branch, collapse_spots):
    parts = source_parts(branch.sources, collapse_spots)
    route = list(branch.chain)
    # Omit a mic branch's terminal WAV capture format from the compact source
    # summary. Explicit multitrack/software paths retain their capture details.
    explicit_multitrack = any(
        re.search(r"\bmultitrack\b", source, re.I) for source in branch.sources
    )
    if (
        len(route) > 1
        and CAPTURE_WAV.fullmatch(route[-1])
        and not explicit_multitrack
        and not any(PROCESS_HINTS.search(token) for token in route[:-1])
    ):
        route.pop()
    return " > ".join([" + ".join(parts), *route])


def parse_recording_section(lines):
    """Return (source, lineage) from the section between header and taper."""
    groups = []
    current = []
    for raw in lines:
        line = raw.strip()
        if line:
            current.append(line)
        elif current:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    if not groups:
        raise ValueError(
            "No recording source or lineage found before the taper line."
        )

    if all(line.startswith(">") for line in groups[-1]):
        lineage = chain_tokens(groups.pop())
    else:
        # Accommodate a single source and its processing line without a blank.
        last = groups[-1]
        marker = next(
            (
                i
                for i in range(len(last) - 1, -1, -1)
                if last[i].startswith(">") and PROCESS_HINTS.search(last[i])
            ),
            None,
        )
        if marker is None:
            raise ValueError(
                "Cannot identify the final processing chain; expected a "
                "final > WAV/... or > DAW/... block."
            )
        lineage = chain_tokens(last[marker:])
        groups[-1] = last[:marker]
    if not lineage:
        raise ValueError("Final processing chain is empty.")
    branches = parse_branches(groups)
    if not branches:
        raise ValueError(
            "Cannot find recording sources before the final processing chain."
        )
    collapse = sum(len(branch.sources) for branch in branches) > 1
    route = common_route(branches)
    if route:
        parts = []
        for branch in branches:
            for part in source_parts(branch.sources, collapse):
                if part != "Multitrack" or part not in parts:
                    parts.append(part)
        source = "[" + " + ".join(parts) + "]"
        lineage = route + lineage
    else:
        rendered = [render_branch(branch, collapse) for branch in branches]
        source = (
            rendered[0]
            if len(rendered) == 1
            else " + ".join(f"[{part}]" for part in rendered)
        )
    return source, " > ".join(lineage)
