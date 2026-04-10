"""Config flow for Battery MPC Controller."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import DOMAIN


class BatteryMPCFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle config flow for Battery MPC Controller."""

    VERSION = 2

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> BatteryMPCOptionsFlowHandler:
        """Get the options flow handler."""
        return BatteryMPCOptionsFlowHandler(config_entry)

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
                vol.Required("latitude", default=self.hass.config.latitude): NumberSelector(
                    NumberSelectorConfig(min=-90, max=90, step=0.0001, mode="box"),
                ),
                vol.Required("longitude", default=self.hass.config.longitude): NumberSelector(
                    NumberSelectorConfig(min=-180, max=180, step=0.0001, mode="box"),
                ),
                vol.Optional("open_meteo_api_key", default=""): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD),
                ),
            }),
        )

    async def async_step_battery(
        self, user_input: dict[str, Any] | None = None,
    ) -> config_entries.FlowResult:
        """Step 2: Battery specifications."""
        if user_input is not None:
            user_input["efficiency"] = user_input["efficiency"] / 100.0
            self._data.update(user_input)
            return await self.async_step_tariff()

        return self.async_show_form(
            step_id="battery",
            data_schema=vol.Schema({
                vol.Required("battery_capacity_kwh", default=15.0): NumberSelector(
                    NumberSelectorConfig(min=0.5, max=200, step=0.1, unit_of_measurement="kWh", mode="box"),
                ),
                vol.Required("max_charge_kw", default=4.8): NumberSelector(
                    NumberSelectorConfig(min=0.1, max=50, step=0.1, unit_of_measurement="kW", mode="box"),
                ),
                vol.Required("max_discharge_kw", default=4.8): NumberSelector(
                    NumberSelectorConfig(min=0.1, max=50, step=0.1, unit_of_measurement="kW", mode="box"),
                ),
                vol.Required("min_soc", default=10): NumberSelector(
                    NumberSelectorConfig(min=0, max=50, step=1, unit_of_measurement="%", mode="box"),
                ),
                vol.Required("efficiency", default=95): NumberSelector(
                    NumberSelectorConfig(min=70, max=100, step=1, unit_of_measurement="%", mode="box"),
                ),
            }),
        )

    async def async_step_tariff(
        self, user_input: dict[str, Any] | None = None,
    ) -> config_entries.FlowResult:
        """Step 3: Electricity tariff (Spain 2.0TD time bands)."""
        if user_input is not None:
            vp = user_input["valley_price"]
            sp = user_input["shoulder_price"]
            pp = user_input["peak_price"]
            self._data["tariff"] = {
                "valley": {"hours": (0, 8), "price": vp},
                "shoulder_morning": {"hours": (8, 10), "price": sp},
                "peak_morning": {"hours": (10, 14), "price": pp},
                "shoulder_afternoon": {"hours": (14, 18), "price": sp},
                "peak_evening": {"hours": (18, 22), "price": pp},
                "shoulder_night": {"hours": (22, 24), "price": sp},
            }
            self._data["export_rate"] = user_input["export_rate"]
            return await self.async_step_sensors()

        return self.async_show_form(
            step_id="tariff",
            data_schema=vol.Schema({
                vol.Required("valley_price", default=0.085): NumberSelector(
                    NumberSelectorConfig(min=0, max=1, step=0.001, unit_of_measurement="EUR/kWh", mode="box"),
                ),
                vol.Required("shoulder_price", default=0.135): NumberSelector(
                    NumberSelectorConfig(min=0, max=1, step=0.001, unit_of_measurement="EUR/kWh", mode="box"),
                ),
                vol.Required("peak_price", default=0.197): NumberSelector(
                    NumberSelectorConfig(min=0, max=1, step=0.001, unit_of_measurement="EUR/kWh", mode="box"),
                ),
                vol.Required("export_rate", default=0.08): NumberSelector(
                    NumberSelectorConfig(min=0, max=1, step=0.001, unit_of_measurement="EUR/kWh", mode="box"),
                ),
            }),
        )

    async def async_step_sensors(
        self, user_input: dict[str, Any] | None = None,
    ) -> config_entries.FlowResult:
        """Step 4: Sensors and inverter type."""
        if user_input is not None:
            self._data.update(user_input)
            if user_input.get("inverter_type") == "goodwe":
                return await self.async_step_inverter_goodwe()
            return await self.async_step_inverter_generic()

        return self.async_show_form(
            step_id="sensors",
            data_schema=vol.Schema({
                vol.Required("soc_sensor_entity_id"): EntitySelector(
                    EntitySelectorConfig(domain="sensor", device_class="battery"),
                ),
                vol.Optional("pv_power_entity_id"): EntitySelector(
                    EntitySelectorConfig(domain="sensor", device_class="power"),
                ),
                vol.Optional("load_sensor_entity_id"): EntitySelector(
                    EntitySelectorConfig(domain="sensor", device_class="power"),
                ),
                vol.Required("inverter_type", default="goodwe"): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            {"value": "goodwe", "label": "GoodWe"},
                            {"value": "generic", "label": "Generic (switch/number)"},
                        ],
                        mode=SelectSelectorMode.DROPDOWN,
                    ),
                ),
            }),
        )

    async def async_step_inverter_goodwe(
        self, user_input: dict[str, Any] | None = None,
    ) -> config_entries.FlowResult:
        """Step 5: GoodWe inverter entity mapping."""
        if user_input is not None:
            self._data.update(user_input)
            return self.async_create_entry(
                title="Battery MPC (GoodWe)", data=self._data,
            )

        return self.async_show_form(
            step_id="inverter_goodwe",
            data_schema=vol.Schema({
                vol.Required("goodwe_operation_mode_entity_id"): EntitySelector(
                    EntitySelectorConfig(domain="select"),
                ),
            }),
        )

    async def async_step_inverter_generic(
        self, user_input: dict[str, Any] | None = None,
    ) -> config_entries.FlowResult:
        """Step 5: Generic inverter entity mapping."""
        if user_input is not None:
            self._data.update(user_input)
            return self.async_create_entry(
                title="Battery MPC", data=self._data,
            )

        return self.async_show_form(
            step_id="inverter_generic",
            data_schema=vol.Schema({
                vol.Optional("charge_switch_entity_id"): EntitySelector(
                    EntitySelectorConfig(domain="switch"),
                ),
                vol.Optional("discharge_switch_entity_id"): EntitySelector(
                    EntitySelectorConfig(domain="switch"),
                ),
                vol.Optional("charge_power_entity_id"): EntitySelector(
                    EntitySelectorConfig(domain="number"),
                ),
            }),
        )


class BatteryMPCOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow — edit settings after initial setup."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize."""
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None,
    ) -> config_entries.FlowResult:
        """Main options page."""
        if user_input is not None:
            # Merge into config entry data
            new_data = dict(self._config_entry.data)
            # Handle efficiency conversion
            if "efficiency" in user_input:
                user_input["efficiency"] = user_input["efficiency"] / 100.0
            # Rebuild tariff from prices
            if "valley_price" in user_input:
                new_data["tariff"] = {
                    "valley": {"hours": (0, 8), "price": user_input.pop("valley_price")},
                    "shoulder_morning": {"hours": (8, 10), "price": user_input.get("shoulder_price", 0.135)},
                    "peak_morning": {"hours": (10, 14), "price": user_input.get("peak_price", 0.197)},
                    "shoulder_afternoon": {"hours": (14, 18), "price": user_input.pop("shoulder_price", 0.135)},
                    "peak_evening": {"hours": (18, 22), "price": user_input.pop("peak_price", 0.197)},
                    "shoulder_night": {"hours": (22, 24), "price": new_data["tariff"]["shoulder_morning"]["price"]},
                }
                new_data["export_rate"] = user_input.pop("export_rate", 0.08)
            new_data.update(user_input)
            self.hass.config_entries.async_update_entry(self._config_entry, data=new_data)
            return self.async_create_entry(title="", data={})

        # Pre-fill with current values
        d = self._config_entry.data
        tariff = d.get("tariff", {})
        valley_price = tariff.get("valley", {}).get("price", 0.085)
        shoulder_price = tariff.get("shoulder_morning", {}).get("price", 0.135)
        peak_price = tariff.get("peak_morning", {}).get("price", 0.197)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                # Battery
                vol.Required("battery_capacity_kwh", default=d.get("battery_capacity_kwh", 15.0)): NumberSelector(
                    NumberSelectorConfig(min=0.5, max=200, step=0.1, unit_of_measurement="kWh", mode="box"),
                ),
                vol.Required("max_charge_kw", default=d.get("max_charge_kw", 4.8)): NumberSelector(
                    NumberSelectorConfig(min=0.1, max=50, step=0.1, unit_of_measurement="kW", mode="box"),
                ),
                vol.Required("max_discharge_kw", default=d.get("max_discharge_kw", 4.8)): NumberSelector(
                    NumberSelectorConfig(min=0.1, max=50, step=0.1, unit_of_measurement="kW", mode="box"),
                ),
                vol.Required("min_soc", default=d.get("min_soc", 10)): NumberSelector(
                    NumberSelectorConfig(min=0, max=50, step=1, unit_of_measurement="%", mode="box"),
                ),
                vol.Required("efficiency", default=round(d.get("efficiency", 0.95) * 100)): NumberSelector(
                    NumberSelectorConfig(min=70, max=100, step=1, unit_of_measurement="%", mode="box"),
                ),
                # Tariff
                vol.Required("valley_price", default=valley_price): NumberSelector(
                    NumberSelectorConfig(min=0, max=1, step=0.001, unit_of_measurement="EUR/kWh", mode="box"),
                ),
                vol.Required("shoulder_price", default=shoulder_price): NumberSelector(
                    NumberSelectorConfig(min=0, max=1, step=0.001, unit_of_measurement="EUR/kWh", mode="box"),
                ),
                vol.Required("peak_price", default=peak_price): NumberSelector(
                    NumberSelectorConfig(min=0, max=1, step=0.001, unit_of_measurement="EUR/kWh", mode="box"),
                ),
                vol.Required("export_rate", default=d.get("export_rate", 0.08)): NumberSelector(
                    NumberSelectorConfig(min=0, max=1, step=0.001, unit_of_measurement="EUR/kWh", mode="box"),
                ),
                # Sensors
                vol.Required("soc_sensor_entity_id", default=d.get("soc_sensor_entity_id", "")): EntitySelector(
                    EntitySelectorConfig(domain="sensor", device_class="battery"),
                ),
                vol.Optional("pv_power_entity_id", default=d.get("pv_power_entity_id", "")): EntitySelector(
                    EntitySelectorConfig(domain="sensor", device_class="power"),
                ),
                vol.Optional("load_sensor_entity_id", default=d.get("load_sensor_entity_id", "")): EntitySelector(
                    EntitySelectorConfig(domain="sensor", device_class="power"),
                ),
                # Inverter
                vol.Optional("goodwe_operation_mode_entity_id", default=d.get("goodwe_operation_mode_entity_id", "")): EntitySelector(
                    EntitySelectorConfig(domain="select"),
                ),
            }),
        )
