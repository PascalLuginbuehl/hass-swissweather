"""Resolve a Swiss post code to the locality codes MeteoSwiss forecasts by.

MeteoSwiss addresses a place as its four digit post code followed by the two
digit Swiss Post ZAZ that distinguishes the localities sharing that code, so a
bare post code only resolves where that suffix happens to be ``00``.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging

import requests

LOCALITY_LAYER = "ch.swisstopo-vd.ortschaftenverzeichnis_plz"
FIND_URL = "https://api3.geo.admin.ch/rest/services/api/MapServer/find"
IDENTIFY_URL = "https://api3.geo.admin.ch/rest/services/api/MapServer/identify"
REQUEST_TIMEOUT = 15

_LOGGER = logging.getLogger(__name__)


@dataclass
class Locality:
    """A place MeteoSwiss can produce a forecast for."""

    name: str
    code: str
    """Post code plus ZAZ: the six digits MeteoSwiss expects."""


def _to_locality(attributes: dict) -> Locality | None:
    post_code = attributes.get("plz")
    zaz = attributes.get("zusziff")
    name = attributes.get("langtext")
    if post_code is None or zaz is None:
        return None
    return Locality(name or str(post_code), f"{int(post_code):04d}{str(zaz):>02s}")


def localities_for_post_code(post_code) -> list[Locality]:
    """List every locality sharing a post code, alphabetically."""
    response = requests.get(FIND_URL, timeout=REQUEST_TIMEOUT, params={
        "layer": LOCALITY_LAYER,
        "searchText": str(post_code).strip(),
        "searchField": "plz",
        "returnGeometry": "false",
        "sr": "4326",
    })
    response.raise_for_status()
    localities = [_to_locality(result.get("attributes", {}))
                  for result in response.json().get("results", [])]
    return sorted((it for it in localities if it is not None), key=lambda it: it.name)


def locality_at(lat: float, lng: float) -> Locality | None:
    """The locality containing a point, or None if it is outside Switzerland."""
    response = requests.get(IDENTIFY_URL, timeout=REQUEST_TIMEOUT, params={
        "geometry": f"{lng},{lat}",
        "geometryType": "esriGeometryPoint",
        "layers": f"all:{LOCALITY_LAYER}",
        "tolerance": "0",
        "returnGeometry": "false",
        "sr": "4326",
    })
    response.raise_for_status()
    for result in response.json().get("results", []):
        locality = _to_locality(result.get("attributes", result.get("properties", {})))
        if locality is not None:
            return locality
    return None
