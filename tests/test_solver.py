"""The LP must always return a plan, and stay unchanged where the old bounds were feasible."""

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

_PATH = Path(__file__).resolve().parents[1] / "custom_components" / "battery_mpc" / "solver.py"
_spec = importlib.util.spec_from_file_location("battery_mpc_solver", _PATH)
solver = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = solver
_spec.loader.exec_module(solver)

TARIFF = {
    "valley": {"hours": [0, 8], "price": 0.1228},
    "shoulder_morning": {"hours": [8, 10], "price": 0.1519},
    "peak_morning": {"hours": [10, 14], "price": 0.2558},
    "shoulder_afternoon": {"hours": [14, 18], "price": 0.1519},
    "peak_evening": {"hours": [18, 22], "price": 0.2558},
    "shoulder_night": {"hours": [22, 24], "price": 0.1519},
}
N = 288


def _solve(soc_pct, net, start_hour=19, solar=None):
    hours = (start_hour + np.arange(N) * 5 // 60) % 24
    solar = np.zeros(N) if solar is None else solar
    return solver.solve_mpc(
        solar_forecast=solar, load_forecast=solar + net, hours=hours, tariff_schedule=TARIFF,
        export_rate=0.0509, dt_hours=5 / 60, battery_capacity=15.36, max_charge_rate=5.0,
        max_discharge_rate=5.0, efficiency=0.95, current_soc_kwh=soc_pct / 100 * 15.36,
        min_soc_frac=0.10, max_grid_import=5.0, max_grid_export=5.0,
        is_weekend=np.zeros(N, dtype=bool),
    )


@pytest.mark.parametrize("soc_pct", [10.0, 9.99, 8.0, 2.0])
@pytest.mark.parametrize("net_kw", [5.5, 8.0, 9.5])
def test_floor_with_load_above_grid_limit_still_solves(soc_pct, net_kw):
    net = np.full(N, 1.5)
    net[:12] = net_kw
    result = _solve(soc_pct, net)
    assert result.success
    assert result.next_action == "idle"


def test_below_floor_is_not_forced_to_grid_charge_at_peak():
    result = _solve(8.0, np.full(N, 1.0), start_hour=19)
    assert result.success
    assert result.next_action != "charge"


def test_full_battery_with_pv_above_export_cap_solves():
    solar = np.zeros(N)
    solar[:6] = 6.0
    result = _solve(100.0, np.full(N, 0.2), start_hour=13, solar=solar)
    assert result.success


def test_import_bounds_match_old_rule_when_battery_has_energy():
    net = np.array([1.0, 6.0, 9.0, 4.0])
    bounds = solver._import_bounds(net, 5.0, 5.0, energy_above_floor=10.0, efficiency=0.95, dt=5 / 60)
    np.testing.assert_allclose(bounds, np.maximum(5.0, net - 5.0))


def test_import_bounds_rise_when_battery_is_empty():
    net = np.array([6.0, 8.0])
    bounds = solver._import_bounds(net, 5.0, 5.0, energy_above_floor=0.0, efficiency=0.95, dt=5 / 60)
    np.testing.assert_allclose(bounds, net)
