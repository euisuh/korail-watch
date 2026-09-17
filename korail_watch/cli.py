from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import tempfile
import tomllib
from pathlib import Path


def app_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "korail-watch"


def credentials_path() -> Path:
    return app_dir() / "credentials.toml"


def default_state_dir() -> Path:
    return app_dir() / "state"


def configure() -> str:
    values = {
        "KORAIL_ID": getpass.getpass("Korail ID: "),
        "KORAIL_PASSWORD": getpass.getpass("Korail password: "),
        "TELEGRAM_BOT_TOKEN": getpass.getpass("Telegram bot token: "),
        "TELEGRAM_CHAT_ID": getpass.getpass("Telegram chat ID: "),
    }
    if not values["KORAIL_ID"] or not values["KORAIL_PASSWORD"]:
        raise ValueError("Korail ID and password are required")
    if bool(values["TELEGRAM_BOT_TOKEN"]) != bool(values["TELEGRAM_CHAT_ID"]):
        raise ValueError("Telegram token and chat ID must both be set or both be blank")
    if any("\n" in value or "\0" in value for value in values.values()):
        raise ValueError("credential values must be one line")

    directory = app_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    target = credentials_path()
    fd, temporary = tempfile.mkstemp(prefix="credentials.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("[credentials]\n")
            for key, value in values.items():
                handle.write(f"{key} = {json.dumps(value, ensure_ascii=False)}\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return "Credentials saved to the private macOS application-support directory."


def load_credentials(*required: str) -> None:
    path = credentials_path()
    try:
        if path.stat().st_mode & 0o077:
            raise ValueError("credentials permissions are unsafe; run `chmod 600` on the credentials file")
        with path.open("rb") as handle:
            values = tomllib.load(handle)["credentials"]
    except FileNotFoundError:
        values = {}
    except (KeyError, tomllib.TOMLDecodeError) as exc:
        raise ValueError("credentials are invalid; run `korail-watch configure`") from exc
    for key in required:
        if os.environ.get(key):
            continue
        value = values.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError("required credentials are missing; run `korail-watch configure`")
        os.environ[key] = value


def load_trip(path: Path):
    from .domain import Trip

    try:
        with path.open("rb") as handle:
            trip = tomllib.load(handle)["trip"]
    except (FileNotFoundError, KeyError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"trip config is missing or invalid: {path}") from exc
    try:
        return Trip(
            date=trip["date"],
            start=trip["start"],
            end=trip["end"],
            departures=tuple(trip["departures"]),
            arrivals=tuple(trip["arrivals"]),
            adults=trip.get("adults", 1),
            allow_waitlist=trip.get("allow_waitlist", False),
            allow_standing=trip.get("allow_standing", False),
            allow_mixed=trip.get("allow_mixed", False),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"trip config is invalid: {path}: {exc}") from exc


def provider_interval(path: Path) -> float:
    with path.open("rb") as handle:
        value = tomllib.load(handle).get("provider", {}).get("interval", 5.0)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 5:
        raise ValueError("provider.interval must be at least 5 seconds")
    return float(value)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="korail-watch")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("configure", help="save credentials outside the repository")

    for name, help_text in (
        ("check", "run one read-only search"),
        ("watch", "watch for an eligible entitlement"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--config", type=Path, default=Path("trip.toml"))
        command.add_argument("--state-dir", type=Path, default=None)
        if name == "watch":
            command.add_argument("--arm", action="store_true", required=True)
            command.add_argument(
                "--continuous",
                action="store_true",
                help="continue after verified expiry or payment until trip cutoff",
            )

    status_parser = commands.add_parser("status", help="show durable watcher state")
    status_parser.add_argument("--state-dir", type=Path, default=None)
    commands.add_parser("notify-test", help="send a Telegram test message")
    commands.add_parser("demo", help="run the offline demonstration")
    return parser


def _state_dir(value: Path | None) -> Path:
    return value if value is not None else default_state_dir()


def _status_for_display(value):
    if isinstance(value, list):
        return [_status_for_display(item) for item in value]
    if isinstance(value, dict):
        displayed = {key: _status_for_display(item) for key, item in value.items()}
        if value.get("paid") is True and "reference" in value:
            displayed["reference"] = "[redacted]"
        return displayed
    return value


def _warn_unsupported(trip, provider) -> None:
    supported = getattr(provider, "supported_modes", frozenset())
    labels = {
        "waitlist": "waitlist",
        "standing": "standing-only",
        "mixed": "standing+seat",
    }
    for mode, label in labels.items():
        if getattr(trip, f"allow_{mode}") and mode not in supported:
            print(
                f"warning: {label} was requested but is unsupported by this provider; "
                "use the official Korail app for that mode.",
                file=sys.stderr,
            )


def _execute(args: argparse.Namespace) -> str:
    if args.command == "configure":
        return configure()

    if args.command == "status":
        from .engine import status

        snapshot = _status_for_display(status(_state_dir(args.state_dir)))
        return json.dumps(snapshot, ensure_ascii=False, indent=2, default=str)

    if args.command == "demo":
        from .engine import demo

        with tempfile.TemporaryDirectory(prefix="korail-watch-demo-") as directory:
            result = demo(Path(directory))
        return f"Offline demo: {result} (no live reservation)."

    if args.command == "notify-test":
        from .notifier import TelegramNotifier

        load_credentials("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
        TelegramNotifier().send("Korail Watch notification test.")
        return "Notification sent."

    from .engine import run
    from .korail import KorailProvider

    trip = load_trip(args.config)
    required = ["KORAIL_ID", "KORAIL_PASSWORD"]
    if args.command == "watch":
        required += ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
    load_credentials(*required)
    provider = KorailProvider(interval=provider_interval(args.config))
    provider.login()
    _warn_unsupported(trip, provider)
    if args.command == "check":
        print("Read-only check started; scanning the full configured window at the safe request rate.", flush=True)
        return run(trip, provider, None, _state_dir(args.state_dir), armed=False, once=True)

    from .notifier import TelegramNotifier

    notifier = TelegramNotifier()
    mode = " continuously" if args.continuous else ""
    notifier.send(f"Korail Watch is armed and starting{mode}.")
    print(f"Armed watch started{mode}; Ctrl-C stops it.", flush=True)
    return run(
        trip,
        provider,
        notifier,
        _state_dir(args.state_dir),
        armed=True,
        continuous=args.continuous,
    )


def main(argv: list[str] | None = None) -> int:
    try:
        result = _execute(_parser().parse_args(argv))
    except KeyboardInterrupt:
        print("Stopped.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if result:
        print(result)
    return 0
