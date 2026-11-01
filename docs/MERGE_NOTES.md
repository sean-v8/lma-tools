# Merge verification

The source is taken from the two user-supplied v0.1.0 ZIPs, including working
tree content. Local histories were inspected; old Git directories are not
part of the merged source archive. No remote repositories were modified.

The views menu text, renderer and terminal display module, and uploader
editor remain unchanged. A subsequent views fix removed cursor-up erasure
after screen restoration and preserves cached search results on failures.
Two views tests contained menu expectations predating the supplied
manual edit; their expectations now match the attached menu.

The Internet Archive fork remains pinned to commit
`29b7644ff53826a4ee53d5ef3aa72bbc6b7cbf46`, from the uploader lockfile.

Validation performed with Python 3.12:

- 207 offline tests passed, including Ruff and strict text line limits.
- Ruff formatting and lint checks passed.
- Bash syntax checks passed for installation and completion scripts.
- Isolated uv tool installation created lmau, lmav and ia executables.
- Installed help/version smoke checks passed from outside the checkout.
- Source distribution and wheel builds succeeded.
- Migration was checked against both supplied private configs and mappings,
  without including those values in the deliverable.

Archive HTTP behavior and uploads were tested with mocks. The new naming
lookup was also checked with live read-only collection searches. No live
metadata update, transfer or derive request was performed. Dependency
installation used the exact cached/resolved revision. tmux forwarding,
quoting, bypasses and collision handling were checked with mocks; an actual
SSH disconnect/reconnect was not exercised. CI includes Python 3.11 through
3.13; those additional versions were not executed in this workspace.

Collection mappings now live under `[uploader.collections]`. A comment-aware
TOML writer retains private settings and formatting. Mapping updates lock
the config directory and replace the file atomically, avoiding lost local
updates. Tests exercise concurrent processes and failed replacements.
Migration imports old CSV mappings and can upgrade an existing merged config
with a private backup. Legacy CSV overrides are read-only inputs.

Upload recovery is bounded and verifies remote MD5 plus size before treating
a lost acknowledgement as success. Tests cover timeout confirmation without
resending, delayed HEAD confirmation, absent-file retries, unreachable
verification endpoints, checksum conflicts, retry exhaustion, file changes,
interrupts and disabled logging. Upload-only recovery is checked to avoid
metadata/runtime edits while still queueing derivation after completion.
Preflight HEAD checks recognize completed uploads without a success log.
The implementation was checked against the pinned internetarchive source,
including its wrapped urllib3 read timeout and connection reset behavior.
No live uploads, metadata writes or derive requests were performed.

Concurrent uploads use a bounded pool with independent Archive sessions,
serialized journal writes and coordinated progress rows using the pinned
fork's byte/rate formatter. New-item creation waits for the first TXT before
starting other transfers. Existing metadata edits remain before transfers.
Tests run real Archive request construction against an in-memory transport;
they check simultaneous small/FLAC transfers, Content-Length, creation
ordering, independent timeout recovery, session cleanup, fatal failure
handling and derivation only after all files succeed. Additional checks cover
cancellation, interruptible retry waits, progress byte counts excluding local
hash reads, and concurrent journal integrity. No live uploads were performed.

Concurrent progress rows now sort by the original file order and close gaps
as files finish. Healthy terminal runs suppress repetitive start/finish
messages; errors and recovery notices remain visible. Long labels shorten
in the middle to retain track suffixes and leave room for byte counts/rates.
Sequential and redirected output retain their existing status messages.

Descriptions are HTML-escaped only at the network boundary. Reviewed text,
TXT files and saved review metadata remain plain text. Tests cover literal
email credits, ampersands and angle brackets for creation and later updates.
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
