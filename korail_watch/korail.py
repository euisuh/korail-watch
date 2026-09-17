from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import random
import threading
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

import requests

from . import diagnostics
from .domain import (
    AmbiguousReservation,
    BlockedError,
    Hold,
    ReservationNotSent,
    SoldOut,
    Train,
    TransientError,
)


_PACE_LOCK = threading.Lock()
_NEXT_REQUEST = 0.0
_KST = ZoneInfo("Asia/Seoul")
_RESERVATION_WAIT = (
    "https://smart.letskorail.com:443/classes/"
    "com.korail.mobile.reservationWait.ReservationWait"
)
# MyTicketList's observed empty code plus the pinned SDK's generic no-result
# codes. Its train-search-only WRD000061 code is deliberately excluded.
_EMPTY_TICKET_CODES = frozenset(("WRT300005", "P100", "WRG000000"))


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            return max(0.0, (parsedate_to_datetime(value) - datetime.now().astimezone()).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


class _PacedSession(requests.Session):
    def __init__(self, interval: float, timeout: float):
        super().__init__()
        self.interval = max(5.0, float(interval))
        self.timeout = float(timeout)
        self.reservation_status: dict[
            str, tuple[str | None, int | None, int | None] | None
        ] = {}
        self.search_status: dict[str, tuple[str | None, str | None]] = {}
        self.ticket_status: dict[str, tuple[str, str]] = {}
        self.ticket_complete = False
        self.reservation_overrides: dict[str, str] = {}
        self.last_reservation_response: dict | None = None
        self.reservation_active = False
        self.last_operation = "provider"
        self.last_stage = "request"

    @contextlib.contextmanager
    def reservation_attempt(self):
        self.reservation_active = True
        self.last_operation, self.last_stage = "reserve", "preflight"
        try:
            yield
        finally:
            self.reservation_active = False
            self.reservation_overrides = {}

    def request(self, method, url, **kwargs):
        global _NEXT_REQUEST
        endpoint = url.rstrip("/")
        operation, stage = _request_context(endpoint, self.reservation_active)
        self.last_operation, self.last_stage = operation, stage
        if (
            endpoint.endswith(".certification.TicketReservation")
            or endpoint == _RESERVATION_WAIT
        ):
            # A redirect could follow a completed write and turn a later connect
            # timeout into a false "not sent" signal. Never follow one here.
            kwargs["allow_redirects"] = False
            if endpoint.endswith(".certification.TicketReservation") and self.reservation_overrides:
                key = "params" if "params" in kwargs else "data"
                kwargs[key] = {**(kwargs.get(key) or {}), **self.reservation_overrides}
        with _PACE_LOCK:
            delay = _NEXT_REQUEST - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            # ponytail: process-global pacing; the engine's file lock prevents
            # multiple local processes, so cross-process coordination adds no value.
            _NEXT_REQUEST = time.monotonic() + self.interval + random.uniform(0.0, 0.5)
        kwargs.setdefault("timeout", self.timeout)
        try:
            response = super().request(method, url, **kwargs)
        except Exception as exc:
            if operation == "reserve" and stage == "initial" and isinstance(
                exc, requests.ConnectTimeout
            ):
                self._diagnose(operation, stage, "not_sent", exc=exc)
                raise ReservationNotSent() from None
            outcome = "ambiguous" if operation == "reserve" else "transient"
            self._diagnose(operation, stage, outcome, exc=exc)
            raise
        if operation == "reserve" and 300 <= response.status_code < 400:
            exc = requests.HTTPError(response=response)
            self._diagnose(operation, stage, "ambiguous", response=response, exc=exc)
            raise exc
        if response.status_code == 429:
            retry = _retry_after(response.headers.get("Retry-After"))
            if retry is not None:
                with _PACE_LOCK:
                    _NEXT_REQUEST = max(_NEXT_REQUEST, time.monotonic() + retry)
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            outcome = (
                "blocked"
                if response.status_code in (401, 403)
                else "ambiguous" if operation == "reserve" else "transient"
            )
            self._diagnose(operation, stage, outcome, response=response, exc=exc)
            raise
        self._diagnose(operation, stage, "received", response=response)
        if endpoint.endswith(".seatMovie.ScheduleView"):
            self._capture_search_status(response)
        elif endpoint.endswith(".certification.TicketReservation"):
            try:
                payload = response.json()
            except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                payload = None
            self.last_reservation_response = payload if isinstance(payload, dict) else None
        elif endpoint.endswith(".reservation.ReservationView"):
            self._capture_reservation_status(response)
        elif endpoint.endswith(".myTicket.MyTicketList"):
            self._capture_ticket_status(response)
        return response

    def _diagnose(self, operation, stage, outcome, *, response=None, exc=None) -> None:
        code = None
        if response is not None:
            try:
                payload = response.json()
                code = payload.get("h_msg_cd") if isinstance(payload, dict) else None
            except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                pass
        diagnostics.event(
            "provider",
            operation=operation,
            stage=stage,
            outcome=outcome,
            http_status=getattr(response, "status_code", None),
            provider_code=_safe_provider_code(code),
            error_type=_safe_error_type(type(exc).__name__) if exc is not None else None,
        )

    def _capture_search_status(self, response) -> None:
        snapshot: dict[str, tuple[str | None, str | None]] = {}
        try:
            trains = response.json().get("trn_infos", {}).get("trn_info", [])
            for train in trains:
                snapshot[_record_key(train)] = (
                    train.get("h_gen_rsv_cd"),
                    train.get("h_stnd_rsv_cd"),
                )
        except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            pass
        self.search_status = snapshot

    def _capture_reservation_status(self, response) -> None:
        snapshot: dict[str, tuple[str | None, int | None, int | None] | None] = {}
        try:
            journeys = response.json().get("jrny_infos", {}).get("jrny_info", [])
            for journey in journeys:
                for train in journey.get("train_infos", {}).get("train_info", []):
                    reference = str(train["h_pnr_no"])
                    value = (
                        train.get("h_rsv_tp_cd"),
                        _integer(train.get("h_tot_seat_cnt")),
                        _integer(train.get("h_tot_stnd_cnt")),
                    )
                    snapshot[reference] = value if reference not in snapshot or snapshot[reference] == value else None
        except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            pass
        self.reservation_status = snapshot

    def _capture_ticket_status(self, response) -> None:
        self.ticket_status = {}
        self.ticket_complete = False
        try:
            payload = response.json()
            if payload.get("h_msg_cd") in _EMPTY_TICKET_CODES:
                if (
                    payload.get("strResult") in ("SUCC", "FAIL")
                    and payload.get("tickets") in (None, [])
                    and payload.get("reservation_list") in (None, [])
                ):
                    self.ticket_complete = True
                return
            if payload.get("strResult") != "SUCC":
                return

            records = payload["reservation_list"]
            if not isinstance(records, list):
                return
            snapshot: dict[str, tuple[str, str]] = {}
            for record in records:
                ticket_list = record["ticket_list"]
                if not isinstance(ticket_list, list) or len(ticket_list) != 1:
                    return
                train_info = ticket_list[0]["train_info"]
                if not isinstance(train_info, list) or len(train_info) != 1:
                    return
                row = train_info[0]
                if not isinstance(row, dict) or row.get("h_psg_tp_cd") != "1":
                    return
                if _integer(row.get("h_seat_cnt")) != 1:
                    return
                pnr = _required(row, "h_pnr_no")
                reference = "-".join(
                    _required(row, name)
                    for name in (
                        "h_orgtk_wct_no",
                        "h_orgtk_ret_sale_dt",
                        "h_orgtk_sale_sqno",
                        "h_orgtk_ret_pwd",
                    )
                )
                record_key = _record_key(row)
                if not all(record_key.split("|")) or reference in snapshot:
                    return
                snapshot[reference] = (pnr, record_key)
        except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return
        self.ticket_status = snapshot
        self.ticket_complete = True


class KorailProvider:
    supported_modes = frozenset(("waitlist", "standing"))

    def __init__(self, interval: float = 5.0, timeout: float = 15.0):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.interval = max(5.0, float(interval))
        self.timeout = float(timeout)
        self._client = None
        self._sdk = None
        self._session = _PacedSession(self.interval, self.timeout)

    def login(self) -> None:
        member = os.environ.get("KORAIL_ID", "")
        password = os.environ.get("KORAIL_PASSWORD", "")
        if not member or not password or any(c in member + password for c in "\r\n\0"):
            raise BlockedError("Korail credentials are missing or invalid")

        self._sdk = importlib.import_module("korail2")
        implementation = importlib.import_module("korail2.korail2")
        client = self._sdk.Korail(member, password, auto_login=False, want_feedback=False)
        client._session = self._session
        client._session.headers.update({"User-Agent": implementation.DEFAULT_USER_AGENT})
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                authenticated = client.login()
        except Exception as exc:
            self._raise(exc, operation="login")
        if not authenticated:
            raise BlockedError("Korail authentication was rejected")
        self._client = client

    def search(self, trip, departure: str, arrival: str, after: str) -> list[Train]:
        def read(client, sdk):
            self._session.search_status = {}
            try:
                raw_trains = client.search_train(
                    departure,
                    arrival,
                    trip.date.replace("-", ""),
                    after.replace(":", ""),
                    sdk.TrainType.ALL,
                    [sdk.AdultPassenger(trip.adults)],
                    include_no_seats=True,
                )
            except Exception as exc:
                if isinstance(exc, sdk.NoResultsError):
                    return []
                raise
            return [self._train(train) for train in raw_trains]

        try:
            return self._read("search", read)
        except Exception as exc:
            self._raise(exc, operation="search")

    def reserve(self, train: Train, seat_class: str, adults: int) -> Hold:
        client, sdk = self._ready()
        options = {
            "general": sdk.ReserveOption.GENERAL_ONLY,
            "special": sdk.ReserveOption.SPECIAL_ONLY,
            "waitlist": sdk.ReserveOption.GENERAL_ONLY,
            "standing": sdk.ReserveOption.GENERAL_ONLY,
        }
        if seat_class not in options:
            raise ValueError("unsupported reservation kind")
        if adults != 1:
            raise ValueError("exactly one adult is supported")
        if train.raw is None:
            raise ValueError("train does not contain its provider record")
        if seat_class == "waitlist" and (
            not train.waitlist
            or not bool(getattr(train.raw, "has_general_waiting_list", lambda: False)())
        ):
            raise SoldOut("this train is not waitlist eligible")
        if seat_class == "standing" and (
            not train.standing
            or self._session.search_status.get(_raw_key(train.raw)) != ("13", "11")
        ):
            raise SoldOut("this train has no supported standing inventory")

        with self._session.reservation_attempt():
            self._session.last_reservation_response = None
            overrides = {}
            raw = train.raw
            try_waiting = seat_class == "waitlist"
            if seat_class == "waitlist":
                # The pinned SDK selects 1101 from stale seat availability before
                # consulting try_waiting. The app instead keys queue intent on the
                # exact h_wait_rsv_flg=9 search flag, so pin the evidenced job id.
                overrides = {
                    "txtJobId": "1102",
                    "txtStndFlg": "Y" if train.standing else "N",
                }
            elif seat_class == "standing":
                overrides = {"txtStndFlg": "Y"}
                raw = _StandingPreflightTrain(train.raw)
            self._session.reservation_overrides = overrides
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    reservation = client.reserve(
                        raw,
                        [sdk.AdultPassenger(adults)],
                        option=options[seat_class],
                        try_waiting=try_waiting,
                    )
            except Exception as exc:
                if seat_class != "waitlist" or self._standby_reference() is None:
                    self._raise(exc, mutation=True, operation="reserve")
                try:
                    self._raise(exc, mutation=True, operation="reserve")
                except BlockedError:
                    raise
                except Exception:
                    pass
                reservation = None
            finally:
                self._session.reservation_overrides = {}

            if seat_class == "waitlist":
                return self._confirm_waitlist(train)
            if reservation is None:
                raise AmbiguousReservation("Korail did not return the created reservation")
            try:
                hold = self._hold(reservation, paid=False)
                accepted = {"standing", "seated"} if seat_class == "standing" else {"seated"}
                if hold.kind not in accepted:
                    raise ValueError
                return hold
            except Exception as exc:
                if isinstance(exc, BlockedError):
                    raise
                diagnostics.event(
                    "provider",
                    operation="reserve",
                    stage="readback",
                    outcome="ambiguous",
                    error_type=_safe_error_type(type(exc).__name__),
                )
                raise AmbiguousReservation("Korail reservation outcome is unknown") from None

    def reservations(self) -> list[Hold]:
        def read(client, _sdk):
            self._session.reservation_status = {}
            return [self._hold(item, paid=False) for item in client.reservations()]

        try:
            return self._read("reservations", read)
        except Exception as exc:
            self._raise(exc, operation="reservations")

    def tickets(self) -> list[Hold]:
        def read(client, _sdk):
            self._session.ticket_status = {}
            self._session.ticket_complete = False
            try:
                raw_tickets = client.tickets()
            except Exception:
                if self._session.ticket_complete and not self._session.ticket_status:
                    return []
                raise
            if not self._session.ticket_complete or not isinstance(raw_tickets, list):
                raise AmbiguousReservation("Korail ticket list is incomplete")
            holds = [self._hold(item, paid=True) for item in raw_tickets]
            if len(holds) != len(self._session.ticket_status):
                raise AmbiguousReservation("Korail ticket list is incomplete")
            return holds

        try:
            return self._read("tickets", read)
        except Exception as exc:
            self._raise(exc, operation="tickets")

    def _read(self, operation: str, read):
        renewed = False
        while True:
            client, sdk = self._ready()
            try:
                return read(client, sdk)
            except Exception as exc:
                if renewed or not self._expired_read_session(exc, sdk):
                    raise
                renewed = True
                self._renew_read_session(operation, exc)

    def _expired_read_session(self, exc: Exception, sdk) -> bool:
        return bool(
            not self._session.reservation_active
            and isinstance(exc, sdk.NeedToLoginError)
            and getattr(exc, "code", None) == "P058"
        )

    def _renew_read_session(self, operation: str, expired: Exception) -> None:
        stage = (
            self._session.last_stage
            if self._session.last_operation == operation
            else "request"
        )
        diagnostics.event(
            "session.renewal",
            operation=operation,
            stage=stage,
            outcome="starting",
            provider_code="P058",
            error_type=_safe_error_type(type(expired).__name__),
        )
        try:
            self.login()
        except Exception as exc:
            outcome = (
                "transient"
                if isinstance(exc, TransientError)
                else "blocked" if isinstance(exc, BlockedError) else "error"
            )
            diagnostics.event(
                "session.renewal",
                operation=operation,
                stage=stage,
                outcome=outcome,
                provider_code="P058",
                error_type=_safe_error_type(type(exc).__name__),
            )
            raise
        diagnostics.event(
            "session.renewal",
            operation=operation,
            stage=stage,
            outcome="success",
            provider_code="P058",
        )

    def _ready(self):
        if self._client is None or self._sdk is None:
            raise BlockedError("Korail login is required")
        return self._client, self._sdk

    @staticmethod
    def _raw_train(raw, *, standing: bool = False, waitlist: bool = False) -> Train:
        date = _date(raw.dep_date)
        dep_time = _clock(raw.dep_time)
        arr_time = _clock(raw.arr_time)
        key = "|".join(
            (date, str(raw.train_type), str(raw.train_no), raw.dep_name, dep_time, raw.arr_name, arr_time)
        )
        return Train(
            key=key,
            date=date,
            departure=raw.dep_name,
            arrival=raw.arr_name,
            dep_time=dep_time,
            arr_time=arr_time,
            general=bool(raw.has_general_seat()),
            special=bool(raw.has_special_seat()),
            raw=raw,
            waitlist=waitlist,
            standing=standing,
        )

    def _train(self, raw) -> Train:
        general, standing = self._session.search_status.get(
            _raw_key(raw),
            (getattr(raw, "general_seat", None), None),
        )
        return self._raw_train(
            raw,
            waitlist=bool(getattr(raw, "has_general_waiting_list", lambda: False)()),
            standing=general == "13" and standing == "11",
        )

    def _hold(self, raw, *, paid: bool) -> Hold:
        if paid:
            reference = raw.get_ticket_no()
            deadline = None
            seats = _integer(getattr(raw, "seat_no_count", None))
            status = self._session.ticket_status.get(str(reference))
            if seats == 1 and status is not None and status[1] == _raw_key(raw):
                kind = "seated"
            else:
                raise AmbiguousReservation("Korail ticket seating status is unavailable")
        else:
            reference = raw.rsv_id
            status = self._session.reservation_status.get(str(reference))
            if status is None:
                status = (
                    getattr(raw, "reservation_type_code", None),
                    getattr(raw, "seat_no_count", None),
                    getattr(raw, "standing_no_count", None),
                )
            code = str(status[0]).lstrip("0")
            seats, standing = status[1:]
            if code == "8" and seats == 1 and standing == 0:
                kind = "waitlist"
                deadline = None
            elif code == "3" and seats == 1 and standing == 0:
                kind = "seated"
                deadline = _deadline(raw.buy_limit_date, raw.buy_limit_time)
            elif code == "3" and seats == 0 and standing == 1:
                kind = "standing"
                deadline = _deadline(raw.buy_limit_date, raw.buy_limit_time)
            else:
                raise AmbiguousReservation("Korail reservation seating status is unavailable")
        if reference in (None, ""):
            raise ValueError("provider record has no reference")
        return Hold(
            reference=str(reference),
            train=self._train(raw),
            deadline=deadline,
            price=_integer(getattr(raw, "price", None)),
            paid=paid,
            kind=kind,
        )

    def _standby_reference(self) -> str | None:
        payload = self._session.last_reservation_response
        if not isinstance(payload, dict):
            return None
        if payload.get("strResult") != "SUCC" or payload.get("h_msg_cd") != "IRR000014":
            return None
        reference = payload.get("h_pnr_no")
        return str(reference) if reference not in (None, "") else None

    def _confirm_waitlist(self, train: Train) -> Hold:
        client, _ = self._ready()
        reference = self._standby_reference()
        if reference is None:
            raise AmbiguousReservation("Korail waitlist outcome is unknown")
        data = {
            "Device": client._device,
            "Version": client._version,
            "Key": client._key,
            "txtPnrNo": reference,
            "txtPsrmClChgFlg": "Y",
            "txtSmsSndFlg": "N",
        }
        try:
            response = self._session.post(_RESERVATION_WAIT, data=data)
            payload = response.json()
            with contextlib.redirect_stdout(io.StringIO()):
                client._result_check(payload)
            if payload.get("strResult") != "SUCC" or payload.get("h_msg_cd") != "IRZ000003":
                raise ValueError
            matches = [item for item in self.reservations() if item.reference == reference]
        except Exception as exc:
            try:
                self._raise(exc, mutation=True, operation="reserve")
            except BlockedError:
                raise
            except Exception:
                raise AmbiguousReservation("Korail waitlist outcome is unknown") from None
        if len(matches) != 1 or matches[0].kind != "waitlist" or matches[0].train.key != train.key:
            raise AmbiguousReservation("Korail waitlist outcome is unknown")
        return matches[0]

    def _raise(self, exc: Exception, *, mutation: bool = False, operation: str = "provider"):
        stage = (
            self._session.last_stage
            if self._session.last_operation == operation
            else "request"
        )
        accepted = self._initial_reservation_possible()

        if isinstance(exc, ReservationNotSent):
            if mutation and stage == "initial" and not accepted:
                raise exc
            self._diagnose_error(operation, stage, "ambiguous", exc)
            raise AmbiguousReservation("Korail reservation outcome is unknown") from None
        if isinstance(exc, TransientError):
            if mutation:
                self._diagnose_error(operation, stage, "ambiguous", exc)
                raise AmbiguousReservation("Korail reservation outcome is unknown") from None
            raise exc
        if isinstance(exc, SoldOut):
            if mutation and (stage != "preflight" or accepted):
                self._diagnose_error(operation, stage, "ambiguous", exc)
                raise AmbiguousReservation("Korail reservation outcome is unknown") from None
            self._diagnose_error(operation, stage, "sold_out", exc)
            raise exc
        if isinstance(exc, (BlockedError, AmbiguousReservation)):
            raise exc
        sdk = self._sdk
        if sdk is not None and isinstance(exc, sdk.SoldOutError):
            safe = stage == "preflight" or (
                stage == "initial" and self._initial_sold_out_response()
            )
            if mutation and (not safe or accepted):
                self._diagnose_error(operation, stage, "ambiguous", exc)
                raise AmbiguousReservation("Korail reservation outcome is unknown") from None
            self._diagnose_error(operation, stage, "sold_out", exc)
            raise SoldOut from None
        if sdk is not None and isinstance(exc, sdk.NeedToLoginError):
            self._diagnose_error(operation, stage, "blocked", exc)
            raise BlockedError("Korail authentication is required") from None

        if isinstance(exc, requests.HTTPError):
            response = exc.response
            status = response.status_code if response is not None else None
            if status in (401, 403):
                self._diagnose_error(operation, stage, "blocked", exc)
                raise BlockedError("Korail rejected the authenticated session") from None
            retry = _retry_after(response.headers.get("Retry-After")) if response is not None else None
            if mutation:
                self._diagnose_error(operation, stage, "ambiguous", exc)
                raise AmbiguousReservation("Korail reservation outcome is unknown") from None
            self._diagnose_error(operation, stage, "transient", exc)
            raise TransientError(retry_after=retry) from None
        if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
            if mutation:
                self._diagnose_error(operation, stage, "ambiguous", exc)
                raise AmbiguousReservation("Korail reservation outcome is unknown") from None
            self._diagnose_error(operation, stage, "transient", exc)
            raise TransientError() from None

        if sdk is not None and isinstance(exc, sdk.KorailError):
            signal = f"{getattr(exc, 'code', '')} {getattr(exc, 'msg', '')}".lower()
            if any(word in signal for word in ("login", "auth", "security", "blocked", "차단", "로그인", "인증")):
                self._diagnose_error(operation, stage, "blocked", exc)
                raise BlockedError("Korail reported an authentication or security block") from None
        if mutation:
            self._diagnose_error(operation, stage, "ambiguous", exc)
            raise AmbiguousReservation("Korail reservation outcome is unknown") from None
        self._diagnose_error(operation, stage, "transient", exc)
        raise TransientError() from None

    def _initial_reservation_possible(self) -> bool:
        payload = self._session.last_reservation_response
        return bool(
            isinstance(payload, dict)
            and (
                payload.get("strResult") == "SUCC"
                or payload.get("h_pnr_no") not in (None, "")
            )
        )

    def _initial_sold_out_response(self) -> bool:
        payload = self._session.last_reservation_response
        return bool(
            isinstance(payload, dict)
            and payload.get("strResult") == "FAIL"
            and payload.get("h_msg_cd") == "ERR211161"
            and payload.get("h_pnr_no") in (None, "")
        )

    def _diagnose_error(self, operation: str, stage: str, outcome: str, exc: Exception) -> None:
        response = exc.response if isinstance(exc, requests.HTTPError) else None
        code = getattr(exc, "code", None)
        if code is None and response is not None:
            try:
                payload = response.json()
                code = payload.get("h_msg_cd") if isinstance(payload, dict) else None
            except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                pass
        diagnostics.event(
            "provider",
            operation=operation,
            stage=stage,
            outcome=outcome,
            http_status=getattr(response, "status_code", None),
            provider_code=_safe_provider_code(code),
            error_type=_safe_error_type(type(exc).__name__),
        )


def _request_context(endpoint: str, reservation_active: bool) -> tuple[str, str]:
    if endpoint.endswith(".login.Login"):
        return "login", "request"
    if endpoint.endswith(".seatMovie.ScheduleView"):
        return "search", "request"
    if endpoint.endswith(".certification.TicketReservation"):
        return "reserve", "initial"
    if endpoint == _RESERVATION_WAIT:
        return "reserve", "followup"
    if endpoint.endswith(".reservation.ReservationView"):
        return ("reserve", "readback") if reservation_active else ("reservations", "request")
    if endpoint.endswith(".myTicket.MyTicketList"):
        return "tickets", "list"
    if endpoint.endswith(".refunds.SelTicketInfo"):
        return "tickets", "detail"
    return "provider", "request"


def _safe_provider_code(value) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= 16 or not value.isascii():
        return None
    return value if "A" <= value[0] <= "Z" and value.isalnum() else None


def _safe_error_type(value: str) -> str | None:
    if not 1 <= len(value) <= 64 or not value.isascii() or not value[0].isalpha():
        return None
    return value if all(character.isalnum() or character == "_" for character in value) else None


def _date(value: str) -> str:
    return datetime.strptime(value, "%Y%m%d").date().isoformat()


def _clock(value: str) -> str:
    return datetime.strptime(value, "%H%M%S").time().strftime("%H:%M")


def _deadline(date: str | None, clock: str | None) -> str | None:
    if not date or not clock:
        return None
    return datetime.strptime(date + clock, "%Y%m%d%H%M%S").replace(tzinfo=_KST).isoformat()


def _integer(value) -> int | None:
    return int(value) if value not in (None, "") else None


def _record_key(raw: dict) -> str:
    return "|".join(
        str(raw.get(name, ""))
        for name in (
            "h_dpt_dt",
            "h_trn_clsf_cd",
            "h_trn_no",
            "h_dpt_rs_stn_nm",
            "h_dpt_tm",
            "h_arv_rs_stn_nm",
            "h_arv_tm",
        )
    )


def _required(raw: dict, name: str) -> str:
    value = raw.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError
    return value


def _raw_key(raw) -> str:
    return "|".join(
        str(value)
        for value in (
            raw.dep_date,
            raw.train_type,
            raw.train_no,
            raw.dep_name,
            raw.dep_time,
            raw.arr_name,
            raw.arr_time,
        )
    )


class _StandingPreflightTrain:
    """Pass one proven 13/11 row through korail2's seat-only preflight."""

    def __init__(self, raw):
        self._raw = raw

    def __getattr__(self, name):
        return getattr(self._raw, name)

    def has_seat(self):
        return True

    def has_general_seat(self):
        return True
