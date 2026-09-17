from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path


_MAX_BYTES = 1024 * 1024
_BACKUP_COUNT = 3
_FIELDS = frozenset(
    {
        "operation",
        "stage",
        "outcome",
        "http_status",
        "provider_code",
        "error_type",
        "attempt_id",
    }
)
_EVENT = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_PROVIDER_CODE = re.compile(r"^[A-Z][A-Z0-9]{0,15}$")
_ERROR_TYPE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_ATTEMPT_ID = re.compile(r"^[0-9a-f]{32}$")
_OPERATIONS = frozenset({"login", "search", "reserve", "reservations", "tickets", "provider"})
_STAGES = frozenset(
    {
        "request",
        "preflight",
        "initial",
        "followup",
        "readback",
        "list",
        "detail",
        "dispatch",
        "response",
        "reconcile",
        "validate",
        "confirm",
        "candidate_refresh",
    }
)
_OUTCOMES = frozenset(
    {
        "received",
        "success",
        "starting",
        "not_sent",
        "sold_out",
        "ambiguous",
        "confirmed",
        "departed",
        "transient",
        "blocked",
        "error",
        "invalid",
        "missing",
        "duplicate",
        "unavailable",
        "immediate",
        "waitlist",
        "existing-hold",
        "existing-ticket",
        "allocated",
    }
)
_logger = logging.getLogger("korail_watch.diagnostics")
_logger.propagate = False
_logger.setLevel(logging.INFO)
_warning_emitted = False


def _warn_once() -> None:
    global _warning_emitted
    if not _warning_emitted:
        _warning_emitted = True
        try:
            print("warning: private diagnostics logging is unavailable", file=sys.stderr)
        except Exception:
            pass


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(record.msg, ensure_ascii=True, separators=(",", ":"))


class _PrivateRotatingFileHandler(RotatingFileHandler):
    def _open(self):
        stream = super()._open()
        try:
            os.chmod(self.baseFilename, 0o600)
        except Exception:
            stream.close()
            raise
        return stream

    def doRollover(self) -> None:
        super().doRollover()
        paths = [Path(self.baseFilename)] + [
            Path(f"{self.baseFilename}.{index}") for index in range(1, self.backupCount + 1)
        ]
        for path in paths:
            if path.exists():
                os.chmod(path, 0o600)

    def handleError(self, record: logging.LogRecord) -> None:
        _warn_once()


def configure(state_dir: Path) -> None:
    """Configure one private bounded diagnostics file, or fail safely at startup."""
    global _warning_emitted
    try:
        directory = Path(state_dir)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        handler = _PrivateRotatingFileHandler(
            directory / "diagnostics.jsonl",
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(_JsonFormatter())
    except Exception:
        raise RuntimeError("unable to initialize private diagnostics") from None

    for old in _logger.handlers[:]:
        _logger.removeHandler(old)
        old.close()
    _logger.addHandler(handler)
    _logger.disabled = False
    _logger.propagate = False
    _warning_emitted = False


def _validated(event_name: str, fields: dict) -> dict:
    if not isinstance(event_name, str) or not _EVENT.fullmatch(event_name):
        raise ValueError
    if not fields.keys() <= _FIELDS:
        raise ValueError

    clean = {}
    for name, value in fields.items():
        if value is None:
            continue
        if name == "http_status":
            if type(value) is not int or not 100 <= value <= 599:
                raise ValueError
        elif name == "attempt_id":
            if not isinstance(value, str) or not _ATTEMPT_ID.fullmatch(value):
                raise ValueError
        elif name == "error_type":
            if not isinstance(value, str) or not _ERROR_TYPE.fullmatch(value):
                raise ValueError
        elif name == "provider_code":
            if not isinstance(value, str) or not _PROVIDER_CODE.fullmatch(value):
                value = "UNRECOGNIZED"
        elif name == "operation":
            if value not in _OPERATIONS:
                raise ValueError
        elif name == "stage":
            if value not in _STAGES:
                raise ValueError
        elif name == "outcome":
            if value not in _OUTCOMES:
                raise ValueError
        elif not isinstance(value, str) or not _TOKEN.fullmatch(value):
            raise ValueError
        clean[name] = value

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "event": event_name,
        **clean,
    }


def event(event: str, **safe_fields) -> None:
    """Write one validated record; diagnostics can never interrupt runtime work."""
    try:
        _logger.info(_validated(event, safe_fields))
    except Exception:
        _warn_once()
