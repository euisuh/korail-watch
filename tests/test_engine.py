from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from korail_watch.domain import (
    KST,
    AmbiguousReservation,
    BlockedError,
    Hold,
    SoldOut,
    Train,
    TransientError,
    Trip,
)
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
        return [
            item
            for item in self.trains
            if item.departure == departure and item.arrival == arrival and item.dep_time + ":00" >= after
        ]

    def reserve(self, selected, seat_class, adults):
        self.reserve_calls.append((selected, seat_class, adults))
        result = Hold("R1", selected, "2099-09-24T12:20:00+09:00", 50_000)
        self.remote.append(result)
        return result


class QueueProvider(Provider):
    supported_modes = frozenset(("general", "special", "waitlist"))

    def reserve(self, selected, kind, adults):
        self.reserve_calls.append((selected, kind, adults))
        result = Hold("Q1", selected, None, None, kind="waitlist")
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

    def test_flexible_fields_preserve_old_positional_constructors(self):
        raw = object()
        selected = Train("K", "2099-09-24", "서울", "대전", "12:00", "13:00", True, False, raw)
        self.assertIs(selected.raw, raw)
        self.assertFalse(selected.waitlist)
        self.assertEqual(Hold("R", selected, None, None, False).kind, "seated")
        self.assertTrue(trip(allow_waitlist=True).allow_waitlist)
        with self.assertRaises(ValueError):
            trip(allow_standing=1)
        with self.assertRaises(ValueError):
            Hold("Q", selected, "2099-09-24T12:20:00+09:00", None, kind="waitlist")


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

    def test_dense_hour_continues_from_last_result_without_starving_routes(self):
        first = train(key="1", dep_time="12:10")
        last = train(key="2", dep_time="12:35")
        later = train(key="3", dep_time="12:40")

        class Paged(Provider):
            def search(self, requested, departure, arrival, after):
                self.calls.append((departure, arrival, after))
                if after == "12:00:00":
                    return [first, last]
                if after == "12:35:01":
                    return [later, train(key="4", dep_time="13:10")]
                return []

        provider = Paged()
        requested = trip(end="13:00", departures=("서울",), arrivals=("대전",))
        self.assertEqual(run(requested, provider, None, self.state, once=True), "available")
        self.assertIn(("서울", "대전", "12:35:01"), provider.calls)

    def test_immediate_seat_wins_over_earlier_waitlist_candidate(self):
        queued = train(key="queue", departure="서울", dep_time="12:00", general=False, special=False, waitlist=True)
        seated = train(key="seat", departure="용산", dep_time="12:00", general=True, special=False)

        class Priority(QueueProvider):
            def search(self, requested, departure, arrival, after):
                self.calls.append((departure, arrival, after))
                return [queued] if departure == "서울" else [seated]

            def reserve(self, selected, kind, adults):
                self.reserve_calls.append((selected, kind, adults))
                return Hold("S1", selected, None, None)

        provider = Priority()
        requested = trip(
            start="12:00",
            end="12:00",
            departures=("서울", "용산"),
            arrivals=("대전",),
            allow_waitlist=True,
        )
        self.assertEqual(run(requested, provider, Notifier(), self.state, armed=True, once=True), "reserved")
        self.assertEqual([call[1] for call in provider.reserve_calls], ["general"])
        self.assertEqual(len(provider.calls), 2)

    def test_waitlist_is_attempted_only_after_complete_pass_and_persisted_distinctly(self):
        candidate = train(dep_time="12:00", general=False, special=False, waitlist=True)

        class Ordered(QueueProvider):
            def __init__(self):
                super().__init__([candidate])
                self.events = []

            def search(self, *args):
                self.events.append("search")
                return super().search(*args)

            def reserve(self, *args):
                self.events.append("reserve")
                return super().reserve(*args)

        provider = Ordered()
        requested = trip(
            start="12:00",
            end="12:00",
            departures=("서울", "용산"),
            arrivals=("대전",),
            allow_waitlist=True,
        )
        notifier = Notifier()
        self.assertEqual(run(requested, provider, notifier, self.state, armed=True, once=True), "queued")
        self.assertGreaterEqual(len(provider.calls), 2)
        self.assertEqual(provider.events[-1], "reserve")
        self.assertEqual([call[1] for call in provider.reserve_calls], ["waitlist"])
        snapshot = status(self.state)
        self.assertEqual(snapshot["state"], "queued")
        self.assertEqual(snapshot["queue"]["kind"], "waitlist")
        self.assertIsNone(snapshot["hold"])
        self.assertIn("no seat is guaranteed", notifier.messages[0])
        self.assertNotIn("payment deadline", notifier.messages[0])

    def test_incomplete_pass_never_joins_waitlist(self):
        candidate = train(dep_time="12:00", general=False, special=False, waitlist=True)

        class Partial(QueueProvider):
            def search(self, requested, departure, arrival, after):
                self.calls.append((departure, arrival, after))
                if len(self.calls) == 1:
                    raise TransientError(retry_after=0)
                return [candidate]

        provider = Partial()
        requested = trip(
            start="12:00",
            end="12:00",
            departures=("서울", "용산"),
            arrivals=("대전",),
            allow_waitlist=True,
        )
        with patch("korail_watch.engine.time.sleep"):
            self.assertEqual(run(requested, provider, Notifier(), self.state, armed=True, once=True), "incomplete")
        self.assertEqual(provider.reserve_calls, [])
        self.assertIsNone(status(self.state)["intent"])

    def test_pagination_anomaly_never_joins_waitlist(self):
        candidate = train(dep_time="12:00", general=False, special=False, waitlist=True)

        class StalePage(QueueProvider):
            def search(self, requested, departure, arrival, after):
                self.calls.append((departure, arrival, after))
                return [candidate]

        provider = StalePage()
        requested = trip(
            start="12:00",
            end="13:00",
            departures=("서울",),
            arrivals=("대전",),
            allow_waitlist=True,
        )
        self.assertEqual(run(requested, provider, Notifier(), self.state, armed=True, once=True), "incomplete")
        self.assertEqual(provider.reserve_calls, [])

    def test_waitlist_candidate_is_rechecked_before_deferred_mutation(self):
        candidate = train(general=False, special=False, waitlist=True)
        provider = QueueProvider([candidate])
        requested = trip(allow_waitlist=True)
        with patch.object(Trip, "matches", side_effect=(True, False)):
            self.assertEqual(run(requested, provider, Notifier(), self.state, armed=True, once=True), "available")
        self.assertEqual(provider.reserve_calls, [])

    def test_baseline_seated_modes_remain_supported_with_extra_mode_contract(self):
        class ExtraOnly(Provider):
            supported_modes = frozenset(("waitlist",))

        provider = ExtraOnly([train(general=True, special=False, waitlist=True)])
        self.assertEqual(
            run(trip(allow_waitlist=True), provider, Notifier(), self.state, armed=True, once=True),
            "reserved",
        )
        self.assertEqual(provider.reserve_calls[0][1], "general")

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

    def test_legacy_pending_intent_without_new_fields_reconciles_safely(self):
        class Crash(Provider):
            def reserve(self, selected, kind, adults):
                raise KeyboardInterrupt

        selected = train()
        with self.assertRaises(KeyboardInterrupt):
            run(trip(), Crash([selected]), Notifier(), self.state, armed=True, once=True)
        path = self.state / "state.sqlite3"
        with sqlite3.connect(path) as db:
            data = json.loads(db.execute("SELECT value FROM state WHERE key='intent'").fetchone()[0])
            for field in ("waitlist", "standing", "mixed"):
                data["train"].pop(field)
            data["seat_class"] = data.pop("kind")
            db.execute("UPDATE state SET value=? WHERE key='intent'", (json.dumps(data),))

        provider = Provider()
        provider.remote = [Hold("LEGACY", train(general=False, special=False), None, None)]
        self.assertEqual(run(trip(), provider, Notifier(), self.state, armed=True, once=True), "existing-hold")
        self.assertEqual(status(self.state)["hold"]["kind"], "seated")

    def test_departed_matching_hold_still_stops_scanning(self):
        provider = Provider([train(key="later", dep_time="14:00")])
        provider.remote = [Hold("EARLIER", train(general=False, special=False), None, None)]
        with patch("korail_watch.engine._now", return_value=datetime(2099, 9, 24, 13, 0, tzinfo=KST)):
            self.assertEqual(run(trip(), provider, Notifier(), self.state, armed=True, once=True), "existing-hold")
        self.assertEqual(provider.calls, [])
        self.assertEqual(provider.reserve_calls, [])

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

    def test_queue_restart_monitors_same_reference_without_search_or_booking(self):
        candidate = train(general=False, special=False, waitlist=True)
        first = QueueProvider([candidate])
        requested = trip(allow_waitlist=True)
        self.assertEqual(run(requested, first, Notifier(), self.state, armed=True, once=True), "queued")

        second = QueueProvider([train(key="other", general=True)])
        second.remote = [Hold("Q1", candidate, None, None, kind="waitlist")]
        self.assertEqual(run(trip(date="2099-09-25"), second, Notifier(), self.state, armed=True, once=True), "queued")
        self.assertEqual(second.calls, [])
        self.assertEqual(second.reserve_calls, [])

    def test_crash_after_waitlist_mutation_stays_ambiguous_without_replaying_followup(self):
        candidate = train(general=False, special=False, waitlist=True)

        class CrashAfterFollowup(QueueProvider):
            def reserve(self, selected, kind, adults):
                self.reserve_calls.append((selected, kind, adults))
                self.remote.append(Hold("Q1", selected, None, None, kind="waitlist"))
                raise KeyboardInterrupt

        requested = trip(allow_waitlist=True)
        first = CrashAfterFollowup([candidate])
        with self.assertRaises(KeyboardInterrupt):
            run(requested, first, Notifier(), self.state, armed=True, once=True)
        self.assertEqual(status(self.state)["intent"]["kind"], "waitlist")

        second = QueueProvider([candidate])
        second.remote = [Hold("Q1", candidate, None, None, kind="waitlist")]
        self.assertEqual(run(requested, second, Notifier(), self.state, armed=True, once=True), "ambiguous")
        self.assertEqual(second.reserve_calls, [])
        snapshot = status(self.state)
        self.assertEqual(snapshot["state"], "ambiguous")
        self.assertEqual(snapshot["intent"]["kind"], "waitlist")
        self.assertEqual(snapshot["queue_unknown"]["reason"], "waitlist-followup-unproven")

    def test_queue_allocation_transitions_atomically_and_notifies_payment(self):
        candidate = train(general=False, special=False, waitlist=True)
        requested = trip(allow_waitlist=True)
        first, first_notifier = QueueProvider([candidate]), Notifier()
        self.assertEqual(run(requested, first, first_notifier, self.state, armed=True, once=True), "queued")

        allocated = Hold("Q1", train(general=False, special=False), "2099-09-24T12:20:00+09:00", 50_000)
        second, second_notifier = QueueProvider(), Notifier()
        second.remote = [allocated]
        self.assertEqual(run(requested, second, second_notifier, self.state, armed=True, once=True), "allocated")
        snapshot = status(self.state)
        self.assertEqual(snapshot["state"], "held")
        self.assertEqual(snapshot["hold"]["reference"], "Q1")
        self.assertIsNone(snapshot["queue"])
        self.assertIn("payment deadline", second_notifier.messages[-1])
        self.assertEqual(second.reserve_calls, [])

    def test_missing_queue_is_durable_ambiguity_and_never_rebooks(self):
        candidate = train(general=False, special=False, waitlist=True)
        requested = trip(allow_waitlist=True)
        first = QueueProvider([candidate])
        self.assertEqual(run(requested, first, Notifier(), self.state, armed=True, once=True), "queued")

        missing, notifier = QueueProvider([train(key="new")]), Notifier()
        self.assertEqual(run(requested, missing, notifier, self.state, armed=True, once=True), "queue-missing")
        snapshot = status(self.state)
        self.assertEqual(snapshot["state"], "ambiguous")
        self.assertEqual(snapshot["queue_unknown"]["reason"], "missing")
        self.assertIn("can no longer be confirmed", notifier.messages[-1])

        later = QueueProvider([train(key="new")])
        later.remote = [Hold("Q1", candidate, None, None, kind="waitlist")]
        self.assertEqual(run(requested, later, Notifier(), self.state, armed=True, once=True), "ambiguous")
        self.assertEqual(later.calls, [])
        self.assertEqual(later.reserve_calls, [])

    def test_unverified_external_waitlist_is_not_adopted_as_completed_queue(self):
        candidate = train(general=False, special=False, waitlist=True)
        provider = QueueProvider()
        provider.remote = [Hold("EXTERNAL", candidate, None, None, kind="waitlist")]
        self.assertEqual(run(trip(allow_waitlist=True), provider, Notifier(), self.state, armed=True, once=True), "ambiguous")
        snapshot = status(self.state)
        self.assertEqual(snapshot["state"], "ambiguous")
        self.assertIsNone(snapshot["queue"])
        self.assertEqual(snapshot["queue_unknown"]["reason"], "existing-waitlist-unverified")

    def test_queue_transient_read_retries_without_new_booking(self):
        candidate = train(general=False, special=False, waitlist=True)
        requested = trip(allow_waitlist=True)
        first = QueueProvider([candidate])
        self.assertEqual(run(requested, first, Notifier(), self.state, armed=True, once=True), "queued")

        allocated = Hold("Q1", train(general=False, special=False), None, None)

        class Recovering(QueueProvider):
            def __init__(self):
                super().__init__()
                self.reads = 0

            def reservations(self):
                self.reads += 1
                if self.reads == 1:
                    raise TransientError(retry_after=7)
                return [allocated]

        provider = Recovering()
        with patch("korail_watch.engine.time.sleep") as sleep:
            self.assertEqual(run(requested, provider, Notifier(), self.state, armed=True), "allocated")
        sleep.assert_called_once_with(7)
        self.assertEqual(provider.calls, [])
        self.assertEqual(provider.reserve_calls, [])

    def test_queue_notification_outage_does_not_delay_allocation_monitoring(self):
        candidate = train(general=False, special=False, waitlist=True)
        allocated = Hold(
            "Q1",
            train(general=False, special=False),
            "2099-09-24T12:20:00+09:00",
            50_000,
        )

        class ImmediateAllocation(QueueProvider):
            def reserve(self, selected, kind, adults):
                self.reserve_calls.append((selected, kind, adults))
                self.remote = [allocated]
                return Hold("Q1", selected, None, None, kind="waitlist")

        provider = ImmediateAllocation([candidate])
        notifier = Notifier(TransientError(retry_after=600))
        self.assertEqual(run(trip(allow_waitlist=True), provider, notifier, self.state, armed=True), "allocated")
        self.assertEqual(len(provider.reserve_calls), 1)
        self.assertEqual(len(notifier.messages), 2)
        self.assertIn("no seat is guaranteed", notifier.messages[0])
        self.assertIn("payment deadline", notifier.messages[1])
        snapshot = status(self.state)
        self.assertEqual(snapshot["state"], "held")
        self.assertEqual(snapshot["notifications_pending"], 0)

    def test_unsupported_mode_never_creates_intent(self):
        unsupported = train(general=False, special=False, waitlist=True, standing=True, mixed=True)
        provider = Provider([unsupported])
        requested = trip(allow_waitlist=True, allow_standing=True, allow_mixed=True)
        self.assertEqual(run(requested, provider, Notifier(), self.state, armed=True, once=True), "not-found")
        self.assertEqual(provider.reserve_calls, [])
        self.assertIsNone(status(self.state)["intent"])

    def test_supported_standing_and_mixed_require_matching_return_kind(self):
        class Flexible(Provider):
            supported_modes = frozenset(("general", "special", "standing", "mixed"))

            def reserve(self, selected, kind, adults):
                self.reserve_calls.append((selected, kind, adults))
                return Hold("F1", selected, None, None, kind=kind)

        for capability in ("standing", "mixed"):
            with self.subTest(capability=capability), tempfile.TemporaryDirectory() as directory:
                selected = train(general=False, special=False, **{capability: True})
                provider = Flexible([selected])
                requested = trip(**{f"allow_{capability}": True})
                self.assertEqual(
                    run(requested, provider, Notifier(), Path(directory) / "state", armed=True, once=True),
                    "reserved",
                )
                self.assertEqual(provider.reserve_calls[0][1], capability)

    def test_ambiguous_reconciliation_is_durable_without_local_intent(self):
        class Uncertain(Provider):
            def reservations(self):
                raise AmbiguousReservation()

        provider = Uncertain([train()])
        self.assertEqual(run(trip(), provider, Notifier(), self.state, armed=True, once=True), "ambiguous")
        self.assertEqual(status(self.state)["state"], "ambiguous")

        safe_later = Provider([train()])
        self.assertEqual(run(trip(), safe_later, Notifier(), self.state, armed=True, once=True), "ambiguous")
        self.assertEqual(safe_later.calls, [])
        self.assertEqual(safe_later.reserve_calls, [])

    def test_notification_failure_cannot_trigger_another_hold(self):
        provider = Provider([train()])
        notifier = Notifier(TransientError(retry_after=60))
        self.assertEqual(run(trip(), provider, notifier, self.state, armed=True, once=True), "reserved")
        self.assertEqual(status(self.state)["notifications_pending"], 1)
        self.assertEqual(len(provider.reserve_calls), 1)

    def test_watch_retries_notification_without_returning_to_provider(self):
        provider = Provider([train()])
        notifier = Notifier(TransientError(retry_after=7))
        with patch("korail_watch.engine.time.sleep") as sleep:
            self.assertEqual(run(trip(), provider, notifier, self.state, armed=True), "reserved")
        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args.args[0], 7, places=2)
        self.assertEqual(len(notifier.messages), 2)
        self.assertEqual(len(provider.reserve_calls), 1)
        self.assertEqual(status(self.state)["notifications_pending"], 0)

    def test_blocked_notification_stops_watcher_with_hold_and_outbox_durable(self):
        provider = Provider([train()])
        notifier = Notifier(BlockedError("Telegram rejected notification"))
        with self.assertRaises(BlockedError):
            run(trip(), provider, notifier, self.state, armed=True)
        self.assertEqual(len(notifier.messages), 1)
        self.assertEqual(len(provider.reserve_calls), 1)
        snapshot = status(self.state)
        self.assertEqual(snapshot["state"], "held")
        self.assertEqual(snapshot["notifications_pending"], 1)
        self.assertEqual(run(trip(), provider, Notifier(), self.state, armed=True, once=True), "existing-hold")
        self.assertEqual(len(provider.reserve_calls), 1)

    def test_duplicate_process_lock_fails_closed(self):
        with _lock(self.state) as acquired:
            self.assertTrue(acquired)
            self.assertEqual(run(trip(), Provider(), None, self.state, once=True), "locked")

    def test_status_reads_committed_hold_while_watch_lock_is_held(self):
        self.assertEqual(run(trip(), Provider([train()]), Notifier(), self.state, armed=True, once=True), "reserved")
        with _lock(self.state) as acquired:
            self.assertTrue(acquired)
            snapshot = status(self.state)
        self.assertEqual(snapshot["state"], "held")
        self.assertEqual(snapshot["hold"]["reference"], "R1")

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
            self.assertEqual(run(trip(), provider, None, self.state, once=True), "incomplete")
        sleep.assert_called_once_with(7)
        self.assertEqual(provider.reserve_calls, [])

    def test_demo_is_offline_and_uses_only_caller_state(self):
        with patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            self.assertEqual(demo(self.state), "reserved")
        self.assertEqual(status(self.state)["hold"]["reference"], "DEMO")


if __name__ == "__main__":
    unittest.main()
