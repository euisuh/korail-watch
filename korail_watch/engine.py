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
        "waitlist": train.waitlist,
        "standing": train.standing,
        "mixed": train.mixed,
    }


def _hold_data(hold: Hold) -> dict:
    return {
        "reference": hold.reference,
        "train": _train_data(hold.train),
        "deadline": hold.deadline,
        "price": hold.price,
        "paid": hold.paid,
        "kind": hold.kind,
    }


def _hold_from_data(data: dict) -> Hold:
    train = dict(data["train"])
    for capability in ("waitlist", "standing", "mixed"):
        train.setdefault(capability, False)
    return Hold(
        reference=data["reference"],
        train=Train(**train),
        deadline=data.get("deadline"),
        price=data.get("price"),
        paid=data.get("paid", False),
        kind=data.get("kind", "seated"),
    )


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
    if hold.kind == "waitlist":
        return (
            f"Korail waitlist {hold.reference}: {hold.train.departure} → {hold.train.arrival}, "
            f"{hold.train.date} {hold.train.dep_time}; queued only, no seat is guaranteed. "
            "Check the Korail app; payment is not due unless allocation is confirmed."
        )
    payment = hold.deadline or "unavailable; check the Korail app immediately"
    price = f"; price {hold.price:,} KRW" if hold.price is not None else ""
    return (
        f"Korail {'ticket' if hold.paid else hold.kind + ' reservation'} {hold.reference}: "
        f"{hold.train.departure} → {hold.train.arrival}, {hold.train.date} "
        f"{hold.train.dep_time}; payment deadline {payment}{price}"
    )


def _confirm(db: sqlite3.Connection, hold: Hold) -> None:
    with db:
        _set(db, "hold", _hold_data(hold))
        db.execute("DELETE FROM state WHERE key IN ('intent', 'ambiguous_hold', 'queue', 'queue_unknown')")
        db.execute(
            "INSERT OR REPLACE INTO outbox(id, payload) VALUES (1, ?)",
            (_message(hold),),
        )


def _confirm_queue(db: sqlite3.Connection, hold: Hold) -> None:
    with db:
        _set(db, "queue", _hold_data(hold))
        db.execute("DELETE FROM state WHERE key IN ('intent', 'ambiguous_hold', 'hold', 'queue_unknown')")
        db.execute(
            "INSERT OR REPLACE INTO outbox(id, payload) VALUES (1, ?)",
            (_message(hold),),
        )


def _preserve_mismatch(db: sqlite3.Connection, hold: Hold) -> None:
    with db:
        _set(db, "ambiguous_hold", _hold_data(hold))


def _preserve_queue_unknown(db: sqlite3.Connection, queued: dict, remote: list[Hold], reason: str) -> None:
    with db:
        _set(
            db,
            "queue_unknown",
            {
                "observed_at": _now().isoformat(),
                "reason": reason,
                "queue": queued,
                "remote": [_hold_data(hold) for hold in remote],
            },
        )
        db.execute(
            "INSERT OR REPLACE INTO outbox(id, payload) VALUES (1, ?)",
            (
                f"Korail waitlist {queued['reference']} can no longer be confirmed ({reason}). "
                "Do not create another booking automatically; check the Korail app.",
            ),
        )


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
        except TransientError as exc:
            attempts = row[2] + 1
            retry_after = exc.retry_after
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


def _drain_queue_outbox(db: sqlite3.Connection, notifier) -> None:
    try:
        _drain_outbox(db, notifier, wait=False)
    except BlockedError:
        # A broken notification channel must not delay allocation detection.
        pass


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


def _supported_modes(provider) -> frozenset[str]:
    modes = getattr(provider, "supported_modes", frozenset(("general", "special")))
    allowed = {"general", "special", "waitlist", "standing", "mixed"}
    if not isinstance(modes, frozenset) or not modes <= allowed:
        raise TypeError("provider supported_modes is invalid")
    return modes | frozenset(("general", "special"))


def _remote_holds(provider) -> list[Hold]:
    reservations = provider.reservations()
    tickets = provider.tickets()
    if not isinstance(reservations, list) or not isinstance(tickets, list):
        raise TypeError("provider reconciliation must return lists")
    if any(not isinstance(item, Hold) for item in reservations + tickets):
        raise TypeError("provider reconciliation returned invalid data")
    return reservations + tickets


def _monitor_queue(db: sqlite3.Connection, provider, notifier, *, wait: bool) -> str:
    queued = _get(db, "queue")
    while True:
        _drain_queue_outbox(db, notifier)
        try:
            remote = _remote_holds(provider)
        except BlockedError:
            return "blocked"
        except TransientError as exc:
            if not wait:
                return "queue-incomplete"
            time.sleep(max(1.0, exc.retry_after if exc.retry_after is not None else 30.0))
            continue
        except Exception:
            _preserve_queue_unknown(db, queued, [], "read-unknown")
            _drain_queue_outbox(db, notifier)
            return "ambiguous"

        current = next((hold for hold in remote if hold.reference == queued["reference"]), None)
        if current is None:
            _preserve_queue_unknown(db, queued, remote, "missing")
            _drain_queue_outbox(db, notifier)
            return "queue-missing"
        original = _hold_from_data(queued)
        if not _same_train(current.train, original.train):
            _preserve_queue_unknown(db, queued, remote, "identity-mismatch")
            _drain_queue_outbox(db, notifier)
            return "ambiguous"
        if current.kind != "waitlist":
            _confirm(db, current)
            _drain_outbox(db, notifier, wait=wait)
            return "allocated"
        with db:
            _set(db, "queue", _hold_data(current))
        if not wait:
            return "queued"
        time.sleep(30.0)


def _reconcile(db: sqlite3.Connection, trip: Trip, provider) -> str | None:
    intent = _get(db, "intent")
    try:
        remote = _remote_holds(provider)
    except BlockedError:
        return "blocked"
    except AmbiguousReservation:
        if not intent:
            with db:
                _set(db, "reconciliation_unknown", {"created_at": _now().isoformat()})
        return "ambiguous"
    except Exception:
        return "ambiguous" if intent else "error"

    if intent:
        intended = Train(**intent["train"])
        exact = next((hold for hold in remote if _same_train(hold.train, intended)), None)
        if exact:
            requested = intent.get("kind", intent.get("seat_class"))
            expected = "seated" if requested in ("general", "special") else requested
            if requested == "waitlist" and exact.kind != "waitlist":
                _confirm(db, exact)
                return "allocated"
            if requested == "waitlist" and exact.kind == "waitlist":
                _preserve_queue_unknown(
                    db,
                    _hold_data(exact),
                    remote,
                    "waitlist-followup-unproven",
                )
                return "ambiguous"
            if exact.kind == expected or (requested == "standing" and exact.kind == "seated"):
                _confirm(db, exact)
                return "existing-ticket" if exact.paid else "existing-hold"
            _preserve_mismatch(db, exact)
            return "ambiguous"
        matching = next((hold for hold in remote if _matches_trip(trip, hold.train)), None)
        if matching:
            _preserve_mismatch(db, matching)
        return "ambiguous"

    matching = next(
        (hold for hold in remote if hold.kind != "waitlist" and _matches_trip(trip, hold.train)),
        None,
    ) or next((hold for hold in remote if _matches_trip(trip, hold.train)), None)
    if matching:
        if matching.kind == "waitlist":
            _preserve_queue_unknown(
                db,
                _hold_data(matching),
                remote,
                "existing-waitlist-unverified",
            )
            return "ambiguous"
        _confirm(db, matching)
        return "existing-ticket" if matching.paid else "existing-hold"
    return None


def _attempt(
    db: sqlite3.Connection,
    trip: Trip,
    provider,
    train: Train,
    kinds: tuple[tuple[str, bool], ...],
) -> str | None:
    for kind, available in kinds:
        if not available:
            continue
        intent = {"train": _train_data(train), "kind": kind, "adults": trip.adults, "created_at": _now().isoformat()}
        with db:
            _set(db, "intent", intent)
        try:
            hold = provider.reserve(train, kind, trip.adults)
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
        expected = "seated" if kind in ("general", "special") else kind
        accepted_kind = isinstance(hold, Hold) and (
            hold.kind == expected or (kind == "standing" and hold.kind == "seated")
        )
        if not isinstance(hold, Hold) or not _same_train(hold.train, train) or not accepted_kind:
            if isinstance(hold, Hold):
                _preserve_mismatch(db, hold)
            return "ambiguous"
        if hold.kind == "waitlist":
            _confirm_queue(db, hold)
            return "waitlisted"
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
            local = _get(db, "hold")
            queue = _get(db, "queue")
            queue_active = bool(queue or (local and local.get("kind", "seated") == "waitlist"))
            if queue_active:
                _drain_queue_outbox(db, notifier)
            else:
                _drain_outbox(db, notifier, wait=wait_for_notification)
            if local:
                if local.get("kind", "seated") == "waitlist":
                    _confirm_queue(db, _hold_from_data(local))
                    return _monitor_queue(db, provider, notifier, wait=wait_for_notification)
                return "existing-ticket" if local.get("paid") else "existing-hold"
            if _get(db, "queue_unknown"):
                return "ambiguous"
            if queue:
                return _monitor_queue(db, provider, notifier, wait=wait_for_notification)
            if _get(db, "reconciliation_unknown") or (_get(db, "ambiguous_hold") and not _get(db, "intent")):
                return "ambiguous"
            result = _reconcile(db, trip, provider)
            if result:
                _drain_outbox(db, notifier, wait=wait_for_notification)
                return result
            if _past_cutoff(trip):
                return "cutoff"

            try:
                supported = _supported_modes(provider)
            except TypeError:
                return "error"

            targets = _targets(trip)
            start = int(_get(db, "cursor") or 0) % len(targets)
            cycles = 1 if once or not armed else max_cycles
            available = False
            cycle = 0
            incomplete = False
            while cycles is None or cycle < cycles:
                attempted: set[str] = set()
                waitlist_candidates: list[Train] = []
                waitlist_keys: set[str] = set()
                pass_incomplete = False
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
                        pass_incomplete = True
                        time.sleep(max(0.0, exc.retry_after if exc.retry_after is not None else 5.0))
                        continue
                    except BlockedError:
                        return "blocked"
                    except Exception:
                        return "error"
                    if not isinstance(trains, list) or any(not isinstance(train, Train) for train in trains):
                        return "error"
                    for train in trains:
                        if not trip.matches(train):
                            continue
                        immediate = (
                            ("general", train.general and "general" in supported),
                            ("special", train.special and "special" in supported),
                            ("standing", train.standing and trip.allow_standing and "standing" in supported),
                            ("mixed", train.mixed and trip.allow_mixed and "mixed" in supported),
                        )
                        can_waitlist = train.waitlist and trip.allow_waitlist and "waitlist" in supported
                        if not any(available for _, available in immediate) and not can_waitlist:
                            continue
                        available = True
                        if can_waitlist and train.key not in waitlist_keys:
                            waitlist_keys.add(train.key)
                            waitlist_candidates.append(train)
                        if armed and train.key not in attempted:
                            attempted.add(train.key)
                            result = _attempt(db, trip, provider, train, immediate)
                            if result:
                                if result == "waitlisted":
                                    _drain_queue_outbox(db, notifier)
                                    return _monitor_queue(db, provider, notifier, wait=wait_for_notification)
                                _drain_outbox(db, notifier, wait=wait_for_notification)
                                return result
                    if trains:
                        next_after = max(train.dep_time for train in trains) + ":01"
                        if next_after <= after:
                            incomplete = True
                            pass_incomplete = True
                        elif next_after < limit:
                            pages.append((departure, arrival, next_after, limit, position))
                if armed and waitlist_candidates and not pass_incomplete:
                    for candidate in waitlist_candidates:
                        if not trip.matches(candidate):
                            continue
                        result = _attempt(db, trip, provider, candidate, (("waitlist", True),))
                        if result:
                            if result == "waitlisted":
                                _drain_queue_outbox(db, notifier)
                                return _monitor_queue(db, provider, notifier, wait=wait_for_notification)
                            _drain_outbox(db, notifier, wait=wait_for_notification)
                            return result
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
    path = state_dir / "state.sqlite3"
    if not path.exists():
        return {
            "state": "new",
            "intent": None,
            "hold": None,
            "queue": None,
            "queue_unknown": None,
            "ambiguous_hold": None,
            "reconciliation_unknown": None,
            "notifications_pending": 0,
        }
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
        intent, hold, queue = _get(db, "intent"), _get(db, "hold"), _get(db, "queue")
        mismatch, queue_unknown = _get(db, "ambiguous_hold"), _get(db, "queue_unknown")
        reconciliation_unknown = _get(db, "reconciliation_unknown")
        pending = db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        state = (
            "held"
            if hold
            else "ambiguous"
            if intent or mismatch or reconciliation_unknown or queue_unknown
            else "queued"
            if queue
            else "idle"
        )
        return {
            "state": state,
            "intent": intent,
            "hold": hold,
            "queue": queue,
            "queue_unknown": queue_unknown,
            "ambiguous_hold": mismatch,
            "reconciliation_unknown": reconciliation_unknown,
            "notifications_pending": pending,
        }


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
