"""Open-Meteo solar forecast client for Battery MPC."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
from aiohttp import ClientSession

from .const import (
    DEFAULT_GHI_TO_PV_FACTOR,
    DEFAULT_MONTHLY_FACTORS,
    LOGGER,
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

    def get_pv_forecast(self, times: list[datetime]) -> np.ndarray:
        """PV power forecast in kW at each timezone-aware time."""
        if len(self._timestamps) == 0:
            return np.zeros(len(times))
        # Open-Meteo hourly radiation is the mean of the preceding hour.
        centers = np.array([(ts - timedelta(minutes=30)).timestamp() for ts in self._timestamps])
        targets = np.array([t.timestamp() for t in times])
        ghi = np.interp(targets, centers, self._ghi, left=0.0, right=0.0)
        months = np.array([self._monthly.get(t.month, 1.0) for t in times])
        return ghi * self._factor * months / 1000.0


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
        "timezone": "GMT",
        "forecast_days": forecast_days,
        "past_days": 1,
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

        timestamps = [datetime.fromisoformat(t).replace(tzinfo=timezone.utc) for t in times_raw]
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

    def forecast(self, times: list[datetime]) -> np.ndarray:
        """Load forecast in kW at each local time."""
        return np.array([self._profile.get((t.hour, t.weekday() >= 5), 1.4) for t in times])
