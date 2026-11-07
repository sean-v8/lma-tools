"""Keep interactive upload sessions alive across terminal disconnects."""

import os
import re
import shlex
import shutil
import subprocess
import sys

from lma_tools.config import config_path, read_section

from . import cli
from .dashboard import state_directory

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


def ensure_dashboard():
    """Create one detached view; an unrelated existing lmau is left alone."""
    if (
        subprocess.run(
            ["tmux", "has-session", "-t", "=lmau"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        == 0
    ):
        return
    command = [
        sys.executable,
        "-B",
        "-m",
        "lma_tools.uploader.dashboard",
        "--state-dir",
        str(state_directory()),
    ]
    result = subprocess.run(
        ["tmux", "new-session", "-d", "-s", "lmau", shlex.join(command)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        print("Dashboard not started; continuing show session.", flush=True)


def launch(argv):
    args = cli.parser().parse_args(argv)
    if args.prepare:
        return cli.main(argv)
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
        if (
            os.environ.get("TMUX")
            and not (args.preview or args.list or args.session_name)
            and args.complete is None
            and os.environ.get("LMA_NO_TMUX") != "1"
            and sys.stdin.isatty()
            and sys.stdout.isatty()
            and shutil.which("tmux")
        ):
            ensure_dashboard()
            result = subprocess.run(
                ["tmux", "display-message", "-p", "#{session_name}"],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                previous = os.environ.get("LMA_UPLOAD_SESSION")
                os.environ["LMA_UPLOAD_SESSION"] = result.stdout.strip()
                try:
                    return cli.main(argv)
                finally:
                    if previous is None:
                        os.environ.pop("LMA_UPLOAD_SESSION", None)
                    else:
                        os.environ["LMA_UPLOAD_SESSION"] = previous
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
    env_args.extend(["-e", f"LMA_UPLOAD_SESSION={session}"])
    # A tmux server started earlier may have different XDG defaults.
    env_args.extend(["-e", f"LMA_UPLOAD_STATE_DIR={state_directory()}"])
    ensure_dashboard()
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
