import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from korail_watch import diagnostics


class DiagnosticsTests(unittest.TestCase):
    def tearDown(self):
        for handler in diagnostics._logger.handlers[:]:
            diagnostics._logger.removeHandler(handler)
            handler.close()

    def test_private_jsonl_and_safe_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            diagnostics.configure(state)
            diagnostics.event(
                "provider",
                operation="reserve",
                stage="initial",
                outcome="success",
                http_status=200,
                provider_code="SUCC",
                error_type=None,
                attempt_id="a" * 32,
            )

            path = state / "diagnostics.jsonl"
            record = json.loads(path.read_text())
            self.assertEqual(0o700, stat.S_IMODE(state.stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
            self.assertEqual("provider", record["event"])
            self.assertEqual("a" * 32, record["attempt_id"])
            self.assertNotIn("error_type", record)
            self.assertRegex(record["timestamp"], r"^\d{4}-\d\d-\d\dT.*Z$")
            self.assertFalse(diagnostics._logger.propagate)

    def test_unsafe_values_and_fields_are_dropped(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            diagnostics.configure(state)
            diagnostics.event("provider", operation="search", outcome="success")
            diagnostics.event("provider", raw_payload="credential-secret")
            diagnostics.event("provider", provider_code="PNR\nrefund-secret")
            diagnostics.event("provider", operation="https://example.invalid/?token=secret")
            diagnostics.event("bad\nevent", outcome="secret")

            text = (state / "diagnostics.jsonl").read_text()
            records = [json.loads(line) for line in text.splitlines()]
            self.assertEqual(2, len(records))
            self.assertEqual("UNRECOGNIZED", records[1]["provider_code"])
            self.assertNotIn("credential-secret", text)
            self.assertNotIn("refund-secret", text)
            self.assertNotIn("token", text)

    def test_malformed_provider_code_preserves_safe_context(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            diagnostics.configure(state)
            diagnostics.event(
                "provider",
                operation="reserve",
                stage="response",
                outcome="ambiguous",
                http_status=502,
                provider_code="secret\nprovider-message",
                error_type="HTTPError",
            )

            record = json.loads((state / "diagnostics.jsonl").read_text())
            self.assertEqual(502, record["http_status"])
            self.assertEqual("response", record["stage"])
            self.assertEqual("HTTPError", record["error_type"])
            self.assertEqual("UNRECOGNIZED", record["provider_code"])
            self.assertNotIn("secret", json.dumps(record))

    def test_rotation_keeps_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(diagnostics, "_MAX_BYTES", 300):
            state = Path(directory)
            unrelated = state / "diagnostics.jsonl.unrelated"
            unrelated.write_text("leave me alone")
            unrelated.chmod(0o644)
            diagnostics.configure(state)
            for _ in range(20):
                diagnostics.event("engine", operation="search", stage="request", outcome="success")

            paths = sorted(state.glob("diagnostics.jsonl*"))
            owned = [path for path in paths if path != unrelated]
            self.assertGreater(len(owned), 1)
            self.assertLessEqual(len(owned), 4)
            self.assertTrue(all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in owned))
            self.assertEqual(0o644, stat.S_IMODE(unrelated.stat().st_mode))

    def test_event_io_failure_never_propagates(self):
        with tempfile.TemporaryDirectory() as directory:
            diagnostics.configure(Path(directory))
            handler = diagnostics._logger.handlers[0]
            with patch.object(handler, "emit", side_effect=OSError("private filesystem detail")):
                diagnostics.event("engine", operation="reserve", outcome="success")

    def test_event_failure_survives_broken_stderr(self):
        class BrokenStderr:
            def write(self, _text):
                raise OSError("private stderr detail")

            def flush(self):
                raise OSError("private stderr detail")

        with tempfile.TemporaryDirectory() as directory:
            diagnostics.configure(Path(directory))
            handler = diagnostics._logger.handlers[0]
            with patch.object(handler, "emit", side_effect=OSError("private filesystem detail")), patch.object(
                sys, "stderr", BrokenStderr()
            ):
                diagnostics.event("engine", operation="reserve", outcome="success")

    def test_configure_failure_is_sanitized(self):
        with patch.object(Path, "mkdir", side_effect=OSError("private filesystem detail")):
            with self.assertRaisesRegex(RuntimeError, "unable to initialize private diagnostics") as raised:
                diagnostics.configure(Path("ignored"))
        self.assertIsNone(raised.exception.__cause__)


if __name__ == "__main__":
    unittest.main()
