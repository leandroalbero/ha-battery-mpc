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

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Single-page setup."""
        if user_input is not None:
            vp = user_input.get("valley_price", 0.085)
            sp = user_input.get("shoulder_price", 0.135)
            pp = user_input.get("peak_price", 0.197)
            user_input["tariff"] = {
                "valley": {"hours": [0, 8], "price": vp},
                "shoulder_morning": {"hours": [8, 10], "price": sp},
                "peak_morning": {"hours": [10, 14], "price": pp},
                "shoulder_afternoon": {"hours": [14, 18], "price": sp},
                "peak_evening": {"hours": [18, 22], "price": pp},
                "shoulder_night": {"hours": [22, 24], "price": sp},
            }
            user_input["efficiency"] = user_input.get("efficiency", 95) / 100.0
            return self.async_create_entry(title="Battery MPC", data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                # Location
                vol.Required("latitude", default=self.hass.config.latitude): vol.Coerce(float),
                vol.Required("longitude", default=self.hass.config.longitude): vol.Coerce(float),
                vol.Optional("open_meteo_api_key", default=""): str,
                # Battery
                vol.Required("battery_capacity_kwh", default=15.0): vol.Coerce(float),
                vol.Required("max_charge_kw", default=4.8): vol.Coerce(float),
                vol.Required("max_discharge_kw", default=4.8): vol.Coerce(float),
                vol.Required("min_soc", default=10): vol.Coerce(int),
                vol.Required("efficiency", default=95): vol.Coerce(int),
                # Tariff (EUR/kWh)
                vol.Required("valley_price", default=0.085): vol.Coerce(float),
                vol.Required("shoulder_price", default=0.135): vol.Coerce(float),
                vol.Required("peak_price", default=0.197): vol.Coerce(float),
                vol.Required("export_rate", default=0.08): vol.Coerce(float),
                # Sensors — entity pickers
                vol.Required("soc_sensor_entity_id"): EntitySelector(
                    EntitySelectorConfig(domain="sensor"),
                ),
                vol.Optional("pv_power_entity_id"): EntitySelector(
                    EntitySelectorConfig(domain="sensor"),
                ),
                vol.Optional("load_sensor_entity_id"): EntitySelector(
                    EntitySelectorConfig(domain="sensor"),
                ),
                # Inverter
                vol.Required("inverter_type", default="goodwe"): vol.In(
                    {"goodwe": "GoodWe", "generic": "Generic"},
                ),
                vol.Optional("goodwe_operation_mode_entity_id"): EntitySelector(
                    EntitySelectorConfig(domain="select"),
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
                vol.Required("battery_capacity_kwh", default=d.get("battery_capacity_kwh", 15.0)): vol.Coerce(float),
                vol.Required("max_charge_kw", default=d.get("max_charge_kw", 4.8)): vol.Coerce(float),
                vol.Required("max_discharge_kw", default=d.get("max_discharge_kw", 4.8)): vol.Coerce(float),
                vol.Required("min_soc", default=d.get("min_soc", 10)): vol.Coerce(int),
                vol.Required("efficiency", default=round(d.get("efficiency", 0.95) * 100)): vol.Coerce(int),
                vol.Required("valley_price", default=tariff.get("valley", {}).get("price", 0.085)): vol.Coerce(float),
                vol.Required("shoulder_price", default=tariff.get("shoulder_morning", {}).get("price", 0.135)): vol.Coerce(float),
                vol.Required("peak_price", default=tariff.get("peak_morning", {}).get("price", 0.197)): vol.Coerce(float),
                vol.Required("export_rate", default=d.get("export_rate", 0.08)): vol.Coerce(float),
                vol.Required("soc_sensor_entity_id", default=d.get("soc_sensor_entity_id", "")): EntitySelector(
                    EntitySelectorConfig(domain="sensor"),
                ),
                vol.Optional("pv_power_entity_id", default=d.get("pv_power_entity_id", "")): EntitySelector(
                    EntitySelectorConfig(domain="sensor"),
                ),
                vol.Optional("load_sensor_entity_id", default=d.get("load_sensor_entity_id", "")): EntitySelector(
                    EntitySelectorConfig(domain="sensor"),
                ),
                vol.Optional("goodwe_operation_mode_entity_id", default=d.get("goodwe_operation_mode_entity_id", "")): EntitySelector(
                    EntitySelectorConfig(domain="select"),
                ),
            }),
        )
