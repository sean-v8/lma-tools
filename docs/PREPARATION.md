# Preparation and cleanup

Normal `lmau` runs prepare local audio before Archive metadata review and
upload approval. Use a complete show folder. For a partial folder or an already
prepared subset, use `-u` against an existing item instead.

## Local approval and modes

`-p` / `--prepare` performs local preparation only, without Archive access.
Normal runs continue to metadata review and the separate `UPLOAD` prompt.
Ctrl-S in the preparation editor approves local mutation; Ctrl-C cancels.
Cancelling a later stage does not undo completed preparation.

`-u` uploads existing supported files without audio edits or item metadata and
runtime changes. `-m` and `-r` bypass preparation. Standalone `--clean` only
reviews and deletes eligible remote originals: it does not prepare, upload
files, change audio or mutate item metadata. It may queue derivation after
deletion. `--no-clean` omits cleanup review during a normal upload. `--preview`
performs no writes, network requests or interactive editing.

## Matching and editable TOML

Notes start with artist, date, venue and city/coverage. The notes date must
match the folder date. Use `--notes` when notes selection is ambiguous.
Numbered setlist rows supply titles; `Set N` headings supply disc numbers.
Parenthesized set annotations such as `Set 1 (partial)` are accepted.
Standalone trailing symbol groups are removed without requiring a legend
or a fixed marker list. Guest annotations such as `*#` and `&%` are omitted.
Leading/trailing `//` cut markers and `>` segues remain in Title, including
when guest symbols appear alongside them. Internal medley slashes and normal
punctuation attached to words remain. The full TXT is unchanged.
Review cleaned titles, especially intentionally standalone punctuation.
Without set headings, names use `bandYYYY-MM-DDt01.flac`. Explicit `Set N`
headings preserve `sNt01`, even when only that set was recorded. Track numbers
restart at one within each recorded set; tag track numbers run across all
recorded tracks. The disc denominator is the highest recorded set number.

Preparation reads WAV/FLAC title tags and filenames to match setlist rows.
Ambiguous or missing matches remain blank for correction in editable TOML.
Every local WAV/FLAC must map exactly once. Same-basename WAV/FLAC pairs appear
as reviewed `[[redundant_wavs]]` entries with `source` and `track_source`. The
WAV is removed only after decoded audio matches its staged FLAC; a mismatch
leaves both originals intact. Sources and destinations must be unique direct
filenames; existing unrelated destinations are rejected. Missing earlier sets
are allowed. Every locally recorded track must be mapped; use `-u` when files
have been temporarily moved out of the folder.

Common fields are `artist`, `album`, `genre`, `venue`, `date` and `artwork`.
Each `[[tracks]]` row has `source`, `destination`, `title`, `disc` and `track`.
Disc and track values display `number/total`, such as `"1/2"` and `"7/18"`.
Totals must match the recorded set/track inventory. FLAC tags store combined
`TRACKNUMBER` and `DISCNUMBER` values; no separate total fields are written.
Genre is optional; a saved per-collection Genre seeds future preparation. An
approved Genre is saved even with `-p` when a collection mapping exists. A
single JPG seeds artwork; otherwise choose an existing JPG or leave empty.

Album defaults to date, city/coverage and an AUD/SBD/MTX classification from
recording notes. This classification only seeds the editable Album field; it is
not a separate tag or a change to Archive source/lineage inference.

## Encoding, tags and verification

Install the OS dependencies separately from Python/uv, for example:

```bash
sudo apt install flac ffmpeg
```

WAV encoding uses reference `flac` with verification, constant and fixed
subframes disabled, LPC order zero and mid-side disabled. This produces
verbatim subframes; it is not compression level `-0`. Existing FLAC audio is
copied for staging rather than re-encoded.

The resulting tag set contains exactly these sample fields:

| Tag | Value |
| --- | --- |
| `artist` | Approved artist |
| `album` | Approved Album |
| `genre` | Approved Genre |
| `venue` | Approved venue |
| `date` | Year only, from the approved ISO date |
| `title` | Approved track title |
| `tracknumber` | Global track number / total recorded tracks, e.g. `7/18` |
| `discnumber` | Set number / highest recorded set number, e.g. `2/2` |

Other tags and pictures are removed when tagging is needed. Selected artwork is
embedded as one JPEG front cover; empty artwork removes embedded pictures. Tag
writes use a modest 1024-byte padding allowance. Already matching tags and
artwork avoid rewriting the staged FLAC. When names, tags, artwork and manifest
bytes already match, preparation leaves local outputs unchanged.

Both source and destination are decoded through ffmpeg to signed 32-bit PCM and
compared using streaming SHA-256. All tracks are staged and verified before
committing outputs. Successful WAV sources are deleted only after this
verification; renamed FLAC sources are also removed during commit. Commit
failures attempt rollback from temporary backups. This is not a power-loss-safe
filesystem transaction. Keep independent source backups.

Preparation regenerates canonical show FFP and MD5 files. FFP records FLAC PCM
fingerprints; MD5 covers supported output files except the MD5 itself. Local
preparation can complete even if a later upload fails.

## Existing-file manifests

Upload-only verifies existing manifests before transfer. Listed absent files
produce warnings for intentionally partial folders; every listed present file
must match, and present supported files cannot be omitted. Stale or malformed
manifests require typing `REBUILD` to regenerate FFP/MD5 only. Audio and item
metadata remain unchanged. Missing manifests are permitted. Preview reports
invalid manifests without changing them.

## Remote conflict and cleanup review

Before workers start, differing same-name files show local and remote sizes and
MD5s. Choose `r` to replace one, `a` for all conflicts, or `c` to cancel.
Choosing `a` still skips matching files. `--force` retains the legacy behavior
of uploading all selected files, including matches; it is not that prompt's
replace-all-conflicts choice.

Normal existing-item uploads offer remote extras for cleanup by default. Only
direct supported TXT/JPG/FFP/MD5/FLAC originals with MD5 and size evidence are
candidates. Derivatives, nested files and unknown originals are excluded.
Nothing is selected initially. Enter numbers separated by spaces or `all`, then
type `DELETE`; Enter alone selects none. `--clean` uses this same review
without uploads, replacement prompts or local preparation. A partial folder can
make valid remote tracks look obsolete, so inspect every selection carefully.

For normal uploads with selected cleanup, corrected files upload first and must
all pass remote whole-file MD5/size checks before deletion starts. Per-file
derivation is deferred until cleanup completes. Immediately before each delete,
the uploader rechecks original status, MD5 and size. Changed remote evidence
stops cleanup. Deletes use conditional requests with cascade and automatic
derive disabled. Bounded metadata/HEAD polling must confirm absence before the
final item derive. Accepted does not mean finalized: if deletion remains
pending, the run exits without final derivation and retains `delete_accepted`
in the log. Rerun cleanup later to confirm completion. Unfinished cleanup is
replayed from the log; final derivation is retried even if the deleted file has
disappeared before the next review. Cleanup-only can operate on a named local
output directory without notes or audio. Acceptance of a derive request does
not prove task completion.

Known local multipart checkpoints in uploading/completing/assembling phases
block deletion of that filename. Replacement can resume its own matching
payload checkpoint, but a different payload requires resolving the previous
checkpoint first. A multipart assembly started on another host without a known
local checkpoint cannot be guaranteed detectable. These guards do not establish
that unrelated item-wide derivation can never see staging parts.

## Progress and validation limits

Existing terminal wording, spacing and interaction flow are retained except for
the explicit preparation stage and TOTAL progress bar. Dashboard counters are
display state, not evidence permitting deletion or an upload skip.

Multipart resume is enabled by default. Users report successful end-to-end
Archive uploads, including assembly. Automated network tests use mocks; they do
not independently establish live upload, deletion or derivation success. No
live Archive operation was performed for this documentation.

Real encoding was verified with reference FLAC 1.4.3 using a supplied 24-bit/48
kHz WAV: all 740 subframes were VERBATIM, and decoded PCM matched. Generated
fixtures also exercise conversion, no-op reruns, manifest repair and rollback.
Encoder tests skip when the required command-line tools are absent; install
them to include these checks.

## Parallel preparation

Each preparation worker handles one whole track: conversion when needed,
tagging and decoded-PCM verification. The default is one worker per available
logical CPU, capped by the number of tracks. Set `prepare_workers = 16` under
`[uploader]` in private `config.toml` to use a fixed limit. This is independent
of `upload_workers`; set it to `1` for sequential preparation.

All tracks are staged and verified before any output is committed or WAV is
removed. If a worker fails, scheduling stops and active jobs finish before the
staging directory is discarded. The original show files remain unchanged.
