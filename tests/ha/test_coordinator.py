"""Fail-safe behaviour of the coordinator against a real Home Assistant core."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
from custom_components.battery_mpc import coordinator as coord_mod
from custom_components.battery_mpc.coordinator import BatteryMPCCoordinator
from custom_components.battery_mpc.forecast import SolarForecast
from custom_components.battery_mpc.solver import MpcResult
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

MODE = "select.goodwe_inverter_operation_mode"
DIAG = "sensor.diag_status"
DEFERRABLE = "sensor.deferrable_load"
ALLOW_BATTERY = "binary_sensor.battery_assist"
ECO_POWER = "number.goodwe_eco_mode_power"
DOD = "number.goodwe_depth_of_discharge_on_grid"
CONFIG = {
    "latitude": 38.7, "longitude": -0.77,
    "battery_capacity_kwh": 15.36, "max_charge_kw": 4.5, "max_discharge_kw": 4.6,
    "min_soc": 10, "efficiency": 0.98, "max_grid_import_kw": 5.0,
    "inverter_rated_power_kw": 4.6, "export_rate": 0.0509,
    "soc_sensor_entity_id": "sensor.battery_state_of_charge",
    "pv_power_entity_id": "sensor.pv_power",
    "load_sensor_entity_id": "sensor.house_consumption",
    "battery_power_entity_id": "sensor.battery_power",
    "inverter_type": "goodwe",
    "goodwe_operation_mode_entity_id": MODE,
    "goodwe_eco_mode_power_entity_id": "number.goodwe_eco_mode_power",
    "goodwe_eco_mode_soc_entity_id": "number.goodwe_eco_mode_soc",
    "goodwe_dod_entity_id": "number.goodwe_depth_of_discharge_on_grid",
    "goodwe_diag_status_entity_id": DIAG,
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def calibration_file(tmp_path):
    path = tmp_path / "battery_mpc_solar_factors.json"
    with patch.object(BatteryMPCCoordinator, "_calibration_path", lambda self: path):
        yield path


def _result(action: str, power_w: float = 0.0, n: int = 288) -> MpcResult:
    charge = np.zeros(n)
    discharge = np.zeros(n)
    if action == "charge":
        charge[0] = power_w / 1000
    elif action == "discharge":
        discharge[0] = power_w / 1000
    return MpcResult(
        success=True, solve_time_ms=1.0, total_cost=0.0, charge=charge, discharge=discharge,
        grid_import=np.zeros(n), grid_export=np.zeros(n), soc=np.full(n, 8.0),
        next_action=action, next_power_w=power_w,
    )


class Plan:
    """Controls what the stubbed solver returns."""

    def __init__(self) -> None:
        self.action = "idle"
        self.power_w = 0.0
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return _result(self.action, self.power_w)


def _set_states(hass: HomeAssistant, **overrides: str) -> None:
    states = {
        "sensor.battery_state_of_charge": "55",
        "sensor.pv_power": "0",
        "sensor.house_consumption": "800",
        "sensor.battery_power": "0",
        "sensor.import_grid": "800",
        "sensor.export_grid": "0",
        MODE: "general",
        "number.goodwe_eco_mode_power": "10",
        "number.goodwe_eco_mode_soc": "100",
        "number.goodwe_depth_of_discharge_on_grid": "90",
        DIAG: "Discharge Driver On, Self-use load light",
    }
    states.update(overrides)
    for entity_id, state in states.items():
        hass.states.async_set(entity_id, state)


@pytest.fixture
async def setup(hass: HomeAssistant):
    _set_states(hass)
    entry = MockConfigEntry(domain="battery_mpc", data=CONFIG)
    entry.add_to_hass(hass)
    plan = Plan()
    now = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    forecast = SolarForecast([now + timedelta(hours=h) for h in range(-24, 48)], [0.0] * 72)
    with (
        patch.object(coord_mod, "solve_mpc", plan),
        patch.object(coord_mod, "fetch_solar_forecast", AsyncMock(return_value=forecast)),
        patch.object(BatteryMPCCoordinator, "_update_load_profile", AsyncMock()),
    ):
        coordinator = BatteryMPCCoordinator(hass, entry)
        await coordinator._async_setup()
        yield coordinator, plan


def _modes(calls: list[ServiceCall]) -> list[str]:
    return [c.data["option"] for c in calls]


async def test_soc_unavailable_does_not_plan_and_falls_back_to_general(hass, setup):
    coordinator, plan = setup
    selects = async_mock_service(hass, "select", "select_option")
    async_mock_service(hass, "number", "set_value")
    plan.action, plan.power_w = "charge", 3000
    await coordinator._async_update_data()
    assert _modes(selects) == ["eco_charge"]
    hass.states.async_set(MODE, "eco_charge")

    hass.states.async_set("sensor.battery_state_of_charge", "unavailable")
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    assert len(plan.calls) == 1
    assert _modes(selects) == ["eco_charge", "general"]


async def test_write_that_errors_but_lands_is_corrected(hass, setup):
    """2026-10-01: eco_charge write raised yet reached the inverter; HA kept showing general."""
    coordinator, plan = setup
    async_mock_service(hass, "number", "set_value")
    sent: list[str] = []

    async def flaky_select(call: ServiceCall) -> None:
        sent.append(call.data["option"])
        if call.data["option"] == "eco_charge":
            raise HomeAssistantError("SLAVE DEVICE FAILURE")

    hass.services.async_register("select", "select_option", flaky_select)

    plan.action, plan.power_w = "charge", 3000
    await coordinator._async_update_data()
    await coordinator._async_update_data()
    assert sent == ["eco_charge", "eco_charge"]

    plan.action, plan.power_w = "discharge", 1000
    await coordinator._async_update_data()
    assert sent[-1] == "general"


async def test_hanging_service_call_times_out_and_is_retried(hass, setup):
    coordinator, plan = setup
    sent: list[str] = []
    release = asyncio.Event()

    async def hanging_select(call: ServiceCall) -> None:
        sent.append(call.data["option"])
        await release.wait()

    hass.services.async_register("select", "select_option", hanging_select)
    hass.states.async_set(MODE, "eco_charge")
    with patch.object(coord_mod, "SERVICE_CALL_TIMEOUT_SECONDS", 0.05):
        await coordinator._async_update_data()
        assert coordinator._mode_unconfirmed
        await coordinator._async_update_data()
    assert sent == ["general", "general"]
    release.set()


async def test_hung_cycle_is_bounded_and_fails_safe(hass, setup):
    coordinator, plan = setup
    selects = async_mock_service(hass, "select", "select_option")
    hass.states.async_set(MODE, "eco_charge")

    async def never(*_args, **_kwargs):
        await asyncio.Event().wait()

    with (
        patch.object(coord_mod, "UPDATE_TIMEOUT_SECONDS", 0.05),
        patch.object(BatteryMPCCoordinator, "_async_run_cycle", never),
        pytest.raises(UpdateFailed),
    ):
        await coordinator._async_update_data()
    assert _modes(selects) == ["general"]


async def test_mode_not_resent_every_cycle_but_reasserted_hourly(hass, setup):
    coordinator, plan = setup
    selects = async_mock_service(hass, "select", "select_option")
    for _ in range(3):
        await coordinator._async_update_data()
    assert _modes(selects) == ["general"]

    coordinator._mode_sent_at -= timedelta(minutes=61)
    await coordinator._async_update_data()
    assert _modes(selects) == ["general", "general"]


async def test_mode_resent_after_inverter_comes_back(hass, setup):
    coordinator, plan = setup
    selects = async_mock_service(hass, "select", "select_option")
    await coordinator._async_update_data()
    hass.states.async_set(MODE, "unavailable")
    await coordinator._async_update_data()
    hass.states.async_set(MODE, "general")
    await coordinator._async_update_data()
    assert _modes(selects) == ["general", "general"]


async def test_diag_status_showing_timed_charge_forces_general(hass, setup):
    coordinator, plan = setup
    selects = async_mock_service(hass, "select", "select_option")
    await coordinator._async_update_data()
    hass.states.async_set(DIAG, "Charge time on, Self-use off")
    await coordinator._async_update_data()
    assert _modes(selects) == ["general"]

    coordinator._mode_sent_at -= timedelta(minutes=16)
    await coordinator._async_update_data()
    assert _modes(selects) == ["general", "general"]


async def test_watchdog_reverts_charge_mode_and_notifies(hass, setup):
    coordinator, plan = setup
    selects = async_mock_service(hass, "select", "select_option")
    async_mock_service(hass, "number", "set_value")
    plan.action, plan.power_w = "charge", 3000
    await coordinator._async_update_data()
    hass.states.async_set(MODE, "eco_charge")

    coordinator._last_success -= timedelta(minutes=25)
    with patch.object(BatteryMPCCoordinator, "async_request_refresh", AsyncMock()) as refresh:
        await coordinator.async_watchdog(dt_util.utcnow())
    await hass.async_block_till_done()
    assert _modes(selects) == ["eco_charge", "general"]
    refresh.assert_awaited_once()
    assert coordinator._stall_notified


async def test_missing_load_and_pv_use_forecast_for_first_step(hass, setup):
    coordinator, plan = setup
    async_mock_service(hass, "select", "select_option")
    hass.states.async_set("sensor.house_consumption", "unavailable")
    hass.states.async_set("sensor.pv_power", "unavailable")
    await coordinator._async_update_data()
    kwargs = plan.calls[-1]
    assert kwargs["load_forecast"][0] == pytest.approx(1.4)
    assert kwargs["solar_forecast"][0] == 0.0


async def test_holiday_is_priced_as_valley(hass, setup, freezer):
    coordinator, plan = setup
    async_mock_service(hass, "select", "select_option")
    freezer.move_to("2026-12-08T10:00:00+00:00")
    await coordinator._async_update_data()
    assert plan.calls[-1]["is_weekend"][:12].all()


async def test_calibration_accumulator_survives_restart(hass, setup):
    coordinator, plan = setup
    async_mock_service(hass, "select", "select_option")
    now = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    sunny = SolarForecast([now + timedelta(hours=h) for h in range(-24, 48)], [600.0] * 72)
    coordinator._solar_forecast = sunny
    hass.states.async_set("sensor.pv_power", "2500")
    await coordinator._async_update_data()
    await hass.async_block_till_done()
    assert coordinator._calibration.acc.daylight_cycles == 1

    entry = coordinator.config_entry
    restarted = BatteryMPCCoordinator(hass, entry)
    await restarted._async_setup()
    assert restarted._calibration.acc is not None
    assert restarted._calibration.acc.used_cycles == 1


def _with_deferrable(coordinator: BatteryMPCCoordinator, **extra: object) -> None:
    coordinator._config = {
        **coordinator._config,
        "deferrable_load_entity_id": DEFERRABLE,
        **extra,
    }


def _power_writes(calls: list[ServiceCall]) -> list[float]:
    return [c.data["value"] for c in calls if c.data["entity_id"] == ECO_POWER]


def _dod_writes(calls: list[ServiceCall]) -> list[float]:
    return [c.data["value"] for c in calls if c.data["entity_id"] == DOD]


async def test_idle_with_deferrable_above_threshold_raises_dod_and_stays_general(hass, setup):
    coordinator, plan = setup
    selects = async_mock_service(hass, "select", "select_option")
    numbers = async_mock_service(hass, "number", "set_value")
    _with_deferrable(coordinator)
    hass.states.async_set("sensor.battery_state_of_charge", "22")
    hass.states.async_set(DEFERRABLE, "3700", {"unit_of_measurement": "W"})
    plan.action = "idle"
    await coordinator._async_update_data()
    assert _modes(selects) == ["general"]
    assert _dod_writes(numbers) == [78]
    assert coordinator._deferrable_hold_active is True

    hass.states.async_set(DOD, "78")
    hass.states.async_set(MODE, "general")
    await coordinator._async_update_data()
    assert _modes(selects) == ["general"]
    assert _dod_writes(numbers) == [78]


async def test_idle_deferrable_drop_releases_dod_without_solving(hass, setup):
    coordinator, plan = setup
    selects = async_mock_service(hass, "select", "select_option")
    numbers = async_mock_service(hass, "number", "set_value")
    _with_deferrable(coordinator)
    hass.states.async_set("sensor.battery_state_of_charge", "22")
    hass.states.async_set(DEFERRABLE, "3700", {"unit_of_measurement": "W"})
    plan.action = "idle"
    await coordinator._async_update_data()
    assert _dod_writes(numbers) == [78]
    assert _modes(selects) == ["general"]

    hass.states.async_set(DOD, "78")
    coordinator.async_track_deferrable()
    solved = len(plan.calls)
    hass.states.async_set(DEFERRABLE, "0", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    assert len(plan.calls) == solved
    assert _dod_writes(numbers) == [78, 90]
    assert _modes(selects) == ["general"]
    assert coordinator._deferrable_hold_active is False


async def test_idle_with_deferrable_below_threshold_stays_general(hass, setup):
    coordinator, plan = setup
    selects = async_mock_service(hass, "select", "select_option")
    numbers = async_mock_service(hass, "number", "set_value")
    _with_deferrable(coordinator)
    hass.states.async_set("sensor.battery_state_of_charge", "22")
    hass.states.async_set(DOD, "78")
    hass.states.async_set(DEFERRABLE, "50", {"unit_of_measurement": "W"})
    plan.action = "idle"
    await coordinator._async_update_data()
    assert _modes(selects) == ["general"]
    assert _dod_writes(numbers) == [90]
    assert coordinator._deferrable_hold_active is False


async def test_discharge_with_deferrable_above_threshold_releases_dod(hass, setup):
    coordinator, plan = setup
    selects = async_mock_service(hass, "select", "select_option")
    numbers = async_mock_service(hass, "number", "set_value")
    _with_deferrable(coordinator)
    hass.states.async_set("sensor.battery_state_of_charge", "22")
    hass.states.async_set(DOD, "78")
    hass.states.async_set(DEFERRABLE, "3700", {"unit_of_measurement": "W"})
    plan.action, plan.power_w = "discharge", 1000
    await coordinator._async_update_data()
    assert _modes(selects) == ["general"]
    assert _dod_writes(numbers) == [90]
    assert coordinator._deferrable_hold_active is False


async def test_battery_charge_has_priority_over_the_deferrable_load(hass, setup):
    coordinator, plan = setup
    selects = async_mock_service(hass, "select", "select_option")
    numbers = async_mock_service(hass, "number", "set_value")
    _with_deferrable(coordinator)
    hass.states.async_set("sensor.house_consumption", "4500")
    hass.states.async_set(DEFERRABLE, "3700", {"unit_of_measurement": "W"})
    plan.action, plan.power_w = "charge", 3000
    await coordinator._async_update_data()
    assert _modes(selects) == ["eco_charge"]
    # The EV yields: 5000 - (4500 - 3700) = 4200 W of room covers the 3000 W plan (65%).
    assert _power_writes(numbers) == [65]


async def test_lp_receives_house_load_minus_deferrable(hass, setup):
    coordinator, plan = setup
    async_mock_service(hass, "select", "select_option")
    _with_deferrable(coordinator)
    hass.states.async_set("sensor.house_consumption", "4500")
    hass.states.async_set(DEFERRABLE, "3700", {"unit_of_measurement": "W"})
    data = await coordinator._async_update_data()
    assert plan.calls[-1]["load_forecast"][0] == pytest.approx(0.8)
    assert data["forecast_load"] == 800


async def test_kw_deferrable_sensor_is_scaled_to_watts(hass, setup):
    coordinator, plan = setup
    selects = async_mock_service(hass, "select", "select_option")
    numbers = async_mock_service(hass, "number", "set_value")
    _with_deferrable(coordinator)
    hass.states.async_set("sensor.battery_state_of_charge", "22")
    hass.states.async_set(DEFERRABLE, "3.7", {"unit_of_measurement": "kW"})
    plan.action = "idle"
    await coordinator._async_update_data()
    assert _modes(selects) == ["general"]
    assert _dod_writes(numbers) == [78]


async def test_charge_with_deferrable_keeps_release_dod(hass, setup):
    coordinator, plan = setup
    selects = async_mock_service(hass, "select", "select_option")
    numbers = async_mock_service(hass, "number", "set_value")
    _with_deferrable(coordinator)
    hass.states.async_set("sensor.battery_state_of_charge", "22")
    hass.states.async_set(DOD, "78")
    hass.states.async_set(DEFERRABLE, "3700", {"unit_of_measurement": "W"})
    plan.action, plan.power_w = "charge", 3000
    await coordinator._async_update_data()
    assert _modes(selects) == ["eco_charge"]
    assert _dod_writes(numbers) == [90]
    assert coordinator._deferrable_hold_active is False


async def test_idle_deferrable_without_dod_entity_does_not_hold(hass, setup):
    coordinator, plan = setup
    selects = async_mock_service(hass, "select", "select_option")
    numbers = async_mock_service(hass, "number", "set_value")
    _with_deferrable(coordinator)
    coordinator._config.pop("goodwe_dod_entity_id")
    hass.states.async_set("sensor.battery_state_of_charge", "22")
    hass.states.async_set(DEFERRABLE, "3700", {"unit_of_measurement": "W"})
    plan.action = "idle"
    data = await coordinator._async_update_data()
    assert _modes(selects) == ["general"]
    assert _dod_writes(numbers) == []
    assert data["deferrable_hold_active"] is False


async def test_deferrable_listener_registered_only_when_configured(hass, setup):
    coordinator, _plan = setup
    assert coordinator._deferrable_unsub is None
    entry = MockConfigEntry(
        domain="battery_mpc",
        data={**CONFIG, "deferrable_load_entity_id": DEFERRABLE},
    )
    entry.add_to_hass(hass)
    armed = BatteryMPCCoordinator(hass, entry)
    assert armed._deferrable_unsub is not None
    await armed.async_shutdown()
    assert armed._deferrable_unsub is None


async def test_deferrable_threshold_cross_is_debounced(hass, setup, freezer):
    coordinator, plan = setup
    numbers = async_mock_service(hass, "number", "set_value")
    async_mock_service(hass, "select", "select_option")
    _with_deferrable(coordinator)
    hass.states.async_set("sensor.battery_state_of_charge", "22")
    hass.states.async_set(DEFERRABLE, "0", {"unit_of_measurement": "W"})
    plan.action = "idle"
    await coordinator._async_update_data()
    coordinator.async_track_deferrable()
    solved = len(plan.calls)

    hass.states.async_set(DEFERRABLE, "3700", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    assert _dod_writes(numbers) == [78]
    assert len(plan.calls) == solved

    hass.states.async_set(DOD, "78")
    hass.states.async_set(DEFERRABLE, "0", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    assert _dod_writes(numbers) == [78]

    later = dt_util.utcnow() + timedelta(seconds=31)
    freezer.move_to(later)
    async_fire_time_changed(hass, later)
    await hass.async_block_till_done()
    assert _dod_writes(numbers) == [78, 90]

    freezer.move_to(later + timedelta(seconds=31))
    hass.states.async_set(DOD, "90")
    hass.states.async_set(DEFERRABLE, "3700", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    assert _dod_writes(numbers) == [78, 90, 78]
    assert len(plan.calls) == solved


async def test_deferrable_cross_during_actuation_applies_after(hass, setup):
    coordinator, plan = setup
    numbers = async_mock_service(hass, "number", "set_value")
    async_mock_service(hass, "select", "select_option")
    _with_deferrable(coordinator)
    hass.states.async_set("sensor.battery_state_of_charge", "22")
    hass.states.async_set(DEFERRABLE, "0", {"unit_of_measurement": "W"})
    plan.action = "idle"
    await coordinator._async_update_data()
    coordinator.async_track_deferrable()
    solved = len(plan.calls)

    async with coordinator._actuate():
        hass.states.async_set(DEFERRABLE, "3700", {"unit_of_measurement": "W"})
        await hass.async_block_till_done()
        assert _dod_writes(numbers) == []
        assert coordinator._deferrable_cross_pending is True

    assert _dod_writes(numbers) == [78]
    assert coordinator._deferrable_cross_pending is False
    assert len(plan.calls) == solved


async def test_absent_deferrable_key_keeps_full_load_and_general(hass, setup):
    coordinator, plan = setup
    selects = async_mock_service(hass, "select", "select_option")
    hass.states.async_set("sensor.house_consumption", "4500")
    plan.action = "idle"
    data = await coordinator._async_update_data()
    assert "deferrable_load_entity_id" not in coordinator._config
    assert coordinator._deferrable_unsub is None
    assert _modes(selects) == ["general"]
    assert plan.calls[-1]["load_forecast"][0] == pytest.approx(4.5)
    assert data["forecast_load"] == 4500


async def test_fail_safe_forces_general_while_deferrable_is_high(hass, setup):
    coordinator, plan = setup
    selects = async_mock_service(hass, "select", "select_option")
    _with_deferrable(coordinator)
    hass.states.async_set(DEFERRABLE, "3700", {"unit_of_measurement": "W"})
    await coordinator._async_update_data()
    hass.states.async_set(MODE, "backup")

    hass.states.async_set("sensor.battery_state_of_charge", "unavailable")
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    assert _modes(selects) == ["general", "general"]


async def test_load_history_subtracts_deferrable(hass):
    _set_states(hass)
    entry = MockConfigEntry(
        domain="battery_mpc",
        data={**CONFIG, "deferrable_load_entity_id": DEFERRABLE},
    )
    entry.add_to_hass(hass)
    coordinator = BatteryMPCCoordinator(hass, entry)
    when = dt_util.utcnow().replace(minute=15, second=0, microsecond=0)
    house = MagicMock(state="4500", last_changed=when, attributes={"unit_of_measurement": "W"})
    deferrable = MagicMock(state="3.7", last_changed=when, attributes={"unit_of_measurement": "kW"})
    seen: dict[str, list[str]] = {}

    def fake_history(_hass, _start, _end, entity_ids, *_args, **_kwargs):
        seen["ids"] = list(entity_ids)
        return {
            "sensor.house_consumption": [house],
            DEFERRABLE: [deferrable],
        }

    instance = MagicMock()

    async def run(func, *args):
        return func(*args)

    instance.async_add_executor_job = run
    with (
        patch("homeassistant.components.recorder.get_instance", return_value=instance),
        patch(
            "homeassistant.components.recorder.history.get_significant_states",
            fake_history,
        ),
    ):
        await coordinator._update_load_profile()

    local = dt_util.as_local(when)
    assert seen["ids"] == ["sensor.house_consumption", DEFERRABLE]
    assert coordinator._load_forecaster._profile[(local.hour, local.weekday() >= 5)] == pytest.approx(0.8)


async def test_next_action_exposes_deferrable_attributes(hass, setup):
    from custom_components.battery_mpc.sensor import SENSOR_DESCRIPTIONS, BatteryMPCSensor

    coordinator, plan = setup
    async_mock_service(hass, "select", "select_option")
    _with_deferrable(coordinator)
    hass.states.async_set("sensor.house_consumption", "4500")
    hass.states.async_set(DEFERRABLE, "3700", {"unit_of_measurement": "W"})
    plan.action = "idle"
    coordinator.data = await coordinator._async_update_data()
    sensor = BatteryMPCSensor(coordinator, SENSOR_DESCRIPTIONS[0])
    attrs = sensor.extra_state_attributes
    assert attrs["deferrable_load_w"] == 3700
    assert attrs["deferrable_hold_active"] is True
    assert attrs["deferrable_battery_allowed"] is False
    hold_at = list(attrs).index("deferrable_hold_active")
    assert list(attrs).index("deferrable_battery_allowed") == hold_at + 1
    assert "schedule" in attrs


async def test_fail_safe_and_watchdog_release_a_held_dod(hass, setup):
    coordinator, plan = setup
    async_mock_service(hass, "select", "select_option")
    numbers = async_mock_service(hass, "number", "set_value")
    _with_deferrable(coordinator)
    hass.states.async_set("sensor.battery_state_of_charge", "22")
    hass.states.async_set(DEFERRABLE, "3700", {"unit_of_measurement": "W"})
    plan.action = "idle"
    await coordinator._async_update_data()
    assert _dod_writes(numbers) == [78]
    hass.states.async_set(DOD, "78")

    with patch.object(
        BatteryMPCCoordinator, "_async_run_cycle", AsyncMock(side_effect=RuntimeError("boom")),
    ), pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    assert _dod_writes(numbers) == [78, 90]

    hass.states.async_set(DOD, "78")
    coordinator._last_success -= timedelta(minutes=25)
    with patch.object(BatteryMPCCoordinator, "async_request_refresh", AsyncMock()):
        await coordinator.async_watchdog(dt_util.utcnow())
    await hass.async_block_till_done()
    assert _dod_writes(numbers) == [78, 90, 90]


async def test_idle_deferrable_with_allow_battery_on_returns_dod_to_release(hass, setup):
    coordinator, plan = setup
    selects = async_mock_service(hass, "select", "select_option")
    numbers = async_mock_service(hass, "number", "set_value")
    _with_deferrable(coordinator, deferrable_allow_battery_entity_id=ALLOW_BATTERY)
    hass.states.async_set("sensor.battery_state_of_charge", "22")
    hass.states.async_set(DOD, "78")
    hass.states.async_set(DEFERRABLE, "3700", {"unit_of_measurement": "W"})
    hass.states.async_set(ALLOW_BATTERY, "on")
    plan.action = "idle"
    data = await coordinator._async_update_data()
    assert _modes(selects) == ["general"]
    assert _dod_writes(numbers) == [90]
    assert coordinator._deferrable_hold_active is False
    assert data["deferrable_battery_allowed"] is True
    assert coordinator._deferrable_should_hold("charge") is False
    assert coordinator._deferrable_should_hold("discharge") is False


async def test_allow_battery_on_releases_held_dod_without_solving(hass, setup):
    coordinator, plan = setup
    numbers = async_mock_service(hass, "number", "set_value")
    async_mock_service(hass, "select", "select_option")
    _with_deferrable(coordinator, deferrable_allow_battery_entity_id=ALLOW_BATTERY)
    hass.states.async_set("sensor.battery_state_of_charge", "22")
    hass.states.async_set(DEFERRABLE, "3700", {"unit_of_measurement": "W"})
    hass.states.async_set(ALLOW_BATTERY, "off")
    plan.action = "idle"
    await coordinator._async_update_data()
    assert _dod_writes(numbers) == [78]
    hass.states.async_set(DOD, "78")
    coordinator.async_track_deferrable()
    solved = len(plan.calls)
    hass.states.async_set(ALLOW_BATTERY, "on")
    await hass.async_block_till_done()
    assert len(plan.calls) == solved
    assert _dod_writes(numbers) == [78, 90]
    assert coordinator._deferrable_hold_active is False


async def test_allow_battery_off_reholds_without_solving(hass, setup):
    coordinator, plan = setup
    numbers = async_mock_service(hass, "number", "set_value")
    async_mock_service(hass, "select", "select_option")
    _with_deferrable(coordinator, deferrable_allow_battery_entity_id=ALLOW_BATTERY)
    hass.states.async_set("sensor.battery_state_of_charge", "22")
    hass.states.async_set(DEFERRABLE, "3700", {"unit_of_measurement": "W"})
    hass.states.async_set(ALLOW_BATTERY, "on")
    plan.action = "idle"
    await coordinator._async_update_data()
    hass.states.async_set(DOD, "90")
    coordinator.async_track_deferrable()
    solved = len(plan.calls)
    before = _dod_writes(numbers)
    assert before == []
    hass.states.async_set(ALLOW_BATTERY, "off")
    await hass.async_block_till_done()
    assert len(plan.calls) == solved
    assert _dod_writes(numbers) == [*before, 78]
    assert coordinator._deferrable_hold_active is True


async def test_absent_allow_battery_key_still_holds(hass, setup):
    coordinator, plan = setup
    numbers = async_mock_service(hass, "number", "set_value")
    async_mock_service(hass, "select", "select_option")
    _with_deferrable(coordinator)
    hass.states.async_set("sensor.battery_state_of_charge", "22")
    hass.states.async_set(DEFERRABLE, "3700", {"unit_of_measurement": "W"})
    plan.action = "idle"
    data = await coordinator._async_update_data()
    assert "deferrable_allow_battery_entity_id" not in coordinator._config
    assert getattr(coordinator, "_deferrable_allow_unsub", None) is None
    assert _dod_writes(numbers) == [78]
    assert data["deferrable_hold_active"] is True
    assert data.get("deferrable_battery_allowed") is False


@pytest.mark.parametrize("state", ["unavailable", "unknown"])
async def test_unavailable_allow_battery_is_off_and_holds(hass, setup, state):
    coordinator, plan = setup
    numbers = async_mock_service(hass, "number", "set_value")
    async_mock_service(hass, "select", "select_option")
    _with_deferrable(coordinator, deferrable_allow_battery_entity_id=ALLOW_BATTERY)
    hass.states.async_set("sensor.battery_state_of_charge", "22")
    hass.states.async_set(DEFERRABLE, "3700", {"unit_of_measurement": "W"})
    hass.states.async_set(ALLOW_BATTERY, state)
    plan.action = "idle"
    data = await coordinator._async_update_data()
    assert _dod_writes(numbers) == [78]
    assert coordinator._deferrable_hold_active is True
    assert data.get("deferrable_battery_allowed") is False


async def test_allow_battery_listener_unsubscribes_on_shutdown(hass):
    _set_states(hass)
    entry = MockConfigEntry(
        domain="battery_mpc",
        data={**CONFIG, "deferrable_allow_battery_entity_id": ALLOW_BATTERY},
    )
    entry.add_to_hass(hass)
    armed = BatteryMPCCoordinator(hass, entry)
    assert armed._deferrable_allow_unsub is not None
    await armed.async_shutdown()
    assert armed._deferrable_allow_unsub is None


async def test_battery_assist_overrides_eco_charge_with_general(hass, setup):
    coordinator, plan = setup
    selects = async_mock_service(hass, "select", "select_option")
    numbers = async_mock_service(hass, "number", "set_value")
    _with_deferrable(coordinator, deferrable_allow_battery_entity_id=ALLOW_BATTERY)
    hass.states.async_set("sensor.battery_state_of_charge", "19")
    hass.states.async_set(DEFERRABLE, "4400", {"unit_of_measurement": "W"})
    hass.states.async_set(ALLOW_BATTERY, "on")
    plan.action, plan.power_w = "charge", 3000
    await coordinator._async_update_data()
    assert _modes(selects) == ["general"]
    assert _power_writes(numbers) == []


async def test_battery_assist_without_deferrable_draw_keeps_eco_charge(hass, setup):
    coordinator, plan = setup
    selects = async_mock_service(hass, "select", "select_option")
    async_mock_service(hass, "number", "set_value")
    _with_deferrable(coordinator, deferrable_allow_battery_entity_id=ALLOW_BATTERY)
    hass.states.async_set(DEFERRABLE, "0", {"unit_of_measurement": "W"})
    hass.states.async_set(ALLOW_BATTERY, "on")
    plan.action, plan.power_w = "charge", 3000
    await coordinator._async_update_data()
    assert _modes(selects) == ["eco_charge"]


async def test_battery_assist_flip_switches_eco_charge_to_general_without_solving(hass, setup):
    coordinator, plan = setup
    selects = async_mock_service(hass, "select", "select_option")
    async_mock_service(hass, "number", "set_value")
    _with_deferrable(coordinator, deferrable_allow_battery_entity_id=ALLOW_BATTERY)
    hass.states.async_set(DEFERRABLE, "4400", {"unit_of_measurement": "W"})
    hass.states.async_set(ALLOW_BATTERY, "off")
    plan.action, plan.power_w = "charge", 3000
    await coordinator._async_update_data()
    assert _modes(selects) == ["eco_charge"]
    hass.states.async_set(MODE, "eco_charge")
    coordinator.async_track_deferrable()
    solved = len(plan.calls)

    hass.states.async_set(ALLOW_BATTERY, "on")
    await hass.async_block_till_done()
    assert _modes(selects) == ["eco_charge", "general"]
    assert len(plan.calls) == solved


async def test_battery_assist_lowers_the_floor_to_the_assist_min_soc(hass, setup):
    coordinator, plan = setup
    async_mock_service(hass, "select", "select_option")
    numbers = async_mock_service(hass, "number", "set_value")
    _with_deferrable(
        coordinator,
        deferrable_allow_battery_entity_id=ALLOW_BATTERY,
        deferrable_assist_min_soc=5,
    )
    hass.states.async_set("sensor.battery_state_of_charge", "13")
    hass.states.async_set(DEFERRABLE, "4800", {"unit_of_measurement": "W"})
    hass.states.async_set(ALLOW_BATTERY, "on")
    plan.action, plan.power_w = "charge", 1800
    await coordinator._async_update_data()
    assert _dod_writes(numbers) == [95]

    hass.states.async_set(DOD, "95")
    hass.states.async_set(ALLOW_BATTERY, "off")
    await coordinator._async_update_data()
    assert _dod_writes(numbers) == [95, 90]
