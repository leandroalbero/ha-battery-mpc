"""DataUpdateCoordinator for Battery MPC — runs optimization every 5 minutes."""

from __future__ import annotations

import asyncio
import math
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import numpy as np
from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .calibration import CalibrationState, finalize_day, record_sample
from .const import (
    DEFAULT_DEFERRABLE_THRESHOLD_W,
    DEFAULT_EXPORT_RATE,
    DEFAULT_MAX_GRID_IMPORT_KW,
    DEFAULT_MONTHLY_FACTORS,
    DEFAULT_TARIFF,
    DEFERRABLE_REACT_SECONDS,
    DOMAIN,
    FORECAST_REFRESH_MINUTES,
    LOAD_HISTORY_DAYS,
    LOGGER,
    MODE_REASSERT_MINUTES,
    MPC_HORIZON_HOURS,
    MPC_STEP_MINUTES,
    MPC_UPDATE_INTERVAL_MINUTES,
    SERVICE_CALL_TIMEOUT_SECONDS,
    SOLAR_CALIBRATION_FILE,
    UPDATE_TIMEOUT_SECONDS,
    WATCHDOG_STALE_MINUTES,
)
from .forecast import LoadForecaster, SolarForecast, fetch_solar_forecast
from .pid import PowerPI
from .solver import solve_mpc
from .tariff import offpeak_mask, rate_at, step_times

UNAVAILABLE_STATES = ("unknown", "unavailable")
STALLED_NOTIFICATION_ID = f"{DOMAIN}_stalled"


def _state_is_on(state) -> bool:
    return state is not None and state.state not in UNAVAILABLE_STATES and state.state == "on"


def _state_watts(state) -> float | None:
    """Power sensor reading in watts. Missing unit is treated as W, same as other reads."""
    if state is None or state.state in UNAVAILABLE_STATES:
        return None
    try:
        value = float(state.state)
    except (ValueError, TypeError):
        return None
    unit = state.attributes.get("unit_of_measurement")
    if isinstance(unit, str) and unit.lower() == "kw":
        return value * 1000.0
    return value


def _net_load_history(
    house_states: list,
    deferrable_states: list,
) -> list[tuple[datetime, float]]:
    steps: list[tuple[datetime, float]] = []
    for state in deferrable_states:
        watts = _state_watts(state)
        if watts is None:
            continue
        steps.append((state.last_changed, max(0.0, watts)))
    steps.sort(key=lambda item: item[0])

    history: list[tuple[datetime, float]] = []
    index = 0
    active_w = 0.0
    for state in house_states:
        if state.state in UNAVAILABLE_STATES:
            continue
        try:
            house_kw = float(state.state) / 1000.0
        except (ValueError, TypeError):
            continue
        while index < len(steps) and steps[index][0] <= state.last_changed:
            active_w = steps[index][1]
            index += 1
        history.append((
            dt_util.as_local(state.last_changed),
            max(0.0, house_kw - active_w / 1000.0),
        ))
    return history


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
        self._today: date | None = None
        self._load_profile_updated = False
        self._power_pi = PowerPI(
            rated_power_w=self._config.get("inverter_rated_power_kw", 4.8) * 1000,
        )
        self._last_action: str = "idle"
        self._calibration = CalibrationState(dict(DEFAULT_MONTHLY_FACTORS), None)
        self._actuate_lock = asyncio.Lock()
        # HA's select can disagree with the inverter after a write that errored but landed.
        self._commanded_mode: str | None = None
        self._mode_sent_at: datetime | None = None
        self._mode_unconfirmed = False
        self._last_success: datetime | None = None
        self.sw_version: str | None = None
        self._stall_notified = False
        self._deferrable_hold_active = False
        self._deferrable_unsub = None
        self._deferrable_allow_unsub = None
        self._deferrable_retry_unsub = None
        self._deferrable_reacted_at: datetime | None = None
        self._deferrable_cross_pending = False
        self._cycle_active = False
        self._actuate_held = False
        self.async_track_deferrable()

    async def _async_setup(self) -> None:
        text = await self.hass.async_add_executor_job(self._read_calibration_file)
        if text is not None:
            self._calibration = CalibrationState.from_json(text, DEFAULT_MONTHLY_FACTORS)
            LOGGER.info("Loaded solar calibration: %s", self._calibration.factors)

    async def _async_update_data(self) -> dict[str, Any]:
        self._cycle_active = True
        try:
            try:
                async with asyncio.timeout(UPDATE_TIMEOUT_SECONDS):
                    data = await self._async_run_cycle()
            except Exception as err:
                await self._async_fail_safe()
                await self._flush_deferred_deferrable()
                raise UpdateFailed(f"MPC optimization failed: {err!r}") from err
            await self._flush_deferred_deferrable(data)
            self._last_success = dt_util.utcnow()
            if self._stall_notified:
                persistent_notification.async_dismiss(self.hass, STALLED_NOTIFICATION_ID)
                self._stall_notified = False
            return data
        finally:
            self._cycle_active = False

    async def _async_run_cycle(self) -> dict[str, Any]:
        now_utc = dt_util.utcnow()
        now = dt_util.as_local(now_utc)

        if self._today != now.date():
            self._today = now.date()
            self._cost_savings_today = 0.0
            self._cost_actual_today = 0.0
            self._cost_baseline_today = 0.0
            self._load_profile_updated = False

        if not self._load_profile_updated:
            await self._update_load_profile()
            self._load_profile_updated = True

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
                    monthly_factors=self._calibration.factors,
                )
                if new_forecast and len(new_forecast._timestamps) > 0:
                    self._solar_forecast = new_forecast
                else:
                    LOGGER.warning("Empty forecast returned, keeping previous")
            except Exception as err:
                LOGGER.warning("Forecast fetch failed, using cached: %s", err)

        if self._solar_forecast is None:
            self._solar_forecast = SolarForecast([], [])

        current_soc_pct = self._read(self._config["soc_sensor_entity_id"])
        if current_soc_pct is None:
            raise RuntimeError("battery SoC sensor unavailable")
        battery_cap = self._config["battery_capacity_kwh"]
        current_soc_kwh = current_soc_pct / 100.0 * battery_cap

        current_load_kw = self._read_kw(self._config.get("load_sensor_entity_id"))
        planning_load_kw = current_load_kw
        if current_load_kw is not None and self._config.get("deferrable_load_entity_id"):
            planning_load_kw = max(0.0, current_load_kw - self._deferrable_load_w() / 1000.0)
        current_pv_kw = self._read_kw(self._config.get("pv_power_entity_id"))
        grid_import_w = self._read("sensor.import_grid")
        grid_export_w = self._read("sensor.export_grid")

        tariff = self._config.get("tariff", DEFAULT_TARIFF)
        export_rate = self._config.get("export_rate", DEFAULT_EXPORT_RATE)
        interval_hours = MPC_UPDATE_INTERVAL_MINUTES / 60.0

        if None not in (current_load_kw, current_pv_kw, grid_import_w, grid_export_w):
            current_rate = rate_at(now, tariff)
            actual_cost = (grid_import_w / 1000.0 * current_rate
                           - grid_export_w / 1000.0 * export_rate) * interval_hours
            net_load_kw = max(0.0, current_load_kw - current_pv_kw)
            baseline_cost = net_load_kw * current_rate * interval_hours
            self._cost_actual_today += actual_cost
            self._cost_baseline_today += baseline_cost
            self._cost_savings_today = self._cost_baseline_today - self._cost_actual_today
            self._cost_savings_lifetime += baseline_cost - actual_cost

        await self._update_solar_calibration(
            now, current_soc_pct, current_pv_kw, grid_export_w, interval_hours,
        )

        n_steps = MPC_HORIZON_HOURS * 60 // MPC_STEP_MINUTES
        times = step_times(now_utc, n_steps, MPC_STEP_MINUTES, dt_util.get_default_time_zone())
        solar_fc = self._solar_forecast.get_pv_forecast(times)
        load_fc = self._load_forecaster.forecast(times)
        if current_pv_kw is not None:
            solar_fc[0] = current_pv_kw
        if planning_load_kw is not None:
            load_fc[0] = planning_load_kw

        hours = np.array([t.hour for t in times])
        offpeak = offpeak_mask(times)

        dt_h = MPC_STEP_MINUTES / 60.0
        today_mask = np.array([t.date() == now.date() for t in times])
        solar_remaining_kwh = float(np.sum(solar_fc[today_mask])) * dt_h

        result = await self.hass.async_add_executor_job(
            partial(
                solve_mpc,
                solar_forecast=solar_fc,
                load_forecast=load_fc,
                hours=hours,
                tariff_schedule=tariff,
                export_rate=export_rate,
                dt_hours=dt_h,
                battery_capacity=battery_cap,
                max_charge_rate=self._config["max_charge_kw"],
                max_discharge_rate=self._config["max_discharge_kw"],
                efficiency=self._config.get("efficiency", 0.95),
                current_soc_kwh=current_soc_kwh,
                min_soc_frac=self._config["min_soc"] / 100.0,
                max_grid_import=self._config.get("max_grid_import_kw", DEFAULT_MAX_GRID_IMPORT_KW),
                max_grid_export=self._config.get("max_grid_export_kw", 5.0),
                is_weekend=offpeak,
            )
        )

        if not result.success:
            LOGGER.warning("MPC solve failed, keeping idle")

        await self._apply_action(
            result.next_action, result.next_power_w,
            current_pv_w=(current_pv_kw or 0.0) * 1000,
            current_load_w=(current_load_kw or 0.0) * 1000,
        )

        step_per_hour = 60 // MPC_STEP_MINUTES
        schedule = []
        for i in range(0, n_steps, step_per_hour):
            schedule.append({
                "time": times[i].strftime("%H:%M"),
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
            "solar_remaining_today_kwh": round(solar_remaining_kwh, 2),
            "deferrable_load_w": round(self._deferrable_load_w()),
            "deferrable_hold_active": self._deferrable_hold_active,
            "deferrable_battery_allowed": self._deferrable_battery_allowed(),
        }

    async def _async_fail_safe(self) -> None:
        if self._last_action == "charge":
            self._power_pi.reset()
        self._last_action = "idle"
        if self._config.get("inverter_type", "generic") == "goodwe":
            await self._ensure_goodwe_mode("general")
            await self._sync_goodwe_dod("fail_safe")

    async def async_watchdog(self, _now: datetime) -> None:
        if self._last_success is None:
            return
        stalled_for = dt_util.utcnow() - self._last_success
        if stalled_for < timedelta(minutes=WATCHDOG_STALE_MINUTES):
            return
        LOGGER.warning("No successful MPC cycle for %s", stalled_for)
        if self._config.get("inverter_type", "generic") == "goodwe":
            await self._ensure_goodwe_mode("general")
            await self._sync_goodwe_dod("fail_safe")
        if not self._stall_notified:
            persistent_notification.async_create(
                self.hass,
                f"No successful optimisation since "
                f"{dt_util.as_local(self._last_success):%Y-%m-%d %H:%M}. "
                "The inverter was set to self-consumption (general).",
                title="Battery MPC stalled",
                notification_id=STALLED_NOTIFICATION_ID,
            )
            self._stall_notified = True
        await self.async_request_refresh()

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
        deferrable_entity = self._config.get("deferrable_load_entity_id") or None
        entities = [load_entity]
        if deferrable_entity:
            entities.append(deferrable_entity)

        try:
            states_dict = await get_instance(self.hass).async_add_executor_job(
                get_significant_states,
                self.hass,
                start,
                end,
                entities,
            )
        except Exception as err:
            LOGGER.warning("Failed to fetch load history: %s", err)
            return

        entity_states = states_dict.get(load_entity, [])
        if deferrable_entity:
            history = _net_load_history(entity_states, states_dict.get(deferrable_entity, []))
            if history:
                self._load_forecaster.update_profile(history)
                LOGGER.info(
                    "Load profile updated from %d readings (%d days)",
                    len(history), LOAD_HISTORY_DAYS,
                )
            else:
                LOGGER.debug("No load history available yet, using defaults")
            return

        history: list[tuple[datetime, float]] = []
        for state in entity_states:
            if state.state in UNAVAILABLE_STATES:
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

    def _read(self, entity_id: str | None) -> float | None:
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in UNAVAILABLE_STATES:
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return None

    def _read_kw(self, entity_id: str | None) -> float | None:
        value = self._read(entity_id)
        return None if value is None else value / 1000.0

    def _deferrable_load_w(self) -> float:
        entity_id = self._config.get("deferrable_load_entity_id")
        if not entity_id:
            return 0.0
        watts = _state_watts(self.hass.states.get(entity_id))
        return 0.0 if watts is None else max(0.0, watts)

    def _deferrable_threshold_w(self) -> float:
        raw = self._config.get("deferrable_threshold_w", DEFAULT_DEFERRABLE_THRESHOLD_W)
        try:
            return float(raw)
        except (TypeError, ValueError):
            return float(DEFAULT_DEFERRABLE_THRESHOLD_W)

    def async_track_deferrable(self) -> None:
        if self._deferrable_unsub is None:
            entity_id = self._config.get("deferrable_load_entity_id")
            if entity_id:
                self._deferrable_unsub = async_track_state_change_event(
                    self.hass, entity_id, self._deferrable_state_changed,
                )
        if self._deferrable_allow_unsub is None:
            allow_id = self._config.get("deferrable_allow_battery_entity_id")
            if allow_id:
                self._deferrable_allow_unsub = async_track_state_change_event(
                    self.hass, allow_id, self._deferrable_allow_changed,
                )

    def _battery_assist_active(self) -> bool:
        return (
            self._deferrable_battery_allowed()
            and self._deferrable_load_w() > self._deferrable_threshold_w()
        )

    def _deferrable_battery_allowed(self) -> bool:
        entity_id = self._config.get("deferrable_allow_battery_entity_id")
        if not entity_id:
            return False
        return _state_is_on(self.hass.states.get(entity_id))

    def _deferrable_should_hold(self, action: str) -> bool:
        if not self._config.get("goodwe_dod_entity_id"):
            return False
        if self._deferrable_battery_allowed():
            return False
        return action == "idle" and self._deferrable_load_w() > self._deferrable_threshold_w()

    def _above_deferrable_threshold(self, state) -> bool:
        watts = _state_watts(state)
        return watts is not None and watts > self._deferrable_threshold_w()

    async def _sync_goodwe_dod(self, action: str) -> None:
        dod_entity = self._config.get("goodwe_dod_entity_id")
        min_soc = self._config.get("min_soc", 10)
        # Backup mode on this ES opens a grid-charge window, so hold by raising
        # the on-grid discharge floor instead of changing operation mode.
        target_dod = 100 - min_soc
        holding = self._deferrable_should_hold(action)
        if holding:
            soc = self._read(self._config.get("soc_sensor_entity_id"))
            if soc is None:
                holding = False
            else:
                target_dod = 100 - max(min_soc, math.floor(soc))
        self._deferrable_hold_active = holding
        if not dod_entity:
            return
        current_dod = self._read(dod_entity)
        if current_dod is not None and current_dod != target_dod:
            LOGGER.info("GoodWe: depth_of_discharge %d%% -> %d%%", current_dod, target_dod)
            await self._set_number(dod_entity, target_dod)

    async def _deferrable_state_changed(self, event: Event) -> None:
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")
        if self._above_deferrable_threshold(old_state) == self._above_deferrable_threshold(new_state):
            return
        await self._queue_or_react_to_deferrable()

    async def _deferrable_allow_changed(self, event: Event) -> None:
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")
        if _state_is_on(old_state) == _state_is_on(new_state):
            return
        await self._queue_or_react_to_deferrable()

    async def _queue_or_react_to_deferrable(self) -> None:
        if self._cycle_active or self._actuate_lock.locked():
            self._deferrable_cross_pending = True
            return
        async with self._actuate():
            await self._react_to_deferrable_cross()
        self._publish_deferrable_attrs()

    async def _react_to_deferrable_cross(self) -> None:
        now = dt_util.utcnow()
        window = timedelta(seconds=DEFERRABLE_REACT_SECONDS)
        if self._deferrable_reacted_at is not None and now - self._deferrable_reacted_at < window:
            # Dropping the cross would leave a stale hold (or none) until the next
            # LP cycle, up to 5 min; re-sync once the window ends instead.
            if self._deferrable_retry_unsub is None:
                delay = (self._deferrable_reacted_at + window - now).total_seconds()
                self._deferrable_retry_unsub = async_call_later(
                    self.hass, delay, self._deferrable_retry,
                )
            return
        self._deferrable_reacted_at = now
        await self._sync_goodwe_dod(self._last_action)
        if self._config.get("inverter_type", "generic") != "goodwe":
            return
        if self._battery_assist_active():
            await self._ensure_goodwe_mode("general")
        elif self._last_action == "charge" and self._commanded_mode == "general":
            # Assist just ended mid-plan; re-solve so eco_charge comes back with its power.
            await self.async_request_refresh()

    async def _deferrable_retry(self, _now: datetime) -> None:
        self._deferrable_retry_unsub = None
        await self._queue_or_react_to_deferrable()

    async def _flush_deferred_deferrable(self, data: dict[str, Any] | None = None) -> None:
        while self._deferrable_cross_pending:
            self._deferrable_cross_pending = False
            async with self._actuate():
                await self._react_to_deferrable_cross()
            if data is not None:
                data["deferrable_hold_active"] = self._deferrable_hold_active
                data["deferrable_load_w"] = round(self._deferrable_load_w())
                data["deferrable_battery_allowed"] = self._deferrable_battery_allowed()

    def _publish_deferrable_attrs(self) -> None:
        if not self.data:
            return
        self.data["deferrable_load_w"] = round(self._deferrable_load_w())
        self.data["deferrable_hold_active"] = self._deferrable_hold_active
        self.data["deferrable_battery_allowed"] = self._deferrable_battery_allowed()
        self.async_update_listeners()

    @asynccontextmanager
    async def _actuate(self):
        if self._actuate_held:
            yield
            return
        async with self._actuate_lock:
            self._actuate_held = True
            try:
                yield
            finally:
                while self._deferrable_cross_pending:
                    self._deferrable_cross_pending = False
                    await self._react_to_deferrable_cross()
                self._actuate_held = False

    async def async_shutdown(self) -> None:
        for name in ("_deferrable_unsub", "_deferrable_allow_unsub", "_deferrable_retry_unsub"):
            unsub = getattr(self, name)
            setattr(self, name, None)
            if unsub is not None:
                unsub()
        await super().async_shutdown()

    async def _apply_action(
        self, action: str, power_w: float, *,
        current_pv_w: float = 0.0, current_load_w: float = 0.0,
    ) -> None:
        """Send charge/discharge command to the inverter."""
        self._deferrable_hold_active = False
        inverter_type = self._config.get("inverter_type", "generic")

        async with self._actuate():
            if inverter_type == "goodwe":
                await self._apply_goodwe(action, power_w, current_pv_w, current_load_w)
            else:
                await self._apply_generic(action, power_w)

    async def _apply_goodwe(
        self, action: str, power_w: float,
        current_pv_w: float = 0.0, current_load_w: float = 0.0,
    ) -> None:
        """GoodWe inverter control via the HA goodwe integration.

        Mode mapping:
        - charge + solar surplus covers target → general (self-consumption,
          battery charges from surplus solar without pulling from grid)
        - charge + insufficient solar → eco_charge (grid-assisted charging)
        - discharge/idle → general
        """
        power_pct_entity = self._config.get("goodwe_eco_mode_power_entity_id")
        soc_target_entity = self._config.get("goodwe_eco_mode_soc_entity_id")
        lp_action = action
        if self._battery_assist_active():
            # eco_charge never discharges, so releasing the DoD alone left the
            # deferrable load on the grid while the battery kept charging.
            action = "discharge"
        await self._sync_goodwe_dod(action)

        if action == "charge":
            # eco_charge pulls from the grid even when solar surplus is available,
            # so general mode is used when the surplus alone covers the target.
            solar_surplus_w = max(0, current_pv_w - current_load_w)
            use_solar_only = solar_surplus_w >= power_w * 0.8 and current_pv_w > 100

            if use_solar_only:
                LOGGER.info(
                    "GoodWe: solar surplus %.0fW covers charge target %.0fW, "
                    "using general mode (no grid charging)",
                    solar_surplus_w, power_w,
                )
                if self._last_action == "charge":
                    self._power_pi.reset()
                target_mode = "general"
            else:
                current_pct = self._read(power_pct_entity)
                if current_pct is not None:
                    grid_limit_w = self._config.get(
                        "max_grid_import_kw", DEFAULT_MAX_GRID_IMPORT_KW,
                    ) * 1000
                    # Total house load, including deferrable: that draw keeps the grid budget.
                    load_w = self._read(self._config.get("load_sensor_entity_id"))
                    if load_w is None:
                        load_w = 1500.0
                    safe_charge_w = max(0, min(power_w, grid_limit_w - load_w))

                    actual_charge_w: float | None = None
                    if self._last_action == "charge":
                        raw = self._read(self._config.get("battery_power_entity_id"))
                        if raw is not None:
                            actual_charge_w = abs(raw)

                    target_pct = self._power_pi.compute(safe_charge_w, actual_charge_w)

                    if current_pct != target_pct:
                        LOGGER.info(
                            "GoodWe: eco_mode_power %d%% -> %d%% "
                            "(LP=%.0fW, safe=%.0fW, actual=%s, load=%.0fW)",
                            current_pct, target_pct,
                            power_w, safe_charge_w,
                            f"{actual_charge_w:.0f}W" if actual_charge_w is not None else "n/a",
                            load_w,
                        )
                        await self._set_number(power_pct_entity, target_pct)

                current_soc_target = self._read(soc_target_entity)
                if current_soc_target is not None and current_soc_target != 100:
                    await self._set_number(soc_target_entity, 100)

                target_mode = "eco_charge"
        else:
            if self._last_action == "charge":
                self._power_pi.reset()
            target_mode = "general"

        self._last_action = lp_action
        await self._ensure_goodwe_mode(target_mode, power_w)

    async def _ensure_goodwe_mode(self, target_mode: str, power_w: float = 0.0) -> None:
        mode_entity = self._config.get("goodwe_operation_mode_entity_id")
        if not mode_entity:
            LOGGER.warning("GoodWe operation mode entity not configured")
            return

        async with self._actuate():
            state = self.hass.states.get(mode_entity)
            if state is None or state.state in UNAVAILABLE_STATES:
                self._mode_unconfirmed = True
                return

            now = dt_util.utcnow()
            since_sent = None if self._mode_sent_at is None else now - self._mode_sent_at
            reasons = []
            if target_mode != self._commanded_mode:
                reasons.append("target changed")
            if state.state != target_mode:
                reasons.append(f"select reads {state.state}")
            if self._mode_unconfirmed:
                reasons.append("last write unconfirmed")
            if since_sent is None or since_sent >= timedelta(minutes=MODE_REASSERT_MINUTES):
                reasons.append("periodic re-assert")
            if (
                self._inverter_contradicts(target_mode)
                and since_sent is not None
                and since_sent >= timedelta(minutes=15)
            ):
                reasons.append("inverter status disagrees")
            if not reasons:
                return

            LOGGER.info(
                "GoodWe: %s -> '%s' (LP wants %.0fW; %s)",
                mode_entity, target_mode, power_w, ", ".join(reasons),
            )
            ok = await self._call_service(
                "select", "select_option", {"entity_id": mode_entity, "option": target_mode},
            )
            self._commanded_mode = target_mode
            self._mode_sent_at = now
            self._mode_unconfirmed = not ok

    def _inverter_contradicts(self, target_mode: str) -> bool:
        diag = self._config.get("goodwe_diag_status_entity_id")
        if not diag or target_mode != "general":
            return False
        state = self.hass.states.get(diag)
        return state is not None and "Charge time on" in state.state

    async def _call_service(self, domain: str, service: str, data: dict[str, Any]) -> bool:
        try:
            async with asyncio.timeout(SERVICE_CALL_TIMEOUT_SECONDS):
                await self.hass.services.async_call(domain, service, data, blocking=True)
        except Exception as err:
            LOGGER.error("%s.%s %s failed: %r", domain, service, data, err)
            return False
        return True

    async def _set_number(self, entity_id: str, value: float) -> None:
        await self._call_service("number", "set_value", {"entity_id": entity_id, "value": value})

    async def _apply_generic(self, action: str, power_w: float) -> None:
        """Generic inverter control via HA switch/number entities."""
        charge_entity = self._config.get("charge_switch_entity_id")
        discharge_entity = self._config.get("discharge_switch_entity_id")
        power_entity = self._config.get("charge_power_entity_id")

        if action == "charge" and power_entity:
            await self._set_number(power_entity, power_w)
        if charge_entity:
            service = "turn_on" if action == "charge" else "turn_off"
            await self._call_service("switch", service, {"entity_id": charge_entity})
        if discharge_entity:
            service = "turn_on" if action == "discharge" else "turn_off"
            await self._call_service("switch", service, {"entity_id": discharge_entity})

    # --- Solar calibration ---

    async def _update_solar_calibration(
        self,
        now: datetime,
        soc_pct: float,
        pv_kw: float | None,
        export_w: float | None,
        interval_hours: float,
    ) -> None:
        acc = self._calibration.acc
        if acc is not None and acc.day != now.date().isoformat():
            update = finalize_day(self._calibration, self._expected_daylight_cycles(acc.day))
            if update is not None:
                LOGGER.info("Solar calibration month=%d: %.3f -> %.3f", *update)
            await self._save_solar_calibration()

        if pv_kw is None or self._solar_forecast is None:
            return
        # A full battery or capped export throttles the panels and would bias the factor low.
        max_export_w = self._config.get("max_grid_export_kw", 5.0) * 1000
        curtailed = soc_pct >= 98.0 or (export_w is not None and export_w >= 0.95 * max_export_w)
        predicted_kw = float(self._solar_forecast.get_pv_forecast([now])[0])
        updates = record_sample(
            self._calibration, now.date(), predicted_kw, pv_kw, curtailed, interval_hours,
        )
        for update in updates:
            LOGGER.info("Solar calibration month=%d: %.3f -> %.3f", *update)
        if predicted_kw > 0.05 or updates:
            await self._save_solar_calibration()

    def _expected_daylight_cycles(self, day_iso: str) -> int | None:
        if self._solar_forecast is None:
            return None
        start = dt_util.start_of_local_day(date.fromisoformat(day_iso))
        times = step_times(
            dt_util.as_utc(start), 24 * 60 // MPC_UPDATE_INTERVAL_MINUTES,
            MPC_UPDATE_INTERVAL_MINUTES, dt_util.get_default_time_zone(),
        )
        expected = int(np.sum(self._solar_forecast.get_pv_forecast(times) > 0.05))
        return expected or None

    def _calibration_path(self) -> Path:
        return Path(self.hass.config.path(".storage")) / SOLAR_CALIBRATION_FILE

    def _read_calibration_file(self) -> str | None:
        path = self._calibration_path()
        try:
            return path.read_text() if path.exists() else None
        except OSError as err:
            LOGGER.warning("Failed to read solar calibration: %s", err)
            return None

    async def _save_solar_calibration(self) -> None:
        text = self._calibration.to_json()
        path = self._calibration_path()

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(text)
            tmp.replace(path)

        try:
            await self.hass.async_add_executor_job(_write)
        except OSError as err:
            LOGGER.warning("Failed to save solar calibration: %s", err)
