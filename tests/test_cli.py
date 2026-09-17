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

    def test_launchd_helper_adds_continuous_only_when_requested(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            for name, body in {
                "korail-watch": "#!/bin/sh\necho not-found\n",
                "caffeinate": "#!/bin/sh\nexit 0\n",
                "launchctl": "#!/bin/sh\nexit 0\n",
            }.items():
                executable = fake_bin / name
                executable.write_text(body)
                executable.chmod(0o755)
            config = root / "trip.toml"
            config.write_text("[trip]\n")
            script = Path(__file__).parents[1] / "scripts" / "launchd.sh"
            env = {
                **os.environ,
                "HOME": str(root),
                "UID": "501",
                "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            }

            subprocess.run(["sh", script, "install", config, "--continuous"], env=env, check=True, capture_output=True)
            plist = root / "Library" / "LaunchAgents" / "com.euisuh.korail-watch.plist"
            with plist.open("rb") as handle:
                continuous = plistlib.load(handle)["ProgramArguments"]
            self.assertEqual("--continuous", continuous[-1])

            subprocess.run(["sh", script, "install", config], env=env, check=True, capture_output=True)
            with plist.open("rb") as handle:
                legacy = plistlib.load(handle)["ProgramArguments"]
            self.assertNotIn("--continuous", legacy)

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
