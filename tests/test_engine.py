from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from korail_watch.domain import KST, AmbiguousReservation, Hold, SoldOut, Train, TransientError, Trip
from korail_watch.engine import _lock, demo, run, status


def trip(**changes) -> Trip:
    values = {
        "date": "2099-09-24",
        "start": "12:00",
        "end": "18:00",
        "departures": ("서울", "용산", "수서"),
        "arrivals": ("대전", "서대전"),
        "adults": 1,
    }
    values.update(changes)
    return Trip(**values)


def train(**changes) -> Train:
    values = {
        "key": "KTX-1",
        "date": "2099-09-24",
        "departure": "서울",
        "arrival": "대전",
        "dep_time": "12:30",
        "arr_time": "13:30",
        "general": True,
        "special": True,
    }
    values.update(changes)
    return Train(**values)


class Provider:
    def __init__(self, trains=()):
        self.trains = list(trains)
        self.calls = []
        self.reserve_calls = []
        self.remote = []

    def reservations(self):
        return list(self.remote)

    def tickets(self):
        return []

    def search(self, requested, departure, arrival, after):
        self.calls.append((departure, arrival, after))
        return [item for item in self.trains if item.departure == departure and item.arrival == arrival]

    def reserve(self, selected, seat_class, adults):
        self.reserve_calls.append((selected, seat_class, adults))
        result = Hold("R1", selected, "2099-09-24T12:20:00+09:00", 50_000)
        self.remote.append(result)
        return result


class Notifier:
    def __init__(self, error=None):
        self.messages = []
        self.error = error

    def send(self, message):
        self.messages.append(message)
        if self.error:
            error, self.error = self.error, None
            raise error


class DomainTests(unittest.TestCase):
    def test_trip_validation_and_inclusive_boundaries(self):
        requested = trip()
        before = datetime(2099, 9, 24, 11, 0, tzinfo=KST)
        self.assertTrue(requested.matches(train(dep_time="12:00"), now=before))
        self.assertTrue(requested.matches(train(dep_time="18:00"), now=before))
        self.assertFalse(requested.matches(train(dep_time="18:01"), now=before))
        self.assertFalse(requested.matches(train(departure="광명"), now=before))
        self.assertFalse(requested.matches(train(arrival="부산"), now=before))
        with self.assertRaises(ValueError):
            trip(start="18:00", end="12:00")
        with self.assertRaises(ValueError):
            trip(adults=2)
        with self.assertRaises(ValueError):
            Hold("R", train(), "2099-09-24T12:20:00", None)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.state = Path(self.temporary.name) / "state"

    def tearDown(self):
        self.temporary.cleanup()

    def test_once_covers_all_routes_and_hourly_windows_without_reserving(self):
        provider = Provider()
        self.assertEqual(run(trip(), provider, None, self.state, once=True), "not-found")
        self.assertEqual(len(provider.calls), 42)
        self.assertEqual(
            set(provider.calls),
            {
                (departure, arrival, f"{hour:02d}:00:00")
                for hour in range(12, 19)
                for departure in ("서울", "용산", "수서")
                for arrival in ("대전", "서대전")
            },
        )
        self.assertEqual(provider.reserve_calls, [])

    def test_general_sold_out_falls_back_to_special_and_persists_before_notify(self):
        selected = train()

        class Fallback(Provider):
            def reserve(self, selected, seat_class, adults):
                self.reserve_calls.append((selected, seat_class, adults))
                if seat_class == "general":
                    raise SoldOut()
                return Hold("R2", selected, None, 60_000)

        provider, notifier = Fallback([selected]), Notifier()
        self.assertEqual(run(trip(), provider, notifier, self.state, armed=True, once=True), "reserved")
        self.assertEqual([call[1:] for call in provider.reserve_calls], [("general", 1), ("special", 1)])
        snapshot = status(self.state)
        self.assertEqual(snapshot["state"], "held")
        self.assertEqual(snapshot["hold"]["reference"], "R2")
        self.assertIsNone(snapshot["intent"])
        self.assertIn("check the Korail app immediately", notifier.messages[0])

    def test_crash_before_write_result_leaves_intent_and_restart_does_not_retry(self):
        class Crash(Provider):
            def reserve(self, selected, seat_class, adults):
                self.reserve_calls.append((selected, seat_class, adults))
                raise KeyboardInterrupt

        first = Crash([train()])
        with self.assertRaises(KeyboardInterrupt):
            run(trip(), first, Notifier(), self.state, armed=True, once=True)
        self.assertIsNotNone(status(self.state)["intent"])

        second = Provider([train()])
        self.assertEqual(run(trip(), second, Notifier(), self.state, armed=True, once=True), "ambiguous")
        self.assertEqual(second.reserve_calls, [])

    def test_crash_after_provider_write_reconciles_availability_independent_identity(self):
        selected = train()
        reconciled = Hold("R3", train(general=False, special=False), None, None)

        class CrashAfter(Provider):
            def reserve(self, selected, seat_class, adults):
                self.reserve_calls.append((selected, seat_class, adults))
                self.remote.append(reconciled)
                raise KeyboardInterrupt

        first = CrashAfter([selected])
        with self.assertRaises(KeyboardInterrupt):
            run(trip(), first, Notifier(), self.state, armed=True, once=True)
        second = Provider([selected])
        second.remote = [reconciled]
        self.assertEqual(run(trip(), second, Notifier(), self.state, armed=True, once=True), "existing-hold")
        self.assertEqual(second.reserve_calls, [])
        self.assertEqual(status(self.state)["hold"]["reference"], "R3")

    def test_ambiguous_write_with_empty_reconciliation_never_retries(self):
        class Ambiguous(Provider):
            def reserve(self, selected, seat_class, adults):
                self.reserve_calls.append((selected, seat_class, adults))
                raise AmbiguousReservation()

        provider = Ambiguous([train()])
        self.assertEqual(run(trip(), provider, Notifier(), self.state, armed=True, once=True), "ambiguous")
        self.assertEqual(len(provider.reserve_calls), 1)
        self.assertEqual(run(trip(date="2099-09-25"), provider, Notifier(), self.state, armed=True, once=True), "ambiguous")
        self.assertEqual(len(provider.reserve_calls), 1)

    def test_mismatched_reservation_result_is_preserved_and_blocks(self):
        class Wrong(Provider):
            def reserve(self, selected, seat_class, adults):
                self.reserve_calls.append((selected, seat_class, adults))
                return Hold("WRONG", train(key="KTX-2", dep_time="13:00"), None, None)

        provider = Wrong([train()])
        self.assertEqual(run(trip(), provider, Notifier(), self.state, armed=True, once=True), "ambiguous")
        snapshot = status(self.state)
        self.assertIsNotNone(snapshot["intent"])
        self.assertEqual(snapshot["ambiguous_hold"]["reference"], "WRONG")

    def test_notification_failure_cannot_trigger_another_hold(self):
        provider = Provider([train()])
        notifier = Notifier(TransientError(retry_after=60))
        self.assertEqual(run(trip(), provider, notifier, self.state, armed=True, once=True), "reserved")
        self.assertEqual(status(self.state)["notifications_pending"], 1)
        self.assertEqual(len(provider.reserve_calls), 1)
        self.assertEqual(run(trip(), provider, Notifier(), self.state, armed=True, once=True), "existing-hold")
        self.assertEqual(len(provider.reserve_calls), 1)

    def test_duplicate_process_lock_fails_closed(self):
        with _lock(self.state) as acquired:
            self.assertTrue(acquired)
            self.assertEqual(run(trip(), Provider(), None, self.state, once=True), "locked")

    def test_past_cutoff_and_unknown_reconciliation_fail_closed(self):
        past = trip(date="2000-01-01")
        provider = Provider()
        self.assertEqual(run(past, provider, None, self.state, once=True), "cutoff")
        self.assertEqual(provider.calls, [])

        class Broken(Provider):
            def reservations(self):
                raise RuntimeError("password=do-not-store")

        other = Path(self.temporary.name) / "other"
        self.assertEqual(run(trip(), Broken(), None, other, once=True), "error")
        self.assertNotIn("do-not-store", json.dumps(status(other)))

    def test_transient_search_honors_retry_after_and_stays_read_only(self):
        class Limited(Provider):
            def search(self, requested, departure, arrival, after):
                self.calls.append((departure, arrival, after))
                if len(self.calls) == 1:
                    raise TransientError(retry_after=7)
                return []

        provider = Limited()
        with patch("korail_watch.engine.time.sleep") as sleep:
            self.assertEqual(run(trip(), provider, None, self.state, once=True), "not-found")
        sleep.assert_called_once_with(7)
        self.assertEqual(provider.reserve_calls, [])

    def test_demo_is_offline_and_uses_only_caller_state(self):
        with patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            self.assertEqual(demo(self.state), "reserved")
        self.assertEqual(status(self.state)["hold"]["reference"], "DEMO")


if __name__ == "__main__":
    unittest.main()
