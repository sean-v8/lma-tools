# Merge verification

The source is taken from the two user-supplied v0.1.0 ZIPs, including working
tree content. Local histories were inspected; old Git directories are not part
of the merged source archive. No remote repositories were modified.

The views menu text, renderer and terminal display module, and uploader editor
remain unchanged. A subsequent views fix removed cursor-up erasure after screen
restoration and preserves cached search results on failures. Two views tests
contained menu expectations predating the supplied manual edit; their
expectations now match the attached menu.

The Internet Archive fork remains pinned to commit
`29b7644ff53826a4ee53d5ef3aa72bbc6b7cbf46`, from the uploader lockfile.

Validation performed with Python 3.12:

- 237 offline tests passed, including Ruff and strict text line limits.
- Ruff formatting and lint checks passed.
- Bash syntax checks passed for installation and completion scripts.
- Isolated uv tool installation created lmau, lmav and ia executables.
- Installed help/version smoke checks passed from outside the checkout.
- Source distribution and wheel builds succeeded.
- Migration was checked against both supplied private configs and mappings,
  without including those values in the deliverable.

Archive HTTP behavior and uploads were tested with mocks. The new naming lookup
was also checked with live read-only collection searches. No live metadata
update, transfer or derive request was performed. Dependency installation used
the exact cached/resolved revision. tmux forwarding, quoting, bypasses and
collision handling were checked with mocks; an actual SSH disconnect/reconnect
was not exercised. CI includes Python 3.11 through 3.13; those additional
versions were not executed in this workspace.

Collection mappings now live under `[uploader.collections]`. A comment-aware
TOML writer retains private settings and formatting. Mapping updates lock the
config directory and replace the file atomically, avoiding lost local updates.
Tests exercise concurrent processes and failed replacements. Migration imports
old CSV mappings and can upgrade an existing merged config with a private
backup. Legacy CSV overrides are read-only inputs.

Upload recovery is bounded and verifies remote MD5 plus size before treating a
lost acknowledgement as success. Tests cover timeout confirmation without
resending, delayed HEAD confirmation, absent-file retries, unreachable
verification endpoints, checksum conflicts, retry exhaustion, file changes,
interrupts and disabled logging. Upload-only recovery is checked to avoid
metadata/runtime edits while still queueing derivation after completion.
Preflight HEAD checks recognize completed uploads without a success log. The
implementation was checked against the pinned internetarchive source, including
its wrapped urllib3 read timeout and connection reset behavior. No live
uploads, metadata writes or derive requests were performed.

Concurrent uploads use a bounded pool with independent Archive sessions,
serialized journal writes and coordinated progress rows using the pinned fork's
byte/rate formatter. New-item creation waits for the first TXT before starting
other transfers. Existing metadata edits remain before transfers. Tests run
real Archive request construction against an in-memory transport; they check
simultaneous small/FLAC transfers, Content-Length, creation ordering,
independent timeout recovery, session cleanup, fatal failure handling and the
final derive request only after all files succeed. Additional checks cover
cancellation, interruptible retry waits, progress byte counts excluding local
hash reads, and concurrent journal integrity. No live uploads were performed.

Concurrent progress rows now sort by the original file order and close gaps as
files finish. Healthy terminal runs suppress repetitive start/finish messages;
errors and recovery notices remain visible. Long labels shorten in the middle
to retain track suffixes and leave room for byte counts/rates. Sequential and
redirected output retain their existing status messages.

Descriptions are HTML-escaped only at the network boundary. Reviewed text, TXT
files and saved review metadata remain plain text. Tests cover literal email
credits, ampersands and angle brackets for creation and later updates.
Metadata-only mode can correct Creator and Title with server-side expected
values; normal resumes continue to protect creation fields.

Naming suggestions prefer up to three recent personal uploads, identified by
the configured Archive account email or reviewed transferer name. Up to three
other collection uploads supply a fallback. A strict majority of at least two
matching Creator/Title patterns is required. Existing items seed fresh review
from their current naming; saved reviews are retained. Offline previews and
completion never request history. A live read-only lookup returned a 3/3
personal naming consensus for the user's example collection.

New tmux show sessions omit the old prefix. A detached lmau dashboard reads
private, atomic snapshots with selected-byte totals, wire rates and confirmed
file counts. Checks and successful acknowledgements credit completed files;
retries reset in-flight counts and stopped runs discard unconfirmed bytes.
Snapshots never influence upload decisions. Tests cover concurrent and
sequential transport observation, skips, retries, privacy, stale/expired
snapshots, creation commands and environment forwarding. A real pseudo-
terminal checked ordered multi-show rendering and independent dashboard
closure. The environment disallows Unix sockets, so a real tmux server could
not be exercised; tmux commands and session isolation were checked with mocks.

Dashboard and concurrent file rows now share label, bar and metric widths.
Column widths account for all visible rows; metric widths retain their high
water marks to reduce movement as counters and rates change. Long labels
shorten together when necessary. Tests cover differing names, file sizes, rates
and statuses, removal of rows and narrow terminals. A real pseudo- terminal
verified dashboard alignment and resizing. Upload decisions, transport and
recovery logic are unchanged. The two redundant dashboard introductory lines
are removed; the tmux help line remains.

The dashboard includes a TOTAL row for current work and successful runs
observed in that view. It sums selected bytes, sent bytes, confirmed bytes,
file counts and active transfer rates. Stopped and stale attempts are excluded
from the aggregate. Finished runs no longer disappear after six entries or when
their snapshot expires; they remain in memory until the view closes. A new view
excludes runs finished before it opened. Tests cover weighted totals, rate
exclusions, completion transitions, more than six retained successes, view
reopening and missing/stale snapshots.

FLAC PUTs now request derivation individually using the native IAS3 header.
Small-file PUTs keep it disabled. A final item derive request remains after all
selected files complete or are verified and skipped. Transport tests check the
actual per-file header and still verify final-request ordering. No live uploads
or derive tasks were submitted during this change.

Multipart FLAC resume is opt-in, with 16 MiB parts and no new dependencies. It
uses an independent IAS3 backend rather than importing ia-multipart's
standalone script. Original files supply bounded streams directly. Resume state
is stored as incremental JSON events in the existing project upload log; no
sidecar checkpoints or local partial audio files are created. Signature checks
include the whole-file MD5, size and target. Part events are flushed and synced
before reuse. Supported server part listings are reconciled; when listing is
unavailable, locally acknowledged hashes are validated by completion and final
whole-file size/MD5 checks. Uncertain completion never starts a fresh upload
ID. Derivation remains enabled on completion.

Offline tests exercise prepared requests, Content-MD5 and Content-Length,
interruption/resume, lost part/completion replies, expired IDs, unavailable
part listings, asynchronous assembly, changed files, XML errors within HTTP
200, redirects, rate accounting, checkpoint privacy and concurrent files.
Worker limits still apply across files; parts are serial within each worker. No
local partial audio files or downloaded comparisons are used. No live multipart
upload has been performed: enable it explicitly for the first controlled
interrupted/resumed Archive test before making it the default.

The public flags are --resume and --no-resume, with [uploader.resume]
configuration. Resume remains opt-in. Disabled logging is rejected before
network access or interactive review when resume is enabled. Logs use private
permissions. Replaying interleaved per-file events reconstructs state; torn
lines are ignored and later valid writes remain readable. Completion clears
active state with a tombstone event. Tests cover concurrent delta writes,
replay from a fresh Journal, stale attempt IDs, changed files and no extra
resume files. Existing ordinary-upload and metadata workflows are retained.

Dashboard file counts include only FLACs, including verified skips, in both
normal and upload-only runs. Byte totals still cover all selected files.
The visible label says completed. A mixed-file regression test checks that
TXT/JPG/FFP completion never increments the FLAC count.
