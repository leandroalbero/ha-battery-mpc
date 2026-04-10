"""Battery MPC Controller — optimal battery scheduling via Model Predictive Control.

Uses Open-Meteo solar forecasts and linear programming to minimize electricity cost
by optimally timing battery charge/discharge based on time-of-use tariffs.
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN, LOGGER
from .coordinator import BatteryMPCCoordinator

PLATFORMS = [Platform.SENSOR]


@dataclass
class BatteryMPCData:
    """Runtime data for Battery MPC integration."""

    coordinator: BatteryMPCCoordinator


type BatteryMPCConfigEntry = ConfigEntry[BatteryMPCData]


async def async_setup_entry(hass: HomeAssistant, entry: BatteryMPCConfigEntry) -> bool:
    """Set up Battery MPC from a config entry."""
    coordinator = BatteryMPCCoordinator(hass, entry)

    # Store coordinator in runtime data
    entry.runtime_data = BatteryMPCData(coordinator=coordinator)

    # First refresh — fetches forecast + runs MPC
    await coordinator.async_config_entry_first_refresh()

    # Forward to sensor platform
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Reload on options change
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    LOGGER.info(
        "Battery MPC Controller started (%.1f kWh battery, %d-min intervals)",
        entry.data.get("battery_capacity_kwh", 0),
        5,
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: BatteryMPCConfigEntry) -> bool:
    """Unload Battery MPC config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload entry on options update."""
    await hass.config_entries.async_reload(entry.entry_id)
