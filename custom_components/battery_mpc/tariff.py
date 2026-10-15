"""Spanish 2.0TD import rates, including fixed-date P3 holidays."""

from __future__ import annotations

from datetime import date, datetime, timedelta, tzinfo
from typing import Final, Mapping, Sequence

import numpy as np

# CNMC Circular 3/2020 counts these national fixed dates as P3. No moved Mondays, no Good Friday.
FIXED_P3_HOLIDAYS: Final[frozenset[tuple[int, int]]] = frozenset(
    {
        (1, 1),
        (1, 6),
        (5, 1),
        (8, 15),
        (10, 12),
        (11, 1),
        (12, 6),
        (12, 8),
        (12, 25),
    }
)


def is_offpeak_day(d: date) -> bool:
    """Saturday, Sunday, and fixed-date national P3 holidays."""
    return d.weekday() >= 5 or (d.month, d.day) in FIXED_P3_HOLIDAYS


def _valley_price(tariff: Mapping[str, Mapping[str, object]]) -> float:
    return min(float(slot["price"]) for slot in tariff.values())


def rate_at(local_dt: datetime, tariff: Mapping[str, Mapping[str, object]]) -> float:
    """Valley all day on off-peak days; otherwise the half-open hour slot."""
    valley = _valley_price(tariff)
    if is_offpeak_day(local_dt.date()):
        return valley
    hour = local_dt.hour
    for slot in tariff.values():
        start, end = slot["hours"]
        if int(start) <= hour < int(end):
            return float(slot["price"])
    return valley


def step_times(
    now_utc: datetime,
    n_steps: int,
    step_minutes: int,
    tz: tzinfo,
) -> list[datetime]:
    """Real-time steps labeled in local wall time, including across DST."""
    step = timedelta(minutes=step_minutes)
    return [(now_utc + i * step).astimezone(tz) for i in range(n_steps)]


def import_rates(
    local_times: Sequence[datetime],
    tariff: Mapping[str, Mapping[str, object]],
) -> np.ndarray:
    """Import price at each local timestamp."""
    return np.array([rate_at(moment, tariff) for moment in local_times], dtype=float)


def offpeak_mask(local_times: Sequence[datetime]) -> np.ndarray:
    """True where the local date is an off-peak day."""
    return np.array([is_offpeak_day(moment.date()) for moment in local_times], dtype=bool)
