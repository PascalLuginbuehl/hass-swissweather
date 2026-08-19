"""The Swiss Weather integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import CONF_POST_CODE, DOMAIN
from .coordinator import SwissPollenDataCoordinator, SwissWeatherDataCoordinator
from .locality import forecastable_localities

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.WEATHER]

def get_weather_coordinator_key(entry: ConfigEntry):
    return entry.entry_id + "-weather-coordinator"

def get_pollen_coordinator_key(entry: ConfigEntry):
    return entry.entry_id + "-pollen-coordinator"

async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Bring an older entry up to version 3.

    Entries created before the locality suffix existed store a bare four digit
    post code, which MeteoSwiss only resolves for whichever locality happens to
    carry suffix ``00``. Version 3 pins the code to one locality and moves
    entity identity onto the entry id, so that correcting the post code later no
    longer orphans every entity keyed on it.
    """
    if entry.version >= 3:
        return True

    post_code = str(entry.data.get(CONF_POST_CODE, "")).strip()
    try:
        localities = await hass.async_add_executor_job(forecastable_localities, post_code)
    except Exception:
        _LOGGER.exception("Could not reach the locality register to migrate %s", post_code)
        return False

    if len(localities) == 1:
        resolved = localities[0].code
    else:
        resolved = post_code
        _LOGGER.warning(
            "Post code %s covers %d localities that MeteoSwiss forecasts separately. "
            "Reconfigure the entry to pick one.", post_code, len(localities))

    await _async_rekey_onto_entry(hass, entry, post_code)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_POST_CODE: resolved}, version=3)
    return True


def rekeyed_unique_id(unique_id: str, post_code: str, entry_id: str) -> str | None:
    """The entry id form of a unique id that was keyed on the post code.

    None when the id is not one this integration wrote, which leaves it alone.
    """
    for prefix in (f"pollen-level-{post_code}.", f"pollen-{post_code}.",
                   f"swiss_weather.{post_code}", f"{post_code}."):
        if unique_id.startswith(prefix):
            return prefix.replace(post_code, entry_id, 1) + unique_id[len(prefix):]
    return None


async def _async_rekey_onto_entry(
    hass: HomeAssistant, entry: ConfigEntry, post_code: str
) -> None:
    """Move entity and device identity off the post code and onto the entry id."""
    entry_id = entry.entry_id

    def rekey(registry_entry: er.RegistryEntry) -> dict[str, str] | None:
        rekeyed = rekeyed_unique_id(registry_entry.unique_id, post_code, entry_id)
        return None if rekeyed is None else {"new_unique_id": rekeyed}

    await er.async_migrate_entries(hass, entry_id, rekey)

    device_registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(device_registry, entry_id):
        if any(domain == DOMAIN and identifier.startswith(f"swissweather-{post_code}")
               for domain, identifier in device.identifiers):
            device_registry.async_update_device(
                device.id, new_identifiers={(DOMAIN, f"swissweather-{entry_id}")})


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Swiss Weather from a config entry."""

    coordinator = SwissWeatherDataCoordinator(hass, entry)
    pollen_coordinator = SwissPollenDataCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    await pollen_coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][get_weather_coordinator_key(entry)] = coordinator
    hass.data[DOMAIN][get_pollen_coordinator_key(entry)] = pollen_coordinator
    _LOGGER.debug("Bootstrapped entry %s", entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(get_weather_coordinator_key(entry))
        hass.data[DOMAIN].pop(get_pollen_coordinator_key(entry))
    return unload_ok
