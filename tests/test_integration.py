"""Shared config, migration and installed-launcher regression coverage."""

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
from tests.helpers import migration

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

    def test_migration_preserves_values_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            up, view, dest = (root / name for name in ("u", "v", "merged"))
            up.mkdir()
            view.mkdir()
            (up / "config.toml").write_text(f'shows_root = "{tmp}"\n')
            (view / "config.toml").write_text('query = ["venue:demo"]\n')
            (up / "collections.csv").write_text("band,collection\n")
            with redirect_stdout(io.StringIO()):
                migration.merge(up, view, dest / "config.toml")
            self.assertEqual(
                load_config(dest / "config.toml").query, ("venue:demo",)
            )
            self.assertEqual(
                cli.load_config(dest / "config.toml")["collections"],
                {"band": "collection"},
            )
            before = (dest / "config.toml").read_bytes()
            self.assertFalse((dest / "collections.csv").exists())
            with self.assertRaises(FileExistsError):
                migration.merge(up, view, dest / "config.toml")
            self.assertEqual((dest / "config.toml").read_bytes(), before)

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
                run.side_effect = [Mock(returncode=0), Mock(returncode=1)]
                launcher.launch(argv)
            command = execute.call_args.args[1]
            self.assertIn("lma_band_timestamp_2", command)
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
            ([], {"TMUX": "existing"}),
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

    def test_inside_tmux_tracks_actual_session_and_restores_environment(self):
        with (
            patch.dict(
                os.environ, {"TMUX": "socket", "PATH": "/bin"}, clear=True
            ),
            patch.object(launcher.sys.stdin, "isatty", return_value=True),
            patch.object(launcher.sys.stdout, "isatty", return_value=True),
            patch.object(launcher.shutil, "which", return_value="/bin/tmux"),
            patch.object(launcher, "ensure_dashboard") as ensure,
            patch.object(
                launcher.subprocess,
                "run",
                return_value=Mock(returncode=0, stdout="artist-session\n"),
            ),
            patch.object(cli, "main") as run,
        ):
            run.side_effect = lambda args: os.environ["LMA_UPLOAD_SESSION"]
            self.assertEqual(launcher.launch([]), "artist-session")
            self.assertNotIn("LMA_UPLOAD_SESSION", os.environ)
            ensure.assert_called_once()

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
