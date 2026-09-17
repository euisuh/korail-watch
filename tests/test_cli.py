import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from korail_watch import cli


class CliTests(unittest.TestCase):
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
            cli.load_credentials()
            self.assertEqual("environment-id", os.environ["KORAIL_ID"])

    def test_watch_requires_explicit_arm(self):
        with self.assertRaises(SystemExit) as raised:
            cli._parser().parse_args(["watch"])
        self.assertEqual(2, raised.exception.code)

    def test_interval_cannot_be_faster_than_five_seconds(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "trip.toml"
            config.write_text("[provider]\ninterval = 4.9\n")
            with self.assertRaisesRegex(ValueError, "at least 5"):
                cli.provider_interval(config)


if __name__ == "__main__":
    unittest.main()
