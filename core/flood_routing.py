"""Downstream flood routing from South Lhonak Lake to the Teesta-III dam.

Implements section 3 of docs/architecture-brief.md in three parts:

1. Terrain: trace the flow path down the Copernicus GLO-30 elevation
   model from the lake to the dam, then cut a valley cross-section every
   ~500 m and tabulate its flow area, top width and conveyance by depth.
2. Source: for the 2023 hindcast, the lake outflow reconstructed by
   Sattar et al. (2025); for other lakes, a triangular hydrograph whose
   peak comes from published moraine-breach formulas of volume alone.
3. Routing: variable-parameter Muskingum-Cunge in Ponce's form, with
   wave speed and width read from each reach's own table.

Terrain extraction reads the DEM once and saves the channel as JSON
(`python -m core.flood_routing`); routing needs only that file.
"""
from __future__ import annotations

import heapq
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from pyproj import Transformer

from .sentinel_loader import DEM_RES_M, Grid, read_dem

# Hindcast geometry for the October 2023 flood.
LAKE_SEED_LONLAT = (88.1935, 27.9122)  # inside South Lhonak Lake
TEESTA_III_LONLAT = (88.6505, 27.5981)  # approximate; Global Energy Monitor
CORRIDOR_BBOX = (88.10, 27.50, 88.80, 28.05)
CHANNEL_FIXTURE = Path("tests/fixtures/south_lhonak_channel.json")

# Terrain extraction (section 3.1).
TARGET_SNAP_RADIUS_M = 1000.0  # the path ends at the lowest cell this close
OUTLET_DROP_M = 3.0  # this far below lake level, the path has left the lake
PATH_SMOOTHING_CELLS = 9
REACH_LENGTH_M = 500.0
SECTION_HALF_WIDTH_M = 1000.0
SECTION_SPACING_M = 15.0
THALWEG_SEARCH_M = 100.0
DEPTH_LEVELS_M = np.arange(0.0, 81.0, 1.0)
MIN_SLOPE = 0.001

# Source hydrograph (section 3.2). Peak outflow Qp = a * V**b for a
# moraine-dam breach, V in m3 and Qp in m3/s. Coefficients as cited in
# the GLOF literature; check them against the original papers.
PEAK_FLOW_FORMULAS = {
    "huggel_2002": (0.00077, 1.017),
    "evans_1986": (0.72, 0.53),
    "popov_1991": (0.0048, 0.896),
}
RISE_FRACTIONS = (0.1, 0.3)
BASE_FLOW_M3S = 50.0  # assumed river flow before the flood

# Lake-water outflow just below South Lhonak Lake (cross-section CS-1),
# as (minutes after the 22:12:20 IST moraine collapse, m3/s), digitised
# by eye from Sattar et al. (2025), Science, Fig. 3D, to about +/-1000
# m3/s and +/-0.3 min. A ~3-minute impulse wave overtops the moraine,
# breaching raises a second plateau, and from ~18 minutes the discharge
# is roughly constant. The paper models only these 30 minutes.
RECONSTRUCTED_OUTFLOW = (
    (0.0, 0.0), (1.2, 0.0), (1.9, 48500.0), (2.1, 43000.0),
    (2.3, 46500.0), (2.7, 30000.0), (3.35, 10000.0), (4.0, 4500.0),
    (4.9, 13000.0), (5.9, 16000.0), (7.8, 17000.0), (9.7, 18000.0),
    (11.6, 17000.0), (12.8, 14000.0), (14.1, 12000.0), (16.0, 10000.0),
    (18.5, 9500.0), (21.0, 9500.0), (23.6, 11500.0), (26.1, 11000.0),
    (30.0, 10500.0),
)
# Beyond 30 minutes (an assumption, not from the paper): the constant
# discharge holds until the drained volume is out, then ramps down.
RECESSION_S = 600.0

# Routing (sections 3.3 to 3.5).
TIME_STEP_S = 30.0  # upper limit; shortened when waves are fast
MAX_COURANT = 1.9  # non-negative weights need a Courant number below 2
DURATION_S = 12 * 3600.0
ARRIVAL_FRACTION = 0.1
MANNING_N_SEARCH = (0.01, 1.0)
# Fixed before the first calibration run (brief section 4.5).
MANNING_N_PLAUSIBLE = (0.03, 0.20)

NEIGHBOURS = ((-1, -1), (-1, 0), (-1, 1), (0, -1),
              (0, 1), (1, -1), (1, 0), (1, 1))


@dataclass
class Channel:
    """Reach-by-reach geometry from the lake outlet to the dam.

    Tables are indexed (reach, depth level). Conveyance K is the
    strip-summed A * R**(2/3) times S0**0.5 (see `section_geometry`), so
    Manning's flow is K / n and one table serves every roughness.
    """

    reach_length_m: float
    slopes: np.ndarray
    depths_m: np.ndarray
    areas_m2: np.ndarray
    widths_m: np.ndarray
    conveyance: np.ndarray
    nodes_lonlat: np.ndarray
    meta: dict = field(default_factory=dict)

    @property
    def length_m(self):
        return self.reach_length_m * len(self.slopes)

    def save(self, path):
        record = {
            "reach_length_m": round(self.reach_length_m, 3),
            "slopes": np.round(self.slopes, 6).tolist(),
            "depths_m": self.depths_m.tolist(),
            "areas_m2": np.round(self.areas_m2, 1).tolist(),
            "widths_m": np.round(self.widths_m, 1).tolist(),
            "conveyance": np.round(self.conveyance, 2).tolist(),
            "nodes_lonlat": np.round(self.nodes_lonlat, 6).tolist(),
            "meta": self.meta,
        }
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(record), encoding="utf-8")

    @classmethod
    def load(cls, path):
        record = json.loads(Path(path).read_text(encoding="utf-8"))
        arrays = {key: np.asarray(record[key], dtype=float) for key in (
            "slopes", "depths_m", "areas_m2", "widths_m", "conveyance",
            "nodes_lonlat")}
        return cls(reach_length_m=record["reach_length_m"],
                   meta=record["meta"], **arrays)


@dataclass(frozen=True)
class Hindcast:
    """Flood at the dam for each ensemble member (formula, rise fraction)."""

    members: tuple
    manning_n: float
    source_peak_m3s: np.ndarray
    arrival_s: np.ndarray
    peak_m3s: np.ndarray
    peak_time_s: np.ndarray
    depth_m: np.ndarray
    volume_in_m3: np.ndarray
    volume_out_m3: np.ndarray


def build_channel(seed_lonlat=LAKE_SEED_LONLAT, dam_lonlat=TEESTA_III_LONLAT,
                  bbox=CORRIDOR_BBOX):
    """Trace the flow path on the DEM and cut a cross-section per reach."""
    grid = Grid.around(bbox, res=DEM_RES_M, snap=DEM_RES_M)
    heights = read_dem(grid)
    to_grid = Transformer.from_crs(4326, grid.epsg, always_xy=True)
    to_lonlat = Transformer.from_crs(grid.epsg, 4326, always_xy=True)
    source = _cell(grid, *to_grid.transform(*seed_lonlat))
    dam_xy = to_grid.transform(*dam_lonlat)
    target = _lowest_cell_near(heights, grid, dam_xy, TARGET_SNAP_RADIUS_M)

    rows, cols = trace_flow_path(heights, source, target)
    x, y = (_smooth(values) for values in _cell_centres(grid, rows, cols))
    along = np.concatenate([[0.0], np.cumsum(np.hypot(np.diff(x),
                                                      np.diff(y)))])
    profile = sample_bilinear(heights, grid, x, y)
    lake_level = float(heights[source])
    outlet = _outlet_index(profile, lake_level)
    dam_distance = np.hypot(x - dam_xy[0], y - dam_xy[1])
    dam = outlet + int(np.argmin(dam_distance[outlet:]))

    length = along[dam] - along[outlet]
    reaches = max(1, round(length / REACH_LENGTH_M))
    reach_length = length / reaches
    stations = along[outlet] + reach_length * np.arange(reaches + 1)
    bed = np.interp(stations, along, monotone_profile(profile))
    slopes = np.maximum((bed[:-1] - bed[1:]) / reach_length, MIN_SLOPE)

    tables = [section_geometry(_cross_section(heights, grid, along, x, y,
                                              station + reach_length / 2),
                               SECTION_SPACING_M, DEPTH_LEVELS_M)
              for station in stations[:-1]]
    areas, widths, factors = (np.array(t) for t in zip(*tables))
    conveyance = factors * np.sqrt(slopes)[:, None]

    node_x = np.interp(stations, along, x)
    node_y = np.interp(stations, along, y)
    nodes = np.column_stack(to_lonlat.transform(node_x, node_y))
    meta = {
        "dem": "Copernicus GLO-30",
        "seed_lonlat": list(seed_lonlat),
        "dam_lonlat": list(dam_lonlat),
        "dam_offset_m": round(float(dam_distance[dam]), 1),
        "lake_level_m": round(lake_level, 1),
        "outlet_bed_m": round(float(bed[0]), 1),
        "dam_bed_m": round(float(bed[-1]), 1),
        "path_cells": int(len(rows)),
    }
    return Channel(reach_length, slopes, DEPTH_LEVELS_M.copy(), areas,
                   widths, conveyance, nodes, meta)


def trace_flow_path(heights, source, target):
    """Cells from `source` to `target`, as (rows, cols) arrays.

    Grows outward from the source, always expanding the lowest cell on
    the frontier, as water would: it runs down the valley floor, fills
    any DEM pit until it spills, and never wanders up a hillside while a
    lower route is open. The path is the chain of cells that led to the
    target. Ties break first-in, first-out, so the result is
    deterministic.
    """
    rows, cols = heights.shape
    start = source[0] * cols + source[1]
    goal = target[0] * cols + target[1]
    parent = {start: start}
    frontier = [(float(heights[source]), 0, start)]
    pushed = 1
    while frontier:
        _, _, cell = heapq.heappop(frontier)
        if cell == goal:
            break
        row, col = divmod(cell, cols)
        for d_row, d_col in NEIGHBOURS:
            r, c = row + d_row, col + d_col
            if 0 <= r < rows and 0 <= c < cols:
                neighbour = r * cols + c
                level = float(heights[r, c])
                if neighbour not in parent and math.isfinite(level):
                    parent[neighbour] = cell
                    heapq.heappush(frontier, (level, pushed, neighbour))
                    pushed += 1
    else:
        raise RuntimeError("the target is not reachable from the source")
    path = [goal]
    while path[-1] != start:
        path.append(parent[path[-1]])
    cells = np.array(path[::-1])
    return cells // cols, cells % cols


def monotone_profile(heights):
    """A downhill-only river profile from noisy DEM samples.

    The mean of the running minimum from the source (which removes
    spikes) and the running maximum from the far end (which removes
    pits).
    """
    from_source = np.fmin.accumulate(heights)
    from_mouth = np.fmax.accumulate(heights[::-1])[::-1]
    return (from_source + from_mouth) / 2


def sample_bilinear(heights, grid, x, y):
    """Heights at map coordinates, interpolated between cell centres."""
    col = (np.asarray(x) - grid.transform.c) / grid.res - 0.5
    row = (grid.transform.f - np.asarray(y)) / grid.res - 0.5
    rows, cols = heights.shape
    c0 = np.clip(np.floor(col).astype(int), 0, cols - 2)
    r0 = np.clip(np.floor(row).astype(int), 0, rows - 2)
    fc = np.clip(col - c0, 0.0, 1.0)
    fr = np.clip(row - r0, 0.0, 1.0)
    top = heights[r0, c0] * (1 - fc) + heights[r0, c0 + 1] * fc
    bottom = heights[r0 + 1, c0] * (1 - fc) + heights[r0 + 1, c0 + 1] * fc
    return top * (1 - fr) + bottom * fr


def section_geometry(heights, spacing, depths):
    """Flow area, top width and conveyance factor of a section by depth.

    Water fills the contiguous run of samples around the thalweg (the
    lowest point near the centreline) that lie below the water surface.

    Conveyance is summed strip by strip, each sample's strip using its
    own depth: sum of a * (a / p)**(2/3) over strips of area a and bed
    length p. Taking the whole section at once would make conveyance
    fall as water spreads over a flat valley floor, which it did in 83
    of the 132 South Lhonak reaches; per strip, it only ever grows.
    """
    centre = len(heights) // 2
    search = int(THALWEG_SEARCH_M / spacing)
    thalweg = centre - search + int(
        np.argmin(heights[centre - search:centre + search + 1]))
    bed = heights[thalweg]
    bed_length = np.hypot(spacing, np.gradient(heights))
    areas, widths, factors = (np.zeros(len(depths)) for _ in range(3))
    left = right = thalweg
    for k, depth in enumerate(depths):
        level = bed + depth
        while left > 0 and heights[left - 1] < level:
            left -= 1
        while right < len(heights) - 1 and heights[right + 1] < level:
            right += 1
        strip_area = np.clip(level - heights[left:right + 1], 0.0,
                             None) * spacing
        areas[k] = strip_area.sum()
        widths[k] = (right - left + 1) * spacing
        factors[k] = np.sum(strip_area ** (5 / 3)
                            / bed_length[left:right + 1] ** (2 / 3))
    return areas, widths, factors


def peak_outflow(volume_m3, formula):
    """Breach peak outflow (m3/s) from the drained volume alone."""
    a, b = PEAK_FLOW_FORMULAS[formula]
    return a * volume_m3 ** b


def triangular_hydrograph(volume_m3, peak_m3s, rise_fraction, times_s,
                          base_m3s=BASE_FLOW_M3S):
    """Lake outflow shaped as a triangle holding `volume_m3` above base."""
    duration = 2 * volume_m3 / peak_m3s
    rise = rise_fraction * duration
    t = np.asarray(times_s, dtype=float)
    flood = np.where(t <= rise, peak_m3s * t / rise,
                     peak_m3s * (duration - t) / (duration - rise))
    return base_m3s + np.clip(flood, 0.0, None)


def reconstructed_hydrograph(volume_m3, times_s, base_m3s=BASE_FLOW_M3S):
    """The published 2023 outflow, extended to release `volume_m3`.

    Follows RECONSTRUCTED_OUTFLOW for its 30 minutes, holds the final,
    constant discharge until all but the recession's share of the volume
    is out, then ramps linearly to zero over RECESSION_S.
    """
    minutes, flows = np.array(RECONSTRUCTED_OUTFLOW).T
    seconds = minutes * 60
    published = np.sum(np.diff(seconds) * (flows[1:] + flows[:-1]) / 2)
    held = flows[-1]
    remaining = volume_m3 - published - held * RECESSION_S / 2
    if remaining < 0:
        raise ValueError("volume is below the published 30 minutes' outflow")
    hold_end = seconds[-1] + remaining / held
    knots_s = np.append(seconds, [hold_end, hold_end + RECESSION_S])
    knots_q = np.append(flows, [held, 0.0])
    return base_m3s + np.interp(times_s, knots_s, knots_q, right=0.0)


def source_members(volume_m3, source):
    """Ensemble members (label, rise fraction) and their peak outflows.

    "reconstruction" is the single outflow reconstructed for 2023;
    "formulas" is a triangle per peak formula and rise fraction.
    """
    if source == "reconstruction":
        peak = max(flow for _, flow in RECONSTRUCTED_OUTFLOW)
        return (("sattar_2025", None),), np.array([peak])
    if source == "formulas":
        members = tuple((formula, rise) for formula in PEAK_FLOW_FORMULAS
                        for rise in RISE_FRACTIONS)
        peaks = [peak_outflow(volume_m3, formula) for formula, _ in members]
        return members, np.array(peaks)
    raise ValueError(f"unknown source {source!r}")


def source_hydrographs(volume_m3, times_s, members, peaks):
    """Outflow at the lake for each member, shaped (steps, members)."""
    return np.column_stack([
        reconstructed_hydrograph(volume_m3, times_s) if rise is None
        else triangular_hydrograph(volume_m3, peak, rise, times_s)
        for (_, rise), peak in zip(members, peaks)])


def route(channel, inflows, manning_n, dt, iterations=3):
    """Route outlet hydrographs to the dam with variable-parameter
    Muskingum-Cunge, in storage-consistent form.

    `inflows` is shaped (steps, members), sampled every `dt` seconds and
    starting at base flow; `manning_n` is one value or one per member.
    Returns the flow at every node, shaped (steps, nodes, members). Pick
    `dt` with `stable_time_step`.

    Each reach stores S = K [X I + (1 - X) O] of its inflow I and
    outflow O, with Cunge's parameters K = dx / c and X = (1 - D) / 2:
    wave speed c = dQ/dA and cell Reynolds number D = Q / (B S0 c dx),
    read from the reach's table at the reach's mean flow. Continuity
    over one step,
        S[n+1] - S[n] = dt/2 (I[n] + I[n+1] - O[n] - O[n+1]),
    with each side's storage taken at its own K and X, gives
        O[n+1] = ((dt/2 - K'X') I[n+1] + (dt/2 + KX) I[n]
                  + (K(1 - X) - dt/2) O[n]) / (K'(1 - X') + dt/2),
    primes marking step n+1. K' and X' depend on O[n+1], so they are
    refined over a few passes. With constant K and X this is classic
    Muskingum; letting them vary while each step keeps its own storage
    is the idea behind Todini's (2007) mass-conservative Muskingum-Cunge.
    Storage telescopes from step to step, so water is conserved exactly.
    The common shortcut, one parameter set per step, made up to 80%
    extra water on this flood, whose wave speed varies 80-fold.

    A dam-break front on 5-20% slopes is close to a kinematic shock (D
    near 0) and slow at its foot (Courant number C = c dt / dx well
    below 1). There, X = (1 - D) / 2 makes the I[n+1] weight negative:
    outflow dips below base, goes negative, and the scheme blows up.
    Muskingum's positivity condition, X <= C/2 and X <= 1 - C/2, keeps
    every weight non-negative, so X is lowered to meet it only where it
    binds, adding the least numerical diffusion that keeps flows
    positive.

    Cells are solved one anti-diagonal (step + node) at a time: each
    depends only on earlier diagonals, so a diagonal is one array step.
    """
    inflows = np.asarray(inflows, dtype=float)
    steps, members = inflows.shape
    reaches = len(channel.slopes)
    n = np.broadcast_to(np.asarray(manning_n, dtype=float), (members,))
    waves = _WaveTable(channel)
    dx = channel.reach_length_m
    half = dt / 2

    def muskingum(reach, mean_flow):
        dk_da, width = waves.lookup(reach, mean_flow * n)
        celerity = dk_da / n
        courant = celerity * dt / dx
        if courant.max() > 2.0:
            raise ValueError("Courant number above 2; shorten dt")
        cell_reynolds = mean_flow / (
            width * channel.slopes[reach, None] * celerity * dx)
        x = np.clip((1 - cell_reynolds) / 2, 0.0, 0.5)
        x = np.minimum(x, np.minimum(courant, 2 - courant) / 2)
        return dx / celerity, x

    flow = np.empty((steps, reaches + 1, members))
    flow[0] = inflows[0]
    flow[:, 0] = inflows
    # Each reach's K and X from its latest step, starting at base flow.
    k_last, x_last = muskingum(np.arange(reaches),
                               np.broadcast_to(inflows[0], (reaches, members)))
    for diagonal in range(2, steps + reaches):
        node = np.arange(max(1, diagonal - steps + 1),
                         min(reaches, diagonal - 1) + 1)
        step, reach = diagonal - node, node - 1
        i_new, i_old = flow[step, reach], flow[step - 1, reach]
        o_old = flow[step - 1, node]
        k, x = k_last[reach], x_last[reach]
        carried = (half + k * x) * i_old + (k * (1 - x) - half) * o_old
        o_new = o_old
        for _ in range(iterations):
            k_new, x_new = muskingum(reach, (i_new + o_new) / 2)
            o_new = (((half - k_new * x_new) * i_new + carried)
                     / (k_new * (1 - x_new) + half))
        flow[step, node] = o_new
        k_last[reach], x_last[reach] = k_new, x_new
    return flow


def stable_time_step(channel, peak_m3s, manning_n, limit=TIME_STEP_S):
    """The longest step, up to `limit`, that keeps the Courant number at
    or below MAX_COURANT in every reach for flows up to `peak_m3s`."""
    n = float(np.min(manning_n))
    fastest = _WaveTable(channel).max_dk_da(peak_m3s * n) / n
    return min(limit, MAX_COURANT * channel.reach_length_m / fastest)


def hindcast(channel, volume_m3, manning_n, source="reconstruction",
             duration_s=DURATION_S):
    """Route the lake outflow to the dam at one n (see `source_members`).

    Times are seconds after the release; for 2023, after the 22:12:20
    IST moraine collapse.
    """
    members, source_peaks = source_members(volume_m3, source)
    dt = stable_time_step(channel, source_peaks.max() + BASE_FLOW_M3S,
                          manning_n)
    times = np.arange(0.0, duration_s + dt / 2, dt)
    inflows = source_hydrographs(volume_m3, times, members, source_peaks)
    at_dam = route(channel, inflows, manning_n, dt)[:, -1]
    peaks = at_dam.max(axis=0)
    return Hindcast(
        members=members,
        manning_n=float(manning_n),
        source_peak_m3s=source_peaks,
        arrival_s=arrival_times(times, at_dam),
        peak_m3s=peaks,
        peak_time_s=times[at_dam.argmax(axis=0)],
        depth_m=normal_depth(channel, -1, peaks, manning_n),
        volume_in_m3=(inflows - BASE_FLOW_M3S).sum(axis=0) * dt,
        volume_out_m3=(at_dam - BASE_FLOW_M3S).sum(axis=0) * dt,
    )


def calibrate_manning(channel, volume_m3, travel_time_s,
                      source="reconstruction", search=MANNING_N_SEARCH,
                      tolerance_s=5.0):
    """The one Manning's n whose median hindcast arrival is on time.

    Only arrival time is fitted; the lake outflow never sees n. Bisects
    in log(n), since rougher channels only ever arrive later.
    """
    def lateness(n):
        result = hindcast(channel, volume_m3, n, source)
        return float(np.median(result.arrival_s)) - travel_time_s

    low, high = search
    if lateness(low) > 0 or lateness(high) < 0:
        raise ValueError(f"no n in {search} matches {travel_time_s:.0f} s")
    for _ in range(60):
        middle = math.sqrt(low * high)
        miss = lateness(middle)
        if abs(miss) <= tolerance_s:
            break
        low, high = (middle, high) if miss < 0 else (low, middle)
    return middle


def arrival_times(times_s, flow):
    """When each series first rises past base + 10% of its peak rise.

    Interpolates linearly between time steps. `flow` is (steps, members).
    """
    peak = flow.max(axis=0)
    threshold = BASE_FLOW_M3S + ARRIVAL_FRACTION * (peak - BASE_FLOW_M3S)
    after = np.argmax(flow >= threshold, axis=0)
    before = np.maximum(after - 1, 0)
    member = np.arange(flow.shape[1])
    q0, q1 = flow[before, member], flow[after, member]
    fraction = np.divide(threshold - q0, q1 - q0,
                         out=np.zeros_like(q0), where=q1 > q0)
    return times_s[before] + fraction * (times_s[after] - times_s[before])


def normal_depth(channel, reach, flow_m3s, manning_n):
    """Depth at which Manning's flow in `reach` equals `flow_m3s`."""
    conveyance = np.maximum.accumulate(channel.conveyance[reach])
    needed = np.asarray(flow_m3s) * np.asarray(manning_n)
    return np.interp(needed, conveyance, channel.depths_m)


class _WaveTable:
    """dK/dA and top width per reach on a shared log-conveyance grid.

    With Q = K / n, wave speed c = dQ/dA = (dK/dA) / n, so one table
    serves every n, and the shared grid lets a whole anti-diagonal of
    reaches be looked up at once.
    """

    POINTS = 512

    def __init__(self, channel):
        conveyance = np.maximum.accumulate(channel.conveyance, axis=1)
        self.log_low = math.log(conveyance[conveyance > 0].min())
        self.log_high = math.log(conveyance.max())
        self.step = (self.log_high - self.log_low) / (self.POINTS - 1)
        self.grid = np.exp(np.linspace(self.log_low, self.log_high,
                                       self.POINTS))
        mid = (conveyance[:, 1:] + conveyance[:, :-1]) / 2
        dk_da = np.diff(conveyance, axis=1) / np.diff(channel.areas_m2, axis=1)
        floor = np.median(dk_da) * 1e-3
        self.dk_da = np.array([np.interp(self.grid, m, np.maximum(s, floor))
                               for m, s in zip(mid, dk_da)])
        self.width = np.array([np.interp(self.grid, k, w) for k, w in
                               zip(conveyance, channel.widths_m)])

    def max_dk_da(self, conveyance):
        """The largest dK/dA in any reach for conveyance up to a limit."""
        reachable = self.grid <= max(conveyance, self.grid[0])
        return float(self.dk_da[:, reachable].max())

    def lookup(self, reaches, conveyance):
        position = (np.log(np.maximum(conveyance, 1e-12)) - self.log_low)
        position = np.clip(position / self.step, 0, self.POINTS - 1 - 1e-9)
        low = position.astype(int)
        fraction = position - low
        rows = reaches[:, None]

        def at(table):
            return (table[rows, low] * (1 - fraction)
                    + table[rows, low + 1] * fraction)

        return at(self.dk_da), at(self.width)


def _cross_section(heights, grid, along, x, y, station):
    """DEM samples across the valley, perpendicular to the path."""
    ahead, behind = station + 100.0, station - 100.0
    dx = np.interp(ahead, along, x) - np.interp(behind, along, x)
    dy = np.interp(ahead, along, y) - np.interp(behind, along, y)
    norm = math.hypot(dx, dy)
    normal = (-dy / norm, dx / norm)
    offsets = np.arange(-SECTION_HALF_WIDTH_M,
                        SECTION_HALF_WIDTH_M + SECTION_SPACING_M / 2,
                        SECTION_SPACING_M)
    cx, cy = np.interp(station, along, x), np.interp(station, along, y)
    return sample_bilinear(heights, grid, cx + offsets * normal[0],
                           cy + offsets * normal[1])


def _outlet_index(profile, lake_level):
    """The last path point still at lake level, before the descent."""
    at_lake = np.flatnonzero(profile >= lake_level - OUTLET_DROP_M)
    return int(at_lake[-1]) if at_lake.size else 0


def _lowest_cell_near(heights, grid, xy, radius):
    """The lowest cell within `radius` of a point: the valley floor."""
    row, col = _cell(grid, *xy)
    span = int(radius // grid.res) + 1
    top, left = max(row - span, 0), max(col - span, 0)
    window = heights[top:row + span + 1, left:col + span + 1]
    rows, cols = np.indices(window.shape)
    cx, cy = _cell_centres(grid, rows + top, cols + left)
    near = np.hypot(cx - xy[0], cy - xy[1]) <= radius
    lowest = np.argmin(np.where(near, window, np.inf))
    r, c = np.unravel_index(int(lowest), window.shape)
    return top + int(r), left + int(c)


def _cell(grid, x, y):
    return (int((grid.transform.f - y) // grid.res),
            int((x - grid.transform.c) // grid.res))


def _cell_centres(grid, rows, cols):
    return (grid.transform.c + (np.asarray(cols) + 0.5) * grid.res,
            grid.transform.f - (np.asarray(rows) + 0.5) * grid.res)


def _smooth(values, window=PATH_SMOOTHING_CELLS):
    padded = np.pad(values, window // 2, mode="edge")
    return np.convolve(padded, np.ones(window) / window, mode="valid")


def main():
    """Rebuild the South Lhonak channel fixture from the DEM."""
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else CHANNEL_FIXTURE
    channel = build_channel()
    channel.save(out)
    print(f"{len(channel.slopes)} reaches of {channel.reach_length_m:.1f} m "
          f"({channel.length_m / 1000:.2f} km) -> {out}")
    print(json.dumps(channel.meta, indent=1))


if __name__ == "__main__":
    main()
