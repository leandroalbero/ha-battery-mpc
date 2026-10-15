"""2.0TD period and DST step tests. Loads tariff.py without the HA package."""

from __future__ import annotations

import importlib.util
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest

MADRID = ZoneInfo("Europe/Madrid")

TARIFF = {
    "valley": {"hours": [0, 8], "price": 0.1228},
    "shoulder_morning": {"hours": [8, 10], "price": 0.15},
    "peak_morning": {"hours": [10, 14], "price": 0.20},
    "shoulder_afternoon": {"hours": [14, 18], "price": 0.16},
    "peak_evening": {"hours": [18, 22], "price": 0.21},
    "shoulder_night": {"hours": [22, 24], "price": 0.17},
}


def _load():
    path = Path(__file__).resolve().parents[1] / "custom_components" / "battery_mpc" / "tariff.py"
    spec = importlib.util.spec_from_file_location("battery_mpc_tariff", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tariff = _load()


def _at(year: int, month: int, day: int, hour: int) -> datetime:
    return datetime(year, month, day, hour, tzinfo=MADRID)


@pytest.mark.parametrize(
    ("when", "slot"),
    [
        (_at(2026, 5, 1, 11), "valley"),
        (_at(2026, 10, 12, 19), "valley"),
        (_at(2026, 12, 8, 12), "valley"),
        (_at(2026, 12, 7, 12), "peak_morning"),
        (_at(2026, 10, 14, 9), "shoulder_morning"),
        (_at(2026, 10, 14, 11), "peak_morning"),
        (_at(2026, 10, 14, 19), "peak_evening"),
        (_at(2026, 10, 14, 23), "shoulder_night"),
        (_at(2026, 10, 14, 3), "valley"),
        (_at(2026, 10, 17, 12), "valley"),
        (_at(2027, 1, 6, 12), "valley"),
    ],
)
def test_rate_at_holiday_weekend_and_weekday_slots(when: datetime, slot: str) -> None:
    assert tariff.rate_at(when, TARIFF) == TARIFF[slot]["price"]


@pytest.mark.parametrize(
    ("hour", "slot"),
    [
        (0, "valley"),
        (7, "valley"),
        (8, "shoulder_morning"),
        (10, "peak_morning"),
        (14, "shoulder_afternoon"),
        (18, "peak_evening"),
        (22, "shoulder_night"),
    ],
)
def test_rate_at_half_open_slot_edges(hour: int, slot: str) -> None:
    assert tariff.rate_at(_at(2026, 10, 14, hour), TARIFF) == TARIFF[slot]["price"]


def test_rate_at_accepts_tuple_hours() -> None:
    windows = {
        "valley": {"hours": (0, 8), "price": 0.1},
        "peak": {"hours": (8, 24), "price": 0.2},
    }
    assert tariff.rate_at(_at(2026, 10, 14, 9), windows) == 0.2


def test_good_friday_is_a_normal_weekday() -> None:
    assert tariff.is_offpeak_day(date(2026, 4, 3)) is False
    assert tariff.rate_at(_at(2026, 4, 3, 12), TARIFF) == TARIFF["peak_morning"]["price"]


@pytest.mark.parametrize(
    ("month", "day"),
    [(1, 1), (1, 6), (5, 1), (8, 15), (10, 12), (11, 1), (12, 6), (12, 8), (12, 25)],
)
def test_fixed_national_holidays_are_offpeak(month: int, day: int) -> None:
    assert tariff.is_offpeak_day(date(2026, month, day)) is True


def test_step_times_dst_fallback_keeps_five_minute_utc_spacing() -> None:
    start = datetime(2026, 10, 24, 22, 0, tzinfo=timezone.utc)
    steps = tariff.step_times(start, 288, 5, MADRID)
    utc = [moment.astimezone(timezone.utc) for moment in steps]
    assert len(steps) == 288
    assert all(b - a == timedelta(minutes=5) for a, b in zip(utc, utc[1:]))
    assert sum(moment.hour == 2 for moment in steps) == 24
    # 22:00Z + 287*5min = 21:55Z = 22:55 CET. 20:55 CET would be a 20:00Z start.
    last = steps[-1]
    assert last == datetime(2026, 10, 25, 22, 55, tzinfo=MADRID)
    assert last.utcoffset() == timedelta(hours=1)


def test_step_times_dst_spring_forward_skips_hour_2() -> None:
    start = datetime(2027, 3, 27, 23, 0, tzinfo=timezone.utc)
    steps = tariff.step_times(start, 72, 5, MADRID)
    utc = [moment.astimezone(timezone.utc) for moment in steps]
    hours = [moment.hour for moment in steps]
    assert all(b - a == timedelta(minutes=5) for a, b in zip(utc, utc[1:]))
    assert 2 not in hours
    assert 1 in hours and 3 in hours


def test_import_rates_on_dst_sunday_and_following_monday() -> None:
    start = datetime(2026, 10, 24, 22, 0, tzinfo=timezone.utc)
    sunday = tariff.step_times(start, 288, 5, MADRID)
    sunday_rates = tariff.import_rates(sunday, TARIFF)
    assert sunday_rates.shape == (288,)
    assert np.all(sunday_rates == TARIFF["valley"]["price"])
    assert np.all(tariff.offpeak_mask(sunday))

    monday_hours = (3, 9, 11, 15, 19, 23)
    monday = [datetime(2026, 10, 26, hour, tzinfo=MADRID) for hour in monday_hours]
    rates = tariff.import_rates(monday, TARIFF)
    expected = [
        TARIFF["valley"]["price"],
        TARIFF["shoulder_morning"]["price"],
        TARIFF["peak_morning"]["price"],
        TARIFF["shoulder_afternoon"]["price"],
        TARIFF["peak_evening"]["price"],
        TARIFF["shoulder_night"]["price"],
    ]
    assert list(rates) == expected
    assert not np.any(tariff.offpeak_mask(monday))
