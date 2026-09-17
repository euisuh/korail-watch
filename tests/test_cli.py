import os
import plistlib
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from korail_watch import cli


class CliTests(unittest.TestCase):
    def _trip_config(self, directory: str, extra: str = "") -> Path:
        path = Path(directory) / "trip.toml"
        path.write_text(
            '[trip]\ndate = "2099-09-24"\nstart = "12:00"\nend = "18:00"\n'
            'departures = ["서울"]\narrivals = ["대전"]\nadults = 1\n' + extra
        )
        return path

    def _launchd_fixture(self, root: Path):
        fake_bin = root / "bin"
        fake_bin.mkdir()
        scripts = {
            "korail-watch": """#!/bin/sh
printf '%s\n' "$*" >> "$WATCH_CALLS"
printf '%s\n' "${CHECK_RESULT:-not-found}"
""",
            "caffeinate": "#!/bin/sh\nexit 0\n",
            "id": "#!/bin/sh\n[ \"$1\" = -u ] || exit 2\necho 501\n",
            "launchctl": """#!/bin/sh
printf '%s\n' "$*" >> "$LAUNCHCTL_CALLS"
case "$1" in
  print)
    if [ -n "${PRINT_ERROR_STATUS:-}" ]; then
      echo "inspection failed" >&2
      exit "$PRINT_ERROR_STATUS"
    fi
    if [ -f "$LOADED_MARKER" ]; then
      exit 0
    fi
    echo 'Could not find service "com.euisuh.korail-watch" in domain for user gui: 501' >&2
    exit 113
    ;;
  disable|enable) exit 0 ;;
  bootout)
    if [ -n "${BOOTOUT_ERROR_STATUS:-}" ]; then
      echo "bootout failed" >&2
      exit "$BOOTOUT_ERROR_STATUS"
    fi
    [ "${KEEP_LOADED:-0}" = 1 ] || rm -f "$LOADED_MARKER"
    ;;
  bootstrap) touch "$LOADED_MARKER" ;;
  kickstart) exit 0 ;;
  *) exit 64 ;;
esac
""",
        }
        for name, body in scripts.items():
            executable = fake_bin / name
            executable.write_text(body)
            executable.chmod(0o755)
        config = self._trip_config(str(root))
        calls = root / "launchctl.calls"
        watch_calls = root / "watch.calls"
        loaded = root / "loaded"
        env = {
            **os.environ,
            "HOME": str(root),
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "LAUNCHCTL_CALLS": str(calls),
            "WATCH_CALLS": str(watch_calls),
            "LOADED_MARKER": str(loaded),
        }
        env.pop("UID", None)
        script = Path(__file__).parents[1] / "scripts" / "launchd.sh"
        plist = root / "Library" / "LaunchAgents" / "com.euisuh.korail-watch.plist"
        return script, config, plist, calls, loaded, env

    def test_configure_writes_private_credentials_outside_repository(self):
        values = iter(("member", "password", "token", "chat"))
        with tempfile.TemporaryDirectory() as home, patch.object(Path, "home", return_value=Path(home)), patch(
            "getpass.getpass", side_effect=lambda _: next(values)
        ):
            self.assertEqual(0, cli.main(["configure"]))
            path = cli.credentials_path()
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
            self.assertEqual(0o700, stat.S_IMODE(path.parent.stat().st_mode))
            self.assertIn('KORAIL_ID = "member"', path.read_text())

    def test_credentials_do_not_replace_environment(self):
        values = iter(("file-id", "password", "token", "chat"))
        with tempfile.TemporaryDirectory() as home, patch.object(Path, "home", return_value=Path(home)), patch(
            "getpass.getpass", side_effect=lambda _: next(values)
        ), patch.dict(os.environ, {"KORAIL_ID": "environment-id"}, clear=False):
            cli.configure()
            cli.load_credentials("KORAIL_ID")
            self.assertEqual("environment-id", os.environ["KORAIL_ID"])

    def test_read_only_credentials_do_not_require_telegram(self):
        values = iter(("member", "password", "", ""))
        with tempfile.TemporaryDirectory() as home, patch.object(Path, "home", return_value=Path(home)), patch(
            "getpass.getpass", side_effect=lambda _: next(values)
        ), patch.dict(os.environ, {}, clear=True):
            cli.configure()
            cli.load_credentials("KORAIL_ID", "KORAIL_PASSWORD")
            self.assertEqual("member", os.environ["KORAIL_ID"])

    def test_watch_requires_explicit_arm(self):
        with self.assertRaises(SystemExit) as raised:
            cli._parser().parse_args(["watch"])
        self.assertEqual(2, raised.exception.code)

    def test_continuous_is_explicit_and_watch_only(self):
        args = cli._parser().parse_args(["watch", "--arm", "--continuous"])
        self.assertTrue(args.continuous)

        legacy = cli._parser().parse_args(["watch", "--arm"])
        self.assertFalse(legacy.continuous)

        with self.assertRaises(SystemExit) as raised:
            cli._parser().parse_args(["check", "--continuous"])
        self.assertEqual(2, raised.exception.code)

    def test_interval_cannot_be_faster_than_five_seconds(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "trip.toml"
            config.write_text("[provider]\ninterval = 4.9\n")
            with self.assertRaisesRegex(ValueError, "at least 5"):
                cli.provider_interval(config)

    def test_diagnostics_are_configured_before_login_for_network_commands(self):
        class Provider:
            supported_modes = frozenset()

            def login(self):
                calls.append("login")

        class Notifier:
            def send(self, _text):
                pass

        for command in ("check", "watch"):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as directory:
                calls = []
                config = self._trip_config(directory)
                state = Path(directory) / "state"
                argv = [command, "--config", str(config), "--state-dir", str(state)]
                if command == "watch":
                    argv.append("--arm")
                env = {
                    "KORAIL_ID": "member",
                    "KORAIL_PASSWORD": "password",
                    "TELEGRAM_BOT_TOKEN": "123456:abcdefghijklmnopqrstuvwxyzABCDEFGH",
                    "TELEGRAM_CHAT_ID": "123",
                }
                with patch.dict(os.environ, env, clear=True), patch(
                    "korail_watch.diagnostics.configure", side_effect=lambda path: calls.append(("configure", path))
                ), patch("korail_watch.korail.KorailProvider", return_value=Provider()), patch(
                    "korail_watch.notifier.TelegramNotifier", return_value=Notifier()
                ), patch("korail_watch.engine.run", return_value="not-found"):
                    self.assertEqual(0, cli.main(argv))
                self.assertEqual([("configure", state), "login"], calls)

    def test_trip_reservation_modes_default_off_and_load_booleans(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy = cli.load_trip(self._trip_config(directory))
            self.assertFalse(legacy.allow_waitlist)
            self.assertFalse(legacy.allow_standing)
            self.assertFalse(legacy.allow_mixed)

            configured = cli.load_trip(
                self._trip_config(
                    directory,
                    "allow_waitlist = true\nallow_standing = true\nallow_mixed = true\n",
                )
            )
            self.assertTrue(configured.allow_waitlist)
            self.assertTrue(configured.allow_standing)
            self.assertTrue(configured.allow_mixed)

    def test_requested_unsupported_modes_warn_with_app_handoff(self):
        trip = type(
            "Trip",
            (),
            {"allow_waitlist": True, "allow_standing": True, "allow_mixed": True},
        )()
        provider = type("Provider", (), {"supported_modes": frozenset({"waitlist", "standing"})})()
        output = StringIO()
        with redirect_stderr(output):
            cli._warn_unsupported(trip, provider)
        warning = output.getvalue()
        self.assertNotIn("waitlist was requested", warning)
        self.assertNotIn("standing-only was requested", warning)
        self.assertIn("standing+seat was requested but is unsupported", warning)
        self.assertIn("official Korail app", warning)

    def test_example_enables_only_verified_flexible_modes(self):
        trip = cli.load_trip(Path(__file__).parents[1] / "trip.example.toml")
        self.assertTrue(trip.allow_waitlist)
        self.assertTrue(trip.allow_standing)
        self.assertFalse(trip.allow_mixed)

    def test_launchd_install_modes_and_failed_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script, config, plist, calls, _loaded, env = self._launchd_fixture(root)

            subprocess.run(["sh", script, "install", config, "--continuous"], env=env, check=True, capture_output=True)
            with plist.open("rb") as handle:
                continuous = plistlib.load(handle)["ProgramArguments"]
            self.assertEqual("--continuous", continuous[-1])

            subprocess.run(["sh", script, "install", config], env=env, check=True, capture_output=True)
            with plist.open("rb") as handle:
                legacy = plistlib.load(handle)["ProgramArguments"]
            self.assertNotIn("--continuous", legacy)

            saved = plist.read_bytes()
            calls.write_text("")
            failed = subprocess.run(
                ["sh", script, "install", config],
                env={**env, "CHECK_RESULT": "blocked"},
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(0, failed.returncode)
            self.assertEqual("", calls.read_text())
            self.assertEqual(saved, plist.read_bytes())

    def test_launchd_pause_is_persistent_idempotent_and_preserves_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script, config, plist, calls, loaded, env = self._launchd_fixture(root)
            plist.parent.mkdir(parents=True)
            plist.write_bytes(b"saved plist")
            log = root / "Library" / "Logs" / "korail-watch.log"
            log.parent.mkdir(parents=True)
            log.write_text("saved log")
            loaded.touch()

            paused = subprocess.run(
                ["sh", script, "pause"], env=env, check=True, capture_output=True, text=True
            )
            target = "gui/501/com.euisuh.korail-watch"
            self.assertEqual(
                [f"disable {target}", f"print {target}", f"bootout {target}", f"print {target}"],
                calls.read_text().splitlines(),
            )
            self.assertIn("saved installation remains disabled", paused.stdout)
            self.assertFalse(loaded.exists())
            self.assertEqual(b"saved plist", plist.read_bytes())
            self.assertTrue(config.exists())
            self.assertEqual("saved log", log.read_text())

            calls.write_text("")
            subprocess.run(["sh", script, "pause"], env=env, check=True, capture_output=True)
            self.assertEqual([f"disable {target}", f"print {target}"], calls.read_text().splitlines())

    def test_launchd_propagates_inspection_unload_and_still_loaded_failures(self):
        cases = (
            ({"PRINT_ERROR_STATUS": "1"}, 1, "inspection failed"),
            ({"BOOTOUT_ERROR_STATUS": "7"}, 7, "bootout failed"),
            ({"KEEP_LOADED": "1"}, 1, "still loaded"),
        )
        for overrides, expected_status, message in cases:
            with self.subTest(overrides=overrides), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                script, _config, plist, calls, loaded, env = self._launchd_fixture(root)
                plist.parent.mkdir(parents=True)
                plist.write_bytes(b"saved")
                loaded.touch()
                result = subprocess.run(
                    ["sh", script, "pause"],
                    env={**env, **overrides},
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(expected_status, result.returncode)
                self.assertIn(message, result.stderr.lower())
                self.assertNotIn("Paused", result.stdout)
                self.assertEqual(b"saved", plist.read_bytes())
                self.assertEqual("disable", calls.read_text().split()[0])

    def test_launchd_resume_reuses_saved_install_without_killing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script, _config, plist, calls, loaded, env = self._launchd_fixture(root)
            missing = subprocess.run(
                ["sh", script, "resume"], env=env, capture_output=True, text=True
            )
            self.assertNotEqual(0, missing.returncode)
            self.assertFalse(calls.exists())

            plist.parent.mkdir(parents=True)
            plist.write_bytes(b"saved continuous plist")
            saved = plist.read_bytes()
            resumed = subprocess.run(
                ["sh", script, "resume"], env=env, check=True, capture_output=True, text=True
            )
            target = "gui/501/com.euisuh.korail-watch"
            self.assertEqual(
                [f"enable {target}", f"print {target}", f"bootstrap gui/501 {plist}"],
                calls.read_text().splitlines(),
            )
            self.assertIn("resume requested", resumed.stdout.lower())
            self.assertIn("not verified", resumed.stdout.lower())
            self.assertEqual(saved, plist.read_bytes())

            calls.write_text("")
            subprocess.run(["sh", script, "resume"], env=env, check=True, capture_output=True)
            loaded_calls = calls.read_text().splitlines()
            self.assertEqual(
                [f"enable {target}", f"print {target}", f"kickstart {target}"], loaded_calls
            )
            self.assertFalse(any(" -k" in call or call.startswith("kickstart -k") for call in loaded_calls))
            self.assertFalse(any(call.startswith("bootout") for call in loaded_calls))

    def test_launchd_uninstall_and_argument_validation(self):
        invalid = (
            (),
            ("unknown",),
            ("pause", "extra"),
            ("resume", "extra"),
            ("uninstall", "extra"),
            ("install", "trip.toml", "--continuous", "extra"),
            ("install", "trip.toml", "--invalid"),
        )
        for argv in invalid:
            with self.subTest(argv=argv), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                script, _config, _plist, calls, _loaded, env = self._launchd_fixture(root)
                result = subprocess.run(["sh", script, *argv], env=env, capture_output=True)
                self.assertEqual(2, result.returncode)
                self.assertFalse(calls.exists())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script, _config, plist, calls, loaded, env = self._launchd_fixture(root)
            plist.parent.mkdir(parents=True)
            plist.write_bytes(b"saved")
            loaded.touch()
            subprocess.run(["sh", script, "uninstall"], env=env, check=True, capture_output=True)
            target = "gui/501/com.euisuh.korail-watch"
            self.assertEqual(
                [f"disable {target}", f"print {target}", f"bootout {target}", f"print {target}"],
                calls.read_text().splitlines(),
            )
            self.assertFalse(plist.exists())

    def test_status_redacts_paid_references_recursively(self):
        snapshot = {
            "hold": {"reference": "unpaid-pnr", "paid": False},
            "paid_tickets": [{"reference": "sale-ref-return-password", "paid": True}],
            "evidence": {"remote": [{"reference": "nested-paid-secret", "paid": True}]},
        }
        output = StringIO()
        with patch("korail_watch.engine.status", return_value=snapshot), redirect_stdout(output):
            self.assertEqual(0, cli.main(["status"]))
        displayed = output.getvalue()
        self.assertIn("unpaid-pnr", displayed)
        self.assertEqual(2, displayed.count("[redacted]"))
        self.assertNotIn("sale-ref-return-password", displayed)
        self.assertNotIn("nested-paid-secret", displayed)

    def test_demo_does_not_touch_default_state(self):
        with tempfile.TemporaryDirectory() as home, patch.object(Path, "home", return_value=Path(home)):
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(0, cli.main(["demo"]))
            self.assertEqual("Offline demo: reserved (no live reservation).\n", output.getvalue())
            self.assertFalse(cli.default_state_dir().exists())


if __name__ == "__main__":
    unittest.main()
