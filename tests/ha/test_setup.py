"""Entry setup, unload and the options form with the deploy values."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest
from custom_components.battery_mpc import coordinator as coord_mod
from custom_components.battery_mpc.coordinator import BatteryMPCCoordinator
from custom_components.battery_mpc.forecast import SolarForecast
from custom_components.battery_mpc.solver import MpcResult
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from .test_coordinator import CONFIG, _set_states

N = 288


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def stubs(tmp_path):
    now = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    forecast = SolarForecast([now + timedelta(hours=h) for h in range(-24, 48)], [0.0] * 72)
    result = MpcResult(
        success=True, solve_time_ms=1.0, total_cost=0.0, charge=np.zeros(N), discharge=np.zeros(N),
        grid_import=np.zeros(N), grid_export=np.zeros(N), soc=np.full(N, 8.0),
        next_action="idle", next_power_w=0.0,
    )
    path = tmp_path / "factors.json"
    with (
        patch.object(coord_mod, "solve_mpc", lambda **_: result),
        patch.object(coord_mod, "fetch_solar_forecast", AsyncMock(return_value=forecast)),
        patch.object(BatteryMPCCoordinator, "_update_load_profile", AsyncMock()),
        patch.object(BatteryMPCCoordinator, "_calibration_path", lambda self: path),
    ):
        yield


async def _setup(hass: HomeAssistant, data: dict) -> MockConfigEntry:
    _set_states(hass)
    hass.services.async_register("select", "select_option", AsyncMock())
    hass.services.async_register("number", "set_value", AsyncMock())
    entry = MockConfigEntry(domain="battery_mpc", data=data)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_setup_registers_watchdog_and_unloads(hass):
    entry = await _setup(hass, CONFIG)
    assert entry.state is ConfigEntryState.LOADED
    coordinator = hass.data["battery_mpc"][entry.entry_id].coordinator

    coordinator._last_success = dt_util.utcnow() - timedelta(minutes=30)
    with patch.object(coordinator, "async_request_refresh", AsyncMock()):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=6))
        await hass.async_block_till_done()
    assert coordinator._stall_notified

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


@pytest.mark.parametrize("diag", ["sensor.diag_status", ""])
async def test_options_form_accepts_deploy_values(hass, diag):
    data = {k: v for k, v in CONFIG.items() if k != "goodwe_diag_status_entity_id"}
    data["tariff"] = {
        "valley": {"hours": [0, 8], "price": 0.122807},
        "shoulder_morning": {"hours": [8, 10], "price": 0.151795},
        "peak_morning": {"hours": [10, 14], "price": 0.255971},
        "shoulder_afternoon": {"hours": [14, 18], "price": 0.151795},
        "peak_evening": {"hours": [18, 22], "price": 0.255971},
        "shoulder_night": {"hours": [22, 24], "price": 0.151795},
    }
    data["efficiency"] = 0.95
    entry = await _setup(hass, data)
    hass.states.async_set("sensor.diag_status", "Discharge Driver On")

    flow = await hass.config_entries.options.async_init(entry.entry_id)
    assert flow["type"] is FlowResultType.FORM
    user_input = {
        "battery_capacity_kwh": 16.1, "max_charge_kw": 4.5, "max_discharge_kw": 4.6,
        "min_soc": 10, "efficiency": 98, "max_grid_import_kw": 5.0,
        "inverter_rated_power_kw": 4.6,
        "valley_price": 0.122807, "shoulder_price": 0.151795, "peak_price": 0.255971,
        "export_rate": 0.0509,
        "soc_sensor_entity_id": data["soc_sensor_entity_id"],
        "pv_power_entity_id": data["pv_power_entity_id"],
        "load_sensor_entity_id": data["load_sensor_entity_id"],
        "battery_power_entity_id": data["battery_power_entity_id"],
        "goodwe_operation_mode_entity_id": data["goodwe_operation_mode_entity_id"],
        "goodwe_eco_mode_power_entity_id": data["goodwe_eco_mode_power_entity_id"],
        "goodwe_eco_mode_soc_entity_id": data["goodwe_eco_mode_soc_entity_id"],
        "goodwe_dod_entity_id": data["goodwe_dod_entity_id"],
    }
    if diag:
        user_input["goodwe_diag_status_entity_id"] = diag
    result = await hass.config_entries.options.async_configure(flow["flow_id"], user_input)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY

    saved = hass.config_entries.async_get_entry(entry.entry_id).data
    assert saved["efficiency"] == pytest.approx(0.98)
    assert saved["battery_capacity_kwh"] == 16.1
    assert saved["max_charge_kw"] == 4.5 and saved["max_discharge_kw"] == 4.6
    assert saved["export_rate"] == 0.0509
    assert saved["tariff"]["peak_evening"]["price"] == 0.255971
    assert saved.get("goodwe_diag_status_entity_id", "") == diag
    assert entry.state is ConfigEntryState.LOADED


async def test_options_persist_deferrable_load(hass):
    entry = await _setup(hass, {**CONFIG, "deferrable_hold_mode": "off_grid"})
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    user_input = {
        "battery_capacity_kwh": 15.36, "max_charge_kw": 4.5, "max_discharge_kw": 4.6,
        "min_soc": 10, "efficiency": 98, "max_grid_import_kw": 5.0,
        "inverter_rated_power_kw": 4.6,
        "valley_price": 0.1228, "shoulder_price": 0.1519, "peak_price": 0.2558,
        "export_rate": 0.0509,
        "soc_sensor_entity_id": CONFIG["soc_sensor_entity_id"],
        "deferrable_load_entity_id": "sensor.deferrable_load",
        "deferrable_threshold_w": 250,
    }
    result = await hass.config_entries.options.async_configure(flow["flow_id"], user_input)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    saved = hass.config_entries.async_get_entry(entry.entry_id).data
    assert saved["deferrable_load_entity_id"] == "sensor.deferrable_load"
    assert "deferrable_hold_mode" not in saved
    assert saved["deferrable_threshold_w"] == 250
    assert saved["soc_sensor_entity_id"] == CONFIG["soc_sensor_entity_id"]


async def test_reconfigure_persists_deferrable_load_without_dropping_sensors(hass):
    entry = await _setup(hass, {**CONFIG, "deferrable_hold_mode": "backup"})
    flow = await hass.config_entries.flow.async_init(
        "battery_mpc",
        context={"source": "reconfigure", "entry_id": entry.entry_id},
    )
    assert flow["type"] is FlowResultType.FORM
    assert flow["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"],
        {
            "deferrable_load_entity_id": "sensor.deferrable_load",
            "deferrable_threshold_w": 200,
        },
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    saved = hass.config_entries.async_get_entry(entry.entry_id).data
    assert saved["deferrable_load_entity_id"] == "sensor.deferrable_load"
    assert "deferrable_hold_mode" not in saved
    assert saved["deferrable_threshold_w"] == 200
    assert saved["soc_sensor_entity_id"] == CONFIG["soc_sensor_entity_id"]
    assert saved["goodwe_operation_mode_entity_id"] == CONFIG["goodwe_operation_mode_entity_id"]


async def test_device_reports_manifest_version(hass):
    entry = await _setup(hass, CONFIG)
    from homeassistant.helpers import device_registry as dr

    device = next(iter(dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)))
    assert device.sw_version == "2.9.0"
    assert device.model == "MPC"


ALLOW_BATTERY = "binary_sensor.battery_assist"
ALLOW_DOMAINS = ["binary_sensor", "input_boolean", "switch"]


def _selector_domain(schema, name: str):
    for key, selector in schema.schema.items():
        if getattr(key, "schema", key) == name:
            return selector.config.get("domain")
    return None


async def test_setup_sensors_step_offers_allow_battery_entity(hass):
    flow = await hass.config_entries.flow.async_init("battery_mpc", context={"source": "user"})
    flow = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"latitude": 38.7, "longitude": -0.77},
    )
    flow = await hass.config_entries.flow.async_configure(
        flow["flow_id"],
        {
            "battery_capacity_kwh": 15.36, "max_charge_kw": 4.5, "max_discharge_kw": 4.6,
            "min_soc": 10, "efficiency": 98, "max_grid_import_kw": 5.0,
            "inverter_rated_power_kw": 4.6,
        },
    )
    flow = await hass.config_entries.flow.async_configure(
        flow["flow_id"],
        {"valley_price": 0.1228, "shoulder_price": 0.1519, "peak_price": 0.2558, "export_rate": 0.0509},
    )
    assert flow["step_id"] == "sensors"
    assert _selector_domain(flow["data_schema"], "deferrable_allow_battery_entity_id") == ALLOW_DOMAINS


async def test_options_persist_allow_battery_entity(hass):
    entry = await _setup(hass, CONFIG)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    assert _selector_domain(flow["data_schema"], "deferrable_allow_battery_entity_id") == ALLOW_DOMAINS
    user_input = {
        "battery_capacity_kwh": 15.36, "max_charge_kw": 4.5, "max_discharge_kw": 4.6,
        "min_soc": 10, "efficiency": 98, "max_grid_import_kw": 5.0,
        "inverter_rated_power_kw": 4.6,
        "valley_price": 0.1228, "shoulder_price": 0.1519, "peak_price": 0.2558,
        "export_rate": 0.0509,
        "soc_sensor_entity_id": CONFIG["soc_sensor_entity_id"],
        "deferrable_allow_battery_entity_id": ALLOW_BATTERY,
    }
    result = await hass.config_entries.options.async_configure(flow["flow_id"], user_input)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    saved = hass.config_entries.async_get_entry(entry.entry_id).data
    assert saved["deferrable_allow_battery_entity_id"] == ALLOW_BATTERY
    assert saved["soc_sensor_entity_id"] == CONFIG["soc_sensor_entity_id"]


async def test_reconfigure_persists_allow_battery_entity(hass):
    entry = await _setup(hass, CONFIG)
    flow = await hass.config_entries.flow.async_init(
        "battery_mpc",
        context={"source": "reconfigure", "entry_id": entry.entry_id},
    )
    assert flow["step_id"] == "reconfigure"
    assert _selector_domain(flow["data_schema"], "deferrable_allow_battery_entity_id") == ALLOW_DOMAINS
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"],
        {"deferrable_allow_battery_entity_id": ALLOW_BATTERY, "deferrable_threshold_w": 200},
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    saved = hass.config_entries.async_get_entry(entry.entry_id).data
    assert saved["deferrable_allow_battery_entity_id"] == ALLOW_BATTERY
    assert saved["soc_sensor_entity_id"] == CONFIG["soc_sensor_entity_id"]
