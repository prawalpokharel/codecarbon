from typing import Any, Dict, Hashable, Mapping, Optional

import requests

from codecarbon.core.carbon_intensity.base import (
    CarbonIntensityCooldownError,
    CarbonIntensityError,
    CarbonIntensityProvider,
)
from codecarbon.external.geography import GeoMetadata
from codecarbon.input import DataSource

# ERCOT fuel-mix dashboard: generation by fuel in MW, every 5 minutes, for the
# ERCOT grid (about 90% of Texas load). ERCOT's terms of use allow raw data
# from public pages to be used in analyses: https://www.ercot.com/help/terms
URL: str = "https://www.ercot.com/api/1/services/read/dashboards/fuel-mix.json"

ERCOT_API_TIMEOUT: int = 4
ERCOT_CACHE_TTL: int = 300
ERCOT_COOLDOWN: int = 30
ERCOT_MAX_COOLDOWN: int = 3600

# Keys of carbon_intensity_per_source.json. Power Storage is left out: the
# energy it discharges was generated, and counted, when it was charged.
FUEL_TO_SOURCE: Dict[str, str] = {
    "Coal and Lignite": "coal",
    "Natural Gas": "natural_gas",
    "Nuclear": "nuclear",
    "Solar": "solar",
    "Wind": "wind",
    "Hydro": "hydroelectricity",
    "Other": "fossil",
}


class ERCOTAPIError(CarbonIntensityError):
    pass


class ERCOTAPICooldownError(ERCOTAPIError, CarbonIntensityCooldownError):
    """The request was skipped because a recent one failed."""


def fuel_mix_to_carbon_intensity(
    generation_mw: Mapping[str, float], factors: Mapping[str, float]
) -> float:
    """
    Generation-weighted carbon intensity, in gCO2e/kWh, of one interval of
    ERCOT's fuel mix, using CodeCarbon's per-source factors.
    """
    missing = sorted(set(FUEL_TO_SOURCE) - set(generation_mw))
    if missing:
        raise ERCOTAPIError(f"ERCOT fuel mix is missing {', '.join(missing)}")
    total_mw = 0.0
    weighted = 0.0
    for fuel, source in FUEL_TO_SOURCE.items():
        mw = max(generation_mw[fuel], 0.0)
        total_mw += mw
        weighted += mw * factors[source]
    if total_mw <= 0:
        raise ERCOTAPIError("ERCOT fuel mix has no generation")
    return weighted / total_mw


class ERCOTProvider(CarbonIntensityProvider):
    """
    Live carbon intensity of the ERCOT grid in Texas, from ERCOT's public
    fuel-mix dashboard. Opt-in with ``use_ercot_api``.
    """

    name = "ERCOT"
    cache_ttl = ERCOT_CACHE_TTL
    cooldown = ERCOT_COOLDOWN
    max_cooldown = ERCOT_MAX_COOLDOWN
    error_class = ERCOTAPIError
    cooldown_error_class = ERCOTAPICooldownError

    @classmethod
    def from_config(cls, conf: Mapping[str, Any]) -> Optional["ERCOTProvider"]:
        return cls() if conf.get("use_ercot_api") else None

    def is_supported(self, geo: GeoMetadata) -> bool:
        """
        Return True when the location is in Texas. Parts of Texas (El Paso, the
        Panhandle and parts of East Texas) are outside the ERCOT grid, but the
        detected location cannot tell them apart.
        """
        return geo.country_iso_code == "USA" and (geo.region or "") == "texas"

    def _cache_key(self, geo: GeoMetadata) -> Hashable:
        return None

    def _fetch(self, geo: GeoMetadata) -> float:
        resp = requests.get(URL, timeout=ERCOT_API_TIMEOUT)
        if resp.status_code != 200:
            raise ERCOTAPIError(f"ERCOT API returned status {resp.status_code}")

        try:
            intervals = {
                timestamp: mix
                for day in resp.json()["data"].values()
                for timestamp, mix in day.items()
            }
            latest = intervals[max(intervals)]
            generation_mw = {fuel: float(latest[fuel]["gen"]) for fuel in latest}
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            raise ERCOTAPIError(f"Unexpected response structure: {e}") from e

        factors = DataSource().get_carbon_intensity_per_source_data()
        return fuel_mix_to_carbon_intensity(generation_mw, factors)
