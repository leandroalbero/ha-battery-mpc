"""Config flow for Battery MPC Controller."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import EntitySelector, EntitySelectorConfig

from .const import DOMAIN


class BatteryMPCFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle config flow for Battery MPC Controller."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Step 1: Location."""
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
    ) -> config_entries.ConfigFlowResult:
        """Step 2: Battery and grid specs."""
        if user_input is not None:
            user_input["efficiency"] = user_input["efficiency"] / 100.0
            self._data.update(user_input)
            return await self.async_step_tariff()

        return self.async_show_form(
            step_id="battery",
            data_schema=vol.Schema({
                vol.Required("battery_capacity_kwh", default=15.0): vol.Coerce(float),
                vol.Required("max_charge_kw", default=4.8): vol.Coerce(float),
                vol.Required("max_discharge_kw", default=4.8): vol.Coerce(float),
                vol.Required("min_soc", default=10): vol.Coerce(int),
                vol.Required("efficiency", default=95): vol.Coerce(int),
                vol.Required("max_grid_import_kw", default=5.5): vol.Coerce(float),
                vol.Required("inverter_rated_power_kw", default=4.8): vol.Coerce(float),
            }),
        )

    async def async_step_tariff(
        self, user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Step 3: Electricity tariff."""
        if user_input is not None:
            vp = user_input["valley_price"]
            sp = user_input["shoulder_price"]
            pp = user_input["peak_price"]
            self._data["tariff"] = {
                "valley": {"hours": [0, 8], "price": vp},
                "shoulder_morning": {"hours": [8, 10], "price": sp},
                "peak_morning": {"hours": [10, 14], "price": pp},
                "shoulder_afternoon": {"hours": [14, 18], "price": sp},
                "peak_evening": {"hours": [18, 22], "price": pp},
                "shoulder_night": {"hours": [22, 24], "price": sp},
            }
            self._data["export_rate"] = user_input["export_rate"]
            return await self.async_step_sensors()

        return self.async_show_form(
            step_id="tariff",
            data_schema=vol.Schema({
                vol.Required("valley_price", default=0.085): vol.Coerce(float),
                vol.Required("shoulder_price", default=0.135): vol.Coerce(float),
                vol.Required("peak_price", default=0.197): vol.Coerce(float),
                vol.Required("export_rate", default=0.08): vol.Coerce(float),
            }),
        )

    async def async_step_sensors(
        self, user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Step 4: Sensor entities."""
        if user_input is not None:
            self._data.update(user_input)
            if self._data.get("inverter_type") == "goodwe":
                return await self.async_step_inverter_goodwe()
            return await self.async_step_inverter_generic()

        return self.async_show_form(
            step_id="sensors",
            data_schema=vol.Schema({
                vol.Required("soc_sensor_entity_id"): EntitySelector(
                    EntitySelectorConfig(domain="sensor"),
                ),
                vol.Optional("pv_power_entity_id"): EntitySelector(
                    EntitySelectorConfig(domain="sensor"),
                ),
                vol.Optional("load_sensor_entity_id"): EntitySelector(
                    EntitySelectorConfig(domain="sensor"),
                ),
                vol.Optional("battery_power_entity_id"): EntitySelector(
                    EntitySelectorConfig(domain="sensor"),
                ),
                vol.Required("inverter_type", default="goodwe"): vol.In(
                    {"goodwe": "GoodWe", "generic": "Generic"},
                ),
            }),
        )

    def _find_goodwe_prefix(self) -> str:
        """Detect GoodWe entity prefix from existing HA entities."""
        for state in self.hass.states.async_all("select"):
            if state.entity_id.endswith("_inverter_operation_mode"):
                return state.entity_id.replace("select.", "").replace("_inverter_operation_mode", "")
        return "goodwe"

    async def async_step_inverter_goodwe(
        self, user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Step 5: GoodWe inverter entities — auto-detected defaults, user-overrideable."""
        if user_input is not None:
            self._data.update(user_input)
            return self.async_create_entry(title="Battery MPC (GoodWe)", data=self._data)

        # Auto-detect defaults from GoodWe entity prefix
        prefix = self._find_goodwe_prefix()

        return self.async_show_form(
            step_id="inverter_goodwe",
            data_schema=vol.Schema({
                vol.Required("goodwe_operation_mode_entity_id",
                             default=f"select.{prefix}_inverter_operation_mode"): EntitySelector(
                    EntitySelectorConfig(domain="select"),
                ),
                vol.Optional("goodwe_eco_mode_power_entity_id",
                             default=f"number.{prefix}_eco_mode_power"): EntitySelector(
                    EntitySelectorConfig(domain="number"),
                ),
                vol.Optional("goodwe_eco_mode_soc_entity_id",
                             default=f"number.{prefix}_eco_mode_soc"): EntitySelector(
                    EntitySelectorConfig(domain="number"),
                ),
                vol.Optional("goodwe_dod_entity_id",
                             default=f"number.{prefix}_depth_of_discharge_on_grid"): EntitySelector(
                    EntitySelectorConfig(domain="number"),
                ),
            }),
        )

    async def async_step_inverter_generic(
        self, user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Step 5: Generic inverter entities."""
        if user_input is not None:
            self._data.update(user_input)
            return self.async_create_entry(title="Battery MPC", data=self._data)

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

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> BatteryMPCOptionsFlowHandler:
        return BatteryMPCOptionsFlowHandler()


class BatteryMPCOptionsFlowHandler(config_entries.OptionsFlow):
    """Edit settings after setup."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            new_data = dict(self.config_entry.data)
            if "efficiency" in user_input:
                user_input["efficiency"] = user_input["efficiency"] / 100.0
            vp = user_input.pop("valley_price", None)
            sp = user_input.pop("shoulder_price", None)
            pp = user_input.pop("peak_price", None)
            if vp is not None:
                new_data["tariff"] = {
                    "valley": {"hours": [0, 8], "price": vp},
                    "shoulder_morning": {"hours": [8, 10], "price": sp},
                    "peak_morning": {"hours": [10, 14], "price": pp},
                    "shoulder_afternoon": {"hours": [14, 18], "price": sp},
                    "peak_evening": {"hours": [18, 22], "price": pp},
                    "shoulder_night": {"hours": [22, 24], "price": sp},
                }
            er = user_input.pop("export_rate", None)
            if er is not None:
                new_data["export_rate"] = er
            new_data.update(user_input)
            self.hass.config_entries.async_update_entry(self.config_entry, data=new_data)
            return self.async_create_entry(title="", data={})

        d = self.config_entry.data
        tariff = d.get("tariff", {})

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                # Battery & grid
                vol.Required("battery_capacity_kwh", default=d.get("battery_capacity_kwh", 15.0)): vol.Coerce(float),
                vol.Required("max_charge_kw", default=d.get("max_charge_kw", 4.8)): vol.Coerce(float),
                vol.Required("max_discharge_kw", default=d.get("max_discharge_kw", 4.8)): vol.Coerce(float),
                vol.Required("min_soc", default=d.get("min_soc", 10)): vol.Coerce(int),
                vol.Required("efficiency", default=round(d.get("efficiency", 0.95) * 100)): vol.Coerce(int),
                vol.Required("max_grid_import_kw", default=d.get("max_grid_import_kw", 5.5)): vol.Coerce(float),
                vol.Required("inverter_rated_power_kw", default=d.get("inverter_rated_power_kw", 4.6)): vol.Coerce(float),
                # Tariff
                vol.Required("valley_price", default=tariff.get("valley", {}).get("price", 0.085)): vol.Coerce(float),
                vol.Required("shoulder_price", default=tariff.get("shoulder_morning", {}).get("price", 0.135)): vol.Coerce(float),
                vol.Required("peak_price", default=tariff.get("peak_morning", {}).get("price", 0.197)): vol.Coerce(float),
                vol.Required("export_rate", default=d.get("export_rate", 0.08)): vol.Coerce(float),
                # Sensors
                vol.Required("soc_sensor_entity_id", default=d.get("soc_sensor_entity_id", "")): EntitySelector(
                    EntitySelectorConfig(domain="sensor"),
                ),
                vol.Optional("pv_power_entity_id", default=d.get("pv_power_entity_id", "")): EntitySelector(
                    EntitySelectorConfig(domain="sensor"),
                ),
                vol.Optional("load_sensor_entity_id", default=d.get("load_sensor_entity_id", "")): EntitySelector(
                    EntitySelectorConfig(domain="sensor"),
                ),
                vol.Optional("battery_power_entity_id", default=d.get("battery_power_entity_id", "")): EntitySelector(
                    EntitySelectorConfig(domain="sensor"),
                ),
                # GoodWe entities
                vol.Optional("goodwe_operation_mode_entity_id", default=d.get("goodwe_operation_mode_entity_id", "")): EntitySelector(
                    EntitySelectorConfig(domain="select"),
                ),
                vol.Optional("goodwe_eco_mode_power_entity_id", default=d.get("goodwe_eco_mode_power_entity_id", "")): EntitySelector(
                    EntitySelectorConfig(domain="number"),
                ),
                vol.Optional("goodwe_eco_mode_soc_entity_id", default=d.get("goodwe_eco_mode_soc_entity_id", "")): EntitySelector(
                    EntitySelectorConfig(domain="number"),
                ),
                vol.Optional("goodwe_dod_entity_id", default=d.get("goodwe_dod_entity_id", "")): EntitySelector(
                    EntitySelectorConfig(domain="number"),
                ),
            }),
        )
