"""Hindcast validation of the flood routing (brief section 4.5).

Runs offline on tests/fixtures/south_lhonak_channel.json, which
`python -m core.flood_routing` builds from the Copernicus DEM, so every
result is deterministic. Pass/fail limits were fixed in the brief before
the first calibration run.
"""
from datetime import timedelta
from pathlib import Path

import numpy as np
import pytest

from core import ground_truth as gt
from core.flood_routing import (MANNING_N_PLAUSIBLE, Channel,
                                calibrate_manning, hindcast)

FIXTURE = Path(__file__).parent / "fixtures" / "south_lhonak_channel.json"
BENCHMARK_TRAVEL_S = (gt.TEESTA_III_BREACH_TIME
                      - gt.LAKE_RELEASE_TIME).total_seconds()
ARRIVAL_TOLERANCE = timedelta(minutes=10)
# Peak flow at Chungthang across published studies: 5340 m3/s is Sattar
# et al. (2025); the upper value comes from the blueprint, unverified.
PUBLISHED_PEAK_BAND_M3S = (5340.0, 14673.0)


@pytest.fixture(scope="module")
def channel():
    return Channel.load(FIXTURE)


@pytest.fixture(scope="module")
def calibrated_n(channel):
    return calibrate_manning(channel, gt.DRAINED_VOLUME_M3, BENCHMARK_TRAVEL_S)


@pytest.fixture(scope="module")
def result(channel, calibrated_n):
    return hindcast(channel, gt.DRAINED_VOLUME_M3, calibrated_n)


def test_flow_path_length_matches_published_distance(channel):
    """P1: the traced path is within 5% of the locked 67.5 km."""
    error_km = channel.length_m / 1000 - gt.LAKE_TO_CHUNGTHANG_KM
    assert abs(error_km) <= 0.05 * gt.LAKE_TO_CHUNGTHANG_KM


def test_routing_conserves_water(result):
    """R1: every member delivers what left the lake to the dam, within 1%."""
    np.testing.assert_allclose(result.volume_out_m3, result.volume_in_m3,
                               rtol=0.01)
    np.testing.assert_allclose(result.volume_in_m3, gt.DRAINED_VOLUME_M3,
                               rtol=0.01)


def test_peak_flows_overlap_published_band(result):
    """R2: the modelled peak band at the dam overlaps the published one."""
    low, high = PUBLISHED_PEAK_BAND_M3S
    assert result.peak_m3s.min() <= high and result.peak_m3s.max() >= low


def test_source_peaks_ignore_roughness(channel):
    """The lake outflow is fixed by the reconstruction, never by n."""
    smooth = hindcast(channel, gt.DRAINED_VOLUME_M3, 0.05)
    rough = hindcast(channel, gt.DRAINED_VOLUME_M3, 0.15)
    np.testing.assert_array_equal(smooth.source_peak_m3s,
                                  rough.source_peak_m3s)


def test_calibrated_roughness_is_physically_plausible(calibrated_n):
    """Calibration may only move n, and only within the range fixed
    before calibrating. Outside it, roughness would be standing in for
    physics the model doesn't have."""
    low, high = MANNING_N_PLAUSIBLE
    assert low <= calibrated_n <= high, f"calibrated n = {calibrated_n:.4f}"


def test_flood_reaches_chungthang_at_benchmark_time(result):
    """R3: release (22:12:20 IST) plus the modelled travel time of the
    reconstructed 2023 outflow lands within 10 minutes of 00:30 IST.

    n is calibrated to this benchmark, so on its own this test checks
    the calibration and routing are deterministic; the plausibility test
    above is what makes it a real check.
    """
    travel = timedelta(seconds=float(np.median(result.arrival_s)))
    arrival = gt.LAKE_RELEASE_TIME + travel
    assert abs(arrival - gt.TEESTA_III_BREACH_TIME) <= ARRIVAL_TOLERANCE
