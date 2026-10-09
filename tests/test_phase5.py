"""Phase 5's inputs match docs/phase5-preregistration.md.

Structural checks only: nothing here routes a flood, so no Phase 5 result
is computed by the test suite.
"""
import pytest

from core.flood_routing import RISE_FRACTIONS
from tools.science import phase5


def test_area_to_volume_relations_follow_their_papers():
    # Huggel et al. 2002: V = 0.104 A^1.42 with A in m2.
    assert phase5.volume_huggel_2002(1.0) == pytest.approx(
        0.104 * 1e6 ** 1.42)
    # Fujita et al. 2013: D = 55 A^0.25 m with A in km2, so 1 km2 holds
    # 55 m x 1 km2.
    assert phase5.volume_fujita_2013(1.0) == pytest.approx(55e6)


def test_forecast_has_the_pre_registered_48_members():
    members = phase5.forecast_members()
    keys = {(m["area_km2"], m["volume_relation"], m["drained_fraction"],
             m["peak_formula"], m["rise_fraction"]) for m in members}
    assert len(members) == len(keys) == 48
    assert {m["area_km2"] for m in members} == {1.6438, 1.6543}
    assert {m["drained_fraction"] for m in members} == {0.5, 0.75, 1.0}
    assert {m["rise_fraction"] for m in members} == set(RISE_FRACTIONS)
    assert set(RISE_FRACTIONS) == {0.1, 0.3}


def test_unconfirmed_huggel_peak_formula_is_excluded():
    assert phase5.PEAK_FORMULAS == ("evans_1986", "popov_1991")


def test_fixed_constants():
    assert phase5.MANNING_N == 0.175
    assert phase5.BENCHMARK_TRAVEL_S == 8260.0  # 22:12:20 -> 00:30 IST
    assert phase5.TIMING_TOLERANCE_S == 600.0
    assert phase5.PUBLISHED_PEAK_M3S == (5340.0, 7355.0)


def test_band_and_overlap_helpers():
    stats = phase5.band(range(11))
    assert (stats["min"], stats["p10"], stats["p50"], stats["p90"],
            stats["max"]) == (0, 1, 5, 9, 10)
    assert phase5.overlaps(1, 2, (2, 3))
    assert not phase5.overlaps(1, 2, (2.5, 3))
