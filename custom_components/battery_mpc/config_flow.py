"""Config flow for Battery MPC Controller."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries

from .const import DOMAIN

# Selector imports — graceful fallback if API changes across HA versions
try:
    from homeassistant.helpers.selector import (
        EntitySelector,
        EntitySelectorConfig,
        NumberSelector,
        NumberSelectorConfig,
        SelectSelector,
        SelectSelectorConfig,
        TextSelector,
        TextSelectorConfig,
        TextSelectorType,
    )
    _HAS_SELECTORS = True
except ImportError:
    _HAS_SELECTORS = False


def _entity(domain: str = "sensor", **kwargs: Any) -> Any:
    """Entity selector or plain string fallback."""
    if _HAS_SELECTORS:
        try:
            return EntitySelector(EntitySelectorConfig(domain=domain, **kwargs))
        except TypeError:
            # device_class kwarg may not exist in older HA
            return EntitySelector(EntitySelectorConfig(domain=domain))
    return str


def _number(min_val: float, max_val: float, step: float, unit: str = "", **kw: Any) -> Any:
    """Number selector or float fallback."""
    if _HAS_SELECTORS:
        try:
            return NumberSelector(NumberSelectorConfig(
                min=min_val, max=max_val, step=step,
                unit_of_measurement=unit or None, mode="box",
            ))
        except TypeError:
            return NumberSelector(NumberSelectorConfig(
                min=min_val, max=max_val, step=step,
            ))
    return vol.Coerce(float)


def _select(options: list[dict[str, str]]) -> Any:
    """Select dropdown or vol.In fallback."""
    if _HAS_SELECTORS:
        try:
            return SelectSelector(SelectSelectorConfig(options=options, mode="dropdown"))
        except TypeError:
            return SelectSelector(SelectSelectorConfig(options=options))
    return vol.In({o["value"]: o["label"] for o in options})


def _text_password() -> Any:
    """Password text field or plain string."""
    if _HAS_SELECTORS:
        try:
            return TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
        except (TypeError, AttributeError):
            return str
    return str


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
                vol.Required("latitude", default=self.hass.config.latitude): _number(-90, 90, 0.0001),
                vol.Required("longitude", default=self.hass.config.longitude): _number(-180, 180, 0.0001),
                vol.Optional("open_meteo_api_key", default=""): _text_password(),
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
                vol.Required("battery_capacity_kwh", default=15.0): _number(0.5, 200, 0.1, "kWh"),
                vol.Required("max_charge_kw", default=4.8): _number(0.1, 50, 0.1, "kW"),
                vol.Required("max_discharge_kw", default=4.8): _number(0.1, 50, 0.1, "kW"),
                vol.Required("min_soc", default=10): _number(0, 50, 1, "%"),
                vol.Required("efficiency", default=95): _number(70, 100, 1, "%"),
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
                vol.Required("valley_price", default=0.085): _number(0, 1, 0.001, "EUR/kWh"),
                vol.Required("shoulder_price", default=0.135): _number(0, 1, 0.001, "EUR/kWh"),
                vol.Required("peak_price", default=0.197): _number(0, 1, 0.001, "EUR/kWh"),
                vol.Required("export_rate", default=0.08): _number(0, 1, 0.001, "EUR/kWh"),
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
                vol.Required("soc_sensor_entity_id"): _entity("sensor"),
                vol.Optional("pv_power_entity_id"): _entity("sensor"),
                vol.Optional("load_sensor_entity_id"): _entity("sensor"),
                vol.Required("inverter_type", default="goodwe"): _select([
                    {"value": "goodwe", "label": "GoodWe"},
                    {"value": "generic", "label": "Generic (switch/number)"},
                ]),
            }),
        )

    async def async_step_inverter_goodwe(
        self, user_input: dict[str, Any] | None = None,
    ) -> config_entries.FlowResult:
        """Step 5: GoodWe inverter entity mapping."""
        if user_input is not None:
            self._data.update(user_input)
            return self.async_create_entry(title="Battery MPC (GoodWe)", data=self._data)

        return self.async_show_form(
            step_id="inverter_goodwe",
            data_schema=vol.Schema({
                vol.Required("goodwe_operation_mode_entity_id"): _entity("select"),
            }),
        )

    async def async_step_inverter_generic(
        self, user_input: dict[str, Any] | None = None,
    ) -> config_entries.FlowResult:
        """Step 5: Generic inverter entity mapping."""
        if user_input is not None:
            self._data.update(user_input)
            return self.async_create_entry(title="Battery MPC", data=self._data)

        return self.async_show_form(
            step_id="inverter_generic",
            data_schema=vol.Schema({
                vol.Optional("charge_switch_entity_id"): _entity("switch"),
                vol.Optional("discharge_switch_entity_id"): _entity("switch"),
                vol.Optional("charge_power_entity_id"): _entity("number"),
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
            new_data = dict(self._config_entry.data)
            if "efficiency" in user_input:
                user_input["efficiency"] = user_input["efficiency"] / 100.0
            if "valley_price" in user_input:
                vp = user_input.pop("valley_price")
                sp = user_input.pop("shoulder_price", 0.135)
                pp = user_input.pop("peak_price", 0.197)
                new_data["tariff"] = {
                    "valley": {"hours": [0, 8], "price": vp},
                    "shoulder_morning": {"hours": [8, 10], "price": sp},
                    "peak_morning": {"hours": [10, 14], "price": pp},
                    "shoulder_afternoon": {"hours": [14, 18], "price": sp},
                    "peak_evening": {"hours": [18, 22], "price": pp},
                    "shoulder_night": {"hours": [22, 24], "price": sp},
                }
                new_data["export_rate"] = user_input.pop("export_rate", 0.08)
            new_data.update(user_input)
            self.hass.config_entries.async_update_entry(self._config_entry, data=new_data)
            return self.async_create_entry(title="", data={})

        d = self._config_entry.data
        tariff = d.get("tariff", {})
        valley_price = tariff.get("valley", {}).get("price", 0.085)
        shoulder_price = tariff.get("shoulder_morning", {}).get("price", 0.135)
        peak_price = tariff.get("peak_morning", {}).get("price", 0.197)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required("battery_capacity_kwh", default=d.get("battery_capacity_kwh", 15.0)): _number(0.5, 200, 0.1, "kWh"),
                vol.Required("max_charge_kw", default=d.get("max_charge_kw", 4.8)): _number(0.1, 50, 0.1, "kW"),
                vol.Required("max_discharge_kw", default=d.get("max_discharge_kw", 4.8)): _number(0.1, 50, 0.1, "kW"),
                vol.Required("min_soc", default=d.get("min_soc", 10)): _number(0, 50, 1, "%"),
                vol.Required("efficiency", default=round(d.get("efficiency", 0.95) * 100)): _number(70, 100, 1, "%"),
                vol.Required("valley_price", default=valley_price): _number(0, 1, 0.001, "EUR/kWh"),
                vol.Required("shoulder_price", default=shoulder_price): _number(0, 1, 0.001, "EUR/kWh"),
                vol.Required("peak_price", default=peak_price): _number(0, 1, 0.001, "EUR/kWh"),
                vol.Required("export_rate", default=d.get("export_rate", 0.08)): _number(0, 1, 0.001, "EUR/kWh"),
                vol.Required("soc_sensor_entity_id", default=d.get("soc_sensor_entity_id", "")): _entity("sensor"),
                vol.Optional("pv_power_entity_id", default=d.get("pv_power_entity_id", "")): _entity("sensor"),
                vol.Optional("load_sensor_entity_id", default=d.get("load_sensor_entity_id", "")): _entity("sensor"),
                vol.Optional("goodwe_operation_mode_entity_id", default=d.get("goodwe_operation_mode_entity_id", "")): _entity("select"),
            }),
        )
