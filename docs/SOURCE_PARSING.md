# Recording Source and Lineage inference

The notes file remains the complete record. These rules produce compact
editable metadata; they do not change the TXT or remove anything from
Description.

## Structure

The first four lines are artist, date, venue and location. The recording
section runs from there to the first `Name <email>` line. Blank lines separate
capture branches and the final processing chain.

A source may carry its route inline (`Microphone > Recorder`) or on following
lines (`> Recorder`). Multiple source lines feeding one route are grouped. The
last standalone block of `>` lines becomes shared Lineage. A single source
followed immediately by a processing line is also supported without a blank
separator.

Examples use a generic artist/date header and a generic taper line; only the
recording sections below are shown.

## Single microphone pair

```text
Microphone pair A - 8' NOS, 15' from stage

> Recorder A > WAV (32/48) > Editor > WAV (24/48) > FLAC
```

Source: `Microphone pair A (8' NOS, 15' from stage)`

Lineage: `Recorder A > WAV (32/48) > Editor > WAV (24/48) > FLAC`

Only a spaced dash between equipment and placement is normalized. Equipment
names such as `Model-123` and existing parentheses remain intact. Spaced en/em
dashes work too.

## Room microphones with close or direct sources

```text
Microphone pair A - overhead XY
Input B - kick
Input C - DI bass
Input D - guitar amp

> Recorder A > WAV (32/48) > Editor > FLAC
```

Source: `Microphone pair A (overhead XY) + Multitrack`

Lineage: `Recorder A > WAV (32/48) > Editor > FLAC`

The summarizer uses descriptors, not microphone-model knowledge. Room/array
hints include room, audience, overhead, NOS, ORTF, XY, stage lip, center stage
and split. Explicit close/direct labels such as DI, kick, drums, bass,
keyboards, vocals and guitar amp may collapse to one `Multitrack` entry. A room
hint takes precedence when both appear.

Unknown labels remain visible. A recording with only one source keeps that
source even if it is a DI feed. Explicit console/Multitrack descriptions remain
visible as source identities. Source order is not used as proof that a
microphone is a room pair; placing it first remains useful for readability.

## Equal numbered recorders

```text
Microphone pair A - overhead, 5' split
> Recorder A #1

Input B - DI bass
Input C - kick
> Recorder A #2

> WAV (32/48) > Editor > FLAC
```

Source: `[Microphone pair A (overhead, 5' split) + Multitrack]`

Lineage: `Recorder A x2 > WAV (32/48) > Editor > FLAC`

Matching capture routes can move into shared Lineage. Distinct `#1`, `#2`, etc.
establish the recorder count. Repeated identical routes without numbers do not
establish multiple physical units. Mixed numbered/un-numbered routes remain
separate rather than guessing a count. Different recorder models/routes remain
separate too.

## Different capture branches with shared processing

```text
Microphone pair A - 3' center stage NOS
Microphone pair B - 2' stage lip, 8' split
> Recorder A > WAV (32/48)

Console Multitrack
> Capture application > RF64 (24/48)

> Editor > WAV (24/48) > FLAC
```

Source: `[Microphone pair A (3' center stage NOS) + Microphone pair B (2' stage
lip, 8' split) > Recorder A] + [Console Multitrack > Capture application > RF64
(24/48)]`

Lineage: `Editor > WAV (24/48) > FLAC`

A microphone/recorder branch's terminal WAV capture-format token is omitted
from the compact Source summary. Explicit Multitrack branches retain their
capture details, as do recognizable software-processing routes. Every token
remains in Description regardless of summary formatting.

## Review and future changes

Always review the suggested fields. The code cannot reliably infer the role of
an unfamiliar microphone from its model alone. Labeling placement or intended
use helps. Ambiguous sources are preserved instead of hidden. A section without
an identifiable source or final processing chain stops with an explanatory
error.

`lma_tools/upload/notes.py` contains the policy and parsing helpers.
`ROOM_HINTS`, `SPOT_HINTS` and the route-rendering functions are the places to
adjust future conventions. Tests cover the layouts above, mixed recorder types,
unknown sources, inline routes and multiline processing.

Use `--fresh-metadata` when testing changes or reparsing an edited TXT.
Otherwise the last reviewed Source/Lineage in the project log may override
freshly inferred defaults. Preview remains read-only:

```bash
lmau -m artist2026-08-17.audience.t-flac2448 --fresh-metadata --preview
```

Run without `--preview` to review and submit metadata only. `--fresh-metadata`
resets other previously reviewed defaults too, so revisit any earlier manual
edits before confirming.
