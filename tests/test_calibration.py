"""Solar-factor persistence tests. Loads calibration.py without the HA package."""

from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path

import pytest


def _load():
    path = (
        Path(__file__).resolve().parents[1]
        / "custom_components"
        / "battery_mpc"
        / "calibration.py"
    )
    spec = importlib.util.spec_from_file_location("battery_mpc_calibration", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cal = _load()

DEFAULTS = {month: 1.0 for month in range(1, 13)}


def _fill(state, day: date, predicted_kw: float, actual_kw: float, n: int) -> None:
    for _ in range(n):
        cal.record_sample(state, day, predicted_kw, actual_kw, False, 1.0)


def test_from_json_legacy_fills_missing_months() -> None:
    defaults = dict(DEFAULTS)
    state = cal.CalibrationState.from_json(json.dumps({"1": 1.39, "12": 1.45}), defaults)
    assert state.factors[1] == 1.39
    assert state.factors[12] == 1.45
    assert state.factors[6] == 1.0
    assert state.acc is None
    state.factors[1] = 9.0
    assert defaults[1] == 1.0


def test_to_json_round_trip_keeps_saved_factors() -> None:
    acc = cal.DayAccumulator("2026-06-15", 10.0, 20.0, 4, 3)
    state = cal.CalibrationState({**DEFAULTS, 6: 1.39}, acc)
    decoy = {month: 0.5 for month in range(1, 13)}
    restored = cal.CalibrationState.from_json(state.to_json(), decoy)
    assert restored.factors == state.factors
    assert restored.acc == acc
    assert decoy[6] == 0.5


def test_from_json_keeps_factors_when_acc_is_corrupt() -> None:
    text = json.dumps({"factors": {"6": 1.39}, "acc": {"day": "not-a-date"}})
    state = cal.CalibrationState.from_json(text, DEFAULTS)
    assert state.factors[6] == 1.39
    assert state.acc is None


@pytest.mark.parametrize("text", ["{", "not json", "", "null", "[]", "42"])
def test_from_json_malformed_returns_defaults(text: str) -> None:
    state = cal.CalibrationState.from_json(text, DEFAULTS)
    assert state.factors == DEFAULTS
    assert state.acc is None


def test_ratio_1_5_moves_factor_up_exactly_3_percent() -> None:
    state = cal.CalibrationState(dict(DEFAULTS), None)
    _fill(state, date(2026, 6, 15), predicted_kw=2.0, actual_kw=3.0, n=60)
    assert cal.finalize_day(state) == (6, 1.0, 1.03)
    assert state.factors[6] == 1.03
    assert state.acc is None


def test_ratio_0_5_moves_factor_down_exactly_3_percent() -> None:
    state = cal.CalibrationState({6: 1.0}, None)
    _fill(state, date(2026, 6, 15), predicted_kw=2.0, actual_kw=1.0, n=60)
    assert cal.finalize_day(state) == (6, 1.0, 0.97)
    assert state.factors[6] == 0.97


def test_thirty_daylight_cycles_do_not_update() -> None:
    state = cal.CalibrationState({6: 1.0}, None)
    _fill(state, date(2026, 6, 15), predicted_kw=2.0, actual_kw=3.0, n=30)
    assert state.acc.daylight_cycles == 30
    assert state.acc.predicted_wh >= 100
    assert cal.finalize_day(state) is None
    assert state.factors[6] == 1.0
    assert state.acc is None


def test_record_sample_rollover_finalizes_previous_month() -> None:
    state = cal.CalibrationState({6: 1.0, 7: 1.0}, None)
    _fill(state, date(2026, 6, 30), predicted_kw=2.0, actual_kw=3.0, n=60)
    updates = cal.record_sample(state, date(2026, 7, 1), 1.5, 1.5, False, 1.0)
    assert updates == [(6, 1.0, 1.03)]
    assert state.factors[7] == 1.0
    assert state.acc.day == "2026-07-01"
    assert state.acc.daylight_cycles == 1
    assert state.acc.used_cycles == 1
    assert state.acc.predicted_wh == 1500.0


def test_curtailed_samples_count_as_daylight_only() -> None:
    state = cal.CalibrationState({6: 1.0}, None)
    day = date(2026, 6, 15)
    cal.record_sample(state, day, 1.2, 0.4, True, 0.5)
    assert state.acc.daylight_cycles == 1
    assert state.acc.used_cycles == 0
    assert state.acc.actual_wh == 0.0
    assert state.acc.predicted_wh == 0.0

    cal.record_sample(state, day, 1.2, 0.4, False, 0.5)
    assert state.acc.daylight_cycles == 2
    assert state.acc.used_cycles == 1
    assert state.acc.actual_wh == pytest.approx(200.0)
    assert state.acc.predicted_wh == pytest.approx(600.0)

    before = (state.acc.daylight_cycles, state.acc.used_cycles, state.acc.actual_wh)
    cal.record_sample(state, day, 0.05, 1.0, False, 0.5)
    assert (state.acc.daylight_cycles, state.acc.used_cycles, state.acc.actual_wh) == before

    cal.record_sample(state, day, 1.0, 0.05, False, 0.5)
    assert state.acc.daylight_cycles == 3
    assert state.acc.used_cycles == 1


def test_accumulator_survives_midday_restart_and_finalizes() -> None:
    state = cal.CalibrationState({7: 1.0}, None)
    day = date(2026, 7, 10)
    _fill(state, day, predicted_kw=2.0, actual_kw=3.0, n=40)
    restored = cal.CalibrationState.from_json(state.to_json(), {7: 9.0})
    assert restored.acc == state.acc
    assert restored.factors[7] == 1.0
    _fill(restored, day, predicted_kw=2.0, actual_kw=3.0, n=20)
    assert cal.finalize_day(restored) == (7, 1.0, 1.03)


def test_expected_coverage_and_factor_bounds() -> None:
    thin = cal.CalibrationState(
        {8: 1.0},
        cal.DayAccumulator("2026-08-01", 1500.0, 1000.0, 69, 69),
    )
    assert cal.finalize_day(thin, expected_daylight_cycles=100) is None
    assert thin.factors[8] == 1.0

    enough = cal.CalibrationState(
        {8: 1.0},
        cal.DayAccumulator("2026-08-01", 1500.0, 1000.0, 70, 70),
    )
    assert cal.finalize_day(enough, expected_daylight_cycles=100) == (8, 1.0, 1.03)

    empty_expected = cal.CalibrationState(
        {8: 1.0},
        cal.DayAccumulator("2026-08-01", 1500.0, 1000.0, 80, 80),
    )
    assert cal.finalize_day(empty_expected, expected_daylight_cycles=0) is None

    high = cal.CalibrationState(
        {1: 2.5},
        cal.DayAccumulator("2026-01-02", 1500.0, 1000.0, 60, 60),
    )
    assert cal.finalize_day(high)[2] == 2.5
    low = cal.CalibrationState(
        {1: 0.3},
        cal.DayAccumulator("2026-01-02", 500.0, 1000.0, 60, 60),
    )
    assert cal.finalize_day(low)[2] == 0.3
