"""Base entity for Battery MPC Controller."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import BatteryMPCCoordinator


class BatteryMPCEntity(CoordinatorEntity[BatteryMPCCoordinator]):
    """Base entity for Battery MPC sensors."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: BatteryMPCCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.config_entry.entry_id)},
            name="Battery MPC Controller",
            manufacturer="Powerflow Simulator",
            model="MPC v1.0",
            sw_version="1.0.0",
        )
