"""Keep interactive upload sessions alive across terminal disconnects."""

import os
import re
import shlex
import shutil
import subprocess
import sys

from lma_tools.config import config_path, read_section

from . import cli

VARIABLE = re.compile(
    r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))"
)


def config_names(path):
    names = {"PATH"}

    def visit(value):
        if isinstance(value, str):
            names.update(a or b for a, b in VARIABLE.findall(value))
        elif isinstance(value, dict):
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(read_section(path, "uploader"))
    missing = sorted(name for name in names if name not in os.environ)
    if missing:
        raise ValueError(
            "Export variables referenced by config before running: "
            + ", ".join(missing)
        )
    return sorted(names)


def launch(argv):
    args = cli.parser().parse_args(argv)
    direct = (
        args.preview
        or args.list
        or args.complete is not None
        or args.session_name
        or os.environ.get("TMUX")
        or os.environ.get("LMA_NO_TMUX") == "1"
        or not (sys.stdin.isatty() and sys.stdout.isatty())
    )
    if direct:
        return cli.main(argv)
    if not shutil.which("tmux"):
        raise ValueError(
            "tmux is required for uploads. On Debian/Ubuntu: "
            "sudo apt install tmux; or use LMA_NO_TMUX=1 lmau ..."
        )
    path = config_path(args.config)
    cli.load_config(path)
    names = config_names(path)
    # Resolve the identifier with the same logic as the actual upload.
    from contextlib import redirect_stdout
    from io import StringIO

    with redirect_stdout(StringIO()) as output:
        cli.main([*argv, "--session-name"])
    base = output.getvalue().strip()
    session = base
    number = 2
    while (
        subprocess.run(
            ["tmux", "has-session", "-t", "=" + session],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        == 0
    ):
        session = f"{base}_{number}"
        number += 1
    env_args = []
    for name in names:
        env_args.extend(["-e", f"{name}={os.environ[name]}"])
    print(f"Starting tmux session {session} (detach: Ctrl-b d).", flush=True)
    command = [
        sys.executable,
        "-B",
        "-m",
        "lma_tools.uploader.cli",
        *argv,
        "--config",
        str(path),
    ]
    script = (
        shlex.join(command)
        + '; status=$?; printf "\\nlma exited with status %s. '
        + 'Session retained; type exit to close it.\\n" "$status"; '
        + "exec /bin/bash -i"
    )
    os.execvp(
        "tmux",
        [
            "tmux",
            "new-session",
            "-s",
            session,
            "-c",
            os.getcwd(),
            *env_args,
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-c",
            script,
        ],
    )


def main():
    return cli.cli(sys.argv[1:], runner=launch)
