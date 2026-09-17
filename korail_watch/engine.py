"""Durable single-reservation engine."""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import time
from collections import deque
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterator

from .domain import (
    KST,
    AmbiguousReservation,
    BlockedError,
    Hold,
    SoldOut,
    Train,
    TransientError,
    Trip,
)


def _now() -> datetime:
    return datetime.now(KST)


def _train_data(train: Train) -> dict:
    return {
        "key": train.key,
        "date": train.date,
        "departure": train.departure,
        "arrival": train.arrival,
        "dep_time": train.dep_time,
        "arr_time": train.arr_time,
        "general": train.general,
        "special": train.special,
    }


def _hold_data(hold: Hold) -> dict:
    return {
        "reference": hold.reference,
        "train": _train_data(hold.train),
        "deadline": hold.deadline,
        "price": hold.price,
        "paid": hold.paid,
    }


def _same_train(left: Train, right: Train) -> bool:
    # Reconciliation objects do not carry live seat-availability flags.
    fields = ("key", "date", "departure", "arrival", "dep_time", "arr_time")
    return all(getattr(left, field) == getattr(right, field) for field in fields)


def _matches_trip(trip: Trip, train: Train) -> bool:
    """Match persisted account records without discarding departed trains."""
    return (
        train.date == trip.date
        and train.departure in trip.departures
        and train.arrival in trip.arrivals
        and trip.start <= train.dep_time <= trip.end
    )


def _prepare_dir(state_dir: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state_dir, 0o700)


@contextmanager
def _lock(state_dir: Path) -> Iterator[bool]:
    _prepare_dir(state_dir)
    descriptor = os.open(state_dir / "engine.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True
    finally:
        os.close(descriptor)


def _connect(state_dir: Path) -> sqlite3.Connection:
    path = state_dir / "state.sqlite3"
    db = sqlite3.connect(path, timeout=1)
    os.chmod(path, 0o600)
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS outbox (
            id INTEGER PRIMARY KEY,
            payload TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt REAL NOT NULL DEFAULT 0
        );
        """
    )
    return db


def _get(db: sqlite3.Connection, key: str):
    row = db.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


def _set(db: sqlite3.Connection, key: str, value) -> None:
    db.execute(
        "INSERT INTO state(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value, ensure_ascii=False, separators=(",", ":"))),
    )


def _message(hold: Hold) -> str:
    payment = hold.deadline or "unavailable; check the Korail app immediately"
    price = f"; price {hold.price:,} KRW" if hold.price is not None else ""
    return (
        f"Korail {'ticket' if hold.paid else 'reservation'} {hold.reference}: "
        f"{hold.train.departure} → {hold.train.arrival}, {hold.train.date} "
        f"{hold.train.dep_time}; payment deadline {payment}{price}"
    )


def _confirm(db: sqlite3.Connection, hold: Hold) -> None:
    with db:
        _set(db, "hold", _hold_data(hold))
        db.execute("DELETE FROM state WHERE key IN ('intent', 'ambiguous_hold')")
        db.execute(
            "INSERT OR IGNORE INTO outbox(id, payload) VALUES (1, ?)",
            (_message(hold),),
        )


def _preserve_mismatch(db: sqlite3.Connection, hold: Hold) -> None:
    with db:
        _set(db, "ambiguous_hold", _hold_data(hold))


def _drain_outbox(db: sqlite3.Connection, notifier, *, wait: bool = False) -> None:
    if notifier is None:
        return
    while True:
        row = db.execute(
            "SELECT id, payload, attempts, next_attempt FROM outbox ORDER BY id LIMIT 1"
        ).fetchone()
        if not row:
            return
        delay = max(0.0, row[3] - _now().timestamp())
        if delay:
            if not wait:
                return
            time.sleep(delay)
        try:
            notifier.send(row[1])
        except Exception as exc:
            attempts = row[2] + 1
            retry_after = exc.retry_after if isinstance(exc, TransientError) else None
            delay = min(3600.0, max(1.0, retry_after if retry_after is not None else 5 * 2 ** min(attempts, 8)))
            with db:
                db.execute(
                    "UPDATE outbox SET attempts = ?, next_attempt = ? WHERE id = ?",
                    (attempts, _now().timestamp() + delay, row[0]),
                )
            if not wait:
                return
        else:
            with db:
                db.execute("DELETE FROM outbox WHERE id = ?", (row[0],))
            return


def _windows(trip: Trip) -> list[str]:
    start_hour, _ = map(int, trip.start.split(":"))
    end_hour, _ = map(int, trip.end.split(":"))
    windows = [trip.start + ":00"]
    hour = start_hour + 1
    while hour <= end_hour:
        windows.append(f"{hour:02d}:00:00")
        hour += 1
    return windows


def _targets(trip: Trip) -> list[tuple[str, str, str, str]]:
    routes = [(departure, arrival) for departure in trip.departures for arrival in trip.arrivals]
    windows = _windows(trip)
    limits = windows[1:] + [trip.end + ":59"]
    return [
        (departure, arrival, after, limit)
        for after, limit in zip(windows, limits)
        for departure, arrival in routes
    ]


def _past_cutoff(trip: Trip) -> bool:
    now = _now()
    trip_date = date.fromisoformat(trip.date)
    return now.date() > trip_date or (now.date() == trip_date and now.strftime("%H:%M") >= trip.end)


def _remote_holds(provider) -> list[Hold]:
    reservations = provider.reservations()
    tickets = provider.tickets()
    if not isinstance(reservations, list) or not isinstance(tickets, list):
        raise TypeError("provider reconciliation must return lists")
    if any(not isinstance(item, Hold) for item in reservations + tickets):
        raise TypeError("provider reconciliation returned invalid data")
    return reservations + tickets


def _reconcile(db: sqlite3.Connection, trip: Trip, provider) -> str | None:
    intent = _get(db, "intent")
    try:
        remote = _remote_holds(provider)
    except BlockedError:
        return "blocked"
    except Exception:
        return "ambiguous" if intent else "error"

    if intent:
        intended = Train(**intent["train"])
        exact = next((hold for hold in remote if _same_train(hold.train, intended)), None)
        if exact:
            _confirm(db, exact)
            return "existing-ticket" if exact.paid else "existing-hold"
        matching = next((hold for hold in remote if _matches_trip(trip, hold.train)), None)
        if matching:
            _preserve_mismatch(db, matching)
        return "ambiguous"

    matching = next((hold for hold in remote if _matches_trip(trip, hold.train)), None)
    if matching:
        _confirm(db, matching)
        return "existing-ticket" if matching.paid else "existing-hold"
    return None


def _attempt(db: sqlite3.Connection, trip: Trip, provider, train: Train) -> str | None:
    for seat_class, available in (("general", train.general), ("special", train.special)):
        if not available:
            continue
        intent = {"train": _train_data(train), "seat_class": seat_class, "adults": trip.adults, "created_at": _now().isoformat()}
        with db:
            _set(db, "intent", intent)
        try:
            hold = provider.reserve(train, seat_class, trip.adults)
        except SoldOut:
            with db:
                db.execute("DELETE FROM state WHERE key = 'intent'")
            continue
        except BlockedError:
            result = _reconcile(db, trip, provider)
            return result or "blocked"
        except (AmbiguousReservation, TransientError, Exception):
            result = _reconcile(db, trip, provider)
            return result or "ambiguous"
        if not isinstance(hold, Hold) or not _same_train(hold.train, train):
            if isinstance(hold, Hold):
                _preserve_mismatch(db, hold)
            return "ambiguous"
        _confirm(db, hold)
        return "reserved"
    return None


def run(
    trip: Trip,
    provider,
    notifier,
    state_dir: Path,
    *,
    armed: bool = False,
    once: bool = False,
    max_cycles: int | None = None,
) -> str:
    """Reconcile, scan fairly, and at most create one durable unpaid hold."""
    if not isinstance(trip, Trip):
        raise TypeError("trip must be a Trip")
    if armed and notifier is None:
        return "notifier-required"
    if max_cycles is not None and (type(max_cycles) is not int or max_cycles < 1):
        raise ValueError("max_cycles must be a positive integer")
    state_dir = Path(state_dir)

    with _lock(state_dir) as acquired:
        if not acquired:
            return "locked"
        with closing(_connect(state_dir)) as db:
            wait_for_notification = armed and not once
            _drain_outbox(db, notifier, wait=wait_for_notification)
            local = _get(db, "hold")
            if local:
                return "existing-ticket" if local.get("paid") else "existing-hold"
            if _get(db, "ambiguous_hold") and not _get(db, "intent"):
                return "ambiguous"
            result = _reconcile(db, trip, provider)
            if result:
                _drain_outbox(db, notifier, wait=wait_for_notification)
                return result
            if _past_cutoff(trip):
                return "cutoff"

            targets = _targets(trip)
            start = int(_get(db, "cursor") or 0) % len(targets)
            cycles = 1 if once or not armed else max_cycles
            available = False
            cycle = 0
            incomplete = False
            while cycles is None or cycle < cycles:
                attempted: set[str] = set()
                pages = deque(
                    (*targets[(start + offset) % len(targets)], (start + offset) % len(targets))
                    for offset in range(len(targets))
                )
                while pages:
                    if _past_cutoff(trip):
                        return "cutoff"
                    departure, arrival, after, limit, position = pages.popleft()
                    with db:
                        _set(db, "cursor", (position + 1) % len(targets))
                    try:
                        trains = provider.search(trip, departure, arrival, after)
                    except TransientError as exc:
                        incomplete = True
                        time.sleep(max(0.0, exc.retry_after if exc.retry_after is not None else 5.0))
                        continue
                    except BlockedError:
                        return "blocked"
                    except Exception:
                        return "error"
                    if not isinstance(trains, list) or any(not isinstance(train, Train) for train in trains):
                        return "error"
                    for train in trains:
                        if not trip.matches(train) or not (train.general or train.special):
                            continue
                        available = True
                        if armed and train.key not in attempted:
                            attempted.add(train.key)
                            result = _attempt(db, trip, provider, train)
                            if result:
                                _drain_outbox(db, notifier, wait=wait_for_notification)
                                return result
                    if trains:
                        next_after = max(train.dep_time for train in trains) + ":01"
                        if next_after <= after:
                            incomplete = True
                        elif next_after < limit:
                            pages.append((departure, arrival, next_after, limit, position))
                cycle += 1
                start = (start + 1) % len(targets)
                with db:
                    _set(db, "cursor", start)
            if incomplete:
                return "incomplete"
            return "available" if available else "not-found"


def status(state_dir: Path) -> dict:
    """Return a credential-free snapshot suitable for the CLI."""
    state_dir = Path(state_dir)
    if not (state_dir / "state.sqlite3").exists():
        return {"state": "new", "intent": None, "hold": None, "ambiguous_hold": None, "notifications_pending": 0}
    with _lock(state_dir) as acquired:
        if not acquired:
            return {"state": "running"}
        with closing(_connect(state_dir)) as db:
            intent, hold, mismatch = _get(db, "intent"), _get(db, "hold"), _get(db, "ambiguous_hold")
            pending = db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
            state = "held" if hold else "ambiguous" if intent or mismatch else "idle"
            return {"state": state, "intent": intent, "hold": hold, "ambiguous_hold": mismatch, "notifications_pending": pending}


def demo(state_dir: Path) -> str:
    """Exercise the armed path entirely offline."""
    tomorrow = _now().date() + timedelta(days=1)
    trip = Trip(tomorrow.isoformat(), "12:00", "18:00", ("서울", "용산", "수서"), ("대전", "서대전"))
    train = Train("demo", trip.date, "서울", "대전", "12:30", "13:30", True, True)

    class Provider:
        def reservations(self):
            return []

        def tickets(self):
            return []

        def search(self, _trip, departure, arrival, after):
            return [train] if (departure, arrival, after) == ("서울", "대전", "12:00:00") else []

        def reserve(self, selected, seat_class, adults):
            assert selected is train and seat_class == "general" and adults == 1
            return Hold("DEMO", train, None, 0)

    class Notifier:
        def send(self, text):
            assert "DEMO" in text

    return run(trip, Provider(), Notifier(), state_dir, armed=True, once=True)
