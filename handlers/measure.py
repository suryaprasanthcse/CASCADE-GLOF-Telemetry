"""MeasureLakeMonth: the best scene of one month for one lake.

Writes the observation to DynamoDB and its outline to S3, and returns a
small summary for the state machine. The lake's footprint is read here
but only grown in PublishDossier, so parallel months never race to
write it.
"""
import logging

from pyproj import Transformer
from shapely.geometry import mapping, shape
from shapely.ops import transform

from core.lake_telemetry import Footprint, LakeMonitor

from . import registry, storage

log = logging.getLogger(__name__)


def footprint_key(lake_id):
    return f"footprints/{lake_id}.geojson"


def handler(event, context):
    """{"lake_id": "south_lhonak", "start": "2023-10-01",
    "end": "2023-10-31"}"""
    lake = registry.lake(event["lake_id"])
    monitor = LakeMonitor(lake)
    stored = storage.get_json(footprint_key(lake.lake_id))
    if stored:
        to_grid = Transformer.from_crs(4326, monitor.grid.epsg,
                                       always_xy=True)
        monitor.footprint = Footprint(
            transform(to_grid.transform, shape(stored["geometry"])))

    summary = {"lake_id": lake.lake_id, "start": event["start"]}
    observations = monitor.monthly(event["start"], event["end"])
    if not observations:
        return {**summary, "status": "no_scene"}

    observation = observations[0]
    record = observation.to_record()
    day = observation.acquired.date().isoformat()
    outline_key = f"obs/{lake.lake_id}/{day}/{observation.scene_id}.geojson"
    to_lonlat = Transformer.from_crs(monitor.grid.epsg, 4326, always_xy=True)
    storage.put_json(outline_key, {
        "type": "Feature",
        "geometry": mapping(transform(to_lonlat.transform,
                                      observation.outline)),
        "properties": record,
    })
    storage.put_item({"pk": f"LAKE#{lake.lake_id}",
                      "sk": f"OBS#{day}#{observation.scene_id}",
                      "outline_key": outline_key, **record})
    log.info("%s %s: %s, %.3f-%.3f km2", lake.lake_id, day,
             observation.status, observation.area_low_km2,
             observation.area_high_km2)
    return {**summary, "scene_id": observation.scene_id, "acquired": day,
            "status": observation.status,
            "area_low_km2": record["area_low_km2"],
            "area_high_km2": record["area_high_km2"],
            "outline_key": outline_key}
