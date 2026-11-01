# Development notes

Implementation decisions, invariants and validation limits for lma-tools.
User-facing setup and commands belong in the [README](../README.md).

## Dependencies and configuration

The official Archive client release is pinned in `pyproject.toml` and
`uv.lock`. Byte/rate formatting belongs to our local tqdm subclass; no private
Archive progress class or fork is required. Both sequential and concurrent
uploads use transport-read tracking with the client's own bars disabled.
Inspect and test request construction before changing the pin. Multipart resume
uses the existing HTTP dependencies, rather than a separate uploader package.

Config discovery is independent of the working directory: explicit config,
the environment override, the editable checkout, then the XDG location.
Private config, credentials, recordings and logs must stay out of Git.

Collection mappings live in `[uploader.collections]`. The TOML writer keeps
comments and unrelated settings, locks the config directory and replaces the
file atomically. Tests cover concurrent writers and failed replacements.

## Metadata and review

Reviewed metadata stays in memory and in the optional project log. Show files
are never edited, moved or deleted. Runtime comes from exact FLAC sample
counts and is rounded once; full metadata/runtime modes require the complete
local FLAC inventory. Upload-only mode deliberately avoids computing or
writing a potentially partial runtime.

Descriptions remain literal plain text in TXT files, review and logs. HTML
escaping happens only when sending the description to Archive. This keeps
angle-bracket email credits, ampersands and other literal text visible.

Fresh reviews can suggest Creator/Title naming from recent collection items.
The authenticated uploader email takes precedence over a transferer-name
match. Up to three personal uploads are checked first, then up to three other
uploads if needed. A strict majority of at least two matching naming patterns
is required. Suggestions use the current show's venue and date. Saved reviews
take precedence, and an existing item's current naming seeds a fresh review.
Offline previews and completion do not perform the lookup.

Normal resumes protect existing creation fields. Metadata-only mode permits
reviewed Creator/Title corrections and sends server-side expectations for
changed values. Identifier, Collection and Mediatype remain protected.
Metadata updates complete before file transfers; a new item starts with its
notes TXT and reviewed creation metadata before other transfers begin.

See [source inference](SOURCE_PARSING.md) for source and lineage rules.

## Transfers, recovery and derivation

The worker limit bounds concurrent file transfers, including small files and
FLACs. Each active worker owns an independent Archive session. A fatal error
stops scheduling new files and drains existing workers before the run exits.
Cancellation interrupts body reads and retry waits; a blocked request may
still need to reach its socket timeout.

Remote whole-file MD5 and size establish a verified skip or recovery success.
Preflight HEAD checks also recognize completed files absent from an inventory
response. Known conflicts stop the run unless replacement is authorized with
`--force`. Explicit `--trust-log` can accept a matching prior server
acknowledgement when remote checks are inconclusive; it cannot override a
known conflict.

Ordinary PUT recovery checks uncertain acknowledgements before resending.
Confirmed absence permits a fresh PUT; unknown state triggers further checks
instead of a blind retransmission. Incomplete ordinary PUTs restart in full.

Each FLAC PUT requests derivation; small-file PUTs suppress it. Multipart
completion requests derivation for its assembled FLAC. A final item derive
request follows successful processing of all selected files, including skips.
Direct Tasks API submissions use `derive.php`; `derive` is not a supported
command name. The per-file upload header is a separate mechanism.
Metadata-only and runtime-only modes do not queue file derivation. Task
acceptance does not establish that Archive has finished processing it.

## File resume and the upload log

`--resume` is opt-in, with defaults under `[uploader.resume]`. It streams
bounded parts directly from the original FLAC, serially within each file's
worker slot. The default part size is 16 MiB. Small FLACs that fit in one part
and non-FLAC files retain the ordinary PUT path. An existing checkpoint keeps
its original part size even if the configured default changes.

Resume state lives in the existing project `<identifier>_lma.log`; no separate
resume files, temporary audio copies or comparison downloads are created.
State includes the target, source size/full MD5, upload ID, part size,
acknowledged part hashes and completion phase. API credentials are excluded.

The journal writes incremental `resume_start`, `resume_part` and
`resume_phase` events under its thread lock. Writes are flushed and synced;
logs use private permissions. Replay isolates files and upload IDs, ignores
torn JSON/UTF-8 lines and retains later valid records. A `resume_clear` event
clears active state after verification without deleting the log's history.
Resume with logging disabled is rejected before network access or review.

When supported, server part listings are reconciled against local hashes. If
listing is unavailable, locally acknowledged parts can be reused, but
completion must validate their MD5s and the final object's whole-file MD5 and
size must match before success. A changed source or target cannot reuse an
incompatible checkpoint.

Completion intent is logged before submitting the request. Assembly is
asynchronous; an uncertain completion must not start a fresh upload ID or
resend the entire file. Definite part rejection permits reconciliation, while
pending assembly retains state for a later check. Existing ordinary PUT
progress cannot be converted into multipart checkpoints.

## Terminal displays and tmux

Concurrent file rows retain file order and close gaps as rows finish. Labels,
bars and metrics share column widths; long labels shorten in the middle.
Routine terminal start/finish messages are suppressed, while errors and
recovery notices remain visible. Redirected output uses plain status lines.

Show sessions start with the identifier. The separate `lmau` dashboard reads
private atomic status snapshots. These snapshots are display-only and never
authorize upload skips or retries; the upload log holds resumable part state.

Dashboard bytes cover selected files, including small files in normal mode.
The completed-file counter always counts FLACs, including verified skips.
Partial multipart bytes do not count as completed FLACs. Rates count current
transport reads, excluding hashing and bytes restored from checkpoints.
Interrupted runs retain confirmed files and checkpointed part bytes.
The view smooths heartbeat rates with a 30-second exponential time constant.
Repeated reads of one snapshot do not apply smoothing again. Timing starts
at the current upload process, not the first historical log entry; finished
and stale rows freeze at their final heartbeat. Remaining time covers bytes
only, excluding unpredictable server acknowledgement and derive scheduling.

The TOTAL row includes active runs and successful runs observed in that view.
Failed, cancelled and stale attempts remain visible but are excluded from the
aggregate. Completed rows stay until that dashboard closes, even when their
on-disk snapshots expire. A new view does not restore old finished runs.
Closing the dashboard does not stop uploads.

## Views and terminal state

Progress grids recompute widths before tqdm renders or clears a row. A terminal
resize erases the progress region and resets old padding lengths, preserving
the preceding terminal output. The dashboard reserves a full-width dashed
separator below TOTAL and hides its cursor until the view closes.

After a multipart body is consumed, the row shows `awaiting ack` until the
server responds. A `resume_part_sent` log event distinguishes that wait from
sending bytes; only acknowledgements or reconciled server parts are reusable.

Multipart initiation uses the bare `?uploads` subresource, including redirects.
Request failures log the operation, attempt, exception and HTTP status before
retrying, even if no upload ID exists yet. Credential strings are redacted.

Views batches count requests and retains successful cached values until
reload or exit. A failed replacement search preserves the last successful
cached result. Unknown view counts remain unknown; totals include successful
counts, and zero-view recordings are excluded from averages by default.
Malformed runtimes are excluded from runtime statistics.

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

These checks do not perform live Archive uploads. Automated tmux checks use
mocks for launching, quoting, environment forwarding and session isolation;
they do not reproduce an SSH disconnect/reconnect. A controlled live
interrupted/resumed multipart upload and final size/MD5 verification remain
required before making file resume the default.
