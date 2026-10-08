"""Lakes the pipeline may process, and where their floods are routed.

Runs only ever name lakes from this registry, never coordinates, so no
caller can make the pipeline read an arbitrary part of the planet.
"""
import json
from pathlib import Path

from shapely.geometry import shape

from core import ground_truth as gt
from core.flood_routing import (CORRIDOR_BBOX, LAKE_SEED_LONLAT,
                                TEESTA_III_LONLAT)
from core.lake_telemetry import Lake

SEEDS = Path(__file__).parent / "seeds"


def _seed(name):
    """A lake outline (lon/lat GeoJSON) that anchors its footprint.

    Months are measured in parallel and footprints only grow after a
    run, so a lake needs a real outline from its first run: seeded with
    just a point, every month would be scored on that point alone.
    """
    feature = json.loads((SEEDS / name).read_text(encoding="utf-8"))
    return shape(feature["geometry"])


LAKES = {
    # Seed: the confident 2023-09-14 outline (see the file's properties).
    "south_lhonak": Lake(
        lake_id="south_lhonak",
        name="South Lhonak Lake",
        bbox=(88.16, 27.88, 88.23, 27.93),
        seed=_seed("south_lhonak.geojson"),
    ),
}

# Each lake's dam and flood scenario. Manning's n was calibrated on 8 Oct
# against the 00:30 IST arrival at Chungthang (tests/test_routing.py);
# volume and release time are the 2023 hindcast's.
ROUTES = {
    "south_lhonak": {
        "dam_id": "teesta_iii",
        "dam_name": "Teesta-III, Chungthang",
        "source_lonlat": LAKE_SEED_LONLAT,
        "dam_lonlat": TEESTA_III_LONLAT,
        "corridor_bbox": CORRIDOR_BBOX,
        "volume_m3": gt.DRAINED_VOLUME_M3,
        "manning_n": 0.175,
        "release_time": gt.LAKE_RELEASE_TIME,
    },
}


def lake(lake_id):
    """The registered lake, or ValueError for anything else."""
    try:
        return LAKES[lake_id]
    except KeyError:
        raise ValueError(f"unknown lake {lake_id!r}") from None


def route(lake_id):
    """The registered flood route, or ValueError for anything else."""
    try:
        return ROUTES[lake_id]
    except KeyError:
        raise ValueError(f"no flood route for lake {lake_id!r}") from None
