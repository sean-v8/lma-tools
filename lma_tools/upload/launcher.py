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

    visit(read_section(path, "upload"))
    missing = sorted(name for name in names if name not in os.environ)
    if missing:
        raise ValueError(
            "Export variables referenced by config before running: "
            + ", ".join(missing)
        )
    return sorted(names)


def ensure_dashboard():
    """Create or reuse our session without changing unrelated sessions."""
    command = [
        sys.executable,
        "-B",
        "-m",
        "lma_tools.upload.dashboard",
        "--state-dir",
        str(state_directory()),
    ]
    exists = (
        subprocess.run(
            ["tmux", "has-session", "-t", "=lmau"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        == 0
    )
    if exists:
        panes = subprocess.run(
            [
                "tmux",
                "list-panes",
                "-s",
                "-t",
                "=lmau",
                "-F",
                "#{window_id}\t#{pane_start_command}",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        overview = next(
            (
                line.split("\t", 1)[0]
                for line in panes.splitlines()
                if "lma_tools.upload.dashboard" in line
            ),
            None,
        )
        marker = subprocess.run(
            [
                "tmux",
                "show-options",
                "-qv",
                "-t",
                "=lmau",
                "@lma-tools-session",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if overview is None and marker != "1":
            raise ValueError(
                "An unrelated tmux session is named lmau; "
                "rename it before starting upload views."
            )
        if overview:
            subprocess.run(
                ["tmux", "rename-window", "-t", overview, "Overview"],
                check=True,
            )
        else:
            subprocess.run(
                [
                    "tmux",
                    "new-window",
                    "-d",
                    "-t",
                    "lmau:",
                    "-n",
                    "Overview",
                    shlex.join(command),
                ],
                check=True,
            )
    else:
        subprocess.run(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                "lmau",
                "-n",
                "Overview",
                shlex.join(command),
            ],
            check=True,
        )
    # Preserve user root bindings, including mouse controls, in our table.
    bindings = subprocess.run(
        ["tmux", "list-keys", "-T", "root"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    bindings = "\n".join(
        line.replace(" -T root ", " -T lmau-navigation ", 1)
        for line in bindings.splitlines()
    )
    if bindings:
        subprocess.run(
            ["tmux", "source-file", "-"],
            input=bindings + "\n",
            text=True,
            check=True,
        )
    for command in (
        ["set-option", "-t", "=lmau", "@lma-tools-session", "1"],
        ["set-option", "-t", "=lmau", "key-table", "lmau-navigation"],
        ["bind-key", "-T", "lmau-navigation", "M-Left", "previous-window"],
        ["bind-key", "-T", "lmau-navigation", "M-Right", "next-window"],
    ):
        subprocess.run(["tmux", *command], check=True)


def launch(argv):
    args = cli.parser().parse_args(argv)
    if args.prepare:
        return cli.main(argv)
    direct = (
        args.preview
        or args.dry_run
        or args.list
        or args.complete is not None
        or args.session_name
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
    config_names(path)
    # Resolve the identifier with the same logic as the actual upload.
    from contextlib import redirect_stdout
    from io import StringIO

    with redirect_stdout(StringIO()) as output:
        cli.main([*argv, "--session-name"])
    name = output.getvalue().strip()
    ensure_dashboard()
    start_window(name, argv, path)
    print("Starting tmux session lmau (detach: Ctrl-b d).", flush=True)
    if os.environ.get("TMUX"):
        subprocess.run(["tmux", "switch-client", "-t", "=lmau"], check=True)
        return 0
    os.execvp("tmux", ["tmux", "attach-session", "-t", "=lmau"])


def start_window(name, argv, path, *, select=True):
    """Give each upload its own terminal in the shared upload session."""
    names = config_names(path)
    env_args = []
    for variable in names:
        env_args.extend(["-e", f"{variable}={os.environ[variable]}"])
    env_args.extend(["-e", f"LMA_UPLOAD_SESSION={name}"])
    env_args.extend(["-e", f"LMA_UPLOAD_STATE_DIR={state_directory()}"])
    command = [
        sys.executable,
        "-B",
        "-m",
        "lma_tools.upload.cli",
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
    command = [
        "tmux",
        "new-window",
        "-t",
        "lmau:",
        "-n",
        name,
        "-c",
        os.getcwd(),
        *env_args,
    ]
    if not select:
        command.append("-d")
    command.extend(["/bin/bash", "--noprofile", "--norc", "-c", script])
    subprocess.run(command, check=True)


def main():
    return cli.cli(sys.argv[1:], runner=launch)
