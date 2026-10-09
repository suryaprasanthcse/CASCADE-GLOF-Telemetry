"""Write the Sentinel-2 true-colour lake images for the dossier page.

    python -m tools.site.make_lake_images --lake south_lhonak \
        --scene S2A_T45RXL_20230914T044830_L2A \
        --scene S2B_T45RXL_20231029T045728_L2A --out web/img

Each scene's true-colour (TCI) window is read on the same 10 m grid the
measurement uses and written as a JPEG. images.json lists every image with
its four corners in lon/lat (top left, top right, bottom right, bottom
left), which is how the page places it on the map. Contains modified
Copernicus Sentinel data 2023.
"""
import argparse
import json
import pathlib
import warnings
from datetime import datetime, timedelta

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.errors import NotGeoreferencedWarning

from core.sentinel_loader import Grid, read_true_colour, search_scenes
from handlers import registry


def find_item(bbox, scene_id):
    """The Earth Search item for a scene id like S2A_T45RXL_20230914T..."""
    day = datetime.strptime(scene_id.split("_")[2][:8], "%Y%m%d").date()
    for item in search_scenes(bbox, day, day + timedelta(days=1)):
        if item.id == scene_id:
            return item
    raise SystemExit(f"scene {scene_id} not found in Earth Search")


def corners_lonlat(grid):
    xmin, ymin, xmax, ymax = grid.bounds
    to_lonlat = Transformer.from_crs(grid.epsg, 4326, always_xy=True)
    return [[round(v, 6) for v in to_lonlat.transform(x, y)]
            for x, y in ((xmin, ymax), (xmax, ymax), (xmax, ymin),
                         (xmin, ymin))]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--lake", required=True)
    parser.add_argument("--scene", action="append", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    lake = registry.lake(args.lake)
    grid = Grid.around(lake.bbox, res=10.0)
    images = []
    # The JPEGs carry no georeferencing on purpose: images.json places them.
    warnings.filterwarnings("ignore", category=NotGeoreferencedWarning)
    for scene_id in args.scene:
        item = find_item(lake.bbox, scene_id)
        rgb = read_true_colour(item, grid).astype(np.uint8)
        name = f"{scene_id}.jpg"
        with rasterio.open(out / name, "w", driver="JPEG", width=rgb.shape[2],
                           height=rgb.shape[1], count=3, dtype="uint8",
                           quality=85) as dst:
            dst.write(rgb)
        images.append({"lake_id": lake.lake_id, "scene_id": scene_id,
                       "acquired": item.datetime.date().isoformat(),
                       "file": f"img/{name}",
                       "corners_lonlat": corners_lonlat(grid)})
        print(f"{name}: {rgb.shape[2]} x {rgb.shape[1]} px")
    (out / "images.json").write_text(json.dumps(images, indent=1) + "\n",
                                     encoding="utf-8")


if __name__ == "__main__":
    main()
