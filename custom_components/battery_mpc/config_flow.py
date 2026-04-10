"""Config flow for Battery MPC Controller."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback

from .const import DOMAIN


class BatteryMPCFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle config flow for Battery MPC Controller."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize."""
        self._data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None,
    ) -> config_entries.FlowResult:
        """Step 1: Location and API key."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_battery()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required("latitude", default=self.hass.config.latitude): vol.Coerce(float),
                vol.Required("longitude", default=self.hass.config.longitude): vol.Coerce(float),
                vol.Optional("open_meteo_api_key", default=""): str,
            }),
        )

    async def async_step_battery(
        self, user_input: dict[str, Any] | None = None,
    ) -> config_entries.FlowResult:
        """Step 2: Battery specifications."""
        if user_input is not None:
            user_input["efficiency"] = user_input["efficiency"] / 100.0
            self._data.update(user_input)
            return await self.async_step_sensors()

        return self.async_show_form(
            step_id="battery",
            data_schema=vol.Schema({
                vol.Required("battery_capacity_kwh", default=15.0): vol.Coerce(float),
                vol.Required("max_charge_kw", default=4.8): vol.Coerce(float),
                vol.Required("max_discharge_kw", default=4.8): vol.Coerce(float),
                vol.Required("min_soc", default=10): vol.Coerce(int),
                vol.Required("efficiency", default=95): vol.Coerce(int),
            }),
        )

    async def async_step_sensors(
        self, user_input: dict[str, Any] | None = None,
    ) -> config_entries.FlowResult:
        """Step 3: Sensors and inverter type."""
        if user_input is not None:
            self._data.update(user_input)
            if user_input.get("inverter_type") == "goodwe":
                return await self.async_step_inverter_goodwe()
            return await self.async_step_inverter_generic()

        return self.async_show_form(
            step_id="sensors",
            data_schema=vol.Schema({
                vol.Required("soc_sensor_entity_id"): str,
                vol.Optional("pv_power_entity_id", default=""): str,
                vol.Optional("load_sensor_entity_id", default=""): str,
                vol.Required("inverter_type", default="goodwe"): vol.In(
                    {"goodwe": "GoodWe", "generic": "Generic (switch/number)"}
                ),
            }),
        )

    async def async_step_inverter_goodwe(
        self, user_input: dict[str, Any] | None = None,
    ) -> config_entries.FlowResult:
        """Step 4: GoodWe inverter entity mapping."""
        if user_input is not None:
            self._data.update(user_input)
            return self.async_create_entry(
                title="Battery MPC (GoodWe)", data=self._data,
            )

        return self.async_show_form(
            step_id="inverter_goodwe",
            data_schema=vol.Schema({
                vol.Required("goodwe_operation_mode_entity_id"): str,
            }),
        )

    async def async_step_inverter_generic(
        self, user_input: dict[str, Any] | None = None,
    ) -> config_entries.FlowResult:
        """Step 4: Generic inverter entity mapping."""
        if user_input is not None:
            self._data.update(user_input)
            return self.async_create_entry(
                title="Battery MPC", data=self._data,
            )

        return self.async_show_form(
            step_id="inverter_generic",
            data_schema=vol.Schema({
                vol.Optional("charge_switch_entity_id", default=""): str,
                vol.Optional("discharge_switch_entity_id", default=""): str,
                vol.Optional("charge_power_entity_id", default=""): str,
            }),
        )
