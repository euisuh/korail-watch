from __future__ import annotations

import io
import json
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
    general_seat = "11"
    wait_reserve_flag = 0

    def has_general_seat(self):
        return True

    def has_special_seat(self):
        return False

    def has_general_waiting_list(self):
        return self.wait_reserve_flag == 9


class RawReservation(RawTrain):
    rsv_id = "reservation-1"
    buy_limit_date = "20260924"
    buy_limit_time = "143000"
    price = 23700
    seat_no_count = 1
    standing_no_count = 0
    reservation_type_code = "3"


class RawTicket(RawTrain):
    price = 23700
    seat_no_count = 1

    def get_ticket_no(self):
        return "ticket-1"


class FakeClient:
    _device = "AD"
    _version = "250601002"
    _key = "session-key"

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

    def _result_check(self, payload):
        if payload.get("strResult") == "FAIL":
            raise _KorailError()
        return True


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
        self.assertFalse(trains[0].waitlist)
        self.assertFalse(trains[0].standing)
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
        self.assertEqual("seated", hold.kind)

    def test_reserve_unknown_or_missing_result_is_ambiguous(self):
        for failure in (requests.Timeout(), None):
            client = FakeClient()
            client.reserve = lambda *args, value=failure, **kwargs: (_ for _ in ()).throw(value) if value else None
            with self.subTest(failure=failure):
                with self.assertRaises(AmbiguousReservation):
                    provider(client).reserve(provider()._train(RawTrain()), "general", 1)

    def test_reserve_rejects_unexpected_seat_count(self):
        class TwoSeatReservation(RawReservation):
            seat_no_count = 2

        client = FakeClient()
        client.reserve = lambda *args, **kwargs: TwoSeatReservation()
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

    def test_reconciliation_distinguishes_waitlist_allocation_and_unknown(self):
        raw = RawReservation()
        raw.reservation_type_code = "8"
        client = FakeClient()
        client.reservations = lambda: [raw]
        queued = provider(client).reservations()[0]
        self.assertEqual("waitlist", queued.kind)
        self.assertIsNone(queued.deadline)

        raw.reservation_type_code = "3"
        allocated = provider(client).reservations()[0]
        self.assertEqual("seated", allocated.kind)
        self.assertIsNotNone(allocated.deadline)

        raw.reservation_type_code = None
        with self.assertRaises(AmbiguousReservation):
            provider(client).reservations()

    def test_waitlist_uses_1102_followup_and_preserves_stale_seat_intent(self):
        class WaitTrain(RawTrain):
            wait_reserve_flag = 9

        class WaitReservation(RawReservation):
            rsv_id = "queue-1"
            reservation_type_code = "8"

        client = FakeClient()
        adapter = provider(client)

        def reserve(*args, **kwargs):
            client.reserve_args = (args, kwargs)
            self.assertEqual("1102", adapter._session.reservation_overrides["txtJobId"])
            adapter._session.last_reservation_response = {
                "strResult": "SUCC",
                "h_msg_cd": "IRR000014",
                "h_pnr_no": "queue-1",
            }
            return WaitReservation()

        client.reserve = reserve
        client.reservations = lambda: [WaitReservation()]
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps({"strResult": "SUCC", "h_msg_cd": "IRZ000003"}).encode()

        with patch.object(adapter._session, "post", return_value=response) as post:
            hold = adapter.reserve(adapter._train(WaitTrain()), "waitlist", 1)

        self.assertEqual("waitlist", hold.kind)
        self.assertIsNone(hold.deadline)
        self.assertTrue(client.reserve_args[1]["try_waiting"])
        data = post.call_args.kwargs["data"]
        self.assertEqual(korail._RESERVATION_WAIT, post.call_args.args[0])
        self.assertEqual("queue-1", data["txtPnrNo"])
        self.assertEqual("Y", data["txtPsrmClChgFlg"])
        self.assertEqual("N", data["txtSmsSndFlg"])
        self.assertNotIn("txtCpNo", data)

    def test_waitlist_followup_failure_is_ambiguous_and_not_retried(self):
        class WaitTrain(RawTrain):
            general_seat = "13"
            wait_reserve_flag = 9

            def has_general_seat(self):
                return False

        client = FakeClient()
        adapter = provider(client)

        def reserve(*args, **kwargs):
            adapter._session.last_reservation_response = {
                "strResult": "SUCC",
                "h_msg_cd": "IRR000014",
                "h_pnr_no": "queue-1",
            }
            return None

        client.reserve = reserve
        with patch.object(adapter._session, "post", side_effect=requests.Timeout()) as post:
            with self.assertRaises(AmbiguousReservation):
                adapter.reserve(adapter._train(WaitTrain()), "waitlist", 1)
        post.assert_called_once()

    def test_waitlist_post_create_security_block_stops_before_followup(self):
        class WaitTrain(RawTrain):
            wait_reserve_flag = 9

        client = FakeClient()
        adapter = provider(client)

        def reserve(*args, **kwargs):
            adapter._session.last_reservation_response = {
                "strResult": "SUCC",
                "h_msg_cd": "IRR000014",
                "h_pnr_no": "queue-1",
            }
            response = requests.Response()
            response.status_code = 403
            raise requests.HTTPError(response=response)

        client.reserve = reserve
        with patch.object(adapter._session, "post") as post:
            with self.assertRaises(BlockedError):
                adapter.reserve(adapter._train(WaitTrain()), "waitlist", 1)
        post.assert_not_called()

    def test_standing_requires_exact_search_codes_and_readback_count(self):
        class StandingTrain(RawTrain):
            general_seat = "13"

            def has_general_seat(self):
                return False

        class StandingReservation(RawReservation):
            seat_no_count = 0
            standing_no_count = 1

        raw = StandingTrain()
        client = FakeClient()
        adapter = provider(client)
        adapter._session.search_status[korail._raw_key(raw)] = ("13", "11")

        def reserve(selected, *args, **kwargs):
            self.assertIs(selected._raw, raw)
            self.assertTrue(selected.has_general_seat())
            self.assertFalse(raw.has_general_seat())
            self.assertEqual({"txtStndFlg": "Y"}, adapter._session.reservation_overrides)
            return StandingReservation()

        client.reserve = reserve
        train = adapter._train(raw)
        self.assertTrue(train.standing)
        hold = adapter.reserve(train, "standing", 1)
        self.assertEqual("standing", hold.kind)
        self.assertEqual({}, adapter._session.reservation_overrides)
        self.assertFalse(raw.has_general_seat())

        # Inventory may reopen between search and 1101. One confirmed seat on
        # the exact train is a stronger entitlement than the standing request.
        client.reserve = lambda *args, **kwargs: RawReservation()
        upgraded = adapter.reserve(train, "standing", 1)
        self.assertEqual("seated", upgraded.kind)

        adapter._session.search_status[korail._raw_key(raw)] = ("13", "13")
        with self.assertRaises(SoldOut):
            adapter.reserve(adapter._train(raw), "standing", 1)

    def test_reserve_rejects_unsupported_modes_and_non_single_adult(self):
        adapter = provider()
        train = adapter._train(RawTrain())
        self.assertEqual(frozenset(("waitlist", "standing")), adapter.supported_modes)
        for kind in ("mixed", "other"):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                adapter.reserve(train, kind, 1)
        with self.assertRaises(ValueError):
            adapter.reserve(train, "general", 2)

    def test_pinned_sdk_discarded_status_is_retained_at_session_boundary(self):
        import korail2

        payload = {
            "strResult": "SUCC",
            "jrny_infos": {
                "jrny_info": [
                    {
                        "train_infos": {
                            "train_info": [
                                {
                                    "h_pnr_no": "reservation-1",
                                    "h_rsv_tp_cd": "3",
                                    "h_tot_seat_cnt": "1",
                                    "h_tot_stnd_cnt": "0",
                                    "h_run_dt": "20260924",
                                    "h_ntisu_lmt_dt": "20260924",
                                    "h_ntisu_lmt_tm": "143000",
                                    "h_rsv_amt": "23700",
                                    "h_trn_clsf_cd": "100",
                                    "h_trn_no": "001",
                                    "h_trn_gp_cd": "100",
                                    "h_dpt_rs_stn_nm": "서울",
                                    "h_dpt_tm": "120000",
                                    "h_arv_rs_stn_nm": "대전",
                                    "h_arv_tm": "130000",
                                }
                            ]
                        }
                    }
                ]
            },
        }
        response = requests.Response()
        response.status_code = 200
        response.url = "https://smart.letskorail.com/reservation"
        response._content = json.dumps(payload).encode()
        response.encoding = "utf-8"
        adapter = KorailProvider()
        client = korail2.Korail("member", "password", auto_login=False)
        client._session = adapter._session
        adapter._sdk, adapter._client = korail2, client

        with patch.object(requests.Session, "request", return_value=response):
            holds = adapter.reservations()

        self.assertEqual("reservation-1", holds[0].reference)
        self.assertEqual("seated", holds[0].kind)
        self.assertFalse(hasattr(holds[0].train.raw, "reservation_type_code"))

        payload["jrny_infos"]["jrny_info"][0]["train_infos"]["train_info"][0]["h_rsv_tp_cd"] = "8"
        response._content = json.dumps(payload).encode()
        korail._NEXT_REQUEST = 0.0
        with patch.object(requests.Session, "request", return_value=response):
            holds = adapter.reservations()
        self.assertEqual("waitlist", holds[0].kind)
        self.assertIsNone(holds[0].deadline)

    def test_pinned_sdk_waitlist_wire_and_followup_are_integrated(self):
        import korail2
        from korail2 import korail2 as implementation

        raw = implementation.Train(
            {
                "h_trn_clsf_cd": "100",
                "h_trn_clsf_nm": "KTX",
                "h_trn_no": "001",
                "h_trn_gp_cd": "100",
                "h_dpt_rs_stn_nm": "서울",
                "h_dpt_rs_stn_cd": "0001",
                "h_dpt_dt": "20260924",
                "h_dpt_tm": "120000",
                "h_arv_rs_stn_nm": "대전",
                "h_arv_rs_stn_cd": "0010",
                "h_arv_dt": "20260924",
                "h_arv_tm": "130000",
                "h_run_dt": "20260924",
                "h_rsv_psb_flg": "Y",
                "h_rsv_psb_nm": "예약 가능",
                "h_gen_rsv_cd": "11",
                "h_spe_rsv_cd": "13",
                "h_wait_rsv_flg": " 9",
            }
        )
        history = {
            "strResult": "SUCC",
            "jrny_infos": {
                "jrny_info": [
                    {
                        "train_infos": {
                            "train_info": [
                                {
                                    "h_pnr_no": "queue-1",
                                    "h_rsv_tp_cd": "8",
                                    "h_tot_seat_cnt": "1",
                                    "h_tot_stnd_cnt": "0",
                                    "h_run_dt": "20260924",
                                    "h_ntisu_lmt_dt": "20260924",
                                    "h_ntisu_lmt_tm": "143000",
                                    "h_rsv_amt": "23700",
                                    "h_trn_clsf_cd": "100",
                                    "h_trn_no": "001",
                                    "h_trn_gp_cd": "100",
                                    "h_dpt_rs_stn_nm": "서울",
                                    "h_dpt_tm": "120000",
                                    "h_arv_rs_stn_nm": "대전",
                                    "h_arv_tm": "130000",
                                }
                            ]
                        }
                    }
                ]
            },
        }
        first = {"strResult": "SUCC", "h_msg_cd": "IRR000014", "h_pnr_no": "queue-1"}
        followup = {"strResult": "SUCC", "h_msg_cd": "IRZ000003"}
        calls = []

        def response(payload, url):
            result = requests.Response()
            result.status_code = 200
            result.url = url
            result._content = json.dumps(payload).encode()
            result.encoding = "utf-8"
            return result

        def request(_session, method, url, **kwargs):
            calls.append((method, url, kwargs))
            if url.endswith(".certification.TicketReservation"):
                self.assertEqual("1102", kwargs["params"]["txtJobId"])
                self.assertEqual("N", kwargs["params"]["txtStndFlg"])
                return response(first, url)
            if url == korail._RESERVATION_WAIT:
                self.assertEqual("POST", method)
                self.assertEqual("Y", kwargs["data"]["txtPsrmClChgFlg"])
                self.assertNotIn("txtCpNo", kwargs["data"])
                return response(followup, url)
            if url.endswith(".reservation.ReservationView"):
                return response(history, url)
            raise AssertionError(f"unexpected offline request: {url}")

        adapter = KorailProvider()
        client = korail2.Korail("member", "password", auto_login=False, want_feedback=False)
        client._session = adapter._session
        adapter._sdk, adapter._client = korail2, client

        with patch.object(requests.Session, "request", request), patch(
            "korail_watch.korail.time.sleep"
        ):
            hold = adapter.reserve(adapter._train(raw), "waitlist", 1)

        self.assertEqual("queue-1", hold.reference)
        self.assertEqual("waitlist", hold.kind)
        self.assertEqual(4, len(calls))

    def test_schedule_capture_drives_pinned_sdk_standing_1101_wire(self):
        import korail2

        row = {
            "h_trn_clsf_cd": "100",
            "h_trn_clsf_nm": "KTX",
            "h_trn_no": "001",
            "h_trn_gp_cd": "100",
            "h_dpt_rs_stn_nm": "서울",
            "h_dpt_rs_stn_cd": "0001",
            "h_dpt_dt": "20260924",
            "h_dpt_tm": "120000",
            "h_arv_rs_stn_nm": "대전",
            "h_arv_rs_stn_cd": "0010",
            "h_arv_dt": "20260924",
            "h_arv_tm": "130000",
            "h_run_dt": "20260924",
            "h_rsv_psb_flg": "Y",
            "h_rsv_psb_nm": "입석 가능",
            "h_gen_rsv_cd": "13",
            "h_spe_rsv_cd": "13",
            "h_stnd_rsv_cd": "11",
            "h_wait_rsv_flg": " 0",
        }
        history_row = {
            "h_pnr_no": "standing-1",
            "h_rsv_tp_cd": "3",
            "h_tot_seat_cnt": "0",
            "h_tot_stnd_cnt": "1",
            "h_run_dt": "20260924",
            "h_ntisu_lmt_dt": "20260924",
            "h_ntisu_lmt_tm": "143000",
            "h_rsv_amt": "19800",
            "h_trn_clsf_cd": "100",
            "h_trn_no": "001",
            "h_trn_gp_cd": "100",
            "h_dpt_rs_stn_nm": "서울",
            "h_dpt_tm": "120000",
            "h_arv_rs_stn_nm": "대전",
            "h_arv_tm": "130000",
        }
        calls = []

        def response(payload, url):
            result = requests.Response()
            result.status_code = 200
            result.url = url
            result._content = json.dumps(payload).encode()
            result.encoding = "utf-8"
            return result

        def request(_session, method, url, **kwargs):
            calls.append((method, url, kwargs))
            if url.endswith(".seatMovie.ScheduleView"):
                return response({"strResult": "SUCC", "trn_infos": {"trn_info": [row]}}, url)
            if url.endswith(".certification.TicketReservation"):
                self.assertEqual("1101", kwargs["params"]["txtJobId"])
                self.assertEqual("Y", kwargs["params"]["txtStndFlg"])
                return response({"strResult": "SUCC", "h_pnr_no": "standing-1"}, url)
            if url.endswith(".reservation.ReservationView"):
                return response(
                    {
                        "strResult": "SUCC",
                        "jrny_infos": {
                            "jrny_info": [{"train_infos": {"train_info": [history_row]}}]
                        },
                    },
                    url,
                )
            raise AssertionError(f"unexpected offline request: {url}")

        adapter = KorailProvider()
        client = korail2.Korail("member", "password", auto_login=False, want_feedback=False)
        client._session = adapter._session
        adapter._sdk, adapter._client = korail2, client
        trip = Trip("2026-09-24", "12:00", "18:00", ("서울",), ("대전",))

        with patch.object(requests.Session, "request", request), patch(
            "korail_watch.korail.time.sleep"
        ):
            train = adapter.search(trip, "서울", "대전", "12:00:00")[0]
            self.assertTrue(train.standing)
            self.assertFalse(train.general)
            self.assertFalse(hasattr(train.raw, "standing_reservation_code"))
            hold = adapter.reserve(train, "standing", 1)

        self.assertEqual("standing", hold.kind)
        self.assertEqual("standing-1", hold.reference)
        self.assertEqual(3, len(calls))

    def test_paid_zero_seats_is_not_assumed_to_be_one_standing_adult(self):
        raw = RawTicket()
        raw.seat_no_count = 0
        client = FakeClient()
        client.tickets = lambda: [raw]
        with self.assertRaises(AmbiguousReservation):
            provider(client).tickets()

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
        body = io.BytesIO(b'{"parameters":{"retry_after":17}}')
        error = urllib.error.HTTPError(
            f"https://api.telegram.org/bot{token}/sendMessage", 429, token, {"Retry-After": "17"}, body
        )
        with patch.dict(os.environ, self.env, clear=True), patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(TransientError) as raised:
                TelegramNotifier().send("test")
        self.assertEqual(17.0, raised.exception.retry_after)
        self.assertNotIn(token, str(raised.exception))
        self.assertTrue(body.closed)


if __name__ == "__main__":
    unittest.main()
