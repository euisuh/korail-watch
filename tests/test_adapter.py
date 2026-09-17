from __future__ import annotations

import io
import os
import types
import unittest
import urllib.error
import urllib.parse
from unittest.mock import patch

import requests

from korail_watch.domain import AmbiguousReservation, BlockedError, Hold, SoldOut, TransientError, Trip
from korail_watch import korail
from korail_watch.korail import KorailProvider
from korail_watch.notifier import TelegramNotifier


class _SoldOut(Exception):
    pass


class _NeedLogin(Exception):
    pass


class _KorailError(Exception):
    pass


class _NoResults(Exception):
    pass


class _Adult:
    def __init__(self, count=1):
        self.count = count


SDK = types.SimpleNamespace(
    SoldOutError=_SoldOut,
    NeedToLoginError=_NeedLogin,
    KorailError=_KorailError,
    NoResultsError=_NoResults,
    TrainType=types.SimpleNamespace(ALL="all"),
    ReserveOption=types.SimpleNamespace(GENERAL_ONLY="general-only", SPECIAL_ONLY="special-only"),
    AdultPassenger=_Adult,
)


class RawTrain:
    train_type = "100"
    train_no = "001"
    train_group = "100"
    dep_name = "서울"
    dep_code = "0001"
    dep_date = "20260924"
    dep_time = "120000"
    arr_name = "대전"
    arr_code = "0010"
    arr_date = "20260924"
    arr_time = "130000"
    run_date = "20260924"

    def has_general_seat(self):
        return True

    def has_special_seat(self):
        return False


class RawReservation(RawTrain):
    rsv_id = "reservation-1"
    buy_limit_date = "20260924"
    buy_limit_time = "143000"
    price = 23700


class RawTicket(RawTrain):
    price = 23700

    def get_ticket_no(self):
        return "ticket-1"


class FakeClient:
    def __init__(self):
        self.search_args = None
        self.reserve_args = None

    def search_train(self, *args, **kwargs):
        self.search_args = (args, kwargs)
        return [RawTrain()]

    def reserve(self, *args, **kwargs):
        print("SDK SHOULD NOT PRINT")
        self.reserve_args = (args, kwargs)
        return RawReservation()

    def reservations(self):
        return [RawReservation()]

    def tickets(self):
        return [RawTicket()]


def provider(client=None):
    result = KorailProvider()
    result._sdk = SDK
    result._client = client or FakeClient()
    return result


class ProviderTests(unittest.TestCase):
    def setUp(self):
        korail._NEXT_REQUEST = 0.0

    def test_search_requests_all_trains_including_sold_out_and_preserves_raw(self):
        client = FakeClient()
        adapter = provider(client)
        trip = Trip("2026-09-24", "12:00", "18:00", ("서울",), ("대전",))

        trains = adapter.search(trip, "서울", "대전", "12:00:00")

        args, kwargs = client.search_args
        self.assertEqual(("서울", "대전", "20260924", "120000", "all"), args[:5])
        self.assertEqual(1, args[5][0].count)
        self.assertTrue(kwargs["include_no_seats"])
        self.assertEqual("2026-09-24", trains[0].date)
        self.assertEqual("12:00", trains[0].dep_time)
        self.assertTrue(trains[0].general)
        self.assertFalse(trains[0].special)
        self.assertIsInstance(trains[0].raw, RawTrain)

    def test_reserve_is_exact_class_never_waits_and_suppresses_sdk_print(self):
        client = FakeClient()
        adapter = provider(client)
        train = adapter._train(RawTrain())
        output = io.StringIO()

        with patch("sys.stdout", output):
            hold = adapter.reserve(train, "general", 1)

        args, kwargs = client.reserve_args
        self.assertIs(args[0], train.raw)
        self.assertEqual(1, args[1][0].count)
        self.assertEqual("general-only", kwargs["option"])
        self.assertFalse(kwargs["try_waiting"])
        self.assertEqual("", output.getvalue())
        self.assertEqual("reservation-1", hold.reference)
        self.assertEqual("2026-09-24T14:30:00+09:00", hold.deadline)
        self.assertFalse(hold.paid)

    def test_reserve_unknown_or_missing_result_is_ambiguous(self):
        for failure in (requests.Timeout(), None):
            client = FakeClient()
            client.reserve = lambda *args, value=failure, **kwargs: (_ for _ in ()).throw(value) if value else None
            with self.subTest(failure=failure):
                with self.assertRaises(AmbiguousReservation):
                    provider(client).reserve(provider()._train(RawTrain()), "general", 1)

    def test_reserve_definitive_sold_out_is_not_ambiguous(self):
        client = FakeClient()
        client.reserve = lambda *args, **kwargs: (_ for _ in ()).throw(_SoldOut())
        with self.assertRaises(SoldOut):
            provider(client).reserve(provider()._train(RawTrain()), "general", 1)

    def test_reconciliation_distinguishes_unpaid_and_paid(self):
        adapter = provider()
        reservations, tickets = adapter.reservations(), adapter.tickets()
        self.assertEqual([False], [item.paid for item in reservations])
        self.assertEqual([True], [item.paid for item in tickets])
        self.assertEqual("ticket-1", tickets[0].reference)
        self.assertIsNone(tickets[0].deadline)

    def test_session_paces_every_request_and_applies_timeout(self):
        clock = [100.0]
        sleeps = []
        calls = []
        response = types.SimpleNamespace(status_code=200, headers={}, raise_for_status=lambda: None)

        def monotonic():
            return clock[0]

        def sleep(delay):
            sleeps.append(delay)
            clock[0] += delay

        def request(_self, method, url, **kwargs):
            calls.append((method, url, kwargs))
            return response

        with patch("korail_watch.korail.time.monotonic", side_effect=monotonic), patch(
            "korail_watch.korail.time.sleep", side_effect=sleep
        ), patch("korail_watch.korail.random.uniform", return_value=0.25), patch.object(
            requests.Session, "request", request
        ):
            session = korail._PacedSession(1.0, 7.0)
            session.get("https://example.invalid/one")
            session.get("https://example.invalid/two")

        self.assertEqual([5.25], sleeps)
        self.assertEqual([7.0, 7.0], [call[2]["timeout"] for call in calls])

    def test_http_failures_are_sanitized(self):
        response = requests.Response()
        response.status_code = 403
        response.url = "https://example.invalid/?password=secret"
        error = requests.HTTPError("secret", response=response)
        with self.assertRaises(BlockedError) as raised:
            provider()._raise(error)
        self.assertNotIn("secret", str(raised.exception))


class _Response:
    def __init__(self, body=b'{"ok":true}'):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.body


class TelegramTests(unittest.TestCase):
    env = {
        "TELEGRAM_BOT_TOKEN": "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
        "TELEGRAM_CHAT_ID": "-1001234567890",
    }

    def test_validates_credentials(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "bad", "TELEGRAM_CHAT_ID": "chat"}, clear=True):
            with self.assertRaises(ValueError):
                TelegramNotifier()

    def test_send_uses_plain_form_post_and_timeout(self):
        captured = {}

        def open_request(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return _Response()

        with patch.dict(os.environ, self.env, clear=True), patch(
            "urllib.request.urlopen", side_effect=open_request
        ):
            TelegramNotifier(timeout=9).send("서울 < 대전 & pay")

        fields = urllib.parse.parse_qs(captured["request"].data.decode())
        self.assertEqual("POST", captured["request"].method)
        self.assertEqual(["서울 < 대전 & pay"], fields["text"])
        self.assertNotIn("parse_mode", fields)
        self.assertEqual(9.0, captured["timeout"])

    def test_429_uses_retry_after_without_leaking_token(self):
        token = self.env["TELEGRAM_BOT_TOKEN"]
        error = urllib.error.HTTPError(
            f"https://api.telegram.org/bot{token}/sendMessage", 429, token, {"Retry-After": "17"}, None
        )
        with patch.dict(os.environ, self.env, clear=True), patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(TransientError) as raised:
                TelegramNotifier().send("test")
        self.assertEqual(17.0, raised.exception.retry_after)
        self.assertNotIn(token, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
