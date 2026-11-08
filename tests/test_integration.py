"""Shared config and installed-launcher regression coverage."""

import io
import os
import shlex
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from lma_tools.config import config_path
from lma_tools.report.config import load_config
from lma_tools.upload import cli, launcher

ROOT = Path(__file__).resolve().parents[1]


class IntegrationTests(unittest.TestCase):
    def test_shared_config_and_relative_mapping_from_other_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                f'[upload]\nshows_root = "{tmp}"\n'
                '[report]\nquery = ["collection:demo"]\nsort = "v"\n'
            )
            with patch.dict(os.environ, LMA_TOOLS_CONFIG=str(path)):
                self.assertEqual(config_path(), path)
                self.assertEqual(load_config().sort, "venue")
                self.assertEqual(
                    cli.load_config(path)["shows_root"], path.parent
                )
                with patch.object(cli, "scan_outputs", return_value=[]):
                    self.assertEqual(cli.main(["--list"]), 0)
            # Explicit config takes precedence over the environment.
            with patch.dict(os.environ, LMA_TOOLS_CONFIG="/missing"):
                self.assertEqual(config_path(path), path)
                with self.assertRaises(FileNotFoundError):
                    load_config()

    def test_tmux_forwards_environment_and_quotes_arguments(self):
        with tempfile.TemporaryDirectory(prefix="lma tools ") as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                '[upload]\nshows_root = "$RECORDINGS"\n'
                '[report]\nquery = ["$UNEXPORTED_VIEW_VALUE"]\n'
            )
            env = {"PATH": os.environ["PATH"], "RECORDINGS": tmp}
            argv = ["--config", str(path), "band2026-08-17.$literal"]

            def fake_main(args):
                self.assertIn("--session-name", args)
                print("lma_band_timestamp")
                return 0

            with (
                patch.dict(os.environ, env, clear=True),
                redirect_stdout(io.StringIO()),
                patch.object(launcher.sys.stdin, "isatty", return_value=True),
                patch.object(launcher.sys.stdout, "isatty", return_value=True),
                patch.object(launcher.shutil, "which", return_value="/tmux"),
                patch.object(cli, "main", side_effect=fake_main),
                patch.object(launcher.subprocess, "run") as run,
                patch.object(launcher.os, "execvp") as execute,
                patch.object(launcher, "ensure_dashboard"),
            ):
                run.return_value = Mock(returncode=0)
                launcher.launch(argv)
            self.assertEqual(
                execute.call_args.args[1],
                ["tmux", "attach-session", "-t", "=lmau"],
            )
            command = run.call_args.args[0]
            self.assertEqual(command[:2], ["tmux", "new-window"])
            self.assertIn("lma_band_timestamp", command)
            self.assertIn(f"RECORDINGS={tmp}", command)
            self.assertNotIn("UNEXPORTED_VIEW_VALUE", " ".join(command))
            script = command[-1]
            self.assertIn(shlex.quote(argv[-1]), script)
            self.assertIn(shlex.quote(str(path)), script)
            self.assertIn("status=$?", script)

    def test_direct_modes_and_tmux_bypass(self):
        for arguments, env in (
            (["--preview"], {}),
            (["--list"], {}),
            (["--complete", "x"], {}),
            ([], {"LMA_NO_TMUX": "1"}),
        ):
            with (
                self.subTest(arguments=arguments, env=env),
                patch.dict(os.environ, env, clear=True),
                patch.object(launcher.sys.stdin, "isatty", return_value=True),
                patch.object(launcher.sys.stdout, "isatty", return_value=True),
                patch.object(cli, "main", return_value=7) as run,
                patch.object(launcher.shutil, "which", return_value=None),
            ):
                self.assertEqual(launcher.launch(arguments), 7)
                run.assert_called_once_with(arguments)

    def test_inside_tmux_switches_to_shared_upload_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(f'[upload]\nshows_root = "{tmp}"\n')
            with (
                patch.dict(os.environ, {"TMUX": "socket"}),
                patch.object(launcher.sys.stdin, "isatty", return_value=True),
                patch.object(launcher.sys.stdout, "isatty", return_value=True),
                patch.object(launcher.shutil, "which", return_value="/tmux"),
                patch.object(launcher, "ensure_dashboard"),
                patch.object(launcher, "start_window") as window,
                patch.object(launcher.subprocess, "run") as run,
                patch.object(
                    cli, "main", side_effect=lambda a: print("Review")
                ),
            ):
                self.assertEqual(launcher.launch(["--config", str(path)]), 0)
                window.assert_called_once()
                self.assertEqual(window.call_args.args[0], "Review")
                run.assert_called_once_with(
                    ["tmux", "switch-client", "-t", "=lmau"], check=True
                )

    def test_missing_config_exports(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text('[upload]\nshows_root = "$MISSING_EXPORT"\n')
            with patch.dict(os.environ, {"PATH": "/bin"}, clear=True):
                with self.assertRaisesRegex(ValueError, "MISSING_EXPORT"):
                    launcher.config_names(path)

    def test_config_file_defaults_do_not_follow_working_directory(self):
        self.assertEqual(config_path(), ROOT / "config.toml")
        result = subprocess.run(
            [str(ROOT / ".venv/bin/lmar"), "--help"],
            cwd="/tmp",
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_config_requires_known_table_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            for text in (
                'query=["collection:demo"]',
                '[other]\nsort="views"',
                'upload="invalid"\n[report]\nsort="views"',
            ):
                with self.subTest(text=text):
                    path.write_text(text)
                    before = path.read_bytes()
                    with self.assertRaises(ValueError):
                        load_config(path)
                    self.assertEqual(path.read_bytes(), before)
