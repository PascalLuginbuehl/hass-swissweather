"""Config flow for Swiss Weather integration."""
from __future__ import annotations

import asyncio
import csv
from dataclasses import dataclass
import logging
from typing import Any

import requests
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
from homeassistant.util.location import distance

from .const import (
    CONF_POLLEN_STATION_CODE,
    CONF_POST_CODE,
    CONF_STATION_CODE,
    CONF_WEATHER_WARNINGS_NUMBER,
    DOMAIN,
)
from .locality import Locality, forecastable_localities, locality_at
from .pollen import PollenClient

STATION_LIST_URL = "https://data.geo.admin.ch/ch.meteoschweiz.messnetz-automatisch/ch.meteoschweiz.messnetz-automatisch_en.csv"

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA_BACKUP = vol.Schema(
    {
        vol.Required(CONF_POST_CODE): str,
        vol.Optional(CONF_STATION_CODE): str,
        vol.Optional(CONF_POLLEN_STATION_CODE): str,
    }
)

@dataclass
class WeatherStation:
    """Describes a single weather station as retrieved from the database."""

    name: str
    code: str
    altitude: int | None
    lat: float
    lng: float
    canton: str

class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Swiss Weather."""

    VERSION = 3

    def __init__(self) -> None:
        self._pending_input: dict[str, Any] = {}
        self._localities: list[Locality] = []
        self._reconfiguring = False
        self._selector_options: tuple[list, list] | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        if user_input is None:
            return await self._show_user_form({}, {})

        _LOGGER.info("User chose %s", user_input)
        post_code, errors = await self._resolve_post_code(user_input)
        if errors:
            return await self._show_user_form(user_input, errors)
        if post_code is None:
            return await self.async_step_locality()
        return self._finish({**user_input, CONF_POST_CODE: post_code})

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the reconfigure step."""
        _LOGGER.info("Reconfigure with user dict %s", user_input)
        self._reconfiguring = True

        entry = self._get_reconfigure_entry()
        stored = dict(entry.data) if entry is not None else {}
        if not user_input:
            return await self._show_reconfigure_form(stored, {})

        self._abort_if_unique_id_mismatch()
        post_code, errors = await self._resolve_post_code(user_input)
        if errors:
            return await self._show_reconfigure_form({**stored, **user_input}, errors)
        if post_code is None:
            return await self.async_step_locality()
        return self._finish({**user_input, CONF_POST_CODE: post_code})

    async def async_step_locality(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick which locality of a shared post code to forecast for."""
        if user_input is None:
            home = await self.hass.async_add_executor_job(self._home_locality)
            options = sorted(
                self._localities,
                key=lambda it: (home is None or it.code != home.code, it.name),
            )
            return self.async_show_form(
                step_id="locality",
                data_schema=vol.Schema({
                    vol.Required(CONF_POST_CODE, default=options[0].code): SelectSelector(
                        SelectSelectorConfig(
                            options=[SelectOptionDict(value=it.code,
                                                      label=f"{it.name} ({it.code})")
                                     for it in options],
                            mode=SelectSelectorMode.DROPDOWN
                        )
                    )
                }),
                description_placeholders={
                    "post_code": self._pending_input.get(CONF_POST_CODE, "")},
            )

        return self._finish({**self._pending_input, **user_input})

    async def _show_user_form(
        self, defaults: dict[str, Any], errors: dict[str, str]
    ) -> ConfigFlowResult:
        try:
            return self.async_show_form(
                step_id="user",
                data_schema=await self._config_schema(defaults),
                errors=errors,
            )
        except Exception:
            _LOGGER.exception("Failed to retrieve station list, back to manual mode!")
            # If the API broke, we still give user the option to manually enter the
            # station code and continue.
            return self.async_show_form(
                data_schema=STEP_USER_DATA_SCHEMA_BACKUP, errors=errors
            )

    async def _show_reconfigure_form(
        self, defaults: dict[str, Any], errors: dict[str, str]
    ) -> ConfigFlowResult:
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=await self._config_schema(defaults),
            errors=errors,
        )

    async def _config_schema(self, defaults: dict[str, Any]) -> vol.Schema:
        station_options, pollen_station_options = await self._get_selector_options()
        given = {key: value for key, value in defaults.items() if value is not None}
        return vol.Schema({
            vol.Required(CONF_POST_CODE,
                         default=given.get(CONF_POST_CODE, vol.UNDEFINED)): str,
            vol.Optional(CONF_STATION_CODE,
                         default=given.get(CONF_STATION_CODE, vol.UNDEFINED)): SelectSelector(
                SelectSelectorConfig(
                    options=station_options,
                    mode=SelectSelectorMode.DROPDOWN
                ),
            ),
            vol.Optional(CONF_POLLEN_STATION_CODE,
                         default=given.get(CONF_POLLEN_STATION_CODE, vol.UNDEFINED)): SelectSelector(
                SelectSelectorConfig(
                    options=pollen_station_options,
                    mode=SelectSelectorMode.DROPDOWN
                )
            ),
            vol.Required(CONF_WEATHER_WARNINGS_NUMBER,
                         default=given.get(CONF_WEATHER_WARNINGS_NUMBER, 1)): NumberSelector(
                NumberSelectorConfig(min=0, max=10, mode=NumberSelectorMode.BOX, step=1)
            )
        })

    async def _get_selector_options(self) -> tuple[list, list]:
        """Both station dropdowns, fetched once and reused when a form redisplays."""
        if self._selector_options is None:
            self._selector_options = await asyncio.gather(
                self._get_weather_station_options(),
                self._get_pollen_station_options(),
            )
        return self._selector_options

    async def _resolve_post_code(
        self, user_input: dict[str, Any]
    ) -> tuple[str | None, dict[str, str]]:
        """Pin a post code to one locality, remembering the form if we must ask.

        Returns the six digit code MeteoSwiss wants, or None with either errors
        to redisplay or, when there are none, a locality choice left pending.
        """
        post_code = str(user_input.get(CONF_POST_CODE, "")).strip()
        if not post_code.isdigit() or len(post_code) not in (4, 6):
            return None, {CONF_POST_CODE: "invalid_post_code"}
        try:
            localities = await self.hass.async_add_executor_job(
                forecastable_localities, post_code)
        except Exception:
            _LOGGER.exception("Could not check post code %s", post_code)
            return None, {CONF_POST_CODE: "cannot_check_post_code"}

        if not localities:
            return None, {CONF_POST_CODE: "no_forecast_for_post_code"}
        if len(localities) == 1:
            return localities[0].code, {}

        self._pending_input = dict(user_input)
        self._localities = localities
        return None, {}

    def _home_locality(self) -> Locality | None:
        lat = self.hass.config.latitude
        lng = self.hass.config.longitude
        if lat is None or lng is None:
            return None
        try:
            return locality_at(lat, lng)
        except Exception:
            _LOGGER.exception("Could not look up the home locality")
            return None

    def _finish(self, user_input: dict[str, Any]) -> ConfigFlowResult:
        if self._reconfiguring:
            return self.async_update_reload_and_abort(
                self._get_reconfigure_entry(), data_updates=user_input
            )
        return self._create_entry(user_input)

    def _create_entry(self, user_input: dict[str, Any]) -> ConfigFlowResult:
        station_code = user_input.get(CONF_STATION_CODE) or "No Station"
        post_code = user_input.get(CONF_POST_CODE)
        pollen_station_code = user_input.get(CONF_POLLEN_STATION_CODE)
        return self.async_create_entry(title=f"Weather at {post_code} / {station_code or "No weather station"} / {pollen_station_code or "No pollen station"}", data=user_input,
            description=f"{user_input[CONF_POST_CODE]}")

    def format_station_name_for_dropdown(self, station: WeatherStation) -> str:
        distance = self._get_distance_to_station(station)
        if distance is None:
            return f"{station.name} ({station.canton})"
        else:
            return f"{station.name} ({station.canton}) - {distance / 1000:.0f} km away"

    async def _get_weather_station_options(self):
        stations = await self.hass.async_add_executor_job(self.load_station_list)
        _LOGGER.debug("Stations received.", extra={"Stations": stations})
        if (self.hass.config.latitude is not None and
            self.hass.config.longitude is not None):
                stations = sorted(stations, key=lambda it: self._get_distance_to_station(it))
        return [SelectOptionDict(value=station.code,
                                    label=self.format_station_name_for_dropdown(station))
                                    for station in stations]

    async def _get_pollen_station_options(self):
        pollen_stations = await self.hass.async_add_executor_job(self.load_pollen_station_list)
        if (self.hass.config.latitude is not None and
            self.hass.config.longitude is not None):
                stations = sorted(pollen_stations, key=lambda it: self._get_distance_to_station(it))
        return [SelectOptionDict(value=station.code,
                                    label=self.format_station_name_for_dropdown(station))
                                    for station in stations]

    def _get_distance_to_station(self, station: WeatherStation):
        h_lat = self.hass.config.latitude
        h_lng = self.hass.config.longitude
        if h_lat is None or h_lng is None:
            return None
        return distance(h_lat, h_lng, station.lat, station.lng)

    def load_station_list(self, encoding='ISO-8859-1') -> list[WeatherStation]:
        _LOGGER.info("Requesting station list data...")
        with requests.get(STATION_LIST_URL, stream = True) as r:
            lines = (line.decode(encoding) for line in r.iter_lines())
            reader = csv.DictReader(lines, delimiter=';')
            stations = []
            for row in reader:
                _LOGGER.debug(row)
                code =  row.get("Abbr.")
                if code is None:
                    _LOGGER.debug("No code in row.", extra={"Station": row})
                    continue
                # Skip stations that have almost no useable data
                measurements = row.get("Measurements")
                if measurements is None:
                    _LOGGER.debug("No measurements in row.", extra={"Station": row})
                    continue
                if "Temperature" not in measurements:
                    _LOGGER.debug("Skipping station due to lack of data.", extra={"Station": row})
                    continue

                stations.append(WeatherStation(row.get("Station"),
                                               row.get("Abbr."),
                                               _int_or_none(row.get("Station height m a. sea level")),
                                               _float_or_none(row.get("Latitude")),
                                               _float_or_none(row.get("Longitude")),
                                               row.get("Canton")))
            _LOGGER.info("Retrieved %d stations.", len(stations))
            return stations

    def load_pollen_station_list(self, encoding='ISO-8859-1') -> list[WeatherStation]:
        _LOGGER.info("Requesting pollen station list data...")
        pollen_client = PollenClient()
        pollen_station_list = pollen_client.get_pollen_station_list()
        if pollen_station_list is None:
            return []
        stations = []
        for station in pollen_station_list:
            _LOGGER.debug(station)
            stations.append(WeatherStation(
                station.name,
                station.abbreviation,
                int(station.altitude),
                station.lat,
                station.lng,
                station.canton
            ))
        return stations

def _int_or_none(val: str) -> int|None:
    if val is None:
        return None
    return int(val)

def _float_or_none(val: str) -> float|None:
    if val is None:
        return None
    return float(val)
