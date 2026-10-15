"""Restart-safe monthly GHI-to-PV factor."""

import json
from dataclasses import dataclass
from datetime import date

DAYLIGHT_KW = 0.05
MIN_PREDICTED_WH = 100.0
MIN_COVERAGE = 0.7
MIN_OBSERVED_DAYLIGHT_CYCLES = 60
STEP_GAIN = 0.3
MAX_ABS_STEP = 0.03
FACTOR_MIN = 0.3
FACTOR_MAX = 2.5


def _clip(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def _month_key(key: object) -> int | None:
    if isinstance(key, bool) or not isinstance(key, (str, int)):
        return None
    try:
        month = int(key)
    except ValueError:
        return None
    if 1 <= month <= 12:
        return month
    return None


@dataclass
class DayAccumulator:
    day: str
    actual_wh: float = 0.0
    predicted_wh: float = 0.0
    daylight_cycles: int = 0
    used_cycles: int = 0

    def to_dict(self) -> dict[str, str | float | int]:
        return {
            "day": self.day,
            "actual_wh": self.actual_wh,
            "predicted_wh": self.predicted_wh,
            "daylight_cycles": self.daylight_cycles,
            "used_cycles": self.used_cycles,
        }

    @classmethod
    def from_dict(cls, data: dict[str, str | float | int]) -> "DayAccumulator":
        return cls(
            day=str(data["day"]),
            actual_wh=float(data.get("actual_wh", 0.0)),
            predicted_wh=float(data.get("predicted_wh", 0.0)),
            daylight_cycles=int(data.get("daylight_cycles", 0)),
            used_cycles=int(data.get("used_cycles", 0)),
        )


@dataclass
class CalibrationState:
    factors: dict[int, float]
    acc: DayAccumulator | None

    def to_json(self) -> str:
        return json.dumps(
            {
                "factors": {str(month): self.factors[month] for month in sorted(self.factors)},
                "acc": None if self.acc is None else self.acc.to_dict(),
            }
        )

    @classmethod
    def from_json(cls, text: str, defaults: dict[int, float]) -> "CalibrationState":
        """Accept new or legacy JSON. Malformed input returns defaults, never raises."""
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, TypeError, ValueError):
            return cls(dict(defaults), None)
        if not isinstance(payload, dict):
            return cls(dict(defaults), None)
        try:
            if "factors" in payload:
                factors = _merge_factors(payload["factors"], defaults)
                try:
                    acc = _acc_from_payload(payload.get("acc"))
                except (TypeError, ValueError):
                    acc = None
                return cls(factors, acc)
            return cls(_merge_factors(payload, defaults), None)
        except (TypeError, ValueError):
            return cls(dict(defaults), None)


def _merge_factors(raw: object, defaults: dict[int, float]) -> dict[int, float]:
    if not isinstance(raw, dict):
        raise TypeError("factors must be an object")
    factors = dict(defaults)
    for key, value in raw.items():
        month = _month_key(key)
        if month is None:
            continue
        factors[month] = float(value)
    return factors


def _acc_from_payload(raw: object) -> DayAccumulator | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("day"), str):
        raise TypeError("acc must be an object with a day")
    date.fromisoformat(raw["day"])
    return DayAccumulator.from_dict(raw)


def _coverage_ok(daylight_cycles: int, expected: int | None) -> bool:
    if expected is None:
        return daylight_cycles >= MIN_OBSERVED_DAYLIGHT_CYCLES
    if expected <= 0:
        return False
    return daylight_cycles / expected >= MIN_COVERAGE


def finalize_day(
    state: CalibrationState,
    expected_daylight_cycles: int | None = None,
) -> tuple[int, float, float] | None:
    """Apply a capped factor step when the day has enough energy and coverage."""
    acc = state.acc
    state.acc = None
    if acc is None or acc.predicted_wh < MIN_PREDICTED_WH:
        return None
    if not _coverage_ok(acc.daylight_cycles, expected_daylight_cycles):
        return None
    month = date.fromisoformat(acc.day).month
    old = state.factors.get(month, 1.0)
    step = _clip(STEP_GAIN * (acc.actual_wh / acc.predicted_wh - 1.0), -MAX_ABS_STEP, MAX_ABS_STEP)
    new = _clip(old * (1.0 + step), FACTOR_MIN, FACTOR_MAX)
    state.factors[month] = new
    return month, old, new


def record_sample(
    state: CalibrationState,
    day: date,
    predicted_kw: float,
    actual_kw: float,
    curtailed: bool,
    interval_hours: float,
) -> list[tuple[int, float, float]]:
    """Accumulate one cycle. A date change finalizes the previous day first."""
    updates: list[tuple[int, float, float]] = []
    iso = day.isoformat()
    if state.acc is None or state.acc.day != iso:
        update = finalize_day(state)
        if update is not None:
            updates.append(update)
        state.acc = DayAccumulator(day=iso)
    acc = state.acc
    if predicted_kw > DAYLIGHT_KW:
        acc.daylight_cycles += 1
        if actual_kw > DAYLIGHT_KW and not curtailed:
            acc.actual_wh += actual_kw * 1000.0 * interval_hours
            acc.predicted_wh += predicted_kw * 1000.0 * interval_hours
            acc.used_cycles += 1
    return updates
