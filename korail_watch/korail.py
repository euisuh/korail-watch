from __future__ import annotations

import contextlib
import importlib
import io
import os
import random
import threading
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

import requests

from .domain import AmbiguousReservation, BlockedError, Hold, SoldOut, Train, TransientError


_PACE_LOCK = threading.Lock()
_NEXT_REQUEST = 0.0
_KST = ZoneInfo("Asia/Seoul")


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

    def request(self, method, url, **kwargs):
        global _NEXT_REQUEST
        with _PACE_LOCK:
            delay = _NEXT_REQUEST - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            # ponytail: process-global pacing; the engine's file lock prevents
            # multiple local processes, so cross-process coordination adds no value.
            _NEXT_REQUEST = time.monotonic() + self.interval + random.uniform(0.0, 0.5)
        kwargs.setdefault("timeout", self.timeout)
        response = super().request(method, url, **kwargs)
        if response.status_code == 429:
            retry = _retry_after(response.headers.get("Retry-After"))
            if retry is not None:
                with _PACE_LOCK:
                    _NEXT_REQUEST = max(_NEXT_REQUEST, time.monotonic() + retry)
        response.raise_for_status()
        return response


class KorailProvider:
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
            self._raise(exc)
        if not authenticated:
            raise BlockedError("Korail authentication was rejected")
        self._client = client

    def search(self, trip, departure: str, arrival: str, after: str) -> list[Train]:
        client, sdk = self._ready()
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
            self._raise(exc)
        try:
            return [self._train(train) for train in raw_trains]
        except Exception as exc:
            self._raise(exc)

    def reserve(self, train: Train, seat_class: str, adults: int) -> Hold:
        client, sdk = self._ready()
        options = {
            "general": sdk.ReserveOption.GENERAL_ONLY,
            "special": sdk.ReserveOption.SPECIAL_ONLY,
        }
        if seat_class not in options:
            raise ValueError("seat_class must be general or special")
        if adults < 1:
            raise ValueError("adults must be positive")
        if train.raw is None:
            raise ValueError("train does not contain its provider record")

        try:
            with contextlib.redirect_stdout(io.StringIO()):
                reservation = client.reserve(
                    train.raw,
                    [sdk.AdultPassenger(adults)],
                    option=options[seat_class],
                    try_waiting=False,
                )
        except Exception as exc:
            self._raise(exc, mutation=True)
        if reservation is None:
            raise AmbiguousReservation("Korail did not return the created reservation")
        try:
            return self._hold(reservation, paid=False)
        except Exception:
            raise AmbiguousReservation("Korail reservation outcome is unknown") from None

    def reservations(self) -> list[Hold]:
        client, _ = self._ready()
        try:
            return [self._hold(item, paid=False) for item in client.reservations()]
        except Exception as exc:
            self._raise(exc)

    def tickets(self) -> list[Hold]:
        client, _ = self._ready()
        try:
            return [self._hold(item, paid=True) for item in client.tickets()]
        except Exception as exc:
            self._raise(exc)

    def _ready(self):
        if self._client is None or self._sdk is None:
            raise BlockedError("Korail login is required")
        return self._client, self._sdk

    @staticmethod
    def _train(raw) -> Train:
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
        )

    def _hold(self, raw, *, paid: bool) -> Hold:
        if paid:
            reference = raw.get_ticket_no()
            deadline = None
        else:
            reference = raw.rsv_id
            deadline = _deadline(raw.buy_limit_date, raw.buy_limit_time)
        if reference in (None, ""):
            raise ValueError("provider record has no reference")
        return Hold(
            reference=str(reference),
            train=self._train(raw),
            deadline=deadline,
            price=_integer(getattr(raw, "price", None)),
            paid=paid,
        )

    def _raise(self, exc: Exception, *, mutation: bool = False):
        sdk = self._sdk
        if sdk is not None and isinstance(exc, sdk.SoldOutError):
            raise SoldOut from None
        if sdk is not None and isinstance(exc, sdk.NeedToLoginError):
            raise BlockedError("Korail authentication is required") from None

        if isinstance(exc, requests.HTTPError):
            response = exc.response
            status = response.status_code if response is not None else None
            if status in (401, 403):
                raise BlockedError("Korail rejected the authenticated session") from None
            retry = _retry_after(response.headers.get("Retry-After")) if response is not None else None
            if mutation:
                raise AmbiguousReservation("Korail reservation outcome is unknown") from None
            raise TransientError(retry_after=retry) from None
        if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
            if mutation:
                raise AmbiguousReservation("Korail reservation outcome is unknown") from None
            raise TransientError() from None

        if sdk is not None and isinstance(exc, sdk.KorailError):
            signal = f"{getattr(exc, 'code', '')} {getattr(exc, 'msg', '')}".lower()
            if any(word in signal for word in ("login", "auth", "security", "blocked", "차단", "로그인", "인증")):
                raise BlockedError("Korail reported an authentication or security block") from None
        if mutation:
            raise AmbiguousReservation("Korail reservation outcome is unknown") from None
        raise TransientError() from None


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
