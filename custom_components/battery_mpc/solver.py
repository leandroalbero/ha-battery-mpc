"""MPC Linear Programming solver for battery scheduling.

Ported from powerflow-simulator's oracle LP solver. Runs in an executor thread
since scipy.optimize.linprog is blocking.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np


@dataclass
class MpcResult:
    """Result from solving the MPC LP."""

    success: bool
    solve_time_ms: float
    total_cost: float
    # Per-step arrays (kW power, kWh for SoC)
    charge: np.ndarray
    discharge: np.ndarray
    grid_import: np.ndarray
    grid_export: np.ndarray
    soc: np.ndarray
    # Convenience: first-step action
    next_action: str  # "charge", "discharge", "idle"
    next_power_w: float  # absolute power in watts


def build_import_rates(hours: np.ndarray, tariff_schedule: dict) -> np.ndarray:
    """Map hour-of-day array to import rates using tariff schedule."""
    rates = np.zeros(len(hours))
    for slot in tariff_schedule.values():
        start_h, end_h = slot["hours"]
        price = slot["price"]
        mask = (hours >= start_h) & (hours < end_h)
        rates[mask] = price
    return rates


def solve_mpc(
    solar_forecast: np.ndarray,
    load_forecast: np.ndarray,
    hours: np.ndarray,
    tariff_schedule: dict,
    export_rate: float,
    dt_hours: float,
    battery_capacity: float,
    max_charge_rate: float,
    max_discharge_rate: float,
    efficiency: float,
    current_soc_kwh: float,
    min_soc_frac: float,
    max_grid_import: float = 5.0,
    max_grid_export: float = 5.0,
) -> MpcResult:
    """Solve the MPC LP for optimal battery scheduling.

    All power values in kW, energy in kWh, prices in EUR/kWh.
    """
    from scipy.optimize import linprog
    from scipy.sparse import diags, hstack, vstack
    from scipy.sparse import eye as speye

    t0 = time.monotonic()

    n = len(solar_forecast)
    if n == 0:
        return MpcResult(
            success=True, solve_time_ms=0, total_cost=0,
            charge=np.array([]), discharge=np.array([]),
            grid_import=np.array([]), grid_export=np.array([]),
            soc=np.array([]),
            next_action="idle", next_power_w=0,
        )

    import_rates = build_import_rates(hours, tariff_schedule)
    export_rates = np.full(n, export_rate)
    dt_arr = np.full(n, dt_hours)

    # Variable layout: [charge(n), discharge(n), grid_import(n), grid_export(n), soc(n)]
    nc = 5 * n
    idx_ch = slice(0, n)
    idx_dis = slice(n, 2 * n)
    idx_imp = slice(2 * n, 3 * n)
    idx_exp = slice(3 * n, 4 * n)

    # Objective: min cost
    c = np.zeros(nc)
    c[idx_imp] = import_rates * dt_arr
    c[idx_exp] = -export_rates * dt_arr

    # Sparse matrix construction
    I = speye(n, format="csr")
    Z = speye(n, format="csr") * 0
    D = diags(dt_arr, 0, format="csr")

    # Energy balance: -charge + discharge + grid_import - grid_export = load - solar
    A_bal = hstack([-I, I, I, -I, Z], format="csr")
    b_bal = load_forecast - solar_forecast

    # Battery dynamics: soc[t] - soc[t-1] - charge[t]*eff*dt + discharge[t]/eff*dt = 0
    soc_block = speye(n, format="csr") + diags([-1.0], [-1], shape=(n, n), format="csr")
    A_dyn = hstack([
        -efficiency * D,
        (1.0 / efficiency) * D,
        Z, Z,
        soc_block,
    ], format="csr")
    b_dyn = np.zeros(n)
    b_dyn[0] = current_soc_kwh

    A_eq = vstack([A_bal, A_dyn], format="csr")
    b_eq = np.concatenate([b_bal, b_dyn])

    # Bounds
    min_soc = min_soc_frac * battery_capacity
    min_grid_needed = np.maximum(load_forecast - solar_forecast - max_discharge_rate, 0)
    effective_grid_import = np.maximum(max_grid_import, min_grid_needed)

    lb = np.concatenate([
        np.zeros(n), np.zeros(n), np.zeros(n), np.zeros(n),
        np.full(n, min_soc),
    ])
    ub = np.concatenate([
        np.full(n, max_charge_rate),
        np.full(n, max_discharge_rate),
        effective_grid_import,
        np.full(n, max_grid_export),
        np.full(n, battery_capacity),
    ])
    bounds = list(zip(lb, ub))

    result = linprog(
        c, A_eq=A_eq, b_eq=b_eq, bounds=bounds,
        method="highs", options={"presolve": True, "time_limit": 30},
    )

    elapsed_ms = (time.monotonic() - t0) * 1000

    if not result.success:
        return MpcResult(
            success=False, solve_time_ms=elapsed_ms, total_cost=0,
            charge=np.zeros(n), discharge=np.zeros(n),
            grid_import=np.zeros(n), grid_export=np.zeros(n),
            soc=np.full(n, current_soc_kwh),
            next_action="idle", next_power_w=0,
        )

    x = result.x
    charge = x[idx_ch]
    discharge = x[idx_dis]

    # Determine first-step action
    net = charge[0] - discharge[0]
    if net > 0.05:
        action = "charge"
        power_w = charge[0] * 1000
    elif net < -0.05:
        action = "discharge"
        power_w = discharge[0] * 1000
    else:
        action = "idle"
        power_w = 0

    return MpcResult(
        success=True,
        solve_time_ms=elapsed_ms,
        total_cost=float(result.fun),
        charge=charge,
        discharge=discharge,
        grid_import=x[idx_imp],
        grid_export=x[idx_exp],
        soc=x[4 * n:],
        next_action=action,
        next_power_w=round(power_w, 0),
    )
