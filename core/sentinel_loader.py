"""Sentinel-2 L2A ingestion from AWS Open Data.

Finds scenes through the Earth Search STAC API, keeps one item per
satellite pass, and range-reads only the requested window of each
Cloud-Optimized GeoTIFF onto a fixed UTM grid. Also reads the Copernicus
GLO-30 elevation model, which the masking engine uses for terrain shadow
and slope.
"""
from __future__ import annotations

import logging
import math
import re
import warnings
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np
import rasterio
from affine import Affine
from pyproj import Transformer
from pystac import Item
from pystac_client import Client
from rasterio.enums import Resampling
from rasterio.errors import RasterioIOError
from rasterio.merge import merge
from rasterio.transform import from_origin
from rasterio.vrt import WarpedVRT
from rasterio.warp import reproject, transform_bounds
from rasterio.windows import from_bounds

log = logging.getLogger(__name__)

STAC_URL = "https://earth-search.aws.element84.com/v1"
# Collection 1 is ESA's reprocessed archive, one item per pass. The older
# collection is the fallback.
COLLECTIONS = ("sentinel-2-c1-l2a", "sentinel-2-l2a")
SCENE_BANDS = ("green", "nir", "swir16", "scl")
DEM_URL = (
    "https://copernicus-dem-30m.s3.amazonaws.com/"
    "Copernicus_DSM_COG_10_{tile}_DEM/Copernicus_DSM_COG_10_{tile}_DEM.tif"
)
DEM_RES_M = 30.0
GDAL_ENV = {
    "AWS_NO_SIGN_REQUEST": "YES",
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "GDAL_HTTP_MAX_RETRY": "3",
    "GDAL_HTTP_RETRY_DELAY": "1",
}
# From processing baseline 04.00, L2A pixel values carry +1000.
BOA_OFFSET_DN = 1000


@dataclass(frozen=True)
class Grid:
    """A north-up raster grid in a UTM coordinate system."""

    epsg: int
    transform: Affine
    shape: tuple[int, int]

    @classmethod
    def around(cls, bbox_lonlat, res=10.0, snap=20.0):
        """UTM grid covering a lon/lat box, its edges on a `snap` grid.

        Snapping to 20 m lines the grid up with Sentinel-2's 10 m and
        20 m pixels, so 10 m bands are read without resampling.
        """
        west, south, east, north = bbox_lonlat
        zone = int(((west + east) / 2 + 180) // 6) + 1
        epsg = (32600 if south + north >= 0 else 32700) + zone
        to_utm = Transformer.from_crs(4326, epsg, always_xy=True)
        bounds = to_utm.transform_bounds(*bbox_lonlat, densify_pts=21)
        return cls.from_bounds(epsg, bounds, res, snap)

    @classmethod
    def from_bounds(cls, epsg, bounds, res, snap=None):
        """Grid covering projected `bounds`, expanded outward to `snap`."""
        snap = snap or res
        xmin = math.floor(bounds[0] / snap) * snap
        ymin = math.floor(bounds[1] / snap) * snap
        xmax = math.ceil(bounds[2] / snap) * snap
        ymax = math.ceil(bounds[3] / snap) * snap
        shape = (round((ymax - ymin) / res), round((xmax - xmin) / res))
        return cls(epsg, from_origin(xmin, ymax, res, res), shape)

    @property
    def res(self):
        return self.transform.a

    @property
    def crs(self):
        return f"EPSG:{self.epsg}"

    @property
    def bounds(self):
        xmin, ymax = self.transform.c, self.transform.f
        rows, cols = self.shape
        return (xmin, ymax - rows * self.res, xmin + cols * self.res, ymax)

    def coarsen(self, factor):
        """The same extent with pixels `factor` times larger."""
        rows, cols = self.shape
        return Grid(self.epsg, self.transform * Affine.scale(factor),
                    (rows // factor, cols // factor))

    def expand(self, margin, res):
        """A wider grid with `margin` metres added on every side."""
        xmin, ymin, xmax, ymax = self.bounds
        wider = (xmin - margin, ymin - margin, xmax + margin, ymax + margin)
        return Grid.from_bounds(self.epsg, wider, res)


@dataclass(frozen=True)
class Scene:
    """One satellite pass on one grid, as surface reflectance."""

    item: Item
    grid: Grid
    green: np.ndarray
    nir: np.ndarray
    swir: np.ndarray
    scl: np.ndarray

    @property
    def sun(self):
        """Sun (azimuth, elevation) in degrees at acquisition."""
        props = self.item.properties
        return (float(props["view:sun_azimuth"]),
                float(props["view:sun_elevation"]))


def search_scenes(bbox_lonlat, start, end, collections=COLLECTIONS):
    """Scenes over a lon/lat box between two dates, one per pass.

    Takes every scene from the first collection that has any, so one
    search never mixes processing chains.
    """
    period = f"{_iso(start)}/{_iso(end)}"
    client = Client.open(STAC_URL)
    for collection in collections:
        with warnings.catch_warnings():
            # pystac can't map Collection 1's bucket URLs to its storage
            # extension and warns on every item; the hrefs work regardless.
            warnings.filterwarnings("ignore", message="Could not parse")
            found = list(client.search(collections=[collection],
                                       bbox=bbox_lonlat,
                                       datetime=period).items())
        items = one_per_pass(found)
        if items:
            log.info("%d scenes in %s from %s", len(items), period,
                     collection)
            return items
    return []


def one_per_pass(items):
    """Keep one item per satellite pass over a tile.

    Earth Search can list the same acquisition several times after
    reprocessing; the newest processing baseline wins.
    """
    best = {}
    for item in items:
        props = item.properties
        key = (props.get("platform"), props.get("grid:code"),
               item.datetime.date())
        if key not in best or _version(item) > _version(best[key]):
            best[key] = item
    if len(best) < len(items):
        log.info("dropped %d duplicate items", len(items) - len(best))
    return sorted(best.values(), key=lambda item: item.datetime)


def load_scene(item, grid):
    """Green, NIR, SWIR and SCL for one pass on `grid`.

    SWIR is 20 m natively, so on a 10 m grid it is interpolated
    bilinearly; every other band uses nearest neighbour.
    """
    with ThreadPoolExecutor(len(SCENE_BANDS)) as pool:
        green, nir, swir, scl = pool.map(
            lambda key: read_band(item, key, grid, _resampling(key)),
            SCENE_BANDS)
    present = boa_offset_present(declares_boa_offset(item), nir, swir)
    return Scene(item, grid,
                 to_reflectance(item, "green", green, present),
                 to_reflectance(item, "nir", nir, present),
                 to_reflectance(item, "swir16", swir, present),
                 scl)


def read_band(item, key, grid, resampling=Resampling.nearest):
    """Range-read one asset onto `grid`. Pixels outside the scene are 0."""
    with rasterio.Env(**GDAL_ENV), \
            rasterio.open(item.assets[key].href) as src:
        if src.crs.to_epsg() != grid.epsg:
            with WarpedVRT(src, crs=grid.crs, transform=grid.transform,
                           width=grid.shape[1], height=grid.shape[0],
                           resampling=resampling, nodata=0) as vrt:
                data = vrt.read()
        else:
            window = from_bounds(*grid.bounds, transform=src.transform)
            data = src.read(window=window,
                            out_shape=(src.count, *grid.shape),
                            resampling=resampling, boundless=True,
                            fill_value=0)
    return data[0] if len(data) == 1 else data


def read_true_colour(item, grid):
    """8-bit true-colour (TCI) composite on `grid`, shaped (3, rows, cols)."""
    return read_band(item, "visual", grid)


def declares_boa_offset(item):
    """Whether the metadata says pixel values still carry the BOA offset.

    Earth Search's older collection already removed it and sets
    `earthsearch:boa_offset_applied`, yet its raster:bands still list
    offset -0.1. Collection 1 keeps the offset and has no flag.
    """
    if item.properties.get("earthsearch:boa_offset_applied"):
        return False
    return _band_meta(item, "nir").get("offset", 0.0) != 0.0


def boa_offset_present(declared, nir_dn, swir_dn):
    """Check the declared BOA offset against the pixels themselves.

    With the offset present, the darkest 1% of both NIR and SWIR sit at
    or above 1000 DN. Without it, a lake window always has water, shadow
    or snow dark enough in one of the two bands to pull that below 1000.
    When the metadata says the offset is gone but the pixels disagree,
    the pixels win.
    """
    darkest = [np.percentile(dn[dn > 0], 1) if (dn > 0).any() else 0.0
               for dn in (nir_dn, swir_dn)]
    looks_present = min(darkest) >= BOA_OFFSET_DN
    if looks_present and not declared:
        log.warning("metadata says the BOA offset was removed, but the "
                    "pixels still carry it; removing it")
        return True
    if declared and not looks_present:
        log.warning("metadata declares a BOA offset, but the pixels look "
                    "offset-free; keeping the declared offset")
    return declared


def to_reflectance(item, key, dn, offset_present):
    """Surface reflectance from digital numbers, NaN where there's no data."""
    scale = _band_meta(item, key).get("scale", 1e-4)
    offset = -BOA_OFFSET_DN * scale if offset_present else 0.0
    reflectance = dn.astype(np.float32) * scale + offset
    reflectance[dn == 0] = np.nan
    return reflectance


def read_dem(grid):
    """Copernicus GLO-30 heights in metres, resampled onto `grid`.

    Reads only the part of each 1-degree tile that `grid` covers.
    """
    west, south, east, north = transform_bounds(
        grid.crs, "EPSG:4326", *grid.bounds, densify_pts=21)
    pad = 0.001  # about three DEM pixels, so bilinear has neighbours
    box = (west - pad, south - pad, east + pad, north + pad)
    urls = [_dem_url(lat, lon)
            for lat in range(math.floor(box[1]), math.floor(box[3]) + 1)
            for lon in range(math.floor(box[0]), math.floor(box[2]) + 1)]
    with rasterio.Env(**GDAL_ENV):
        tiles = []
        for url in urls:
            try:
                tiles.append(rasterio.open(url))
            except RasterioIOError:
                log.warning("no DEM tile at %s", url)
        if not tiles:
            raise RuntimeError(f"no Copernicus DEM tiles cover {box}")
        try:
            mosaic, transform = merge(tiles, bounds=box, nodata=np.nan)
        finally:
            for tile in tiles:
                tile.close()
    heights = np.full(grid.shape, np.nan, dtype=np.float32)
    reproject(mosaic[0], heights, src_transform=transform,
              src_crs="EPSG:4326", src_nodata=np.nan,
              dst_transform=grid.transform, dst_crs=grid.crs,
              dst_nodata=np.nan, resampling=Resampling.bilinear)
    return heights


def _iso(day):
    return day if isinstance(day, str) else day.isoformat()


def _version(item):
    props = item.properties
    baseline = props.get("s2:processing_baseline") or ""
    numbers = tuple(int(n) for n in re.findall(r"\d+", baseline))
    return (numbers, props.get("updated", ""), item.id)


def _resampling(key):
    return Resampling.bilinear if key == "swir16" else Resampling.nearest


def _band_meta(item, key):
    return (item.assets[key].extra_fields.get("raster:bands") or [{}])[0]


def _dem_url(lat, lon):
    tile = (f"{'N' if lat >= 0 else 'S'}{abs(lat):02d}_00_"
            f"{'E' if lon >= 0 else 'W'}{abs(lon):03d}_00")
    return DEM_URL.format(tile=tile)
