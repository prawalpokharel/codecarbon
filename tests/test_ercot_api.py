import unittest

import pytest
import responses

from codecarbon.core.carbon_intensity import build_providers, ercot
from codecarbon.core.carbon_intensity.ercot import (
    ERCOTAPIError,
    ERCOTProvider,
    fuel_mix_to_carbon_intensity,
)
from codecarbon.core.emissions import Emissions
from codecarbon.core.units import Energy
from codecarbon.external.geography import GeoMetadata
from codecarbon.input import DataSource

TEXAS = GeoMetadata(
    country_iso_code="USA", country_name="United States", region="Texas"
)


def _mix(coal, gas, nuclear, wind, solar=0.0, hydro=0.0, other=0.0, storage=0.0):
    return {
        "Coal and Lignite": {"gen": coal},
        "Hydro": {"gen": hydro},
        "Nuclear": {"gen": nuclear},
        "Other": {"gen": other},
        "Power Storage": {"gen": storage},
        "Solar": {"gen": solar},
        "Wind": {"gen": wind},
        "Natural Gas": {"gen": gas},
        "Power Storage Charging": {"gen": min(storage, 0.0)},
        "Power Storage Discharging": {"gen": max(storage, 0.0)},
    }


# CodeCarbon factors: coal 995, natural gas 743, nuclear 29, wind 26 g/kWh.
# (1000 * 995 + 2000 * 743 + 500 * 29 + 1500 * 26) / 5000 = 506.9 g/kWh
LATEST = _mix(coal=1000, gas=2000, nuclear=500, wind=1500, storage=300)
EARLIER = _mix(coal=4000, gas=1000, nuclear=0, wind=0)
RESPONSE = {
    "lastUpdated": "2026-11-15 18:21:00-0600",
    "data": {
        "2026-11-14": {"2026-11-14 23:55:00-0600": EARLIER},
        "2026-11-15": {
            "2026-11-15 18:15:00-0600": EARLIER,
            "2026-11-15 18:20:00-0600": LATEST,
        },
    },
}


class TestERCOTProvider(unittest.TestCase):
    def setUp(self):
        self._energy = Energy.from_energy(kWh=10)
        self._factors = DataSource().get_carbon_intensity_per_source_data()
        ERCOTProvider.reset_cache()

    def tearDown(self):
        ERCOTProvider.reset_cache()

    def test_is_supported_in_texas(self):
        assert ERCOTProvider().is_supported(TEXAS) is True

    def test_is_not_supported_in_other_states(self):
        geo = GeoMetadata(country_iso_code="USA", region="California")
        assert ERCOTProvider().is_supported(geo) is False

    def test_is_not_supported_without_region(self):
        geo = GeoMetadata(country_iso_code="USA", region=None)
        assert ERCOTProvider().is_supported(geo) is False

    def test_is_not_supported_in_other_countries(self):
        geo = GeoMetadata(country_iso_code="MEX", region="Texas")
        assert ERCOTProvider().is_supported(geo) is False

    def test_is_opt_in(self):
        assert build_providers({}) == []
        assert [type(p) for p in build_providers({"use_ercot_api": True})] == [
            ERCOTProvider
        ]

    @responses.activate
    def test_get_emissions_uses_the_latest_interval(self):
        responses.add(responses.GET, ercot.URL, json=RESPONSE, status=200)
        result = ERCOTProvider().get_emissions(self._energy, TEXAS)
        assert result == pytest.approx(5.069)

    def test_storage_is_left_out(self):
        charging = {
            fuel: mix["gen"]
            for fuel, mix in _mix(1000, 2000, 500, 1500, storage=-800).items()
        }
        discharging = {
            fuel: mix["gen"]
            for fuel, mix in _mix(1000, 2000, 500, 1500, storage=800).items()
        }
        assert fuel_mix_to_carbon_intensity(charging, self._factors) == pytest.approx(
            fuel_mix_to_carbon_intensity(discharging, self._factors)
        )

    def test_negative_generation_counts_as_zero(self):
        generation = {fuel: mix["gen"] for fuel, mix in _mix(0, 1000, 0, 0).items()}
        generation["Solar"] = -3.0
        assert fuel_mix_to_carbon_intensity(generation, self._factors) == pytest.approx(
            743
        )

    def test_missing_fuel_raises(self):
        generation = {fuel: mix["gen"] for fuel, mix in LATEST.items()}
        del generation["Natural Gas"]
        with pytest.raises(ERCOTAPIError, match="Natural Gas"):
            fuel_mix_to_carbon_intensity(generation, self._factors)

    @responses.activate
    def test_no_generation_raises(self):
        empty = {"data": {"2026-11-15": {"2026-11-15 18:20:00-0600": _mix(0, 0, 0, 0)}}}
        responses.add(responses.GET, ercot.URL, json=empty, status=200)
        with pytest.raises(ERCOTAPIError, match="no generation"):
            ERCOTProvider().get_emissions(self._energy, TEXAS)

    @responses.activate
    def test_http_error_raises(self):
        responses.add(responses.GET, ercot.URL, status=503)
        with pytest.raises(ERCOTAPIError, match="503"):
            ERCOTProvider().get_emissions(self._energy, TEXAS)

    @responses.activate
    def test_non_json_body_raises(self):
        responses.add(responses.GET, ercot.URL, body="<html>maintenance</html>")
        with pytest.raises(ERCOTAPIError, match="Unexpected response structure"):
            ERCOTProvider().get_emissions(self._energy, TEXAS)

    @responses.activate
    def test_empty_data_raises(self):
        responses.add(responses.GET, ercot.URL, json={"data": {}}, status=200)
        with pytest.raises(ERCOTAPIError, match="Unexpected response structure"):
            ERCOTProvider().get_emissions(self._energy, TEXAS)

    @responses.activate
    def test_null_generation_raises(self):
        broken = {"data": {"2026-11-15": {"2026-11-15 18:20:00-0600": dict(LATEST)}}}
        broken["data"]["2026-11-15"]["2026-11-15 18:20:00-0600"]["Wind"] = {"gen": None}
        responses.add(responses.GET, ercot.URL, json=broken, status=200)
        with pytest.raises(ERCOTAPIError, match="Unexpected response structure"):
            ERCOTProvider().get_emissions(self._energy, TEXAS)


class TestERCOTInEmissions(unittest.TestCase):
    def setUp(self):
        ERCOTProvider.reset_cache()

    def tearDown(self):
        ERCOTProvider.reset_cache()

    @responses.activate
    def test_texas_uses_the_live_value(self):
        responses.add(responses.GET, ercot.URL, json=RESPONSE, status=200)
        emissions = Emissions(DataSource(), providers=[ERCOTProvider()])
        result = emissions.get_private_infra_emissions(Energy.from_energy(kWh=1), TEXAS)
        assert result == pytest.approx(0.5069)

    @responses.activate
    def test_failure_falls_back_to_the_state_average(self):
        responses.add(responses.GET, ercot.URL, status=500)
        live = Emissions(DataSource(), providers=[ERCOTProvider()])
        static = Emissions(DataSource())
        energy = Energy.from_energy(kWh=1)
        assert live.get_private_infra_emissions(energy, TEXAS) == pytest.approx(
            static.get_private_infra_emissions(energy, TEXAS)
        )
