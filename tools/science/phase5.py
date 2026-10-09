"""Phase 5: the forecast replay (R4) and the hindcast band (R3), offline.

    python -m tools.science.phase5 --out docs

Pre-registered in docs/phase5-preregistration.md before the first run;
the inputs and checks below must match that file exactly. Writes
phase5-results.md and phase5-results.json to --out. An existing result
is never silently replaced: a rerun needs --rerun-reason, which is
recorded in the new results next to the first run's time.
"""
import argparse
import json
import pathlib
import subprocess
import sys
from datetime import datetime, timezone

import numpy as np

from core import ground_truth as gt
from core.flood_routing import (BASE_FLOW_M3S, DURATION_S, RISE_FRACTIONS,
                                Channel, arrival_times, calibrate_manning,
                                hindcast, normal_depth, peak_outflow, route,
                                stable_time_step, triangular_hydrograph)

CHANNEL = pathlib.Path("tests/fixtures/south_lhonak_channel.json")
MANNING_N = 0.175  # handlers/registry.py; calibrated 8 Oct, not refitted
BENCHMARK_TRAVEL_S = (gt.TEESTA_III_BREACH_TIME
                      - gt.LAKE_RELEASE_TIME).total_seconds()
TIMING_TOLERANCE_S = 600.0  # the +/-10 minutes fixed on 8 Oct
PUBLISHED_PEAK_M3S = (5340.0, 7355.0)
VOLUME_UNCERTAINTY_M3 = 1.8e6  # 50 +/- 1.8 million m3, Sattar et al.

# R4 inputs.
AREAS_KM2 = (1.6438, 1.6543)  # CASCADE, 14 Sep 2023, status "best"


def volume_huggel_2002(area_km2):
    """V = 0.104 A^1.42, A in m2 and V in m3 (Cook & Quincey 2015, Eq. 3)."""
    return 0.104 * (area_km2 * 1e6) ** 1.42


def volume_fujita_2013(area_km2):
    """V = D A with D = 55 A^0.25, A in km2 and D in m (Fujita et al.
    2013, Eq. 1: their upper envelope for Himalayan lakes)."""
    return 55.0 * area_km2 ** 0.25 * area_km2 * 1e6


VOLUME_RELATIONS = {"huggel_2002": volume_huggel_2002,
                    "fujita_2013": volume_fujita_2013}
DRAINED_FRACTIONS = (0.5, 0.75, 1.0)
PEAK_FORMULAS = ("evans_1986", "popov_1991")


def forecast_members():
    return [{"area_km2": area, "volume_relation": relation,
             "drained_fraction": fraction,
             "volume_m3": VOLUME_RELATIONS[relation](area) * fraction,
             "peak_formula": formula, "rise_fraction": rise}
            for area in AREAS_KM2 for relation in VOLUME_RELATIONS
            for fraction in DRAINED_FRACTIONS for formula in PEAK_FORMULAS
            for rise in RISE_FRACTIONS]


def route_triangles(channel, members, manning_n):
    """Route one triangular outflow per member to the dam, all at once."""
    peaks = np.array([peak_outflow(m["volume_m3"], m["peak_formula"])
                      for m in members])
    dt = stable_time_step(channel, peaks.max() + BASE_FLOW_M3S, manning_n)
    times = np.arange(0.0, DURATION_S + dt / 2, dt)
    inflows = np.column_stack([
        triangular_hydrograph(m["volume_m3"], peak, m["rise_fraction"],
                              times)
        for m, peak in zip(members, peaks)])
    at_dam = route(channel, inflows, manning_n, dt)[:, -1]
    dam_peaks = at_dam.max(axis=0)
    depths = normal_depth(channel, -1, dam_peaks, manning_n)
    volume_in = (inflows - BASE_FLOW_M3S).sum(axis=0) * dt
    volume_out = (at_dam - BASE_FLOW_M3S).sum(axis=0) * dt
    travel = arrival_times(times, at_dam)
    return [{**m, "source_peak_m3s": float(q), "dam_peak_m3s": float(p),
             "travel_min": float(t / 60), "depth_m": float(d),
             "water_balance": float(out / vin)}
            for m, q, p, t, d, vin, out in zip(
                members, peaks, dam_peaks, travel, depths, volume_in,
                volume_out)]


def band(values):
    values = np.asarray(values, dtype=float)
    return {"min": float(values.min()),
            "p10": float(np.percentile(values, 10)),
            "p50": float(np.percentile(values, 50)),
            "p90": float(np.percentile(values, 90)),
            "max": float(values.max())}


def overlaps(low, high, target):
    return low <= target[1] and high >= target[0]


def run_forecast(channel):
    members = route_triangles(channel, forecast_members(), MANNING_N)
    bands = {key: band([m[key] for m in members]) for key in (
        "volume_m3", "source_peak_m3s", "dam_peak_m3s", "travel_min",
        "depth_m")}
    travel_target = ((BENCHMARK_TRAVEL_S - TIMING_TOLERANCE_S) / 60,
                     (BENCHMARK_TRAVEL_S + TIMING_TOLERANCE_S) / 60)
    volume, peak, travel = (bands["volume_m3"], bands["dam_peak_m3s"],
                            bands["travel_min"])
    checks = {
        "F1": {"rule": "drained-volume band (min-max) contains 50 million m3",
               "passed": volume["min"] <= gt.DRAINED_VOLUME_M3
               <= volume["max"]},
        "F2": {"rule": "p10-p90 dam peak overlaps 5,340-7,355 m3/s",
               "passed": overlaps(peak["p10"], peak["p90"],
                                  PUBLISHED_PEAK_M3S)},
        "F3": {"rule": "p10-p90 travel time overlaps 137.7 +/- 10 min "
                       "(not independent: n was calibrated on it)",
               "passed": overlaps(travel["p10"], travel["p90"],
                                  travel_target)},
    }
    return {"members": members, "bands": bands, "checks": checks}


def run_band(channel):
    n_minus = calibrate_manning(channel, gt.DRAINED_VOLUME_M3,
                                BENCHMARK_TRAVEL_S - TIMING_TOLERANCE_S)
    n_plus = calibrate_manning(channel, gt.DRAINED_VOLUME_M3,
                               BENCHMARK_TRAVEL_S + TIMING_TOLERANCE_S)
    members = []
    for volume in (gt.DRAINED_VOLUME_M3 - VOLUME_UNCERTAINTY_M3,
                   gt.DRAINED_VOLUME_M3,
                   gt.DRAINED_VOLUME_M3 + VOLUME_UNCERTAINTY_M3):
        for n in (n_minus, MANNING_N, n_plus):
            result = hindcast(channel, volume, n)
            members.append({
                "volume_m3": volume, "manning_n": float(n),
                "dam_peak_m3s": float(np.median(result.peak_m3s)),
                "travel_min": float(np.median(result.arrival_s) / 60),
                "depth_m": float(np.median(result.depth_m))})
    deployed = next(m for m in members
                    if m["volume_m3"] == gt.DRAINED_VOLUME_M3
                    and m["manning_n"] == MANNING_N)
    peaks = [m["dam_peak_m3s"] for m in members]
    checks = {
        "B0": {"rule": "the 50 million m3, n = 0.175 member reproduces "
                       "10,541 m3/s, 137.7 min and 15.9 m",
               "passed": (round(deployed["dam_peak_m3s"]) == 10541
                          and round(deployed["travel_min"], 1) == 137.7
                          and round(deployed["depth_m"], 1) == 15.9)},
        "B1": {"rule": "dam peak band (min-max) overlaps 5,340-7,355 m3/s",
               "passed": overlaps(min(peaks), max(peaks),
                                  PUBLISHED_PEAK_M3S)},
    }
    stats = {key: {"min": min(m[key] for m in members),
                   "median": float(np.median([m[key] for m in members])),
                   "max": max(m[key] for m in members)}
             for key in ("dam_peak_m3s", "travel_min", "depth_m")}
    return {"n_minus": float(n_minus), "n_plus": float(n_plus),
            "members": members, "stats": stats, "checks": checks}


def code_version():
    def git(*args):
        return subprocess.run(["git", *args], capture_output=True,
                              text=True).stdout.strip()
    dirty = git("status", "--porcelain", "--", "core", "tools", "tests")
    return git("rev-parse", "--short", "HEAD") + (" + uncommitted changes"
                                                  if dirty else "")


def markdown(results):
    f, b = results["forecast"], results["band"]
    fb, bs = f["bands"], b["stats"]
    lines = [
        "# Phase 5 results: forecast replay and hindcast band", "",
        "Generated by `python -m tools.science.phase5` from the inputs and "
        "checks fixed in [phase5-preregistration.md]"
        "(phase5-preregistration.md).", "",
        f"- **Run at:** {results['run_at']}",
        f"- **Code:** `{results['code']}`"]
    for rerun in results.get("reruns", []):
        lines.append(f"- **Rerun:** {rerun['run_at']}, replacing the run "
                     f"of {rerun['replaces']}. Reason: {rerun['reason']}")
    lines += [
        "", "## R4: what CASCADE would have said on 14 Sep 2023", "",
        f"From the lake area it measured on 14 Sep 2023, with no knowledge "
        f"of the event, CASCADE would have forecast **"
        f"{fb['dam_peak_m3s']['p10']:,.0f}–{fb['dam_peak_m3s']['p90']:,.0f}"
        f" m³/s** at Teesta-III, arriving **"
        f"{fb['travel_min']['p10']:.0f}–{fb['travel_min']['p90']:.0f} "
        f"minutes** after a burst (p10–p90 of {len(f['members'])} "
        f"members).", "",
        "| Quantity | Min | p10 | p50 | p90 | Max |",
        "|---|---:|---:|---:|---:|---:|"]
    for key, label, scale, fmt in (
            ("volume_m3", "Drained volume (million m³)", 1e-6, ".1f"),
            ("source_peak_m3s", "Peak leaving the lake (m³/s)", 1, ",.0f"),
            ("dam_peak_m3s", "Peak at the dam (m³/s)", 1, ",.0f"),
            ("travel_min", "Travel time to the dam (min)", 1, ".1f"),
            ("depth_m", "Depth at the dam (m)", 1, ".1f")):
        row = fb[key]
        lines.append(f"| {label} | " + " | ".join(
            format(row[stat] * scale, fmt)
            for stat in ("min", "p10", "p50", "p90", "max")) + " |")
    lines += ["", "| Check | Rule | Result |", "|---|---|---|"]
    lines += [f"| {name} | {check['rule']} | "
              f"{'pass' if check['passed'] else '**fail**'} |"
              for name, check in f["checks"].items()]
    lines += [
        "", "## R3: uncertainty band on the 2023 hindcast", "",
        f"Volume 50 ± 1.8 million m³; Manning's *n* from {b['n_minus']:.4f}"
        f" to {b['n_plus']:.4f} (the values that arrive 10 minutes early "
        f"and late), with 0.175 between.", "",
        "| Quantity | Min | Median | Max |", "|---|---:|---:|---:|"]
    for key, label, fmt in (("dam_peak_m3s", "Peak at the dam (m³/s)",
                             ",.0f"),
                            ("travel_min", "Travel time (min)", ".1f"),
                            ("depth_m", "Depth at the dam (m)", ".1f")):
        row = bs[key]
        lines.append(f"| {label} | " + " | ".join(
            format(row[stat], fmt) for stat in ("min", "median", "max"))
            + " |")
    lines += ["", "| Check | Rule | Result |", "|---|---|---|"]
    lines += [f"| {name} | {check['rule']} | "
              f"{'pass' if check['passed'] else '**fail**'} |"
              for name, check in b["checks"].items()]
    lines += ["", "Every member, with its inputs, is in "
              "[phase5-results.json](phase5-results.json)."]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--rerun-reason")
    args = parser.parse_args()
    out = pathlib.Path(args.out)
    previous_path = out / "phase5-results.json"
    reruns = []
    if previous_path.exists():
        if not args.rerun_reason:
            sys.exit(f"{previous_path} exists; a rerun needs --rerun-reason")
        previous = json.loads(previous_path.read_text(encoding="utf-8"))
        reruns = previous.get("reruns", []) + [{
            "run_at": None, "replaces": previous["run_at"],
            "reason": args.rerun_reason}]

    channel = Channel.load(CHANNEL)
    results = {"run_at": datetime.now(timezone.utc).isoformat(),
               "code": code_version()}
    results["forecast"] = run_forecast(channel)
    results["band"] = run_band(channel)
    for rerun in reruns:
        rerun["run_at"] = rerun["run_at"] or results["run_at"]
    if reruns:
        results["reruns"] = reruns
    out.mkdir(parents=True, exist_ok=True)
    previous_path.write_text(json.dumps(results, indent=1) + "\n",
                             encoding="utf-8")
    (out / "phase5-results.md").write_text(markdown(results),
                                           encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print(markdown(results))


if __name__ == "__main__":
    main()
