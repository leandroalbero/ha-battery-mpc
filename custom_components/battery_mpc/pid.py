"""PI controller for GoodWe eco_mode_power correction.

The GoodWe eco_mode_power is a percentage of inverter rated power, but the
actual battery charge rate doesn't track linearly — especially below 10%.
This PI controller observes the actual battery power each cycle and adjusts
the percentage to converge on the MPC target.

Tuned from real GoodWe GW5048D-ES data (2026-04-12):
  - At 1-3%, actual charge is 73-400W (nominal: 48-144W). Very nonlinear.
  - 5-min cycle means the inverter sometimes hasn't settled when we read.
  - Gains must be conservative to avoid limit cycling between 1% and 10%.
"""

from __future__ import annotations

from .const import LOGGER


class PowerPI:
    """PI controller that adjusts eco_mode_power % to match MPC target.

    Control loop (every 5 min):
      1. MPC outputs target_power_w
      2. Read actual battery charge power from sensor
      3. PI computes corrected eco_mode_power %
      4. Set the corrected % on the inverter

    The error is measured against the PREVIOUS cycle's target, since the
    current actual power is the result of the previous command.
    """

    def __init__(
        self,
        rated_power_w: float,
        kp: float = 0.003,
        ki: float = 0.001,
        max_step: int = 2,
        deadband_w: float = 30.0,
    ) -> None:
        """Initialize PI controller.

        Args:
            rated_power_w: Inverter rated power in watts (e.g., 4800).
            kp: Proportional gain (% correction per W of error).
                0.003 → 1% correction per 333W error.
            ki: Integral gain (% per W accumulated over cycles).
                0.001 → 1% per 1000 W·cycle accumulated.
            max_step: Maximum eco_mode_power % change per cycle.
            deadband_w: Ignore errors smaller than this (W).
        """
        self._rated_w = rated_power_w
        self._kp = kp
        self._ki = ki
        self._max_step = max_step
        self._deadband_w = deadband_w
        self._integral: float = 0.0
        self._prev_target_w: float | None = None
        self._last_set_pct: int | None = None

    def compute(self, target_power_w: float, actual_power_w: float | None) -> int:
        """Compute corrected eco_mode_power percentage.

        Args:
            target_power_w: What the MPC wants this cycle (W).
            actual_power_w: Measured battery charge power (W, positive=charging).
                None if sensor not available — falls back to open-loop.

        Returns:
            eco_mode_power percentage (1–100).
        """
        base_pct = target_power_w / self._rated_w * 100

        if actual_power_w is None or self._prev_target_w is None:
            # No feedback yet — open-loop for first cycle
            self._prev_target_w = target_power_w
            result = _clamp_pct(base_pct)
            self._last_set_pct = result
            return result

        # Error against PREVIOUS target (what we were trying to achieve last cycle)
        error_w = actual_power_w - self._prev_target_w

        # Deadband: ignore small errors to avoid chasing noise
        if abs(error_w) < self._deadband_w:
            error_w = 0.0

        # Reset integral on large target jumps (new MPC solution region)
        if abs(target_power_w - self._prev_target_w) > 1000:
            self._integral = 0.0

        self._prev_target_w = target_power_w

        # Accumulate integral with tight anti-windup
        self._integral = max(-1000, min(1000, self._integral + error_w))

        # PI output: positive error (actual too high) → reduce %
        correction_pct = self._kp * error_w + self._ki * self._integral

        corrected_pct = base_pct - correction_pct

        result = _clamp_pct(corrected_pct)

        # Rate limit: max ±max_step % change per cycle
        if self._last_set_pct is not None:
            result = max(
                self._last_set_pct - self._max_step,
                min(self._last_set_pct + self._max_step, result),
            )
            result = max(1, min(100, result))

        LOGGER.debug(
            "PI: target=%.0fW actual=%.0fW error=%.0fW integral=%.0f "
            "base=%.1f%% correction=%.1f%% raw=%d%% -> %d%% (last=%s)",
            target_power_w, actual_power_w, error_w, self._integral,
            base_pct, correction_pct, _clamp_pct(corrected_pct), result,
            f"{self._last_set_pct}%" if self._last_set_pct is not None else "n/a",
        )

        self._last_set_pct = result
        return result

    def reset(self) -> None:
        """Reset state (call on mode change to/from charge)."""
        self._integral = 0.0
        self._prev_target_w = None
        self._last_set_pct = None


def _clamp_pct(pct: float) -> int:
    return max(1, min(100, round(pct)))
