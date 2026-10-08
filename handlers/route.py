"""RouteLake: route a lake's flood to its dam and store the result.

The channel geometry is built from the DEM the first time a lake is
routed (about 10 s) and read from S3 after that.
"""
import logging
from datetime import timedelta

import numpy as np

from core.flood_routing import Channel, build_channel, hindcast

from . import registry, storage

log = logging.getLogger(__name__)


def channel_key(lake_id):
    return f"terrain/{lake_id}/channel.json"


def load_channel(lake, route):
    """The lake's cached channel geometry, built and cached if missing.

    A fresh build is routed from its cached (rounded) form too, so the
    first run and every later one see exactly the same geometry.
    """
    key = channel_key(lake.lake_id)
    record = storage.get_json(key)
    if record is None:
        record = build_channel(seed_lonlat=route["source_lonlat"],
                               dam_lonlat=route["dam_lonlat"],
                               bbox=route["corridor_bbox"]).to_dict()
        storage.put_json(key, record)
    return Channel.from_dict(record)


def handler(event, context):
    """{"lake_id": "south_lhonak"}"""
    lake = registry.lake(event["lake_id"])
    route = registry.route(lake.lake_id)
    channel = load_channel(lake, route)
    result = hindcast(channel, route["volume_m3"], route["manning_n"])

    release = route["release_time"]
    travel_s = float(np.median(result.arrival_s))
    peak_s = float(np.median(result.peak_time_s))
    summary = {
        "lake_id": lake.lake_id,
        "dam_id": route["dam_id"],
        "dam_name": route["dam_name"],
        "channel_km": round(channel.length_m / 1000, 2),
        "volume_m3": route["volume_m3"],
        "manning_n": route["manning_n"],
        "release_time": release.isoformat(),
        "arrival_time": (release + timedelta(seconds=travel_s)).isoformat(),
        "warning_minutes": round(travel_s / 60, 1),
        "peak_m3s": round(float(np.median(result.peak_m3s))),
        "peak_time": (release + timedelta(seconds=peak_s)).isoformat(),
        "depth_m": round(float(np.median(result.depth_m)), 1),
    }
    storage.put_json(f"scenarios/{lake.lake_id}/hindcast.json", summary)
    storage.put_item({"pk": f"DAM#{route['dam_id']}",
                      "sk": f"LAKE#{lake.lake_id}", **summary})
    log.info("%s -> %s: %.0f min, %d m3/s", lake.lake_id, route["dam_id"],
             summary["warning_minutes"], summary["peak_m3s"])
    return summary
