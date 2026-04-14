"""Sensor platform for Battery MPC Controller."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.components.sensor import RestoreSensor
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import BatteryMPCCoordinator
from .entity import BatteryMPCEntity


SENSOR_DESCRIPTIONS = (
    SensorEntityDescription(
        key="next_action",
        name="Next Action",
        icon="mdi:battery-sync",
    ),
    SensorEntityDescription(
        key="target_power",
        name="Target Power",
        native_unit_of_measurement="W",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:flash",
    ),
    SensorEntityDescription(
        key="target_soc",
        name="Target SoC",
        native_unit_of_measurement="%",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="forecast_pv_power",
        name="Forecast PV Power",
        native_unit_of_measurement="W",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:solar-power",
    ),
    SensorEntityDescription(
        key="forecast_load",
        name="Forecast Load",
        native_unit_of_measurement="W",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:home-lightning-bolt",
    ),
    SensorEntityDescription(
        key="cost_savings_today",
        name="Cost Savings Today",
        native_unit_of_measurement="EUR",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        icon="mdi:cash-plus",
    ),
    SensorEntityDescription(
        key="solve_time_ms",
        name="Solve Time",
        native_unit_of_measurement="ms",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:timer-outline",
        entity_registry_enabled_default=False,
    ),
    SensorEntityDescription(
        key="forecast_age_min",
        name="Forecast Age",
        native_unit_of_measurement="min",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:clock-outline",
        entity_registry_enabled_default=False,
    ),
    SensorEntityDescription(
        key="solar_remaining_today_kwh",
        name="Solar Remaining Today",
        native_unit_of_measurement="kWh",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:solar-power-variant",
    ),
)

LIFETIME_DESCRIPTION = SensorEntityDescription(
    key="cost_savings_lifetime",
    name="Cost Savings Lifetime",
    native_unit_of_measurement="EUR",
    device_class=SensorDeviceClass.MONETARY,
    state_class=SensorStateClass.TOTAL,
    icon="mdi:piggy-bank",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Battery MPC sensors from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: BatteryMPCCoordinator = data.coordinator

    entities: list[SensorEntity] = [
        BatteryMPCSensor(coordinator=coordinator, entity_description=desc)
        for desc in SENSOR_DESCRIPTIONS
    ]
    # Lifetime savings — uses RestoreSensor to persist across restarts
    entities.append(BatteryMPCLifetimeSensor(coordinator=coordinator))

    async_add_entities(entities)


class BatteryMPCSensor(BatteryMPCEntity, SensorEntity):
    """Sensor exposing MPC optimization results."""

    def __init__(
        self,
        coordinator: BatteryMPCCoordinator,
        entity_description: SensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = entity_description
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{entity_description.key}"
        )

    @property
    def native_value(self) -> Any:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get(self.entity_description.key)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose the full MPC schedule on the next_action sensor."""
        if self.entity_description.key != "next_action":
            return None
        if self.coordinator.data is None:
            return None
        return {
            "schedule": self.coordinator.data.get("schedule"),
            "horizon_hours": self.coordinator.data.get("horizon_hours"),
        }


class BatteryMPCLifetimeSensor(BatteryMPCEntity, RestoreSensor):
    """Lifetime cost savings sensor — persists across HA restarts."""

    entity_description = LIFETIME_DESCRIPTION

    def __init__(self, coordinator: BatteryMPCCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_cost_savings_lifetime"
        )
        self._restored_value: float | None = None

    async def async_added_to_hass(self) -> None:
        """Restore previous lifetime value on startup."""
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last and last.native_value is not None:
            try:
                self._restored_value = float(last.native_value)
                self.coordinator._cost_savings_lifetime = self._restored_value
            except (ValueError, TypeError):
                pass

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return self._restored_value
        return self.coordinator.data.get("cost_savings_lifetime", self._restored_value)
