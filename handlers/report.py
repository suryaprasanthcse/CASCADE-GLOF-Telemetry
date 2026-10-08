"""PublishDossier: each dam's report, and footprints grown for next run.

This run's confident ("best") outlines are merged into each lake's
stored footprint here, after the parallel measurements, so no two
measurements ever write a footprint at once.
"""
from datetime import datetime, timezone

from shapely.geometry import mapping, shape
from shapely.ops import unary_union

from . import storage
from .measure import footprint_key


def handler(event, context):
    """{"lakes": [...], "observations": [...], "routes": [...]}

    Observations and routes are the summaries the earlier steps
    returned; failed items carry "status": "failed" and are skipped.
    """
    now = datetime.now(timezone.utc).isoformat()
    observations = event["observations"]
    for lake_id in event["lakes"]:
        confident = [obs for obs in observations
                     if obs["lake_id"] == lake_id
                     and obs.get("status") == "best"]
        grow_footprint(lake_id, confident, now)

    by_dam = {}
    for route in event["routes"]:
        if route.get("status") != "failed":
            by_dam.setdefault(route["dam_id"], []).append(route)
    keys = []
    for dam_id, routes in by_dam.items():
        # Least warning first: the lake to watch most closely leads.
        routes.sort(key=lambda route: route["warning_minutes"])
        lakes = {route["lake_id"] for route in routes}
        key = f"reports/{dam_id}/dossier.json"
        storage.put_json(key, {
            "dam_id": dam_id,
            "dam_name": routes[0]["dam_name"],
            "generated": now,
            "lakes": routes,
            "observations": [obs for obs in observations
                             if obs["lake_id"] in lakes],
        })
        keys.append(key)
    return {"dossiers": keys}


def grow_footprint(lake_id, observations, now):
    """Union confident outlines into the lake's stored footprint."""
    if not observations:
        return
    stored = storage.get_json(footprint_key(lake_id))
    parts = [shape(stored["geometry"])] if stored else []
    for obs in observations:
        feature = storage.get_json(obs["outline_key"])
        if feature:
            parts.append(shape(feature["geometry"]))
    storage.put_json(footprint_key(lake_id), {
        "type": "Feature",
        "geometry": mapping(unary_union(parts)),
        "properties": {"lake_id": lake_id, "updated": now},
    })
