"""PlanJobs: one work item per lake and calendar month.

A month keeps each measurement call to a few minutes. A whole year
would not fit Lambda's 15-minute limit: scoring a year's ~140 scenes
alone takes about 11 minutes at Sprint 1 timings.
"""
from datetime import date, timedelta

from . import registry

# The inline Map keeps every iteration in one execution history (25,000
# events at most) and passes every result through one state (256 KiB at
# most). 500 small items stays well clear of both.
MAX_ITEMS = 500


def handler(event, context):
    """{"start": "2023-09-01", "end": "2023-10-31", "lakes": [...]}

    `lakes` is optional and defaults to every registered lake.
    """
    start = date.fromisoformat(event["start"])
    end = date.fromisoformat(event["end"])
    if end < start:
        raise ValueError("end is before start")
    lakes = event.get("lakes") or sorted(registry.LAKES)
    for lake_id in lakes:
        registry.lake(lake_id)
    items = [{"lake_id": lake_id, "start": first.isoformat(),
              "end": last.isoformat()}
             for lake_id in lakes for first, last in months(start, end)]
    if len(items) > MAX_ITEMS:
        raise ValueError(f"{len(items)} lake-months exceed the {MAX_ITEMS} "
                         "one run allows")
    return {"lakes": lakes, "items": items}


def months(start, end):
    """(first, last) day of each month in start..end, clipped to it."""
    first = start
    while first <= end:
        following = (first.replace(day=1) + timedelta(days=32)).replace(day=1)
        yield first, min(following - timedelta(days=1), end)
        first = following
