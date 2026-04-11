"""MPC Linear Programming solver for battery scheduling.

Uses a numpy-only interior point method (Mehrotra predictor-corrector).
No scipy required — fully self-contained.
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
    charge: np.ndarray
    discharge: np.ndarray
    grid_import: np.ndarray
    grid_export: np.ndarray
    soc: np.ndarray
    next_action: str  # "charge", "discharge", "idle"
    next_power_w: float


def _solve_lp(
    c: np.ndarray,
    A_eq: np.ndarray,
    b_eq: np.ndarray,
    lb: np.ndarray,
    ub: np.ndarray,
    max_iter: int = 50,
    tol: float = 1e-8,
) -> tuple[np.ndarray | None, float, bool]:
    """Solve LP: min c'x s.t. A_eq @ x = b_eq, lb <= x <= ub.

    Mehrotra predictor-corrector interior point method using normal equations.
    Requires only numpy.

    Returns (x_optimal, objective_value, success).
    """
    m, n = A_eq.shape

    # Initial strictly feasible point
    x = np.clip((lb + ub) / 2, lb + 1e-3, ub - 1e-3)
    sl = x - lb  # lower bound slack
    su = ub - x  # upper bound slack
    y = np.zeros(m)
    zl = np.maximum(0.1, np.abs(c) * 0.1 + 0.1)  # dual for lower bound
    zu = zl.copy()  # dual for upper bound

    for it in range(max_iter):
        # Residuals
        rp = b_eq - A_eq @ x
        rd = c - A_eq.T @ y - zl + zu
        mu = (sl @ zl + su @ zu) / (2 * n)

        if mu < tol and np.max(np.abs(rp)) < tol and np.max(np.abs(rd)) < tol:
            return x, float(c @ x), True

        # Diagonal scaling
        dl = zl / sl  # Zl Sl^{-1}
        du = zu / su  # Zu Su^{-1}
        D_inv = 1.0 / (dl + du)

        # Normal equations matrix: A D^{-1} A^T (reused for predictor + corrector)
        AD = A_eq * D_inv[np.newaxis, :]
        ADA = AD @ A_eq.T

        # Add tiny regularization for numerical stability
        ADA[np.diag_indices_from(ADA)] += 1e-12

        try:
            L = np.linalg.cholesky(ADA)
        except np.linalg.LinAlgError:
            # Fall back to general solve if not positive definite
            L = None

        def solve_ne(rhs: np.ndarray) -> np.ndarray:
            if L is not None:
                z = np.linalg.solve(L, rhs)
                return np.linalg.solve(L.T, z)
            return np.linalg.solve(ADA, rhs)

        # --- Affine (predictor) step ---
        rhs_aff = rd + zl - zu
        dy_aff = solve_ne(rp + AD @ rhs_aff)
        dx_aff = D_inv * (A_eq.T @ dy_aff - rhs_aff)
        dzl_aff = -zl - dl * dx_aff
        dzu_aff = -zu + du * dx_aff

        # Affine step size
        alpha_aff = 1.0
        for v, dv in [(sl, dx_aff), (su, -dx_aff), (zl, dzl_aff), (zu, dzu_aff)]:
            neg = dv < 0
            if np.any(neg):
                alpha_aff = min(alpha_aff, 0.9999 * float(np.min(-v[neg] / dv[neg])))

        # Centering parameter (Mehrotra heuristic)
        mu_aff = ((sl + alpha_aff * dx_aff) @ (zl + alpha_aff * dzl_aff)
                  + (su - alpha_aff * dx_aff) @ (zu + alpha_aff * dzu_aff)) / (2 * n)
        sigma = (mu_aff / mu) ** 3

        # --- Combined (corrector) step ---
        comp_l = sigma * mu - dx_aff * dzl_aff
        comp_u = sigma * mu + dx_aff * dzu_aff
        rhs_cc = rd - comp_l / sl + zl + comp_u / su - zu
        dy = solve_ne(rp + AD @ rhs_cc)
        dx = D_inv * (A_eq.T @ dy - rhs_cc)
        dzl = comp_l / sl - zl - dl * dx
        dzu = comp_u / su - zu + du * dx

        # Step size
        alpha = 1.0
        for v, dv in [(sl, dx), (su, -dx), (zl, dzl), (zu, dzu)]:
            neg = dv < 0
            if np.any(neg):
                alpha = min(alpha, 0.9999 * float(np.min(-v[neg] / dv[neg])))

        # Update
        x += alpha * dx
        sl += alpha * dx
        su -= alpha * dx
        y += alpha * dy
        zl += alpha * dzl
        zu += alpha * dzu

    # Didn't converge but return best solution
    return x, float(c @ x), False


def build_import_rates(
    hours: np.ndarray,
    tariff_schedule: dict,
    is_weekend: np.ndarray | None = None,
) -> np.ndarray:
    """Map hour-of-day array to import rates using tariff schedule.

    Spain 2.0TD: weekends and public holidays are flat valley rate all day.
    """
    rates = np.zeros(len(hours))
    valley_price = min(slot["price"] for slot in tariff_schedule.values())
    for slot in tariff_schedule.values():
        start_h, end_h = slot["hours"]
        price = slot["price"]
        mask = (hours >= start_h) & (hours < end_h)
        rates[mask] = price

    # Weekends: flat valley rate
    if is_weekend is not None:
        rates[is_weekend] = valley_price

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
    max_grid_import: float = 5.5,
    max_grid_export: float = 5.5,
    is_weekend: np.ndarray | None = None,
) -> MpcResult:
    """Solve the MPC LP for optimal battery scheduling."""
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

    import_rates = build_import_rates(hours, tariff_schedule, is_weekend)
    export_rates = np.full(n, export_rate)
    dt = dt_hours

    # Variables: [charge(n), discharge(n), grid_import(n), grid_export(n), soc(n)]
    nc = 5 * n

    # Objective: min grid_import * rate - grid_export * export_rate (per step)
    c = np.zeros(nc)
    c[2 * n:3 * n] = import_rates * dt
    c[3 * n:4 * n] = -export_rates * dt

    # Equality constraints (dense numpy arrays)
    # Block 1: Energy balance (n rows)
    #   -charge + discharge + grid_import - grid_export = load - solar
    A_bal = np.zeros((n, nc))
    idx = np.arange(n)
    A_bal[idx, idx] = -1.0          # -charge
    A_bal[idx, n + idx] = 1.0       # +discharge
    A_bal[idx, 2 * n + idx] = 1.0   # +grid_import
    A_bal[idx, 3 * n + idx] = -1.0  # -grid_export
    b_bal = load_forecast - solar_forecast

    # Block 2: Battery dynamics (n rows)
    #   soc[t] - soc[t-1] - charge[t]*eff*dt + discharge[t]/eff*dt = 0
    A_dyn = np.zeros((n, nc))
    A_dyn[idx, idx] = -efficiency * dt           # -charge * eff * dt
    A_dyn[idx, n + idx] = (1.0 / efficiency) * dt  # +discharge / eff * dt
    # soc[t] - soc[t-1]
    A_dyn[idx, 4 * n + idx] = 1.0
    for t in range(1, n):
        A_dyn[t, 4 * n + t - 1] = -1.0
    b_dyn = np.zeros(n)
    b_dyn[0] = current_soc_kwh

    A_eq = np.vstack([A_bal, A_dyn])
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

    # Solve
    x, obj, success = _solve_lp(c, A_eq, b_eq, lb, ub)
    elapsed_ms = (time.monotonic() - t0) * 1000

    if x is None or not success:
        return MpcResult(
            success=False, solve_time_ms=elapsed_ms, total_cost=0,
            charge=np.zeros(n), discharge=np.zeros(n),
            grid_import=np.zeros(n), grid_export=np.zeros(n),
            soc=np.full(n, current_soc_kwh),
            next_action="idle", next_power_w=0,
        )

    charge = x[:n]
    discharge = x[n:2 * n]

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
        total_cost=obj,
        charge=charge,
        discharge=discharge,
        grid_import=x[2 * n:3 * n],
        grid_export=x[3 * n:4 * n],
        soc=x[4 * n:],
        next_action=action,
        next_power_w=round(power_w, 0),
    )
