# lma-tools

Command-line tools for the Internet Archive's Live Music Archive.

| Command | Purpose |
| --- | --- |
| `lma-upload` / `lmau` | Prepare, review, upload and resume recordings |
| `lma-report` / `lmar` | Search and report view counts and runtime |

## Installation

Requirements: Linux, Bash and uv. Interactive uploads require tmux. Local
preparation requires the OS tools `flac` and `ffmpeg` (including `ffprobe`),
available on PATH. On Debian/Ubuntu:

```bash
sudo apt install flac ffmpeg
sudo apt install tmux
```

The installer uses Python 3.12; uv does not install these OS dependencies.

Install uv if it is not already available:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Follow the installer's PATH instructions or open a new terminal before
continuing.

From a cloned or extracted source directory:

```bash
cd lma-tools
bash scripts/install.sh
```

The installer uses locked dependencies and creates `lma-upload` / `lmau`,
`lma-report` / `lmar`, and `ia` commands through `uv tool install`. Keep the
checkout in place: installation is editable. No aliases are required. If the
commands are not on PATH, run `uv tool update-shell` and open a new terminal.

## Updating older installations

Run `bash scripts/install.sh` again after updating the source. Reinstalling
lma-tools replaces its former `lmav` entry point with `lmar`; the installer
also removes a separately uv-installed legacy lma-views package. Run
`hash -r` in existing shells to clear cached executable paths.

Old `[uploader]` and `[views]` config headers remain readable without changing
your private file. To migrate headers to `[upload]` and `[report]` while
retaining values and comments, run:

```bash
uv run --locked python scripts/migrate_config.py --upgrade config.toml
```

The migration keeps an exclusive private backup and never overwrites one.
Collection preference blocks remain at the end. Conflicting old/new headers
are rejected instead of silently merging different settings.

## Configuration

From the checkout, create your private config without replacing an existing
file:

```bash
test -e config.toml || cp config.example.toml config.toml
```

Edit `config.toml` before uploading. For example:

```toml
[upload]
shows_root = "/absolute/path/to/recordings"
log = true

[report]
query = ['collection:(etree OR taperssection)']
sort = "publicdate"

[upload.collections.band]
collection = "ArchiveCollection"
creator = "Example Artist"
genre = "Rock"
```

`shows_root` must be an existing absolute directory. Exported environment
variables and `~` are supported. Collection mappings associate identifier
prefixes with Archive collection names; approved mappings are saved here
without changing unrelated settings or comments. See
[config.example.toml](config.example.toml) for optional defaults, including
upload recovery settings. Keep basic upload and report settings above the
collection tables at the end. Legacy string mappings remain supported. Approved
Creator preferences avoid repeated naming-history lookups; approved Genre is
saved by preparation, including `-p`, for an existing mapping.

Both commands select config independently of the current directory:

1. `--config PATH`, when supplied.
2. The `LMA_TOOLS_CONFIG` environment variable.
3. The editable checkout's `config.toml`.
4. For a noneditable installation, `$XDG_CONFIG_HOME/lma-tools/config.toml`
   or `~/.config/lma-tools/config.toml`.

Upload requires config. Report can use generic defaults if its default file is
absent. An explicitly selected missing file is an error. Keep config,
credentials, recordings and upload logs out of Git; config is Git-ignored.

Configure Archive credentials before uploading:

```bash
ia configure
```

Report does not require an account.

## Bash completion

Optional completion for `lmau` / `lma-upload` provides recording identifiers,
command options and paths. From the checkout, load it in the current shell:

```bash
source "$(pwd)/shell/completion.bash"
```

For future shells, add a source line with the checkout's absolute path to
`~/.bashrc` or another Bash startup file that your shell loads:

```bash
source "/absolute/path/to/lma-tools/shell/completion.bash"
```

Completion covers `lmau` and `lma-upload`. Interactive commands in `lmar`
are entered inside the application and do not need Bash completion.

## Upload recordings

```bash
lmau --list
lmau band2026-08-17.audience --preview
lmau band2026-08-17.audience
```

A recording output folder needs a band/date-style identifier, TXT notes and WAV
or FLAC audio. Discovery checks project directories under `shows_root` and
their immediate output directories. Explicit output paths also work. From
inside an output folder, the identifier can be omitted.

Notes begin with artist, date, venue and city, followed by recording notes and
a `Name <email>` credit. Source and lineage suggestions remain editable; see
[source inference](docs/SOURCE_PARSING.md).

| Mode | Local preparation | Archive action |
| --- | --- | --- |
| Default | Review and apply audio plan | Metadata, uploads, cleanup review |
| `-p` / `--prepare` | Review and apply audio plan | None |
| `--clean` | None | Cleanup only; existing item |
| `-m` | None | Review and update managed metadata and runtime |
| `-r` | None | Runtime only |
| `-u` | None | Upload existing TXT, JPG, FFP, MD5 and FLAC files |

Normal runs first match WAV/FLAC names and title tags to the notes setlist.
Review the editable TOML plan; Ctrl-S approves local changes. Preparation can
rename and retag audio, encode WAVs and rebuild manifests. Successful WAVs are
removed only after decoded PCM verification. This approval is separate from the
later `UPLOAD` approval: cancelling upload retains preparation. Use `-p` for
local preparation without Archive access.

Avoid normal preparation on partial folders. Use `-u` for existing files in an
existing item: it does not edit audio, item metadata or runtime. Manifest
validation and repair integration is described in
[Preparation](docs/PREPARATION.md), including explicit repair approval.
Metadata-only and runtime-only modes also bypass preparation.

Normal uploads review remote original extras with nothing selected by default.
Select numbers or `all`, then type `DELETE` to approve deletion. Use
`--no-clean` to omit that review, or standalone `--clean` for cleanup without
preparation, audio changes, file uploads or item metadata mutation. Conflicting
files offer replace (`r`), replace all conflicts (`a`) or cancel (`c`).
Matching files still skip after `a`. `--force` retains its legacy upload-all
behavior, including matching files.

Corrected uploads must be remotely checksum-verified before approved extras are
deleted. Deletes do not cascade. Accepted deletion is not finalized; bounded
metadata/HEAD checks must confirm absence before final derivation. If still
pending, the run retains the accepted-deletion log and exits; rerun cleanup
later. When cleanup is selected, per-file derivation waits until cleanup
finishes; a final derive request follows. Otherwise each new FLAC requests
derivation, followed by a final request. Small-file PUTs do not request
derivation. Archive controls task timing; upload completion does not mean
derivation is done. See [Preparation](docs/PREPARATION.md) for cleanup
safeguards and edge cases.

`--preview` prints the plan without network access, an editor or writes.
Metadata review uses Ctrl-S to accept and Ctrl-C to cancel; execution requires
typing `UPLOAD`. Existing-item metadata is updated before file transfers. When
no saved Creator applies, new items use recent collection naming before the
interactive review. It prefers the account email from `ia configure`, or
matches the transferer name when that email is unavailable. Up to three recent
personal uploads are checked first, then up to three other uploads if needed.
A majority of at least two matching Creator/Title patterns seeds those fields
with this show's venue and date. Conflicts, unfamiliar title formats and failed
lookups leave local naming in place. A comment identifies the suggestion.

Each ordinary run rebuilds TXT-derived metadata from the current notes. Saved
collection Creator preferences take precedence over the TXT artist for item
Creator and Title; FLAC Artist remains independent. Old log reviews cannot
replace these fresh defaults. Extra reviewed fields without a TXT equivalent
are retained; `--fresh-metadata` or `--reset-review` drops those saved extras.
Interactive review includes a read-only comparison with current Archive
metadata, including line differences for Description. Remote values are
comparison evidence, not replacement defaults. If lookup fails, the editor
reports that comparison is unavailable. Offline `--preview` remains local.

Both normal uploads and `-m` permit reviewed Creator and Title corrections with
server-side checks against concurrent changes. Identifier, Collection and
Mediatype remain protected on existing items; Archive may impose additional
restrictions. Descriptions are sent as
escaped plain text so angle-bracket email credits remain visible. Local changes
occur only in the approved preparation stage.

Only direct TXT/JPG/MD5/FFP/FLAC files are eligible. Runtime is summed from
exact FLAC sample counts and rounded once. Full, metadata and runtime modes
require the complete local FLAC inventory. Preparation rebuilds FFP and MD5
manifests; bypass modes use existing files. See the manifest validation limits
in [Preparation](docs/PREPARATION.md).

### Concurrent uploads

The upload tool runs up to three file transfers at a time by default. For a
new item, the first notes TXT is confirmed before remaining small files and
FLACs share the same bounded pool. Existing-item metadata updates finish before
any file
transfer starts. Upload-only mode does not edit metadata or runtime.

Each active file has its own progress bar, byte count and IEC speed. Active
rows stay in file order; completed rows disappear and the remaining rows close
the gap. Shared label, bar and metric columns keep all rows aligned. Long
filenames shorten to keep speeds visible. Routine start/finish messages are
suppressed in this display. Multipart retry details stay in the journal;
short retry states occupy the affected row until transfers proceed. One file's
retry does not restart the others. Redirected output uses ordinary status lines
without cursor controls.

Set `upload_workers` under `[upload]` in config.toml, or override per run
with `-w` / `--workers`:

```bash
lmau --workers 3 band2026-08-17.audience
lmau -u --workers 1 band2026-08-17.audience
```

`--workers 1` retains sequential uploads and the original single-file display.
Increasing the limit helps only while the connection and Archive can support
more transfers. A few workers are easier to monitor than every track at once.

A failed file stops new files from starting. Already-active transfers finish
and record their outcomes; derivation is queued only when all selected files
succeed. Ctrl-C stops scheduling, interrupts recovery waits and cancels body
reads. Requests awaiting a server reply or a library backoff may need to return
or reach their timeout before the run exits. Resume with the same command to
check and skip completed files.

### Resume and recovery

Matching remote whole-file MD5 and size skip completed files. A conflicting
checksum requires replacement approval before transfers begin. Append-only
`IDENTIFIER_lma.log` files in the project directory record approved metadata,
preparation settings, verified staging, final preparation outcomes, transfer
outcomes and recovery history. `--no-log` or `log = false` disables
logging and log-based reuse; remote verification still works. Notes are always
reparsed. `--fresh-metadata` drops saved extra metadata fields as well.

After a timeout or dropped connection, bounded recovery waits and verifies
remote MD5 and size before resending. A confirmed complete file advances to the
next file automatically. Confirmed absence permits retransmission; unknown
remote state triggers more checks without resending. A 100% progress bar alone
is not confirmation. Ordinary PUTs restart incomplete files in full. Default
multipart uploads resume acknowledged parts. Recovery uses the existing
approval and tmux session.

Defaults allow three additional recovery rounds or retransmissions, with three
checks per uncertain round and exponential waits from 30 to 300 seconds.
Settings are under `[upload.recovery]`; `retries = 0` disables recovery. CLI
`--retries` and `--retry-sleep` separately control the upload library's 503
retries. Authentication errors, certificate errors, local file changes and
Ctrl-C stop the run. Metadata edits and the final derive request do not use
this upload recovery loop.

### Resuming large FLACs

Large FLACs use resumable multipart uploads by default:

```bash
lmau -uw 8 band2026-08-17.audience
```

Defaults in the shared config:

```toml
[upload.resume]
enabled = true
part_size_mib = 16
```

`--no-resume` overrides that default. Small FLACs that fit in one part and all
non-FLAC files keep the ordinary PUT path. A multipart checkpoint already
present is resumed with its original part size. New part sizes must be whole
MiB values from 5 through 1024, with at most 10000 parts per file.

Parts stream directly from the original FLAC. No temporary audio copies,
partial downloads or separate resume files are created. Upload IDs, file
fingerprints, acknowledged part checksums and completion state are stored as
JSON events in the existing `<identifier>_lma.log` in the project root, outside
the upload folder. Part events are incremental, flushed and synced; the whole
checkpoint is not rewritten after every part. Log writes are locked across
upload threads and files use private permissions. API credentials are never
stored in the log.

Keep the log to resume after stopping, restarting or disconnecting. A changed
file or target cannot reuse an old checkpoint. The upload tool checks the
original
file's full MD5 on every run. Multipart resume requires logging: use `log =
true` and omit `--no-log`, or select `--no-resume` for ordinary PUTs.

The existing worker limit still applies across files; parts within a file are
serial, so enabling resume does not multiply network concurrency. On resume,
the upload tool compares local part hashes with Archive's part listing when
supported. If listing is unavailable, locally acknowledged parts are reused;
completion must validate the part MD5s and the final remote size and whole-file
MD5 must match before the file is counted as complete. A missing part
acknowledgement can cost one part again, rather than the whole file.

Recovery uses `[upload.recovery]` for part-request retry budgets and waits.
Completion verification still runs when `retries = 0`. Multipart completion
requests derivation for that FLAC. Assembly is asynchronous, so a 100% bar can
say `assembling` while the file is not yet visible. An uncertain completion
retains the checkpoint and does not start a new upload ID; rerunning checks
assembly instead of resending the whole file. After multipart assembly is
verified, a log event clears its active checkpoint; the history is retained.

Existing plain PUT transfers cannot be converted into multipart resumes. Users
report successful end-to-end Archive uploads, including multipart assembly. Our
automated network tests use mocks; they do not independently verify live
assembly or derivation. The protocol reference is [ia-
multipart](https://github.com/internetarchivecanada/ia-multipart); this package
implements its own backend using the existing dependencies.

### tmux sessions

Interactive uploads and updates open a unique tmux session unless already
inside tmux. Detach with Ctrl-b then d. Reconnect with `tmux attach -t
SESSION_NAME`; the session retains a shell and exit status after completion.
New session names start with the show identifier, without an added prefix. A
host reboot stops the session.

The first interactive tmux run also starts a detached `lmau` dashboard. View it
with `tmux attach -t lmau`. Inside tmux, use `tmux switch-client -t lmau`. Each
show has a compact aggregate byte/rate bar and a count of completed FLACs.
Elapsed time covers the current run. Remaining transfer time uses a smoothed
rate over roughly 30 seconds, excluding previously uploaded bytes from speed.
It estimates transfer time only; server acknowledgement and derivation can take
longer. Normal transfers omit a status label; other states stay visible. Labels
align to the longest visible identifier, and every row shares the same bar and
metric widths. The byte total covers selected files. The file counter always
covers all FLACs in the show folder, including verified skips; TXT/JPG/MD5/FFP
files never count toward it. Verified skips count immediately when checked.
Sent bytes include in-flight data, so 100% may still await acknowledgement or
derivation submission. Retries reset that file's in-flight count; interrupted
runs retain confirmed files and checkpointed multipart bytes. These counters
never authorize a skip; multipart checkpoints and remote verification control
resume.

Progress snapshots are private local state under `$XDG_STATE_HOME/lma-
tools/uploads`, or `~/.local/state/lma-tools/uploads`. The display retains
every run it observes until that dashboard closes. Old finished runs are not
restored when a new dashboard opens. A TOTAL row above the shows sums bytes,
speed and completed-FLAC counts for active runs and retained successful runs,
so finishing a show does not reset the overall progress. Cancelled, failed and
stale attempts remain visible but are excluded from the total. An upload whose
files all verified but whose final derive request failed stays in TOTAL with
status `derive failed`. An already queued/running derive is accepted without
failing the upload. Completed counts verify original uploads, not the
correctness or completion of playback derivatives. Stale runs are labelled.
Snapshots expire after 24 hours without updates; completed rows already
retained in the open dashboard remain visible even after their snapshot
expires. Closing the dashboard does not stop uploads; a later interactive run
recreates it. Runs started before this version cannot report their progress. An
existing session named `lmau` is left untouched.

Listing, help, completion, previews and noninteractive runs execute directly.
`LMA_NO_TMUX=1` bypasses tmux. Exported variables referenced by upload config
are forwarded into new sessions.

## Report on recordings

```bash
lmar
lmar 'collection:etree AND year:2025' --once
lmar 'collection:etree' --sort views --reverse --json
```

Positional query clauses replace configured clauses and are joined with AND.
Quote complete expressions and narrow the generic collection query before
submitting it. The interactive menu supports field filters, sorting, query
editing and favorites. Enter runs a report; `q` quits. `cache` toggles reuse of
search results, `reload` refreshes caches, and `debug` toggles in-memory
request timings. Successful view counts remain cached until reload or exit.

Views are lifetime counts, not unique listeners. Daily means lifetime views
divided by days since publicdate, with a one-day minimum. Unknown counts remain
unknown; totals use successful counts. Zero counts are excluded from averages
by default. Malformed runtime is excluded from runtime statistics. Show and
performance counts use metadata grouping heuristics.

JSON goes to stdout and warnings to stderr. Exit status 1 indicates an error or
incomplete view data; 130 indicates interruption. Views performs no Archive
write operations.

## Updates and checks

Preserve private config and avoid updating the checkout or its environments
during an upload. From the checkout, rerun `bash scripts/install.sh` after
dependency or entry-point changes. Ordinary source edits take effect through
the editable installation. `uv tool uninstall lma-tools` removes installed
commands while leaving the checkout and private data.

To run the offline test suite and style checks:

```bash
uv sync --locked
uv run --locked ruff format --check .
uv run --locked ruff check .
uv run --locked python -B -m tests
```

Checks also run on GitHub through [GitHub
Actions](.github/workflows/checks.yml). HTTP behavior is covered with mocks;
these checks do not perform live uploads. Implementation decisions and
validation limits are in [Development notes](docs/DEVELOPMENT.md).

## License

[GNU Affero General Public License, version 3 only](LICENSE).
