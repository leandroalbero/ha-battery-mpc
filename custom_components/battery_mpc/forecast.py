"""Open-Meteo solar forecast client for Battery MPC."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

import numpy as np
from aiohttp import ClientSession

from .const import (
    DEFAULT_GHI_TO_PV_FACTOR,
    DEFAULT_MONTHLY_FACTORS,
    LOGGER,
    MPC_HORIZON_HOURS,
    MPC_STEP_MINUTES,
    OPEN_METEO_URL,
)


class SolarForecast:
    """Cached solar forecast with GHI-to-PV conversion."""

    def __init__(
        self,
        timestamps: list[datetime],
        ghi_values: list[float],
        ghi_to_pv_factor: float = DEFAULT_GHI_TO_PV_FACTOR,
        monthly_factors: dict[int, float] | None = None,
    ) -> None:
        self._timestamps = timestamps
        self._ghi = np.array(ghi_values, dtype=float)
        self._factor = ghi_to_pv_factor
        self._monthly = monthly_factors or DEFAULT_MONTHLY_FACTORS
        self._fetched_at = datetime.now()

    @property
    def age_minutes(self) -> float:
        return (datetime.now() - self._fetched_at).total_seconds() / 60

    def get_pv_forecast(
        self,
        start: datetime,
        steps: int | None = None,
        step_minutes: int = MPC_STEP_MINUTES,
    ) -> np.ndarray:
        """Get PV power forecast in kW for the MPC horizon."""
        if steps is None:
            steps = MPC_HORIZON_HOURS * 60 // step_minutes

        forecast = np.zeros(steps)
        if len(self._timestamps) == 0:
            return forecast

        for i in range(steps):
            target = start + timedelta(minutes=i * step_minutes)
            # Find nearest hourly GHI value
            ghi = self._interpolate_ghi(target)
            month_factor = self._monthly.get(target.month, 1.0)
            forecast[i] = ghi * self._factor * month_factor / 1000.0  # W -> kW

        return forecast

    def _interpolate_ghi(self, target: datetime) -> float:
        """Find the GHI value for a target time by nearest-hour lookup."""
        if len(self._timestamps) == 0:
            return 0.0

        target_naive = target.replace(tzinfo=None) if target.tzinfo else target
        best_idx = 0
        best_diff = float("inf")
        for idx, ts in enumerate(self._timestamps):
            ts_naive = ts.replace(tzinfo=None) if ts.tzinfo else ts
            diff = abs((ts_naive - target_naive).total_seconds())
            if diff < best_diff:
                best_diff = diff
                best_idx = idx

        return float(self._ghi[best_idx]) if best_diff < 7200 else 0.0


async def fetch_solar_forecast(
    session: ClientSession,
    latitude: float,
    longitude: float,
    forecast_days: int = 2,
    api_key: str | None = None,
    monthly_factors: dict[int, float] | None = None,
) -> SolarForecast:
    """Fetch solar irradiance forecast from Open-Meteo API."""
    # Use commercial endpoint if API key provided, otherwise free tier
    base_url = OPEN_METEO_URL
    if api_key:
        base_url = "https://customer-api.open-meteo.com/v1/forecast"

    params: dict[str, Any] = {
        "latitude": latitude,
        "longitude": longitude,
        "hourly": "shortwave_radiation",
        "timezone": "auto",
        "forecast_days": forecast_days,
    }
    if api_key:
        params["apikey"] = api_key

    try:
        async with session.get(base_url, params=params) as resp:
            resp.raise_for_status()
            data: dict[str, Any] = await resp.json()

        hourly = data.get("hourly", {})
        times_raw = hourly.get("time", [])
        ghi_raw = hourly.get("shortwave_radiation", [])

        timestamps = [datetime.fromisoformat(t) for t in times_raw]
        ghi_values = [float(v) if v is not None else 0.0 for v in ghi_raw]

        LOGGER.info(
            "Fetched %d hours of solar forecast (%.1f-%.1f W/m2 GHI range)",
            len(timestamps),
            min(ghi_values) if ghi_values else 0,
            max(ghi_values) if ghi_values else 0,
        )

        return SolarForecast(timestamps, ghi_values, monthly_factors=monthly_factors)

    except Exception as err:
        LOGGER.error("Failed to fetch solar forecast: %s", err)
        return SolarForecast([], [], monthly_factors=monthly_factors)


class LoadForecaster:
    """Load forecast based on historical hourly averages, split by weekday/weekend.

    In production, this uses the actual HA sensor history. For initial setup,
    uses a flat default.
    """

    def __init__(self) -> None:
        # Default: ~1.4 kW average house load, keyed by (hour, is_weekend)
        self._profile: dict[tuple[int, bool], float] = {
            (h, w): 1.4 for h in range(24) for w in (False, True)
        }

    def update_profile(self, history: list[tuple[datetime, float]]) -> None:
        """Update load profile from HA sensor history."""
        if not history:
            return
        sums: dict[tuple[int, bool], float] = {}
        counts: dict[tuple[int, bool], int] = {}
        for ts, value in history:
            key = (ts.hour, ts.weekday() >= 5)
            sums[key] = sums.get(key, 0.0) + value
            counts[key] = counts.get(key, 0) + 1
        for key in sums:
            self._profile[key] = sums[key] / counts[key]

    def forecast(
        self,
        start: datetime,
        steps: int,
        step_minutes: int = MPC_STEP_MINUTES,
    ) -> np.ndarray:
        """Get load forecast in kW."""
        result = np.zeros(steps)
        for i in range(steps):
            ts = start + timedelta(minutes=i * step_minutes)
            key = (ts.hour, ts.weekday() >= 5)
            result[i] = self._profile.get(key, 1.4)
        return result
