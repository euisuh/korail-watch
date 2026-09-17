"""Durable single-reservation engine."""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import time
import uuid
from collections import deque
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterator

from . import diagnostics
from .domain import (
    KST,
    AmbiguousReservation,
    BlockedError,
    Hold,
    ReservationNotSent,
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
    if hold.paid:
        return (
            f"Korail paid ticket: {hold.train.departure} → {hold.train.arrival}, "
            f"{hold.train.date} {hold.train.dep_time}; payment is already confirmed."
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
        db.execute(
            "DELETE FROM state WHERE key IN "
            "('intent', 'ambiguous_hold', 'queue', 'queue_unknown', 'expiry_absence', "
            "'expiry_unknown', 'paid_overlap')"
        )
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


def _confirm_paid_overlap(
    db: sqlite3.Connection,
    hold: Hold,
    paid: Hold,
    *,
    continuous: bool = True,
) -> None:
    recorded = _get(db, "paid_tickets") or []
    if not isinstance(recorded, list):
        raise TypeError("paid ticket record is invalid")
    known = [_hold_from_data(item) for item in recorded]
    if not any(
        item.reference == paid.reference and _same_train(item.train, paid.train)
        for item in known
    ):
        recorded.append(_hold_data(paid))
    with db:
        _set(db, "paid_tickets", recorded)
        _set(db, "hold", _hold_data(hold))
        _set(db, "paid_overlap", {"hold_reference": hold.reference, "train": _train_data(hold.train)})
        db.execute(
            "DELETE FROM state WHERE key IN "
            "('intent', 'ambiguous_hold', 'queue', 'queue_unknown', 'expiry_absence', 'expiry_unknown')"
        )
        db.execute(
            "INSERT OR REPLACE INTO outbox(id, payload) VALUES (1, ?)",
            (
                f"Korail payment is confirmed for {hold.train.departure} → {hold.train.arrival}, "
                f"{hold.train.date} {hold.train.dep_time}; "
                + (
                    "waiting for the unpaid reservation record to clear before continuing."
                    if continuous
                    else "no further payment action is needed."
                ),
            ),
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


def _reset_expiry_absence(db: sqlite3.Connection) -> None:
    with db:
        db.execute("DELETE FROM state WHERE key = 'expiry_absence'")


def _preserve_expiry_unknown(db: sqlite3.Connection, hold, reason: str) -> None:
    reference = hold.get("reference", "unknown") if isinstance(hold, dict) else "unknown"
    with db:
        db.execute("DELETE FROM state WHERE key = 'expiry_absence'")
        _set(
            db,
            "expiry_unknown",
            {"observed_at": _now().isoformat(), "reason": reason, "hold": hold},
        )
        db.execute(
            "INSERT OR REPLACE INTO outbox(id, payload) VALUES (1, ?)",
            (
                f"Korail reservation {reference} monitoring stopped "
                f"({reason}); check the official Korail app. No new booking will be attempted.",
            ),
        )


def _archive_expired(db: sqlite3.Connection, hold: dict, first_absent_at: str) -> None:
    archived = _get(db, "expired_holds") or []
    if not isinstance(archived, list):
        raise TypeError("expired hold archive is invalid")
    now = _now().isoformat()
    archived.append(
        {
            "hold": hold,
            "verification": {
                "first_absent_at": first_absent_at,
                "confirmed_absent_at": now,
                "known_reference_absent": True,
                "matching_account_record_absent": True,
            },
        }
    )
    with db:
        _set(db, "expired_holds", archived)
        db.execute(
            "DELETE FROM state WHERE key IN "
            "('hold', 'expiry_absence', 'expiry_unknown', 'paid_overlap')"
        )
        db.execute(
            "INSERT OR REPLACE INTO outbox(id, payload) VALUES (1, ?)",
            (
                f"Korail reservation {hold['reference']} expired and two account checks confirmed "
                "it absent; searching may resume.",
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


def _until_cutoff(trip: Trip) -> float:
    cutoff = datetime.fromisoformat(f"{trip.date}T{trip.end}:00").replace(tzinfo=KST)
    return max(0.0, (cutoff - _now()).total_seconds())


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


def _record_paid(
    db: sqlite3.Connection,
    paid: list[Hold],
    *,
    continuing: bool = False,
    clear_hold: bool = False,
    clear_queue: bool = False,
) -> None:
    recorded = _get(db, "paid_tickets") or []
    if not isinstance(recorded, list):
        raise TypeError("paid ticket record is invalid")
    known = [_hold_from_data(item) for item in recorded]
    for ticket in paid:
        if not any(
            item.reference == ticket.reference and _same_train(item.train, ticket.train)
            for item in known
        ):
            recorded.append(_hold_data(ticket))
            known.append(ticket)
    with db:
        _set(db, "paid_tickets", recorded)
        if clear_hold:
            db.execute(
                "DELETE FROM state WHERE key IN "
                "('hold', 'expiry_absence', 'expiry_unknown', 'paid_overlap')"
            )
        if clear_queue:
            db.execute("DELETE FROM state WHERE key IN ('queue', 'queue_unknown', 'paid_overlap')")
        if continuing:
            ticket = paid[0]
            db.execute(
                "INSERT OR REPLACE INTO outbox(id, payload) VALUES (1, ?)",
                (
                    f"Korail payment is confirmed for {ticket.train.departure} → "
                    f"{ticket.train.arrival}, {ticket.train.date} {ticket.train.dep_time}; "
                    "continuing to watch for one alternative. No further payment action is needed.",
                ),
            )


def _paid_train(db: sqlite3.Connection, train: Train) -> bool:
    return any(
        _same_train(_hold_from_data(item).train, train)
        for item in (_get(db, "paid_tickets") or [])
    )


def _preserve_account_unknown(db: sqlite3.Connection, reason: str, remote: list[Hold]) -> None:
    if reason in {"search-ambiguous", "reconciliation-ambiguous"}:
        explanation = (
            "A booking outcome could not be confirmed; this is not proof that the request was blocked."
        )
    elif reason == "provider-blocked" or reason.endswith("-blocked"):
        explanation = "Korail reported an authentication or security block that requires review."
    else:
        explanation = "A provider or account safety check requires review."
    with db:
        _set(
            db,
            "reconciliation_unknown",
            {
                "created_at": _now().isoformat(),
                "reason": reason,
                "remote": [_hold_data(hold) for hold in remote],
            },
        )
        db.execute(
            "INSERT OR REPLACE INTO outbox(id, payload) VALUES (1, ?)",
            (
                f"Korail continuous watch stopped ({reason}). {explanation} Check the official "
                "Korail app and the private diagnostics.jsonl file. No new booking will be attempted.",
            ),
        )


def _reconcile_continuous(db: sqlite3.Connection, trip: Trip, provider) -> str | None:
    try:
        remote = _remote_holds(provider)
    except BlockedError:
        _preserve_account_unknown(db, "provider-blocked", [])
        return "blocked"
    except TransientError:
        raise
    except Exception:
        _preserve_account_unknown(db, "account-read-unknown", [])
        return "ambiguous"

    paid = [hold for hold in remote if hold.paid and _matches_trip(trip, hold.train)]
    try:
        _record_paid(db, paid)
    except Exception:
        _preserve_account_unknown(db, "paid-record-invalid", remote)
        return "ambiguous"
    unpaid = [hold for hold in remote if not hold.paid]
    if not unpaid:
        return None
    if len(unpaid) != 1:
        _preserve_account_unknown(db, "multiple-unpaid-records", remote)
        return "ambiguous"
    existing = unpaid[0]
    if not _matches_trip(trip, existing.train):
        _preserve_account_unknown(db, "other-unpaid-record", remote)
        return "ambiguous"
    if existing.kind == "waitlist":
        _preserve_queue_unknown(db, _hold_data(existing), remote, "existing-waitlist-unverified")
        return "ambiguous"
    paid_existing = next((hold for hold in paid if _same_train(hold.train, existing.train)), None)
    if paid_existing:
        _confirm_paid_overlap(db, existing, paid_existing)
    else:
        _confirm(db, existing)
    return "existing-hold"


def _continuous_preflight(db: sqlite3.Connection, trip: Trip, provider, train: Train) -> str | None:
    result = _reconcile_continuous(db, trip, provider)
    if result:
        return result
    return "paid-excluded" if _paid_train(db, train) else None


def _monitor_hold(
    db: sqlite3.Connection,
    trip: Trip,
    provider,
    notifier,
    *,
    wait: bool,
) -> str:
    local = _get(db, "hold")
    try:
        held = _hold_from_data(local)
    except Exception:
        _preserve_expiry_unknown(db, local, "invalid-local-hold")
        _drain_queue_outbox(db, notifier)
        return "ambiguous"
    try:
        deadline = datetime.fromisoformat(held.deadline) if held.deadline else None
    except (TypeError, ValueError):
        deadline = None

    while True:
        if _past_cutoff(trip):
            return "cutoff"
        _drain_queue_outbox(db, notifier)
        try:
            remote = _remote_holds(provider)
        except BlockedError:
            _preserve_expiry_unknown(db, local, "provider-blocked")
            _drain_queue_outbox(db, notifier)
            return "blocked"
        except TransientError as exc:
            _reset_expiry_absence(db)
            if not wait:
                return "payment-incomplete"
            time.sleep(max(1.0, exc.retry_after if exc.retry_after is not None else 30.0))
            continue
        except AmbiguousReservation:
            _preserve_expiry_unknown(db, local, "account-ambiguous")
            _drain_queue_outbox(db, notifier)
            return "ambiguous"
        except Exception:
            _preserve_expiry_unknown(db, local, "read-unknown")
            _drain_queue_outbox(db, notifier)
            return "ambiguous"

        paid = [item for item in remote if item.paid and _matches_trip(trip, item.train)]
        try:
            _record_paid(db, paid)
        except Exception:
            _preserve_expiry_unknown(db, local, "paid-record-invalid")
            _drain_queue_outbox(db, notifier)
            return "ambiguous"

        paid_current = next((item for item in paid if _same_train(item.train, held.train)), None)
        unpaid = [item for item in remote if not item.paid]
        current = next(
            (item for item in unpaid if item.reference == held.reference),
            None,
        )
        other_unpaid = [item for item in unpaid if item is not current]
        if other_unpaid:
            _reset_expiry_absence(db)
            _preserve_mismatch(db, other_unpaid[0])
            _preserve_expiry_unknown(db, local, "conflicting-unpaid-record")
            _drain_queue_outbox(db, notifier)
            return "ambiguous"
        if current:
            _reset_expiry_absence(db)
            if not _same_train(current.train, held.train) or current.kind == "waitlist":
                _preserve_mismatch(db, current)
                _preserve_expiry_unknown(db, local, "identity-mismatch")
                _drain_queue_outbox(db, notifier)
                return "ambiguous"
            local = _hold_data(current)
            held = current
            deadline = datetime.fromisoformat(held.deadline) if held.deadline else None
            with db:
                _set(db, "hold", local)
                overlap = _get(db, "paid_overlap")
                marker = {"hold_reference": held.reference, "train": _train_data(held.train)}
                if paid_current and overlap != marker:
                    _set(db, "paid_overlap", marker)
                    db.execute(
                        "INSERT OR REPLACE INTO outbox(id, payload) VALUES (1, ?)",
                        (
                            f"Korail payment is confirmed for {held.train.departure} → "
                            f"{held.train.arrival}, {held.train.date} {held.train.dep_time}; "
                            "waiting for the unpaid reservation record to clear before continuing.",
                        ),
                    )
            if paid_current:
                _drain_queue_outbox(db, notifier)
            if not wait:
                return "payment-pending"
            time.sleep(30.0)
            continue

        if paid_current:
            try:
                _record_paid(db, [paid_current], continuing=True, clear_hold=True)
            except Exception:
                _preserve_expiry_unknown(db, local, "paid-record-invalid")
                _drain_queue_outbox(db, notifier)
                return "ambiguous"
            _drain_queue_outbox(db, notifier)
            return "paid"

        now = _now()
        if deadline is None or now < deadline + timedelta(seconds=60):
            _reset_expiry_absence(db)
            if not wait:
                return "payment-pending" if deadline else "deadline-unknown"
            time.sleep(30.0)
            continue

        absence = _get(db, "expiry_absence")
        if not isinstance(absence, dict) or absence.get("reference") != held.reference:
            with db:
                _set(
                    db,
                    "expiry_absence",
                    {"reference": held.reference, "first_absent_at": now.isoformat()},
                )
            if not wait:
                return "expiry-verifying"
            time.sleep(30.0)
            continue
        try:
            first = datetime.fromisoformat(absence["first_absent_at"])
        except (KeyError, TypeError, ValueError):
            _reset_expiry_absence(db)
            continue
        remaining = 30.0 - (now - first).total_seconds()
        if remaining > 0:
            if not wait:
                return "expiry-verifying"
            time.sleep(remaining)
            continue
        try:
            _archive_expired(db, local, absence["first_absent_at"])
        except Exception:
            _preserve_expiry_unknown(db, local, "archive-invalid")
            _drain_queue_outbox(db, notifier)
            return "ambiguous"
        _drain_queue_outbox(db, notifier)
        return "expired"


def _monitor_queue(
    db: sqlite3.Connection,
    provider,
    notifier,
    *,
    wait: bool,
    trip: Trip,
    continuous: bool = False,
) -> str:
    queued = _get(db, "queue")
    while True:
        if continuous and _past_cutoff(trip):
            return "cutoff"
        _drain_queue_outbox(db, notifier)
        try:
            remote = _remote_holds(provider)
        except BlockedError:
            if continuous:
                _preserve_queue_unknown(db, queued, [], "provider-blocked")
                _drain_queue_outbox(db, notifier)
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

        original = _hold_from_data(queued)
        paid = [hold for hold in remote if hold.paid and _matches_trip(trip, hold.train)]
        try:
            _record_paid(db, paid)
        except Exception:
            _preserve_queue_unknown(db, queued, remote, "paid-record-invalid")
            _drain_queue_outbox(db, notifier)
            return "ambiguous"
        unpaid = [hold for hold in remote if not hold.paid]
        paid_current = next((hold for hold in paid if _same_train(hold.train, original.train)), None)
        current = next((hold for hold in unpaid if hold.reference == queued["reference"]), None)
        other_unpaid = [hold for hold in unpaid if hold is not current]
        if other_unpaid:
            _preserve_queue_unknown(db, queued, remote, "conflicting-unpaid-record")
            _drain_queue_outbox(db, notifier)
            return "ambiguous"
        if current is None:
            if paid_current:
                try:
                    _record_paid(
                        db,
                        [paid_current],
                        continuing=continuous,
                        clear_queue=True,
                    )
                    if not continuous:
                        with db:
                            db.execute(
                                "INSERT OR REPLACE INTO outbox(id, payload) VALUES (1, ?)",
                                (_message(paid_current),),
                            )
                except Exception:
                    _preserve_queue_unknown(db, queued, remote, "paid-record-invalid")
                    _drain_queue_outbox(db, notifier)
                    return "ambiguous"
                _drain_queue_outbox(db, notifier)
                return "paid"
            _preserve_queue_unknown(db, queued, remote, "missing")
            _drain_queue_outbox(db, notifier)
            return "queue-missing"
        if not _same_train(current.train, original.train):
            _preserve_queue_unknown(db, queued, remote, "identity-mismatch")
            _drain_queue_outbox(db, notifier)
            return "ambiguous"
        if current.kind != "waitlist":
            if paid_current:
                try:
                    _confirm_paid_overlap(db, current, paid_current, continuous=continuous)
                except Exception:
                    _preserve_queue_unknown(db, queued, remote, "paid-record-invalid")
                    _drain_queue_outbox(db, notifier)
                    return "ambiguous"
            else:
                _confirm(db, current)
            if continuous:
                _drain_queue_outbox(db, notifier)
            else:
                _drain_outbox(db, notifier, wait=wait)
            return "allocated"
        with db:
            _set(db, "queue", _hold_data(current))
        if not wait:
            return "queued"
        time.sleep(30.0)


def _reconcile(
    db: sqlite3.Connection,
    trip: Trip,
    provider,
    *,
    continuous: bool = False,
) -> str | None:
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
                paid_exact = next(
                    (
                        hold
                        for hold in remote
                        if hold.paid and _same_train(hold.train, exact.train)
                    ),
                    None,
                )
                if not exact.paid and paid_exact:
                    _confirm_paid_overlap(db, exact, paid_exact, continuous=continuous)
                else:
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
                paid_exact = next(
                    (
                        hold
                        for hold in remote
                        if hold.paid and _same_train(hold.train, exact.train)
                    ),
                    None,
                )
                if not exact.paid and paid_exact:
                    _confirm_paid_overlap(db, exact, paid_exact, continuous=continuous)
                else:
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
        paid_matching = next(
            (
                hold
                for hold in remote
                if hold.paid and _same_train(hold.train, matching.train)
            ),
            None,
        )
        if not matching.paid and paid_matching:
            _confirm_paid_overlap(db, matching, paid_matching, continuous=continuous)
        else:
            _confirm(db, matching)
        return "existing-ticket" if matching.paid else "existing-hold"
    return None


def _attempt(
    db: sqlite3.Connection,
    trip: Trip,
    provider,
    train: Train,
    kinds: tuple[tuple[str, bool], ...],
    *,
    continuous: bool = False,
    wait: bool = False,
) -> str | None:
    for kind, available in kinds:
        if not available:
            continue
        if _past_cutoff(trip):
            return "cutoff"
        if not trip.matches(train):
            return None
        if continuous:
            while True:
                if _past_cutoff(trip):
                    return "cutoff"
                if not trip.matches(train):
                    return None
                try:
                    preflight = _continuous_preflight(db, trip, provider, train)
                    break
                except TransientError as exc:
                    if not wait:
                        return "incomplete"
                    delay = max(1.0, exc.retry_after if exc.retry_after is not None else 30.0)
                    remaining = _until_cutoff(trip)
                    if not remaining:
                        return "cutoff"
                    time.sleep(min(delay, remaining))
            if _past_cutoff(trip):
                return "cutoff"
            if not trip.matches(train):
                return None
            if preflight == "paid-excluded":
                return None
            if preflight:
                return preflight
        attempt_id = uuid.uuid4().hex
        intent = {
            "train": _train_data(train),
            "kind": kind,
            "adults": trip.adults,
            "created_at": _now().isoformat(),
            "attempt_id": attempt_id,
        }
        with db:
            _set(db, "intent", intent)
        diagnostics.event(
            "reservation.attempt",
            operation="reserve",
            stage="dispatch",
            outcome="starting",
            attempt_id=attempt_id,
        )
        try:
            hold = provider.reserve(train, kind, trip.adults)
        except ReservationNotSent as exc:
            with db:
                db.execute("DELETE FROM state WHERE key = 'intent'")
            diagnostics.event(
                "reservation.result",
                operation="reserve",
                stage="dispatch",
                outcome="not_sent",
                error_type=type(exc).__name__,
                attempt_id=attempt_id,
            )
            if not wait:
                return "incomplete"
            delay = max(1.0, exc.retry_after if exc.retry_after is not None else 30.0)
            remaining = _until_cutoff(trip)
            if not remaining:
                return "cutoff"
            time.sleep(min(delay, remaining))
            if _past_cutoff(trip) or not trip.matches(train):
                return "cutoff" if _past_cutoff(trip) else None
            continue
        except SoldOut:
            with db:
                db.execute("DELETE FROM state WHERE key = 'intent'")
            diagnostics.event(
                "reservation.result",
                operation="reserve",
                stage="response",
                outcome="sold_out",
                attempt_id=attempt_id,
            )
            continue
        except BlockedError as exc:
            result = _reconcile(db, trip, provider, continuous=continuous)
            diagnostics.event(
                "reservation.stop",
                operation="reserve",
                stage="reconcile",
                outcome=result or "blocked",
                error_type=type(exc).__name__,
                attempt_id=attempt_id,
            )
            return result or "blocked"
        except Exception as exc:
            result = _reconcile(db, trip, provider, continuous=continuous)
            diagnostics.event(
                "reservation.stop",
                operation="reserve",
                stage="reconcile",
                outcome=result or "ambiguous",
                error_type=type(exc).__name__,
                attempt_id=attempt_id,
            )
            return result or "ambiguous"
        expected = "seated" if kind in ("general", "special") else kind
        accepted_kind = isinstance(hold, Hold) and (
            hold.kind == expected or (kind == "standing" and hold.kind == "seated")
        )
        if not isinstance(hold, Hold) or not _same_train(hold.train, train) or not accepted_kind:
            diagnostics.event(
                "reservation.result",
                operation="reserve",
                stage="validate",
                outcome="ambiguous",
                attempt_id=attempt_id,
            )
            if isinstance(hold, Hold):
                _preserve_mismatch(db, hold)
            return "ambiguous"
        if hold.kind == "waitlist":
            _confirm_queue(db, hold)
            diagnostics.event(
                "reservation.result",
                operation="reserve",
                stage="confirm",
                outcome="confirmed",
                attempt_id=attempt_id,
            )
            return "waitlisted"
        _confirm(db, hold)
        diagnostics.event(
            "reservation.result",
            operation="reserve",
            stage="confirm",
            outcome="confirmed",
            attempt_id=attempt_id,
        )
        return "reserved"
    return None


def _search(
    db: sqlite3.Connection,
    trip: Trip,
    provider,
    notifier,
    *,
    armed: bool,
    once: bool,
    max_cycles: int | None,
    continuous: bool,
) -> str:
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
            if continuous:
                _drain_queue_outbox(db, notifier)
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
                if not any(enabled for _, enabled in immediate) and not can_waitlist:
                    continue
                available = True
                if can_waitlist and train.key not in waitlist_keys:
                    waitlist_keys.add(train.key)
                    waitlist_candidates.append(train)
                if armed and train.key not in attempted:
                    attempted.add(train.key)
                    result = _attempt(
                        db,
                        trip,
                        provider,
                        train,
                        immediate,
                        continuous=continuous,
                        wait=armed and not once,
                    )
                    if result:
                        if result == "waitlisted":
                            _drain_queue_outbox(db, notifier)
                            return _monitor_queue(
                                db,
                                provider,
                                notifier,
                                wait=armed and not once,
                                continuous=continuous,
                                trip=trip,
                            )
                        if continuous:
                            _drain_queue_outbox(db, notifier)
                        else:
                            _drain_outbox(db, notifier, wait=armed and not once)
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
                    diagnostics.event(
                        "waitlist.refresh",
                        operation="search",
                        stage="candidate_refresh",
                        outcome="departed",
                    )
                    continue
                diagnostics.event(
                    "waitlist.refresh",
                    operation="search",
                    stage="candidate_refresh",
                    outcome="starting",
                )
                try:
                    refreshed = provider.search(
                        trip,
                        candidate.departure,
                        candidate.arrival,
                        candidate.dep_time + ":00",
                    )
                except TransientError as exc:
                    incomplete = True
                    diagnostics.event(
                        "waitlist.refresh",
                        operation="search",
                        stage="candidate_refresh",
                        outcome="transient",
                        error_type=type(exc).__name__,
                    )
                    if once:
                        continue
                    remaining = _until_cutoff(trip)
                    if not remaining:
                        return "cutoff"
                    delay = max(1.0, exc.retry_after if exc.retry_after is not None else 5.0)
                    time.sleep(min(delay, remaining))
                    if _past_cutoff(trip):
                        return "cutoff"
                    continue
                except BlockedError as exc:
                    diagnostics.event(
                        "waitlist.refresh",
                        operation="search",
                        stage="candidate_refresh",
                        outcome="blocked",
                        error_type=type(exc).__name__,
                    )
                    return "blocked"
                except Exception as exc:
                    diagnostics.event(
                        "waitlist.refresh",
                        operation="search",
                        stage="candidate_refresh",
                        outcome="error",
                        error_type=type(exc).__name__,
                    )
                    return "error"
                if not isinstance(refreshed, list) or any(
                    not isinstance(item, Train) for item in refreshed
                ):
                    diagnostics.event(
                        "waitlist.refresh",
                        operation="search",
                        stage="candidate_refresh",
                        outcome="invalid",
                    )
                    return "error"
                exact = [item for item in refreshed if _same_train(item, candidate)]
                if len(exact) != 1:
                    diagnostics.event(
                        "waitlist.refresh",
                        operation="search",
                        stage="candidate_refresh",
                        outcome="missing" if not exact else "duplicate",
                    )
                    continue
                refreshed_candidate = exact[0]
                if _past_cutoff(trip) or not trip.matches(refreshed_candidate):
                    diagnostics.event(
                        "waitlist.refresh",
                        operation="search",
                        stage="candidate_refresh",
                        outcome="departed",
                    )
                    continue
                refreshed_kinds = (
                    ("general", refreshed_candidate.general and "general" in supported),
                    ("special", refreshed_candidate.special and "special" in supported),
                    (
                        "standing",
                        refreshed_candidate.standing
                        and trip.allow_standing
                        and "standing" in supported,
                    ),
                    ("mixed", refreshed_candidate.mixed and trip.allow_mixed and "mixed" in supported),
                    (
                        "waitlist",
                        refreshed_candidate.waitlist
                        and trip.allow_waitlist
                        and "waitlist" in supported,
                    ),
                )
                if not any(enabled for _, enabled in refreshed_kinds):
                    diagnostics.event(
                        "waitlist.refresh",
                        operation="search",
                        stage="candidate_refresh",
                        outcome="unavailable",
                    )
                    continue
                diagnostics.event(
                    "waitlist.refresh",
                    operation="search",
                    stage="candidate_refresh",
                    outcome=(
                        "immediate"
                        if any(enabled for kind, enabled in refreshed_kinds if kind != "waitlist")
                        else "waitlist"
                    ),
                )
                result = _attempt(
                    db,
                    trip,
                    provider,
                    refreshed_candidate,
                    refreshed_kinds,
                    continuous=continuous,
                    wait=armed and not once,
                )
                if result:
                    if result == "waitlisted":
                        _drain_queue_outbox(db, notifier)
                        return _monitor_queue(
                            db,
                            provider,
                            notifier,
                            wait=armed and not once,
                            continuous=continuous,
                            trip=trip,
                        )
                    if continuous:
                        _drain_queue_outbox(db, notifier)
                    else:
                        _drain_outbox(db, notifier, wait=armed and not once)
                    return result
        cycle += 1
        start = (start + 1) % len(targets)
        with db:
            _set(db, "cursor", start)
    if incomplete:
        return "incomplete"
    return "available" if available else "not-found"


def run(
    trip: Trip,
    provider,
    notifier,
    state_dir: Path,
    *,
    armed: bool = False,
    once: bool = False,
    max_cycles: int | None = None,
    continuous: bool = False,
) -> str:
    """Reconcile, scan fairly, and at most create one durable unpaid hold."""
    if not isinstance(trip, Trip):
        raise TypeError("trip must be a Trip")
    if continuous and not armed:
        raise ValueError("continuous mode requires armed=True")
    if armed and notifier is None:
        return "notifier-required"
    if max_cycles is not None and (type(max_cycles) is not int or max_cycles < 1):
        raise ValueError("max_cycles must be a positive integer")
    state_dir = Path(state_dir)

    with _lock(state_dir) as acquired:
        if not acquired:
            return "locked"
        with closing(_connect(state_dir)) as db:
            while True:
                local = _get(db, "hold")
                queue = _get(db, "queue")
                queue_active = bool(queue or (local and local.get("kind", "seated") == "waitlist"))
                if continuous or queue_active:
                    _drain_queue_outbox(db, notifier)
                else:
                    _drain_outbox(db, notifier, wait=armed and not once)

                if continuous:
                    durable_unknown = (
                        _get(db, "expiry_unknown")
                        or _get(db, "reconciliation_unknown")
                        or _get(db, "ambiguous_hold")
                        or _get(db, "queue_unknown")
                    )
                    if durable_unknown:
                        return "ambiguous"
                    if local and _get(db, "intent"):
                        _preserve_account_unknown(db, "intent-with-active-hold", [])
                        _drain_queue_outbox(db, notifier)
                        return "ambiguous"
                if local:
                    if local.get("kind", "seated") == "waitlist":
                        _confirm_queue(db, _hold_from_data(local))
                        result = _monitor_queue(
                            db,
                            provider,
                            notifier,
                            wait=armed and not once,
                            continuous=continuous,
                            trip=trip,
                        )
                    elif not continuous:
                        return "existing-ticket" if local.get("paid") else "existing-hold"
                    elif local.get("paid"):
                        try:
                            _record_paid(db, [_hold_from_data(local)], continuing=True, clear_hold=True)
                        except Exception:
                            _preserve_expiry_unknown(db, local, "paid-record-invalid")
                            return "ambiguous"
                        _drain_queue_outbox(db, notifier)
                        result = "paid"
                    else:
                        result = _monitor_hold(
                            db,
                            trip,
                            provider,
                            notifier,
                            wait=armed and not once,
                        )
                    if continuous and not once and result in {"allocated", "expired", "paid"}:
                        continue
                    return result

                if _get(db, "queue_unknown"):
                    return "ambiguous"
                if queue:
                    result = _monitor_queue(
                        db,
                        provider,
                        notifier,
                        wait=armed and not once,
                        continuous=continuous,
                        trip=trip,
                    )
                    if continuous and not once and result in {"allocated", "paid"}:
                        continue
                    return result
                if _get(db, "reconciliation_unknown") or (
                    _get(db, "ambiguous_hold") and not _get(db, "intent")
                ):
                    return "ambiguous"

                try:
                    if continuous and not _get(db, "intent"):
                        result = _reconcile_continuous(db, trip, provider)
                    else:
                        result = _reconcile(db, trip, provider, continuous=continuous)
                except TransientError as exc:
                    if once:
                        return "incomplete"
                    time.sleep(max(1.0, exc.retry_after if exc.retry_after is not None else 30.0))
                    continue
                if result:
                    if continuous:
                        if result in {"ambiguous", "blocked"} and not _get(db, "reconciliation_unknown"):
                            _preserve_account_unknown(db, f"reconciliation-{result}", [])
                        _drain_queue_outbox(db, notifier)
                    else:
                        _drain_outbox(db, notifier, wait=armed and not once)
                    if continuous and not once and result in {
                        "allocated",
                        "existing-hold",
                        "existing-ticket",
                    }:
                        continue
                    return result

                result = _search(
                    db,
                    trip,
                    provider,
                    notifier,
                    armed=armed,
                    once=once,
                    max_cycles=max_cycles,
                    continuous=continuous,
                )
                if (
                    continuous
                    and result in {"ambiguous", "blocked", "error"}
                    and not _get(db, "reconciliation_unknown")
                ):
                    _preserve_account_unknown(db, f"search-{result}", [])
                    _drain_queue_outbox(db, notifier)
                if continuous and not once and result in {
                    "allocated",
                    "existing-hold",
                    "paid",
                    "reserved",
                }:
                    continue
                return result


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
            "expiry_absence": None,
            "expiry_unknown": None,
            "expired_holds": [],
            "paid_tickets": [],
            "notifications_pending": 0,
        }
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
        intent, hold, queue = _get(db, "intent"), _get(db, "hold"), _get(db, "queue")
        mismatch, queue_unknown = _get(db, "ambiguous_hold"), _get(db, "queue_unknown")
        reconciliation_unknown = _get(db, "reconciliation_unknown")
        expiry_absence, expiry_unknown = _get(db, "expiry_absence"), _get(db, "expiry_unknown")
        expired_holds, paid_tickets = _get(db, "expired_holds") or [], _get(db, "paid_tickets") or []
        pending = db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        state = (
            "ambiguous"
            if intent or mismatch or reconciliation_unknown or queue_unknown or expiry_unknown
            else "held"
            if hold
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
            "expiry_absence": expiry_absence,
            "expiry_unknown": expiry_unknown,
            "expired_holds": expired_holds,
            "paid_tickets": paid_tickets,
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
