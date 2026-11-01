# lma-tools

Command-line tools for the Internet Archive's Live Music Archive.

| Command | Purpose |
| --- | --- |
| `lmau` | Review metadata, upload recordings and resume interrupted uploads |
| `lmav` | Search recordings and report view counts and runtime |

## Installation

Requirements: Linux, Bash and uv. Interactive uploads also require tmux
(`sudo apt install tmux` on Debian/Ubuntu). The installer uses Python 3.12.

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

The installer uses locked dependencies and creates `lmau`, `lmav` and `ia`
commands through `uv tool install`. Keep the checkout in place: installation
is editable. No aliases are required. If the commands are not on PATH, run
`uv tool update-shell` and open a new terminal.

## Configuration

From the checkout, create your private config without replacing an existing
file:

```bash
test -e config.toml || cp config.example.toml config.toml
```

Edit `config.toml` before uploading. For example:

```toml
[uploader]
shows_root = "/absolute/path/to/recordings"
log = true

[uploader.collections]
band = "ArchiveCollection"

[views]
query = ['collection:(etree OR taperssection)']
sort = "publicdate"
```

`shows_root` must be an existing absolute directory. Exported environment
variables and `~` are supported. Collection mappings associate identifier
prefixes with Archive collection names; approved mappings are saved here
without changing unrelated settings or comments. See
[config.example.toml](config.example.toml) for optional defaults, including
upload recovery settings.

Both commands select config independently of the current directory:

1. `--config PATH`, when supplied.
2. The `LMA_TOOLS_CONFIG` environment variable.
3. The editable checkout's `config.toml`.
4. For a noneditable installation, `$XDG_CONFIG_HOME/lma-tools/config.toml`
   or `~/.config/lma-tools/config.toml`.

Uploader requires config. Views can use generic defaults if its default file
is absent. An explicitly selected missing file is an error. Keep config,
credentials, recordings and upload logs out of Git; config is Git-ignored.

Configure Archive credentials before uploading:

```bash
ia configure
```

Views does not require an account.

## Bash completion

Optional completion for `lmau` provides recording identifiers, command options
and paths. From the checkout, load it in the current shell:

```bash
source "$(pwd)/shell/completion.bash"
```

For future shells, add a source line with the checkout's absolute path to
`~/.bashrc` or another Bash startup file that your shell loads:

```bash
source "/absolute/path/to/lma-tools/shell/completion.bash"
```

Completion is registered only for `lmau`. The interactive commands in `lmav`
are entered inside the application and do not need Bash completion.

## Upload recordings

```bash
lmau --list
lmau band2026-08-17.audience --preview
lmau band2026-08-17.audience
```

A recording output folder needs a band/date-style identifier, TXT notes and
FLAC audio. Discovery checks project directories under `shows_root` and their
immediate output directories. Explicit output paths also work. From inside
an output folder, the identifier can be omitted.

Notes begin with artist, date, venue and city, followed by recording notes
and a `Name <email>` credit. Source and lineage suggestions remain editable;
see [source inference](docs/SOURCE_PARSING.md).

| Mode | Metadata | File transfers |
| --- | --- | --- |
| Default | Review metadata and runtime | Small files and FLACs |
| `-m` | Review and update managed fields, including runtime | None |
| `-r` | Update runtime only | None |
| `-u` | No metadata or runtime changes | FLACs only |

For example, `lmau -u band2026-08-17.audience` uploads only FLACs. Normal and
upload-only runs queue derivation after successful file processing.

`--preview` prints the plan without network access, an editor or writes.
Metadata review uses Ctrl-S to accept and Ctrl-C to cancel; execution requires
typing `UPLOAD`. Existing-item metadata is updated before file transfers.
Before the interactive review, the uploader checks recent collection naming.
It prefers the account email from `ia configure`, or matches the transferer
name when that email is unavailable. Up to three recent personal uploads are
checked first, then up to three other uploads if needed. A majority of at
least two matching Creator/Title patterns seeds those fields with this show's
venue and date. Conflicts, unfamiliar title formats and failed lookups leave
local naming in place. A comment in the editor identifies the suggestion.
Saved reviews take precedence; `--fresh-metadata` starts a fresh review.
Existing items use their current Creator/Title when starting a fresh review.
The offline `--preview` command does not perform this lookup.

`-m` permits reviewed Creator and Title corrections with server-side checks
against concurrent changes. Normal upload/resume runs still protect those
fields. Identifier, Collection and Mediatype remain protected on existing
items; Archive may impose additional restrictions. Descriptions are sent as
escaped plain text so angle-bracket email credits remain visible.
Show files are never changed, moved or deleted.

Only direct TXT/JPG/MD5/FFP/FLAC files are eligible. Runtime is summed from
exact FLAC sample counts and rounded once. Full, metadata and runtime modes
require the complete local FLAC inventory. FFP inventories and embedded
fingerprints are checked; MD5 manifests are uploaded as supplied.

### Concurrent uploads

The uploader runs up to three file transfers at a time by default. After a
new item's first notes TXT is confirmed, remaining small files and FLACs
share the same bounded pool. Existing-item metadata updates finish before
any file transfer starts. Upload-only mode does not edit metadata or runtime.

Each active file has its own progress bar, byte count and IEC speed. Active
rows stay in file order; completed rows disappear and the remaining rows close
the gap. Long filenames shorten to keep speeds visible. Routine start/finish
messages are suppressed in this display. Recovery messages identify the file;
one file's retry does not restart the others. Redirected output uses ordinary
status lines without cursor controls.

Set `upload_workers` under `[uploader]` in config.toml, or override per run
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
reads. Requests awaiting a server reply or a library backoff may need to
return or reach their timeout before the run exits. Resume with the same
command to check and skip completed files.

### Resume and recovery

Matching remote whole-file MD5 and size skip completed files. A conflicting
checksum stops the run unless `--force` explicitly permits replacement.
Append-only `IDENTIFIER_lma.log` files in the project directory record
approved metadata, transfer outcomes and recovery history. `--no-log` or
`log = false` disables logging and log-based reuse; remote verification still
works. `--fresh-metadata` reparses notes instead of reusing reviewed defaults.

After a timeout or dropped connection, bounded recovery waits and verifies
remote MD5 and size before resending. A confirmed complete file advances to
the next file automatically. Confirmed absence permits retransmission; unknown
remote state triggers more checks without resending. A 100% progress bar alone
is not confirmation. Incomplete files restart in full, without byte-offset
resume. Recovery uses the existing approval and tmux session.

Defaults allow three additional recovery rounds or retransmissions, with
three checks per uncertain round and exponential waits from 30 to 300 seconds.
Settings are under `[uploader.recovery]`; `retries = 0` disables recovery.
CLI `--retries` and `--retry-sleep` separately control the upload library's
503 retries. Authentication errors, certificate errors, local file changes
and Ctrl-C stop the run. Metadata edits and the final derive request do not
use this upload recovery loop.

### tmux sessions

Interactive uploads and updates open a unique tmux session unless already
inside tmux. Detach with Ctrl-b then d. Reconnect with `tmux attach -t
SESSION_NAME`; the session retains a shell and exit status after completion.
New session names start with the show identifier, without an added prefix. A
host reboot stops the session.

The first interactive tmux run also starts a detached `lmau` dashboard. View it
with `tmux attach -t lmau`. Inside tmux, use `tmux switch-client -t lmau`. Each
show has a compact aggregate byte/rate bar and a count of confirmed files. The
total covers files selected by that run; `-u` covers FLACs only. Verified skips
count immediately when checked. Sent bytes include in-flight data, so 100% may
still await acknowledgement or derivation submission. Retries reset that file's
in-flight count; interrupted runs retain only confirmed bytes. These counters
never authorize a skip or provide partial-file resume.

Progress snapshots are private local state under
`$XDG_STATE_HOME/lma-tools/uploads`, or `~/.local/state/lma-tools/uploads`. The
display keeps live runs and the six latest finished runs. Stale runs are
labelled, and snapshots expire after 24 hours without updates. Closing the
dashboard does not stop uploads; a later interactive run recreates it. Runs
started before this version cannot report their progress. An existing session
named `lmau` is left untouched.

Listing, help, completion, previews and noninteractive runs execute directly.
`LMA_NO_TMUX=1` bypasses tmux. Exported variables referenced by uploader config
are forwarded into new sessions.

## Search recordings

```bash
lmav
lmav 'collection:etree AND year:2025' --once
lmav 'collection:etree' --sort views --reverse --json
```

Positional query clauses replace configured clauses and are joined with AND.
Quote complete expressions and narrow the generic collection query before
submitting it. The interactive menu supports field filters, sorting, query
editing and favorites. Enter runs a report; `q` quits. `cache` toggles reuse
of search results, `reload` refreshes caches, and `debug` toggles in-memory
request timings. Successful view counts remain cached until reload or exit.

Views are lifetime counts, not unique listeners. Daily means lifetime views
divided by days since publicdate, with a one-day minimum. Unknown counts
remain unknown; totals use successful counts. Zero counts are excluded from
averages by default. Malformed runtime is excluded from runtime statistics.
Show and performance counts use metadata grouping heuristics.

JSON goes to stdout and warnings to stderr. Exit status 1 indicates an error
or incomplete view data; 130 indicates interruption. Views performs no
Archive write operations.

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
uv run --locked python -m unittest discover -s tests -t .
```

Checks also run on GitHub through
[GitHub Actions](.github/workflows/checks.yml).
HTTP behavior is covered with mocks; these checks do not perform live uploads.

## License

[GNU Affero General Public License, version 3 only](LICENSE).
