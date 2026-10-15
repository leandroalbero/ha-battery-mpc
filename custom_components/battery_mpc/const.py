"""Constants for Battery MPC Controller."""

from logging import Logger, getLogger

LOGGER: Logger = getLogger(__package__)
DOMAIN = "battery_mpc"

# Open-Meteo API
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# Default tariff (Spain 2.0TD, Octopus Energy 2026, taxes included)
DEFAULT_TARIFF = {
    "valley": {"hours": (0, 8), "price": 0.1228},
    "shoulder_morning": {"hours": (8, 10), "price": 0.1519},
    "peak_morning": {"hours": (10, 14), "price": 0.2558},
    "shoulder_afternoon": {"hours": (14, 18), "price": 0.1519},
    "peak_evening": {"hours": (18, 22), "price": 0.2558},
    "shoulder_night": {"hours": (22, 24), "price": 0.1519},
}
DEFAULT_EXPORT_RATE = 0.0509  # EUR/kWh flat, taxes included
DEFAULT_MAX_GRID_IMPORT_KW = 5.5

# GHI to PV conversion (learned from historical data, per-month)
# These are defaults for a typical ~5kWp system in southeastern Spain.
# Users should calibrate with their own data.
DEFAULT_GHI_TO_PV_FACTOR = 4.592  # W/m2 -> W
DEFAULT_MONTHLY_FACTORS = {
    1: 1.39, 2: 1.29, 3: 1.02, 4: 0.84, 5: 0.93, 6: 0.87,
    7: 0.87, 8: 0.94, 9: 0.97, 10: 1.22, 11: 1.41, 12: 1.45,
}

# Solar calibration
SOLAR_CALIBRATION_FILE = "battery_mpc_solar_factors.json"

# MPC defaults
MPC_HORIZON_HOURS = 24
MPC_STEP_MINUTES = 5
MPC_UPDATE_INTERVAL_MINUTES = 5
FORECAST_REFRESH_MINUTES = 60
LOAD_HISTORY_DAYS = 7

# Fail-safe
UPDATE_TIMEOUT_SECONDS = 180
SERVICE_CALL_TIMEOUT_SECONDS = 30
MODE_REASSERT_MINUTES = 60
WATCHDOG_INTERVAL_MINUTES = 5
WATCHDOG_STALE_MINUTES = 20
