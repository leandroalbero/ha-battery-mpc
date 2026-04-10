"""DataUpdateCoordinator for Battery MPC — runs optimization every 5 minutes."""

from __future__ import annotations

from datetime import datetime, timedelta
from functools import partial
from typing import Any

import numpy as np
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_EXPORT_RATE,
    DEFAULT_TARIFF,
    DOMAIN,
    FORECAST_REFRESH_MINUTES,
    LOAD_HISTORY_DAYS,
    LOGGER,
    MPC_HORIZON_HOURS,
    MPC_STEP_MINUTES,
    MPC_UPDATE_INTERVAL_MINUTES,
)
from .forecast import LoadForecaster, SolarForecast, fetch_solar_forecast
from .pid import PowerPI
from .solver import solve_mpc


class BatteryMPCCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetches forecast + runs MPC optimization every 5 minutes."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            LOGGER,
            name=DOMAIN,
            config_entry=config_entry,
            update_interval=timedelta(minutes=MPC_UPDATE_INTERVAL_MINUTES),
        )
        self._config = config_entry.data
        self._solar_forecast: SolarForecast | None = None
        self._load_forecaster = LoadForecaster()
        self._cost_savings_today = 0.0
        self._cost_actual_today = 0.0
        self._cost_baseline_today = 0.0
        self._cost_savings_lifetime = 0.0
        self._today = None
        self._load_profile_updated = False
        self._power_pi = PowerPI(
            rated_power_w=self._config.get("inverter_rated_power_kw", 4.8) * 1000,
        )
        self._last_action: str = "idle"

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            now = datetime.now()

            # Reset daily counters
            if self._today != now.date():
                self._today = now.date()
                self._cost_savings_today = 0.0
                self._cost_actual_today = 0.0
                self._cost_baseline_today = 0.0
                self._load_profile_updated = False

            # Update load profile from recorder history (on startup + daily)
            if not self._load_profile_updated:
                await self._update_load_profile()
                self._load_profile_updated = True

            # Refresh solar forecast if stale or missing
            if (
                self._solar_forecast is None
                or self._solar_forecast.age_minutes > FORECAST_REFRESH_MINUTES
            ):
                try:
                    session = async_get_clientsession(self.hass)
                    new_forecast = await fetch_solar_forecast(
                        session,
                        self._config["latitude"],
                        self._config["longitude"],
                        api_key=self._config.get("open_meteo_api_key"),
                    )
                    # Only replace if we got actual data
                    if new_forecast and len(new_forecast._timestamps) > 0:
                        self._solar_forecast = new_forecast
                    else:
                        LOGGER.warning("Empty forecast returned, keeping previous")
                except Exception as err:
                    LOGGER.warning("Forecast fetch failed, using cached: %s", err)
                    # Keep using the old forecast — don't crash the MPC

            # If we still have no forecast at all, use zeros
            if self._solar_forecast is None:
                from .forecast import SolarForecast
                self._solar_forecast = SolarForecast([], [])

            # Read current SoC from HA sensor
            current_soc_pct = self._get_sensor_value(
                self._config["soc_sensor_entity_id"], default=50.0,
            )
            battery_cap = self._config["battery_capacity_kwh"]
            current_soc_kwh = current_soc_pct / 100.0 * battery_cap

            # Read current house consumption if sensor configured
            current_load_kw = 0.0
            load_entity = self._config.get("load_sensor_entity_id")
            if load_entity:
                current_load_kw = self._get_sensor_value(load_entity, default=0.0) / 1000.0

            # Read current PV power if sensor configured
            current_pv_kw = 0.0
            pv_entity = self._config.get("pv_power_entity_id")
            if pv_entity:
                current_pv_kw = self._get_sensor_value(pv_entity, default=0.0) / 1000.0

            # Calculate cost savings for this 5-min interval.
            # Actual cost: what we really imported/exported from grid.
            # Baseline cost: what we'd import without battery (load - solar).
            grid_import_w = self._get_sensor_value("sensor.import_grid", default=0.0)
            grid_export_w = self._get_sensor_value("sensor.export_grid", default=0.0)

            tariff = self._config.get("tariff", DEFAULT_TARIFF)
            export_rate = self._config.get("export_rate", DEFAULT_EXPORT_RATE)
            current_rate = self._get_current_rate(now.hour, tariff)
            interval_hours = MPC_UPDATE_INTERVAL_MINUTES / 60.0

            actual_cost = (grid_import_w / 1000.0 * current_rate
                           - grid_export_w / 1000.0 * export_rate) * interval_hours
            # Baseline: no battery, all net load from grid
            net_load_w = max(0, current_load_kw * 1000 - current_pv_kw * 1000)
            baseline_cost = (net_load_w / 1000.0 * current_rate) * interval_hours

            self._cost_actual_today += actual_cost
            self._cost_baseline_today += baseline_cost
            self._cost_savings_today = self._cost_baseline_today - self._cost_actual_today
            self._cost_savings_lifetime += baseline_cost - actual_cost

            # Build forecast arrays
            n_steps = MPC_HORIZON_HOURS * 60 // MPC_STEP_MINUTES
            solar_fc = self._solar_forecast.get_pv_forecast(now, n_steps, MPC_STEP_MINUTES)
            load_fc = self._load_forecaster.forecast(now, n_steps, MPC_STEP_MINUTES)

            # Override first step with actual values if available
            if current_pv_kw > 0 or current_load_kw > 0:
                solar_fc[0] = current_pv_kw
                load_fc[0] = current_load_kw

            hours = np.array([
                (now + timedelta(minutes=i * MPC_STEP_MINUTES)).hour
                for i in range(n_steps)
            ])

            tariff = self._config.get("tariff", DEFAULT_TARIFF)
            export_rate = self._config.get("export_rate", DEFAULT_EXPORT_RATE)

            # Solve MPC in executor thread (scipy is blocking)
            result = await self.hass.async_add_executor_job(
                partial(
                    solve_mpc,
                    solar_forecast=solar_fc,
                    load_forecast=load_fc,
                    hours=hours,
                    tariff_schedule=tariff,
                    export_rate=export_rate,
                    dt_hours=MPC_STEP_MINUTES / 60.0,
                    battery_capacity=battery_cap,
                    max_charge_rate=self._config["max_charge_kw"],
                    max_discharge_rate=self._config["max_discharge_kw"],
                    efficiency=self._config.get("efficiency", 0.95),
                    current_soc_kwh=current_soc_kwh,
                    min_soc_frac=self._config["min_soc"] / 100.0,
                    max_grid_import=self._config.get("max_grid_import_kw", 5.0),
                    max_grid_export=self._config.get("max_grid_export_kw", 5.0),
                )
            )

            if not result.success:
                LOGGER.warning("MPC solve failed, keeping idle")

            # Apply action to inverter
            await self._apply_action(result.next_action, result.next_power_w)

            # Build schedule summary (hourly)
            step_per_hour = 60 // MPC_STEP_MINUTES
            schedule = []
            for i in range(0, n_steps, step_per_hour):
                ts = now + timedelta(minutes=i * MPC_STEP_MINUTES)
                schedule.append({
                    "time": ts.strftime("%H:%M"),
                    "action": "charge" if result.charge[i] > 0.05 else (
                        "discharge" if result.discharge[i] > 0.05 else "idle"
                    ),
                    "power_kw": round(result.charge[i] - result.discharge[i], 2),
                    "soc_pct": round(result.soc[i] / battery_cap * 100, 1),
                    "solar_kw": round(float(solar_fc[i]), 2),
                    "load_kw": round(float(load_fc[i]), 2),
                })

            return {
                "next_action": result.next_action,
                "target_power": round(result.next_power_w),
                "target_soc": round(result.soc[0] / battery_cap * 100, 1),
                "current_soc": round(current_soc_pct, 1),
                "forecast_pv_power": round(solar_fc[0] * 1000),
                "forecast_load": round(load_fc[0] * 1000),
                "cost_savings_today": round(self._cost_savings_today, 3),
                "cost_actual_today": round(self._cost_actual_today, 3),
                "cost_baseline_today": round(self._cost_baseline_today, 3),
                "cost_savings_lifetime": round(self._cost_savings_lifetime, 2),
                "solve_time_ms": round(result.solve_time_ms, 1),
                "horizon_hours": MPC_HORIZON_HOURS,
                "schedule": schedule,
                "forecast_age_min": round(self._solar_forecast.age_minutes, 1),
            }

        except Exception as err:
            raise UpdateFailed(f"MPC optimization failed: {err}") from err

    async def _update_load_profile(self) -> None:
        """Fetch recent load sensor history from HA recorder to build hourly profile."""
        load_entity = self._config.get("load_sensor_entity_id")
        if not load_entity:
            return

        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.history import get_significant_states
        except ImportError:
            LOGGER.debug("Recorder not available, using default load profile")
            return

        end = dt_util.utcnow()
        start = end - timedelta(days=LOAD_HISTORY_DAYS)

        try:
            states_dict = await get_instance(self.hass).async_add_executor_job(
                get_significant_states,
                self.hass,
                start,
                end,
                [load_entity],
            )
        except Exception as err:
            LOGGER.warning("Failed to fetch load history: %s", err)
            return

        entity_states = states_dict.get(load_entity, [])
        history: list[tuple[datetime, float]] = []
        for state in entity_states:
            if state.state in ("unknown", "unavailable"):
                continue
            try:
                value_kw = float(state.state) / 1000.0  # sensor reports W
                local_time = dt_util.as_local(state.last_changed)
                history.append((local_time, value_kw))
            except (ValueError, TypeError):
                continue

        if history:
            self._load_forecaster.update_profile(history)
            LOGGER.info(
                "Load profile updated from %d readings (%d days)",
                len(history), LOAD_HISTORY_DAYS,
            )
        else:
            LOGGER.debug("No load history available yet, using defaults")

    @staticmethod
    def _get_current_rate(hour: int, tariff: dict) -> float:
        """Get the import rate for the current hour from tariff config."""
        for slot in tariff.values():
            start_h, end_h = slot["hours"]
            if start_h <= hour < end_h:
                return slot["price"]
        return 0.1  # fallback

    def _get_sensor_value(self, entity_id: str, default: float = 0.0) -> float:
        """Read a numeric sensor value from HA state."""
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return default
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return default

    async def _apply_action(self, action: str, power_w: float) -> None:
        """Send charge/discharge command to the inverter."""
        inverter_type = self._config.get("inverter_type", "generic")

        if inverter_type == "goodwe":
            await self._apply_goodwe(action, power_w)
        else:
            await self._apply_generic(action, power_w)

    async def _apply_goodwe(self, action: str, power_w: float) -> None:
        """GoodWe inverter control via the HA goodwe integration.

        Auto-discovers available GoodWe entities and uses them:
        - select.goodwe_inverter_operation_mode: eco_charge / general
        - number.goodwe_eco_mode_power: charge power as % of rated (0-100)
        - number.goodwe_eco_mode_soc: target SoC for eco charge (0-100)

        Mode mapping:
        - charge  -> set eco_mode_power + eco_mode_soc, then eco_charge
        - discharge/idle -> general (self-use, battery covers house load)
        """
        mode_entity = self._config.get("goodwe_operation_mode_entity_id")
        if not mode_entity:
            LOGGER.warning("GoodWe operation mode entity not configured")
            return

        # Use explicitly configured GoodWe entities (no auto-discovery)
        power_pct_entity = self._config.get("goodwe_eco_mode_power_entity_id")
        soc_target_entity = self._config.get("goodwe_eco_mode_soc_entity_id")
        dod_entity = self._config.get("goodwe_dod_entity_id")

        # Sync inverter DoD with MPC min_soc so they agree on discharge floor.
        # MPC min_soc=10% means DoD=90% (inverter uses DoD = 100 - min_soc).
        if dod_entity and self._entity_exists(dod_entity):
            target_dod = 100 - self._config.get("min_soc", 10)
            current_dod = self._get_sensor_value(dod_entity, default=-1)
            if current_dod != target_dod:
                LOGGER.info("GoodWe: depth_of_discharge %d%% -> %d%%", current_dod, target_dod)
                await self._set_number(dod_entity, target_dod)

        if action == "charge":
            if power_pct_entity and self._entity_exists(power_pct_entity):
                # Safety: leave headroom for house consumption so we don't
                # exceed the contracted grid import limit (5.5 kW typical).
                grid_limit_w = self._config.get("max_grid_import_kw", 5.5) * 1000
                load_entity = self._config.get("load_sensor_entity_id")
                current_load_w = 0.0
                if load_entity:
                    current_load_w = self._get_sensor_value(load_entity, default=1500.0)
                else:
                    current_load_w = 1500.0  # safe default
                safe_charge_w = max(0, min(power_w, grid_limit_w - current_load_w))

                # Read actual battery charge power for PI feedback
                actual_charge_w: float | None = None
                batt_entity = self._config.get("battery_power_entity_id")
                if batt_entity and self._last_action == "charge":
                    raw = self._get_sensor_value(batt_entity, default=float("nan"))
                    if not (raw != raw):  # not NaN
                        # GoodWe: negative = charging. We want charge rate as positive.
                        actual_charge_w = abs(raw)

                # PI controller computes corrected eco_mode_power %
                target_pct = self._power_pi.compute(safe_charge_w, actual_charge_w)

                current_pct = self._get_sensor_value(power_pct_entity, default=-1)
                if current_pct != target_pct:
                    LOGGER.info(
                        "GoodWe: eco_mode_power %d%% -> %d%% "
                        "(LP=%.0fW, safe=%.0fW, actual=%s, load=%.0fW)",
                        current_pct, target_pct,
                        power_w, safe_charge_w,
                        f"{actual_charge_w:.0f}W" if actual_charge_w is not None else "n/a",
                        current_load_w,
                    )
                    await self._set_number(power_pct_entity, target_pct)

            # Set target SoC to 100% for charging
            if soc_target_entity and self._entity_exists(soc_target_entity):
                current_soc_target = self._get_sensor_value(soc_target_entity, default=-1)
                if current_soc_target != 100:
                    await self._set_number(soc_target_entity, 100)

            target_mode = "eco_charge"
        else:
            # Reset PI when leaving charge mode
            if self._last_action == "charge":
                self._power_pi.reset()
            target_mode = "general"

        self._last_action = action

        # Only send mode command if it actually needs to change
        current_state = self.hass.states.get(mode_entity)
        if current_state and current_state.state == target_mode:
            return

        LOGGER.info(
            "GoodWe: %s -> '%s' (LP wants %.0fW)",
            mode_entity, target_mode, power_w,
        )

        try:
            await self.hass.services.async_call(
                "select", "select_option",
                {"entity_id": mode_entity, "option": target_mode},
                blocking=True,
            )
        except Exception as err:
            LOGGER.error("Failed to set GoodWe mode: %s", err)

    def _entity_exists(self, entity_id: str) -> bool:
        """Check if an entity exists in HA."""
        state = self.hass.states.get(entity_id)
        return state is not None and state.state not in ("unavailable",)

    async def _set_number(self, entity_id: str, value: float) -> None:
        """Set a number entity value."""
        try:
            await self.hass.services.async_call(
                "number", "set_value",
                {"entity_id": entity_id, "value": value},
                blocking=True,
            )
        except Exception as err:
            LOGGER.error("Failed to set %s to %s: %s", entity_id, value, err)

    async def _apply_generic(self, action: str, power_w: float) -> None:
        """Generic inverter control via HA switch/number entities."""
        charge_entity = self._config.get("charge_switch_entity_id")
        discharge_entity = self._config.get("discharge_switch_entity_id")
        power_entity = self._config.get("charge_power_entity_id")

        if action == "charge":
            if power_entity:
                await self.hass.services.async_call(
                    "number", "set_value",
                    {"entity_id": power_entity, "value": power_w},
                    blocking=True,
                )
            if charge_entity:
                await self.hass.services.async_call(
                    "switch", "turn_on",
                    {"entity_id": charge_entity},
                    blocking=True,
                )
            if discharge_entity:
                await self.hass.services.async_call(
                    "switch", "turn_off",
                    {"entity_id": discharge_entity},
                    blocking=True,
                )
        elif action == "discharge":
            if charge_entity:
                await self.hass.services.async_call(
                    "switch", "turn_off",
                    {"entity_id": charge_entity},
                    blocking=True,
                )
            if discharge_entity:
                await self.hass.services.async_call(
                    "switch", "turn_on",
                    {"entity_id": discharge_entity},
                    blocking=True,
                )
        else:  # idle
            if charge_entity:
                await self.hass.services.async_call(
                    "switch", "turn_off",
                    {"entity_id": charge_entity},
                    blocking=True,
                )
            if discharge_entity:
                await self.hass.services.async_call(
                    "switch", "turn_off",
                    {"entity_id": discharge_entity},
                    blocking=True,
                )
