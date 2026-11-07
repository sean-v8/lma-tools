# Development notes

Implementation decisions, invariants and validation limits for lma-tools. User-
facing setup and commands belong in the [README](../README.md).

## Dependencies and configuration

The official Archive client release is pinned in `pyproject.toml` and
`uv.lock`. Byte/rate formatting belongs to our local tqdm subclass; no private
Archive progress class or fork is required. Both sequential and concurrent
uploads use transport-read tracking with the client's own bars disabled.
Inspect and test request construction before changing the pin. Multipart resume
uses the existing HTTP dependencies, rather than a separate upload package.

Config discovery is independent of the working directory: explicit config, the
environment override, the editable checkout, then the XDG location. Private
config, credentials, recordings and logs must stay out of Git.

Named preference tables live in `[upload.prefix.PREFIX]`, with
`collection`, optional `creator` and optional `genre`.
Prefix tables are sorted alphabetically after basic upload/report settings.
The TOML
writer keeps comments and unrelated settings, locks the config directory and
replaces the file atomically. Tests cover concurrent writers and failed
replacements.

## Metadata and review

Reviewed Archive metadata stays in memory and the optional project log. Normal
runs first apply an approved local preparation plan; `-p` stops there.
Metadata-only, runtime-only, upload-only and cleanup-only skip preparation. See
[Preparation](PREPARATION.md) for local mutation and rollback boundaries.
Runtime comes from exact FLAC sample counts and is rounded once; full
metadata/runtime modes require the complete local FLAC inventory. Upload-only
mode deliberately avoids computing or writing a potentially partial runtime.

Descriptions remain literal plain text in TXT files, review and logs. HTML
escaping happens only when sending the description to Archive. This keeps
angle-bracket email credits, ampersands and other literal text visible.

Approved Creator preferences are saved and suppress repeated history lookup.
Preparation saves approved Genre for an existing collection mapping, even in
`-p` mode. Otherwise fresh reviews can suggest Creator/Title naming from recent
collection items. The authenticated uploader email takes precedence over a
transferer-name match. Up to three personal uploads are checked first, then up
to three other uploads if needed. A strict majority of at least two matching
naming patterns is required. Suggestions use the current show's venue and date.
Fresh TXT fields and configured Creator naming take precedence over saved
reviews. Extra reviewed fields without a TXT equivalent are retained. The
interactive review compares proposed metadata with a read-only Archive
snapshot; remote values do not replace local defaults. Offline previews and
completion do not perform the lookup.

Normal resumes and metadata-only mode both permit reviewed Creator/Title
corrections and send server-side expectations for changed values. Identifier,
Collection and Mediatype remain protected. Metadata
updates complete before file transfers; a new item starts with its notes TXT
and reviewed creation metadata before other transfers begin.

See [source inference](SOURCE_PARSING.md) for source and lineage rules.

## Transfers, recovery and derivation

The worker limit bounds concurrent file transfers, including small files and
FLACs. Each active worker owns an independent Archive session. A fatal error
stops scheduling new files and drains existing workers before the run exits.
Cancellation interrupts body reads and retry waits; a blocked request may still
need to reach its socket timeout.

Remote whole-file MD5 and size establish a verified skip or recovery success.
Preflight HEAD checks also recognize completed files absent from an inventory
response. Known conflicts require `r`/`a` approval or cancellation before
workers start. Choosing `a` still skips matching files; `--force`
uploads all. Explicit `--trust-log` can accept a matching prior server
acknowledgement when remote checks are inconclusive; it cannot override a known
conflict.

Ordinary PUT recovery checks uncertain acknowledgements before resending.
Confirmed absence permits a fresh PUT; unknown state triggers further checks
instead of a blind retransmission. Incomplete ordinary PUTs restart in full.

Without selected cleanup, each FLAC PUT requests derivation; small-file PUTs
suppress it. Multipart completion requests derivation for its assembled FLAC. A
final item derive request follows successful processing of all selected files,
including skips. Direct Tasks API submissions use `derive.php`; `derive` is not
a supported command name. The per-file upload header is a separate mechanism.
Metadata-only and runtime-only modes do not queue file derivation. Task
acceptance does not establish that Archive has finished processing it. Upload-
only mode selects TXT, JPG, FFP, MD5 and FLAC files. It skips metadata review,
item metadata writes and runtime calculation; ordinary checksum checks and
multipart resume still apply. It requires an existing item.

The exact `derive.php already queued/running` application error is accepted as
an existing pending task. Other errors remain failures and are recorded with
their message. A failure after all selected files verify has dashboard status
`derive failed`; its verified bytes and file counts remain in TOTAL. These
requests are item-wide, even when triggered by one file's completion. Selected
cleanup defers per-file derives until corrected uploads verify and approved
originals are deleted without cascade. Bounded metadata/HEAD polling confirms
absence before final derive. Pending deletion exits while retaining
`delete_accepted`; rerun cleanup later. Acceptance is not finality. Known local
multipart checkpoints block deletion and conflicting payloads. An interrupted
replacement can resume its own matching payload checkpoint. Assembly started on
another host without a local checkpoint cannot be guaranteed detectable;
unrelated derives may still see staging parts. No staging-exclusion mechanism
is implemented.

## File resume and the upload log

Resume is enabled by default, with settings under `[upload.resume]` and
`--no-resume` as the command-line opt-out. It streams bounded parts directly
from the original FLAC, serially within each file's worker slot. The default
part size is 16 MiB. Small FLACs that fit in one part and non-FLAC files retain
the ordinary PUT path. An existing checkpoint keeps its original part size even
if the configured default changes.

Resume state lives in the existing project `<identifier>_lma.log`; no separate
resume files, temporary audio copies or comparison downloads are created. State
includes the target, source size/full MD5, upload ID, part size, acknowledged
part hashes and completion phase. API credentials are excluded.

The journal writes incremental `resume_start`, `resume_part` and `resume_phase`
events under its thread lock. Writes are flushed and synced; logs use private
permissions. Replay isolates files and upload IDs, ignores torn JSON/UTF-8
lines and retains later valid records. A `resume_clear` event clears active
state after verification without deleting the log's history. Resume with
logging disabled is rejected before network access or review. `NoSuchBucket`
during initiation uses the bounded recovery retry budget: a newly accepted item
may not yet be available to multipart requests. This exception does not restart
an existing multipart transfer. XML error responses retain HTTP status in the
log; failed worker rows also display the message. Multipart part PUTs use a
300-second socket timeout for both writes and reads, matching ordinary uploads.
Control requests retain a 30-second connect timeout.

When supported, server part listings are reconciled against local hashes. If
listing is unavailable, locally acknowledged parts can be reused, but
completion must validate their MD5s and the final object's whole-file MD5 and
size must match before success. A changed source or target cannot reuse an
incompatible checkpoint.

Completion intent is logged before submitting the request. Assembly is
asynchronous; an uncertain completion must not start a fresh upload ID or
resend the entire file. Transfer workers return after persisting the assembling
checkpoint. After all transfer jobs drain, a separate bounded verification pass
opens fresh sessions and confirms pending whole-file MD5/size matches. Only
then are checkpoints cleared and upload success recorded. A resumed assembling
checkpoint enters the same pass without sending parts or completion again.
A remote-confirmed skip also clears its matching checkpoint; explicit log trust
alone cannot clear it. Definite part rejection permits reconciliation, while
pending assembly retains state for a later check. Existing ordinary PUT
progress cannot be converted into multipart checkpoints.

## Terminal displays and tmux

Show grids preallocate one row per selected file below TOTAL, including the
creation TXT. Queued rows show waiting; completed, skipped and failed rows stay
in place. Closing the live region prints one final inventory. Labels, bars and
metrics share column widths; long labels shorten in the middle.
Routine terminal start/finish messages are suppressed, while errors and
recovery notices remain visible. Redirected output uses plain status lines.

Show sessions start with the identifier. The separate `lmau` dashboard reads
private atomic status snapshots. These snapshots are display-only and never
authorize upload skips or retries; the upload log holds resumable part state.

Dashboard bytes cover selected files, including small files in normal mode. The
completed-file counter always counts FLACs, including verified skips. Counter
strings are right-aligned across TOTAL and show rows so status text lines up.
Partial
multipart bytes do not count as completed FLACs. Rates count current transport
reads, excluding hashing and bytes restored from checkpoints. Interrupted runs
retain confirmed files and checkpointed part bytes. The view smooths heartbeat
rates with a 30-second exponential time constant. Repeated reads of one
snapshot do not apply smoothing again. Timing starts at the current upload
process, not the first historical log entry; finished and stale rows freeze at
their final heartbeat. Remaining time covers bytes only, excluding
unpredictable server acknowledgement and derive scheduling.

The TOTAL row includes active runs and successful runs observed in that view.
Failed, cancelled and stale attempts remain visible but are excluded from the
aggregate. Completed rows stay until that dashboard closes, even when their on-
disk snapshots expire. A new view does not restore old finished runs. Closing
the dashboard does not stop uploads.

## Views and terminal state

Progress grids recompute widths before tqdm renders or clears a row. A terminal
resize erases the progress region and resets old padding lengths, preserving
the preceding terminal output. The dashboard reserves a full-width dashed
separator below TOTAL and hides its cursor until the view closes. A dashboard
resize resets its dedicated pane and rebuilds the grid under the shared display
lock, avoiding old wrapped rows and stale tqdm padding.

After a multipart body is consumed, the row shows `awaiting ack` until the
server responds. A `resume_part_sent` log event distinguishes that wait from
sending bytes; only acknowledgements or reconciled server parts are reusable.

Multipart initiation uses the bare `?uploads` subresource, including redirects.
Request failures log the operation, attempt, exception and HTTP status before
retrying, even if no upload ID exists yet. Credential strings are redacted.

Views batches count requests and retains successful cached values until reload
or exit. A failed replacement search preserves the last successful cached
result. Unknown view counts remain unknown; totals include successful counts,
and zero-view recordings are excluded from averages by default. Malformed
runtimes are excluded from runtime statistics.

Terminal cleanup avoids cursor-up erasure after screen restoration. Preserve
the established menu, prompts and spacing when changing query or transport
behavior.

## Validation

Use the commands in [Updates and checks](../README.md#updates-and-checks).
Style checks enforce Ruff formatting/lint and a 79-column limit across source,
strings, tests and documentation. CI runs Python 3.11, 3.12 and 3.13.

Offline tests exercise real request construction with in-memory transports,
including Content-Length/MD5, creation ordering, bounded concurrency, retries,
lost replies, checkpoint replay, conflicting or changed files, XML errors
within HTTP 200, redirects, cancellation and final derive ordering. Other
checks cover config writes, caches, completion and installed command behavior.
Pseudo-terminal checks cover display ordering, alignment and resizing.

These checks do not perform live Archive uploads. Network tests use controlled
responses, while encoding tests invoke local `flac` and FFmpeg binaries.
Automated tmux checks cover launching, quoting, environment forwarding and
session isolation; they do not reproduce an SSH disconnect/reconnect.

Run `uv run --locked python -B -m tests` for a passed-test progress bar. Test
stdout/stderr is buffered; only failure tracebacks and their captured output
are shown. Skipped and expected-failure tests are identified separately. The
runner returns nonzero for failures, errors or unexpected successes. Standard
`unittest discover` remains available for debugging individual tests.

Test modules are grouped by behavior: metadata, discovery, runtime, transfers,
preparation, tags, progress, recovery and reporting. Shared fixtures live in
`tests/helpers.py` and `tests/upload/helpers.py`; test modules should not
import
other test modules for fixtures. Keep distinct failure-path cases even when
they share setup. Consolidate repeated setup and duplicate assertions instead
of targeting a particular test count.

The upload/report modules are `lma_tools.upload` and `lma_tools.report`.
Installed commands are `lma-upload`/`lmau` and `lma-report`/`lmar`.
