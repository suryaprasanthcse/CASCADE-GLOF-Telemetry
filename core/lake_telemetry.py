"""Lake surface-area telemetry with snow, slush, cloud and shadow masking.

Implements section 2 of docs/architecture-brief.md. Each pixel near a
lake is classed as water, land or unobservable (cloud, snow/ice/slush,
or shadow that fails the water test). Area is reported as a range: the
visible lake water is the low end, and adding every unobservable pixel
inside the lake's known footprint gives the high end.
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import IntEnum

import numpy as np
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.features import geometry_mask, shapes
from rasterio.warp import reproject
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as transform_geometry
from shapely.ops import unary_union

from .sentinel_loader import (DEM_RES_M, Grid, load_scene, read_dem,
                              search_scenes)

log = logging.getLogger(__name__)

CLASSIFIER_VERSION = "1.0"

# Pixel rules (section 2.2).
NDWI_WATER_MIN = 0.1
# Hall et al.'s snow rule: snow is bright in near-infrared, water isn't.
# Checked on 8 Oct against the Sep 2023 lake (water) and the snow-covered
# Oct 6 basin: 0.11 keeps 97.3% of water pixels and lets 2.3% of snow
# through as water; 0.15 would keep 98.7% but let 8.3% through.
NIR_WATER_MAX = 0.11
NDSI_SNOW_MIN = 0.4
SCL_CLOUD_OR_NODATA = (0, 1, 8, 9, 10)
SCL_CLOUD_SHADOW = 3

# Footprint and confidence (sections 2.3 and 2.4).
SEED_RADIUS_M = 100.0
FOOTPRINT_DILATION_M = 20.0  # two 10 m pixels
BEST_MAX_OBSCURED = 0.05
PUBLISH_MAX_OBSCURED = 0.25
FROZEN_MIN_SNOW = 0.5
POST_MONSOON_MONTHS = (8, 9, 10)

# Terrain beyond the lake grid that can still cast shadow onto it.
SHADOW_MARGIN_M = 5000.0


class PixelClass(IntEnum):
    LAND = 0
    WATER = 1
    CLOUD = 2
    SNOW_ICE = 3
    SHADOW = 4


UNOBSERVABLE = (PixelClass.CLOUD, PixelClass.SNOW_ICE, PixelClass.SHADOW)


@dataclass(frozen=True)
class Lake:
    """A lake to monitor.

    `bbox` (lon/lat) sets the measurement grid. `seed` (lon/lat) is an
    inventory outline, or a point inside the lake that the first
    confident observation grows into a full footprint.
    """

    lake_id: str
    name: str
    bbox: tuple[float, float, float, float]
    seed: BaseGeometry


@dataclass
class Footprint:
    """Where the lake has been seen: its seed plus every confident outline.

    Coordinates are in the lake grid's CRS.
    """

    extent: BaseGeometry

    @property
    def geometry(self):
        """The extent dilated by two pixels, so the lake can grow."""
        return self.extent.buffer(FOOTPRINT_DILATION_M)

    def grow(self, outline):
        self.extent = unary_union([self.extent, outline])


@dataclass(frozen=True)
class Observation:
    """One lake measured in one scene, with its area as a range."""

    lake_id: str
    scene_id: str
    acquired: datetime
    status: str
    area_low_km2: float
    area_high_km2: float
    edge_uncertainty_km2: float
    obscured_fraction: float
    class_shares: dict[str, float]
    outline: BaseGeometry = field(repr=False)
    classifier_version: str = CLASSIFIER_VERSION

    @property
    def published(self):
        """Whether this observation may be published as a number."""
        return self.status in ("best", "range")

    @property
    def area_best_km2(self):
        """A single figure, only when at most 5% of the lake is hidden."""
        return self.area_low_km2 if self.status == "best" else None

    def to_record(self):
        """JSON-ready summary, without the outline geometry."""
        best = self.area_best_km2
        return {
            "lake_id": self.lake_id,
            "scene_id": self.scene_id,
            "acquired": self.acquired.isoformat(),
            "status": self.status,
            "area_best_km2": None if best is None else round(best, 4),
            "area_low_km2": round(self.area_low_km2, 4),
            "area_high_km2": round(self.area_high_km2, 4),
            "edge_uncertainty_km2": round(self.edge_uncertainty_km2, 4),
            "obscured_fraction": round(self.obscured_fraction, 4),
            "class_shares": self.class_shares,
            "classifier_version": self.classifier_version,
        }


class Terrain:
    """Elevation around a lake grid, for sun-dependent shadow."""

    def __init__(self, heights, grid):
        self.heights = heights
        self.grid = grid
        self._shadows = {}

    @classmethod
    def around(cls, grid):
        """Read heights for `grid` plus a margin wide enough for shadows."""
        dem_grid = grid.expand(SHADOW_MARGIN_M, DEM_RES_M)
        return cls(read_dem(dem_grid), dem_grid)

    def shadow(self, sun, grid):
        """Terrain shadow on `grid` for a sun (azimuth, elevation)."""
        if sun not in self._shadows:
            self._shadows[sun] = cast_shadow(
                self.heights, self.grid.res, *sun, SHADOW_MARGIN_M,
            ).astype(np.uint8)
        return self._onto(self._shadows[sun], grid).astype(bool)

    def _onto(self, values, grid):
        out = np.zeros(grid.shape, dtype=values.dtype)
        reproject(values, out, src_transform=self.grid.transform,
                  src_crs=self.grid.crs, dst_transform=grid.transform,
                  dst_crs=grid.crs, resampling=Resampling.nearest)
        return out


class LakeMonitor:
    """Measures one lake scene by scene on a fixed 10 m grid."""

    def __init__(self, lake, res=10.0):
        self.lake = lake
        self.grid = Grid.around(lake.bbox, res)
        self.terrain = Terrain.around(self.grid)
        to_grid = Transformer.from_crs(4326, self.grid.epsg, always_xy=True)
        seed = transform_geometry(to_grid.transform, lake.seed)
        if seed.area == 0:
            seed = seed.buffer(SEED_RADIUS_M)
        self.footprint = Footprint(seed)

    def monthly(self, start, end):
        """Measure the best scene of each month between two dates.

        Confident ("best") observations grow the footprint as they come,
        so months are processed in date order.
        """
        months = defaultdict(list)
        for item in search_scenes(self.lake.bbox, start, end):
            months[(item.datetime.year, item.datetime.month)].append(item)
        observations = []
        for (year, month), items in sorted(months.items()):
            item, obscured = self.best_scene(items, date(year, month, 15))
            observation = self.measure(item)
            log.info("%s %d-%02d: %s (%.0f%% obscured at 20 m) -> %s",
                     self.lake.lake_id, year, month, item.id,
                     100 * obscured, observation.status)
            if observation.status == "best":
                self.footprint.grow(observation.outline)
            observations.append(observation)
        return observations

    def best_scene(self, items, target):
        """The least obscured scene; ties go to the one nearest `target`."""
        scored = [(self.obscured_fraction(item),
                   abs((item.datetime.date() - target).days), item)
                  for item in items]
        obscured, _, item = min(scored, key=lambda entry: entry[:2])
        return item, obscured

    def obscured_fraction(self, item):
        """Unobservable share of the footprint, scored on a 20 m grid.

        The 20 m read comes from the COG overviews, so scoring a
        candidate costs a fraction of a full measurement.
        """
        scene = load_scene(item, self.grid.coarsen(2))
        classes, footprint = self._classify(scene)
        return float(np.isin(classes[footprint], UNOBSERVABLE).mean())

    def measure(self, item):
        """Classify one scene at 10 m and report the lake's area range."""
        classes, footprint = self._classify(load_scene(item, self.grid))
        outline = lake_outline(classes, self.grid, self.footprint.geometry)
        return observe(self.lake.lake_id, item, classes, footprint,
                       outline, self.grid.res)

    def _classify(self, scene):
        grid = scene.grid
        classes = classify(scene.green, scene.nir, scene.swir, scene.scl,
                           self.terrain.shadow(scene.sun, grid))
        return classes, rasterize(self.footprint.geometry, grid)


def classify(green, nir, swir, scl, shadow):
    """Class every pixel from reflectance, SCL and the terrain shadow.

    Precedence runs cloud, snow/ice, water, shadow, land. A shadowed
    pixel that passes the water test stays water; one that fails it is
    unobservable rather than land.

    There is deliberately no slope mask: the 2011-2015 elevation model
    shows about 8% of today's lake as steeper than 15 degrees, where
    glacier has since melted into lake, so a slope rule hides growth.
    """
    with np.errstate(divide="ignore", invalid="ignore"):
        ndwi = (green - nir) / (green + nir)
        ndsi = (green - swir) / (green + swir)
    no_data = ~(np.isfinite(green) & np.isfinite(nir) & np.isfinite(swir))
    water = (ndwi >= NDWI_WATER_MIN) & (nir <= NIR_WATER_MAX)
    snow = (ndsi >= NDSI_SNOW_MIN) & (nir > NIR_WATER_MAX)

    classes = np.full(green.shape, PixelClass.LAND, dtype=np.uint8)
    classes[shadow | (scl == SCL_CLOUD_SHADOW)] = PixelClass.SHADOW
    classes[water] = PixelClass.WATER
    classes[snow] = PixelClass.SNOW_ICE
    classes[no_data | np.isin(scl, SCL_CLOUD_OR_NODATA)] = PixelClass.CLOUD
    return classes


def lake_outline(classes, grid, footprint):
    """Water connected to the footprint (4-connectivity), as one geometry.

    The whole grid is polygonised, so a lake that has spread beyond its
    footprint is measured in full.
    """
    water = classes == PixelClass.WATER
    parts = [shape(geom) for geom, _ in
             shapes(water.astype(np.uint8), mask=water,
                    transform=grid.transform)]
    return unary_union([part for part in parts if part.intersects(footprint)])


def observe(lake_id, item, classes, footprint_mask, outline, res):
    """Area range and confidence for one classified scene (section 2.3)."""
    in_footprint = classes[footprint_mask]
    hidden = np.isin(in_footprint, UNOBSERVABLE)
    if in_footprint.size:
        obscured = float(hidden.mean())
        shares = {cls.name.lower(): round(float((in_footprint == cls).mean()),
                                          4)
                  for cls in PixelClass}
    else:
        obscured = 1.0
        shares = {cls.name.lower(): 0.0 for cls in PixelClass}
    area_low = outline.area / 1e6
    return Observation(
        lake_id=lake_id,
        scene_id=item.id,
        acquired=item.datetime,
        status=status_for(obscured, shares["snow_ice"]),
        area_low_km2=area_low,
        area_high_km2=area_low + hidden.sum() * res * res / 1e6,
        edge_uncertainty_km2=outline.length * res / 2 / 1e6,
        obscured_fraction=obscured,
        class_shares=shares,
        outline=outline,
    )


def status_for(obscured, snow_share):
    """How far an observation can be trusted.

    "frozen": snow or ice covers more than half the footprint (frozen
    over, or under snow and slush). "rejected": more than 25% is hidden,
    so no number is published. "best": at most 5% is hidden, so the low
    end stands as the area. "range": anything in between.
    """
    if snow_share > FROZEN_MIN_SNOW:
        return "frozen"
    if obscured > PUBLISH_MAX_OBSCURED:
        return "rejected"
    if obscured <= BEST_MAX_OBSCURED:
        return "best"
    return "range"


def growth_trend(observations):
    """Each year's post-monsoon area and the linear trend through them.

    For each year, takes the least obscured published observation from
    August to October. A trend needs at least two years.
    """
    yearly = {}
    for obs in observations:
        if not obs.published or obs.acquired.month not in POST_MONSOON_MONTHS:
            continue
        year = obs.acquired.year
        if (year not in yearly
                or obs.obscured_fraction < yearly[year].obscured_fraction):
            yearly[year] = obs
    years = sorted(yearly)
    areas = [yearly[year].area_low_km2 for year in years]
    trend = {"area_km2_by_year": dict(zip(years, areas)),
             "km2_per_year": None, "percent_per_year": None}
    if len(years) >= 2:
        slope = float(np.polyfit(years, areas, 1)[0])
        trend["km2_per_year"] = slope
        trend["percent_per_year"] = 100 * slope / float(np.mean(areas))
    return trend


def rasterize(geometry, grid):
    """Boolean mask of the pixels whose centres fall inside `geometry`."""
    if geometry.is_empty:
        return np.zeros(grid.shape, dtype=bool)
    return geometry_mask([geometry], out_shape=grid.shape,
                         transform=grid.transform, invert=True)


def cast_shadow(heights, res, sun_azimuth, sun_elevation, max_distance):
    """Pixels the sun can't reach at the given position.

    A pixel is in shadow if it faces away from the sun, or if terrain
    along the line towards the sun rises above the sun's elevation
    angle. Marching stops once even the highest point in view could no
    longer reach the line, or at `max_distance`.
    """
    azimuth, elevation = map(math.radians, (sun_azimuth, sun_elevation))
    d_row, d_col = np.gradient(heights, res)
    sun_east = math.sin(azimuth) * math.cos(elevation)
    sun_north = math.cos(azimuth) * math.cos(elevation)
    # Surface normal is (-dz/dx, -dz/dy, 1), and rows run southwards.
    facing = -d_col * sun_east + d_row * sun_north + math.sin(elevation)
    shadow = facing <= 0

    relief = float(np.nanmax(heights) - np.nanmin(heights))
    rise = math.tan(elevation)
    if not math.isfinite(relief) or rise <= 0:
        return shadow
    reach = min(max_distance, relief / rise)
    seen = set()
    for step in range(1, int(reach / res) + 1):
        offset = (round(-step * math.cos(azimuth)),
                  round(step * math.sin(azimuth)))
        if offset in seen:
            continue
        seen.add(offset)
        distance = res * math.hypot(*offset)
        shadow |= _shifted(heights, *offset) > heights + distance * rise
    return shadow


def _shifted(values, d_row, d_col):
    """`values[r + d_row, c + d_col]` at every (r, c); -inf off the edge."""
    rows, cols = values.shape
    out = np.full(values.shape, -np.inf, dtype=values.dtype)
    out[max(-d_row, 0):rows - max(d_row, 0),
        max(-d_col, 0):cols - max(d_col, 0)] = values[
            max(d_row, 0):rows - max(-d_row, 0),
            max(d_col, 0):cols - max(-d_col, 0)]
    return out
