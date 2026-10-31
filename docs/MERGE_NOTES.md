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

- 162 offline tests passed, including Ruff and strict text line limits.
- Ruff formatting and lint checks passed.
- Bash syntax checks passed for installation and completion scripts.
- Isolated uv tool installation created lmau, lmav and ia executables.
- Installed help/version smoke checks passed from outside the checkout.
- Source distribution and wheel builds succeeded.
- Migration was checked against both supplied private configs and mappings,
  without including those values in the deliverable.

Archive HTTP behavior and uploads were tested with mocks. No live Archive
search, metadata update, transfer or derive request was performed. Dependency
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
