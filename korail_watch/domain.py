"""Small, provider-independent types for a single Korail trip."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone


KST = timezone(timedelta(hours=9), "KST")


def _date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("date must be YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError("date must be YYYY-MM-DD")
    return parsed


def _time(value: str) -> time:
    try:
        parsed = time.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("time must be HH:MM") from exc
    if len(value) != 5 or parsed.second or parsed.microsecond:
        raise ValueError("time must be HH:MM")
    return parsed


@dataclass(frozen=True, slots=True)
class Train:
    key: str
    date: str
    departure: str
    arrival: str
    dep_time: str
    arr_time: str
    general: bool
    special: bool
    raw: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not all((self.key, self.departure, self.arrival)):
            raise ValueError("train key and stations are required")
        _date(self.date)
        _time(self.dep_time)
        _time(self.arr_time)
        if type(self.general) is not bool or type(self.special) is not bool:
            raise ValueError("seat availability must be boolean")


@dataclass(frozen=True, slots=True)
class Trip:
    date: str
    start: str
    end: str
    departures: tuple[str, ...]
    arrivals: tuple[str, ...]
    adults: int = 1

    def __post_init__(self) -> None:
        _date(self.date)
        start, end = _time(self.start), _time(self.end)
        if start > end:
            raise ValueError("start must not be after end")
        if not isinstance(self.departures, tuple) or not self.departures:
            raise ValueError("departures must be a non-empty tuple")
        if not isinstance(self.arrivals, tuple) or not self.arrivals:
            raise ValueError("arrivals must be a non-empty tuple")
        if any(not isinstance(station, str) or not station.strip() for station in self.departures + self.arrivals):
            raise ValueError("stations must be non-empty strings")
        if len(set(self.departures)) != len(self.departures) or len(set(self.arrivals)) != len(self.arrivals):
            raise ValueError("stations must not be duplicated")
        if set(self.departures) & set(self.arrivals):
            raise ValueError("departure and arrival stations must differ")
        if type(self.adults) is not int or self.adults != 1:
            raise ValueError("exactly one adult is supported")

    def matches(self, train: Train, *, now: datetime | None = None) -> bool:
        if not isinstance(train, Train):
            return False
        departure = datetime.combine(_date(train.date), _time(train.dep_time), KST)
        current = now.astimezone(KST) if now else datetime.now(KST)
        return (
            train.date == self.date
            and train.departure in self.departures
            and train.arrival in self.arrivals
            and self.start <= train.dep_time <= self.end
            and departure > current
        )


@dataclass(frozen=True, slots=True)
class Hold:
    reference: str
    train: Train
    deadline: str | None
    price: int | None
    paid: bool = False

    def __post_init__(self) -> None:
        if not self.reference:
            raise ValueError("hold reference is required")
        if not isinstance(self.train, Train):
            raise ValueError("hold train is required")
        if self.deadline is not None:
            try:
                parsed = datetime.fromisoformat(self.deadline)
            except (TypeError, ValueError) as exc:
                raise ValueError("deadline must be an aware ISO datetime") from exc
            if parsed.utcoffset() != timedelta(hours=9):
                raise ValueError("deadline must use Asia/Seoul offset")
        if self.price is not None and (type(self.price) is not int or self.price < 0):
            raise ValueError("price must be a non-negative integer")
        if type(self.paid) is not bool:
            raise ValueError("paid must be boolean")


class SoldOut(Exception):
    """The requested seat class is definitively unavailable."""


class TransientError(Exception):
    """A retryable read or notification failure."""

    def __init__(self, message: str = "transient failure", retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class BlockedError(Exception):
    """Authentication, queue, or security policy stopped the operation."""


class AmbiguousReservation(Exception):
    """A reservation write may have reached the provider."""
