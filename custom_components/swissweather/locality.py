"""Resolve a Swiss post code to the locality codes MeteoSwiss forecasts by.

MeteoSwiss addresses a place as its four digit post code followed by the two
digit Swiss Post ZAZ that distinguishes the localities sharing that code, so a
bare post code only resolves where that suffix happens to be ``00``.
"""
from __future__ import annotations

from dataclasses import dataclass

import requests

from .meteo import MeteoClient

LOCALITY_LAYER = "ch.swisstopo-vd.ortschaftenverzeichnis_plz"
FIND_URL = "https://api3.geo.admin.ch/rest/services/api/MapServer/find"
IDENTIFY_URL = "https://api3.geo.admin.ch/rest/services/api/MapServer/identify"
REQUEST_TIMEOUT = 15


@dataclass
class Locality:
    """A place MeteoSwiss can produce a forecast for."""

    name: str
    code: str
    """Post code plus ZAZ: the six digits MeteoSwiss expects."""


def _localities(url: str, params: dict[str, str]) -> list[Locality]:
    response = requests.get(url, timeout=REQUEST_TIMEOUT, params={
        **params, "returnGeometry": "false", "sr": "4326",
    })
    response.raise_for_status()
    found = (_to_locality(result.get("attributes", {}))
             for result in response.json().get("results", []))
    return [it for it in found if it is not None]


def _to_locality(attributes: dict) -> Locality | None:
    post_code = attributes.get("plz")
    zaz = attributes.get("zusziff")
    name = attributes.get("langtext")
    if post_code is None or zaz is None:
        return None
    return Locality(name or str(post_code), f"{int(post_code):04d}{str(zaz).zfill(2)}")


def localities_for_post_code(post_code) -> list[Locality]:
    """List every locality sharing a post code, alphabetically."""
    return sorted(_localities(FIND_URL, {
        "layer": LOCALITY_LAYER,
        "searchText": str(post_code).strip(),
        "searchField": "plz",
    }), key=lambda it: it.name)


def locality_at(lat: float, lng: float) -> Locality | None:
    """The locality containing a point, or None if it is outside Switzerland."""
    found = _localities(IDENTIFY_URL, {
        "geometry": f"{lng},{lat}",
        "geometryType": "esriGeometryPoint",
        "layers": f"all:{LOCALITY_LAYER}",
        "tolerance": "0",
    })
    return found[0] if found else None


def forecastable_localities(post_code: str) -> list[Locality]:
    """The localities under a post code that MeteoSwiss will actually forecast.

    A six digit code already names one locality, so it is narrowed to that entry;
    a four digit one keeps every locality sharing it. Either way the register is
    only a claim that the place exists, so each candidate is confirmed against
    MeteoSwiss before it is offered.
    """
    candidates = localities_for_post_code(post_code[:4])
    if len(post_code) == 6:
        candidates = [it for it in candidates if it.code == post_code]
    client = MeteoClient()
    return [it for it in candidates if client.has_forecast(it.code)]
