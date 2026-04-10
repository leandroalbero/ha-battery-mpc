"""Config flow for Battery MPC Controller."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import selector

from .const import DOMAIN


STEP_LOCATION = vol.Schema({
    vol.Required("latitude"): selector.NumberSelector(
        selector.NumberSelectorConfig(min=-90, max=90, step=0.0001, mode="box"),
    ),
    vol.Required("longitude"): selector.NumberSelector(
        selector.NumberSelectorConfig(min=-180, max=180, step=0.0001, mode="box"),
    ),
    vol.Optional("open_meteo_api_key"): selector.TextSelector(
        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD),
    ),
})

STEP_BATTERY = vol.Schema({
    vol.Required("battery_capacity_kwh", default=15.0): selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=0.5, max=200, step=0.1, unit_of_measurement="kWh", mode="box",
        ),
    ),
    vol.Required("max_charge_kw", default=4.8): selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=0.1, max=50, step=0.1, unit_of_measurement="kW", mode="box",
        ),
    ),
    vol.Required("max_discharge_kw", default=4.8): selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=0.1, max=50, step=0.1, unit_of_measurement="kW", mode="box",
        ),
    ),
    vol.Required("min_soc", default=10): selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=0, max=50, step=1, unit_of_measurement="%", mode="box",
        ),
    ),
    vol.Required("efficiency", default=95): selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=70, max=100, step=1, unit_of_measurement="%", mode="box",
        ),
    ),
})

STEP_SENSORS = vol.Schema({
    vol.Required("soc_sensor_entity_id"): selector.EntitySelector(
        selector.EntitySelectorConfig(domain="sensor"),
    ),
    vol.Optional("pv_power_entity_id"): selector.EntitySelector(
        selector.EntitySelectorConfig(domain="sensor"),
    ),
    vol.Optional("load_sensor_entity_id"): selector.EntitySelector(
        selector.EntitySelectorConfig(domain="sensor"),
    ),
    vol.Required("inverter_type", default="goodwe"): selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[
                selector.SelectOptionDict(value="goodwe", label="GoodWe"),
                selector.SelectOptionDict(value="generic", label="Generic (switch/number entities)"),
            ],
            mode="dropdown",
        ),
    ),
})

STEP_INVERTER_GOODWE = vol.Schema({
    vol.Required("goodwe_operation_mode_entity_id"): selector.EntitySelector(
        selector.EntitySelectorConfig(domain="select"),
    ),
})

STEP_INVERTER_GENERIC = vol.Schema({
    vol.Optional("charge_switch_entity_id"): selector.EntitySelector(
        selector.EntitySelectorConfig(domain="switch"),
    ),
    vol.Optional("discharge_switch_entity_id"): selector.EntitySelector(
        selector.EntitySelectorConfig(domain="switch"),
    ),
    vol.Optional("charge_power_entity_id"): selector.EntitySelector(
        selector.EntitySelectorConfig(domain="number"),
    ),
})


class BatteryMPCFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle config flow for Battery MPC Controller."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Step 1: Location and API key."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_battery()

        suggested = {
            "latitude": self.hass.config.latitude,
            "longitude": self.hass.config.longitude,
        }
        schema = self.add_suggested_values_to_schema(STEP_LOCATION, suggested)
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_battery(
        self, user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Step 2: Battery specifications."""
        if user_input is not None:
            user_input["efficiency"] = user_input["efficiency"] / 100.0
            self._data.update(user_input)
            return await self.async_step_sensors()
        return self.async_show_form(step_id="battery", data_schema=STEP_BATTERY)

    async def async_step_sensors(
        self, user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Step 3: Sensors and inverter type."""
        if user_input is not None:
            self._data.update(user_input)
            if user_input["inverter_type"] == "goodwe":
                return await self.async_step_inverter_goodwe()
            return await self.async_step_inverter_generic()
        return self.async_show_form(step_id="sensors", data_schema=STEP_SENSORS)

    async def async_step_inverter_goodwe(
        self, user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Step 4: GoodWe inverter entity mapping."""
        if user_input is not None:
            self._data.update(user_input)
            return self.async_create_entry(
                title="Battery MPC (GoodWe)", data=self._data,
            )
        return self.async_show_form(
            step_id="inverter_goodwe", data_schema=STEP_INVERTER_GOODWE,
        )

    async def async_step_inverter_generic(
        self, user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Step 4: Generic inverter entity mapping."""
        if user_input is not None:
            self._data.update(user_input)
            return self.async_create_entry(
                title="Battery MPC", data=self._data,
            )
        return self.async_show_form(
            step_id="inverter_generic", data_schema=STEP_INVERTER_GENERIC,
        )
