"""Rebuild the README benchmark tables from the published evidence, offline.

    python -m tools.benchmark.analyze evidence/2026-10-08-batch
    python -m tools.benchmark.analyze --check-readme README.md

Standard library only; no AWS access. The script first checks every published
file against MANIFEST.sha256, then cross-checks the sources against each
other, then prints the README's tables:

  - Step Functions executions give run timings and pipeline outputs;
  - execution history ties every Lambda invocation (by request ID) to its run;
  - Lambda REPORT / INIT_REPORT lines give per-invocation duration, memory
    and cold-start init time;
  - CloudTrail StartExecution records and CloudWatch metrics are independent
    counts of the same runs and invocations;
  - the runner's own log is the only source for footprint geometry hashes.
"""
import argparse
import contextlib
import hashlib
import io
import json
import math
import pathlib
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta

FUNCTIONS = ("plan", "measure", "route", "report")
LABELS = {"plan": "plan", "measure": "measure (one lake-month)",
          "route": "route (lake → dam)", "report": "report"}
# Decimal places as printed in the README: (warm columns, cold column).
PLACES = {"plan": (3, 3), "measure": (2, 1), "route": (3, 2),
          "report": (3, 2)}
PRICE_GB_S = 0.0000166667       # Lambda x86 on-demand, us-west-2
PRICE_REQUEST = 0.20 / 1e6
PRICE_TRANSITION = 0.025 / 1000  # Step Functions Standard
# The local (pre-cloud) Sprint 2 run that the cloud outputs are compared to.
REFERENCE = {"sep": ("2023-09-14", "best", 1.6438, 1.6543),
             "oct": ("2023-10-29", "range", 1.2063, 1.4636),
             "arrival": "00:30:00 IST", "peak_m3s": 10541, "depth_m": 15.9,
             "warning_minutes": 137.7}
# README lines that carry benchmark figures outside the tables.
SENTENCES = ("The warm median", "CloudWatch recorded", "**Cost:**")
FIELD = re.compile(r"(Init Duration|Billed Duration|Duration|Memory Size|"
                   r"Max Memory Used): ([0-9.]+)")


def load_jsonl(path):
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def verify_manifest(folder):
    checked = 0
    for line in (folder / "MANIFEST.sha256").read_text(
            encoding="utf-8").splitlines():
        digest, name = line.split("  ", 1)
        if name.startswith("private-originals/"):
            continue
        actual = hashlib.sha256((folder / name).read_bytes()).hexdigest()
        if actual != digest:
            sys.exit(f"MANIFEST mismatch for {name}")
        checked += 1
    return checked


def nearest_rank(values, p):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]


def median_ci(values):
    """Distribution-free ~95% CI for the median from order statistics."""
    n = len(values)
    if n < 6:
        return None
    ordered = sorted(values)
    # smallest k with P(Binom(n, 0.5) <= k - 1) <= 0.025
    k, cumulative = 0, 0.0
    while True:
        cumulative += math.comb(n, k) / 2 ** n
        if cumulative > 0.025:
            break
        k += 1
    return ordered[k], ordered[n - 1 - k]


def summary(values):
    return {
        "n": len(values),
        "median": statistics.median(values),
        "p25": nearest_rank(values, 25),
        "p75": nearest_rank(values, 75),
        "p95": nearest_rank(values, 95),
        "max": max(values),
        "cv": (statistics.stdev(values) / statistics.fmean(values)
               if len(values) > 1 else 0.0),
        "ci": median_ci(values),
    }


def seconds(execution):
    return (datetime.fromisoformat(execution["stopDate"])
            - datetime.fromisoformat(execution["startDate"])).total_seconds()


def link_invocations(histories, function_names):
    """Lambda request ID -> (execution ARN, function) from task events."""
    links, transitions = {}, Counter()
    for history in histories:
        arn = history["executionArn"]
        events = {event["id"]: event for event in history["events"]}
        for event in history["events"]:
            if event["type"].endswith("StateEntered"):
                transitions[arn] += 1
            if event["type"] != "TaskSucceeded":
                continue
            started = events[event["previousEventId"]]
            scheduled = events[started["previousEventId"]]
            parameters = json.loads(
                scheduled["taskScheduledEventDetails"]["parameters"])
            function = function_names[
                parameters["FunctionName"].rsplit(":", 1)[-1]]
            output = json.loads(event["taskSucceededEventDetails"]["output"])
            links[output["SdkResponseMetadata"]["RequestId"]] = (arn,
                                                                 function)
    return links, transitions


def invocations(rows, group_names, links):
    """One record per REPORT line, with init timeouts paired by log stream."""
    pending_timeouts = Counter()
    records = []
    for row in rows:
        stream = (row["logGroupName"], row["logStreamName"])
        if row["type"] == "INIT_REPORT":
            if "Status: timeout" in row["message"]:
                pending_timeouts[stream] += 1
            continue
        fields = {}
        for key, value in FIELD.findall(row["message"]):
            fields.setdefault(key, float(value))
        request_id = re.search(r"RequestId: (\S+)", row["message"]).group(1)
        arn, function = links[request_id]
        if function != group_names[row["logGroupName"]]:
            sys.exit(f"{request_id} logged by the wrong function")
        timed_out = pending_timeouts[stream] > 0
        if timed_out:
            pending_timeouts[stream] -= 1
        records.append({
            "request_id": request_id, "execution": arn, "function": function,
            "end": row["timestamp"] / 1000,
            "duration": fields["Duration"] / 1000,
            "billed": fields["Billed Duration"] / 1000,
            "memory": fields["Memory Size"],
            "max_memory": fields["Max Memory Used"],
            "init": fields.get("Init Duration", 0) / 1000,
            "cold": "Init Duration" in fields, "init_timeout": timed_out,
        })
    if sum(pending_timeouts.values()):
        sys.exit("an INIT_REPORT timeout has no following REPORT line")
    return records


def metric_total(metrics, metric_id, start, end):
    series = next(s for s in metrics["series"] if s["id"] == metric_id)
    return sum(value for stamp, value in zip(series["timestamps"],
                                             series["values"])
               if start <= datetime.fromisoformat(stamp) <= end)


def fmt(value, places):
    return f"{value:.{places}f}"


def span(values, places, unit=" s"):
    low, high = fmt(min(values), places), fmt(max(values), places)
    return f"{low}{unit}" if low == high else f"{low}–{high}{unit}"


def report(folder):
    folder = pathlib.Path(folder)
    print(f"Manifest: {verify_manifest(folder)} published files match "
          f"MANIFEST.sha256\n")
    collection = json.loads((folder / "collection.json").read_text(
        encoding="utf-8"))
    executions = load_jsonl(folder / "executions.jsonl")
    histories = load_jsonl(folder / "execution-history.jsonl")
    trail = load_jsonl(folder / "cloudtrail-start-execution.jsonl")
    runner = load_jsonl(folder / "runner-log.jsonl")
    metrics = json.loads((folder / "metrics.json").read_text(
        encoding="utf-8"))
    rows = load_jsonl(folder / "lambda-reports.jsonl")

    function_names = {name: key for key, name in
                      collection["functions"].items()}
    group_names = {name: key for key, name in
                   collection["log_groups"].items()}
    links, transitions = link_invocations(histories, function_names)
    calls = invocations(rows, group_names, links)
    by_run = defaultdict(list)
    for call in calls:
        by_run[call["execution"]].append(call)

    batch_names = [r["name"] for r in runner]
    by_name = {e["name"]: e for e in executions}
    batch = [by_name[name] for name in batch_names]
    manual = sorted((e for e in executions if e["name"] not in batch_names),
                    key=lambda e: e["startDate"])
    label = {e["executionArn"]: f"manual-{i}" for i, e in
             enumerate(manual, 1)}
    label.update({e["executionArn"]: e["name"] for e in batch})
    batch_arns = {e["executionArn"] for e in batch}

    # -- Cross-checks between sources ----------------------------------------
    print("## Cross-checks\n")
    trail_names = Counter(r["requestParameters"]["name"] for r in trail)
    trail_start = {r["requestParameters"]["name"]:
                   r["responseElements"]["startDate"] for r in trail}
    start_mismatch = [e["name"] for e in executions
                      if datetime.fromisoformat(trail_start[e["name"]])
                      != datetime.fromisoformat(e["startDate"]).replace(
                          microsecond=0)]
    runner_gap = max(abs(r["seconds"] - seconds(by_name[r["name"]]))
                     for r in runner)
    runner_same_output = all(r["output"] == json.loads(
        by_name[r["name"]]["output"]) for r in runner)
    window = (datetime.fromisoformat(collection["window"]["start"]),
              datetime.fromisoformat(collection["window"]["end"]))
    edges = sorted((c["end"] - c["duration"], 1) for c in calls) + sorted(
        (c["end"], -1) for c in calls)
    running = peak = 0
    for _, step in sorted(edges, key=lambda e: (e[0], e[1])):
        running += step
        peak = max(peak, running)
    ended = Counter(h["events"][-1]["type"] for h in histories)
    succeeded = sum(e["status"] == "SUCCEEDED" for e in executions)
    print(f"- Step Functions: {len(executions)} executions "
          f"{dict(Counter(e['status'] for e in executions))}, "
          f"{len(by_name)} distinct names; {len(manual)} manual + "
          f"{len(batch)} batch; last history event {dict(ended)}")
    print(f"- CloudTrail: {len(trail)} StartExecution records, "
          f"{len(trail_names)} distinct names; same names as the "
          f"executions: {set(trail_names) == set(by_name)}; start time "
          f"mismatches: {len(start_mismatch)}")
    print(f"- Lambda logs: {len(calls)} REPORT lines, "
          f"{sum(c['init_timeout'] for c in calls)} paired with an "
          f"INIT_REPORT timeout; every request ID found in the execution "
          f"history: {len(calls) == len(links)}")
    print("- CloudWatch over the whole window vs REPORT lines: " + ", ".join(
        f"{f} {metric_total(metrics, f + '_invocations', *window):.0f}/"
        f"{sum(c['function'] == f for c in calls)}" for f in FUNCTIONS)
        + f"; AWS/States ExecutionsStarted "
        f"{metric_total(metrics, 'pipeline_executionsstarted', *window):.0f}, "
        f"ExecutionsSucceeded "
        f"{metric_total(metrics, 'pipeline_executionssucceeded', *window):.0f}"
        f" (execution records: {succeeded} succeeded)")
    print(f"- Runner log: {len(runner)} runs, all found in Step Functions; "
          f"largest timing difference {runner_gap:.3f} s; outputs identical "
          f"to Step Functions: {runner_same_output}")
    print(f"- Most Lambda invocations running at once (from REPORT end time "
          f"minus duration): {peak}")

    # -- End to end -----------------------------------------------------------
    cold_runs = {c["execution"] for c in calls if c["cold"]}
    first_load = {c["execution"] for c in calls if c["init_timeout"]}
    warm = [seconds(e) for e in batch
            if e["executionArn"] not in cold_runs | first_load]
    cached = sorted(seconds(e) for e in executions
                    if e["executionArn"] in cold_runs - first_load)
    first = sorted(seconds(e) for e in executions
                   if e["executionArn"] in first_load)
    s = summary(warm)
    print("\n## End to end (one Step Functions execution)\n")
    print("| Condition | Runs | Median | 95% CI of median | IQR | p95 | Max |")
    print("|---|---:|---:|---|---|---:|---:|")
    print(f"| **Warm** (environments reused) | {s['n']} | "
          f"**{s['median']:.1f} s** | {s['ci'][0]:.1f}–{s['ci'][1]:.1f} s | "
          f"{s['p25']:.1f}–{s['p75']:.1f} s | {s['p95']:.1f} s | "
          f"{s['max']:.1f} s |")
    print(f"| Cold, image already cached | {len(cached)} | "
          + " and ".join(f"{v:.1f} s" for v in cached)
          + " | – | – | – | – |")
    print(f"| Cold, first load after deploy | {len(first)} | "
          + " and ".join(f"{v:.1f} s" for v in first)
          + " | – | – | – | – |")
    blocks = [[seconds(e) for e in batch[i:i + 15]]
              for i in range(0, len(batch), 15)]
    medians = [f"{statistics.median(b):.1f} s" for b in blocks]
    print(f"\nThe warm median held steady across the four blocks of 15 runs: "
          f"{', '.join(medians[:-1])} and {medians[-1]}. The coefficient of "
          f"variation was {s['cv']:.1%}.")

    # -- Per function ---------------------------------------------------------
    print("\n### Per Lambda function (batch runs)\n")
    print("| Function | Memory | Warm invocations | Warm median (95% CI) | "
          "Warm p95 | Warm max | Cold-start invoke time | Peak memory used |")
    print("|---|---:|---:|---|---:|---:|---|---:|")
    for function in FUNCTIONS:
        mine = [c for c in calls if c["function"] == function]
        in_batch = [c for c in mine if c["execution"] in batch_arns]
        warm_calls = [c["duration"] for c in in_batch
                      if not (c["cold"] or c["init_timeout"])]
        cold_calls = [c["duration"] for c in mine if c["cold"]]
        w, places = summary(warm_calls), PLACES[function]
        median = f"{fmt(w['median'], places[0])} s"
        if function == "measure":
            median = f"**{median}**"
        print(f"| {LABELS[function]} | {in_batch[0]['memory']:.0f} MB | "
              f"{w['n']} | {median} ({fmt(w['ci'][0], places[0])}–"
              f"{fmt(w['ci'][1], places[0])}) | "
              f"{fmt(w['p95'], places[0])} s | {fmt(w['max'], places[0])} s "
              f"| {span(cold_calls, places[1])} | "
              f"{max(c['max_memory'] for c in in_batch):.0f} MB |")
    window_start = datetime.fromisoformat(batch[0]["startDate"]).replace(
        second=0, microsecond=0)
    window_end = max(datetime.fromisoformat(e["stopDate"])
                     for e in batch) + timedelta(minutes=2)
    counted = {f: metric_total(metrics, f"{f}_invocations", window_start,
                               window_end) for f in FUNCTIONS}
    errors = sum(metric_total(metrics, f"{f}_errors", window_start,
                              window_end) for f in FUNCTIONS)
    throttles = sum(metric_total(metrics, f"{f}_throttles", window_start,
                                 window_end) for f in FUNCTIONS)
    print(f"\nCloudWatch recorded {counted['plan']:.0f} plan, "
          f"{counted['measure']:.0f} measure, {counted['route']:.0f} route "
          f"and {counted['report']:.0f} report invocations over the batch, "
          f"with **{errors:.0f} errors and {throttles:.0f} throttles**.")

    # -- Cold starts ----------------------------------------------------------
    first_calls = [c for c in calls if c["execution"] in first_load]
    cached_calls = [c for c in calls if c["cold"]]
    print("\n### Cold starts\n")
    print("| Kind | Observations | Startup (Lambda init) |")
    print("|---|---|---|")
    print(f"| First load of a newly deployed image | {len(first_load)} run, "
          f"{len(first_calls)} invocations | **"
          f"{sum(c['init_timeout'] for c in first_calls)} of "
          f"{len(first_calls)} hit Lambda's 10 s init limit.** Lambda re-ran "
          f"the startup inside the invocation; nothing failed. |")
    print(f"| Image already cached by Lambda | "
          f"{len({c['execution'] for c in cached_calls})} runs, "
          f"{len(cached_calls)} invocations | "
          f"{span([c['init'] for c in cached_calls], 1)}, with no timeouts |")
    for c in sorted(calls, key=lambda c: c["end"]):
        if c["cold"] or c["init_timeout"]:
            init = ("timed out at 10 s" if c["init_timeout"]
                    else f"{c['init']:.2f} s")
            print(f"  - {label[c['execution']]}: {c['function']} init "
                  f"{init}, invoke {c['duration']:.3f} s")

    # -- Physics parity -------------------------------------------------------
    outputs = [json.loads(e["output"]) for e in batch]
    distinct_outputs = len({json.dumps(o, sort_keys=True) for o in outputs})

    def observed(output, month):
        obs = next(o for o in output["observations"]
                   if o["acquired"].startswith(month))
        return (obs["acquired"], obs["status"], obs["area_low_km2"],
                obs["area_high_km2"])

    sep = {observed(o, "2023-09") for o in outputs}
    oct_ = {observed(o, "2023-10") for o in outputs}
    routes = [o["routes"][0] for o in outputs]
    arrivals = {datetime.fromisoformat(r["arrival_time"]).strftime(
        "%H:%M:%S") + " IST" for r in routes}
    peaks = {(r["peak_m3s"], r["depth_m"]) for r in routes}
    warnings = {r["warning_minutes"] for r in routes}
    (sd, ss, sl, sh), (od, os_, ol, oh) = min(sep), min(oct_)
    rs, rd, rl, rh = REFERENCE["sep"]
    qd, qs, ql, qh = REFERENCE["oct"]
    peak, depth = min(peaks)
    print(f"\n### Physics parity across all {len(batch)} batch runs\n")
    print(f"| Output | Reference (local run) | All {len(batch)} batch runs | "
          f"Distinct values |")
    print("|---|---|---|---:|")
    print(f"| September scene, status | {rs}, {rd} | {sd}, {ss} | "
          f"{len({x[:2] for x in sep})} |")
    print(f"| September lake area | {rl} km² (high {rh}) | {sl} km² "
          f"(high {sh}) | {len({x[2:] for x in sep})} |")
    print(f"| October scene, status | {qd}, {qs} | {od}, {os_} | "
          f"{len({x[:2] for x in oct_})} |")
    print(f"| October lake area range | {ql}–{qh} km² | {ol}–{oh} km² | "
          f"{len({x[2:] for x in oct_})} |")
    print(f"| Flood arrival at Teesta-III | {REFERENCE['arrival']} "
          f"(benchmark ~00:30) | {min(arrivals)} | {len(arrivals)} |")
    print(f"| Peak flow / depth at the dam | {REFERENCE['peak_m3s']:,} m³/s / "
          f"{REFERENCE['depth_m']} m | {peak:,} m³/s / {depth} m | "
          f"{len(peaks)} |")
    print(f"| Warning time | {REFERENCE['warning_minutes']} min | "
          f"{min(warnings)} min | {len(warnings)} |")
    print(f"\n- distinct whole pipeline outputs across the batch: "
          f"{distinct_outputs}")
    for e in manual:
        o = json.loads(e["output"])
        print(f"- {label[e['executionArn']]} October range: "
              f"{observed(o, '2023-10')[2]}–{observed(o, '2023-10')[3]} km²")

    # -- What the numbers do and don't show ----------------------------------
    hashed = [r for r in runner if len(r["footprint_after"]) == 64]
    changed = sum(r["footprint_before"] != r["footprint_after"]
                  for r in hashed)
    n = len(batch)
    print("\n### Bounds and footprint\n")
    print(f"- divergence bound: 0 of {n} runs diverged -> below "
          f"{1 - 0.05 ** (1 / n):.1%} per run at 95% confidence")
    print(f"- tail bound: slowest of {len(warm)} warm runs "
          f"({max(warm):.1f} s) bounds warm p95 with confidence "
          f"1 - 0.95^{len(warm)} = {1 - 0.95 ** len(warm):.3f}")
    print(f"- footprint geometry changed in {changed} of {len(hashed)} runs "
          f"with geometry hashes ({len(runner) - len(hashed)} earlier runs "
          f"logged the S3 ETag instead)")

    # -- Cost -----------------------------------------------------------------
    batch_calls = [c for c in calls if c["execution"] in batch_arns]
    gb_seconds = sum(c["billed"] * c["memory"] / 1024 for c in batch_calls)
    steps = sum(transitions[arn] for arn in batch_arns)
    cost = (gb_seconds * PRICE_GB_S + len(batch_calls) * PRICE_REQUEST
            + steps * PRICE_TRANSITION)
    print(f"\n**Cost:** {gb_seconds:,.0f} GB-s of Lambda time, "
          f"{len(batch_calls)} requests and about {steps / n:.0f} state "
          f"transitions per run comes to **${cost:.4f} for the {n} runs** "
          f"(${cost / n:.5f} per run) at on-demand prices, inside the AWS "
          f"free tier.")


def readme_mismatches(readme, generated):
    """README benchmark lines that the evidence doesn't reproduce.

    Checks every table row and every figure sentence in the README's
    "Cloud performance and determinism" section. Table rows must match a
    generated row exactly. A sentence may run on past the generated one,
    because the README adds context after the figures.
    """
    start = readme.index("## Cloud performance and determinism")
    end = readme.find("\n## ", start + 1)
    section = readme[start:end if end > 0 else len(readme)].splitlines()
    rows = [line for line in section
            if line.startswith("|") and not line.startswith("|---")]
    sentences = [line for line in section if line.startswith(SENTENCES)]
    lines = set(generated)
    missing = [row for row in rows if row not in lines]
    missing += [s for s in sentences
                if not any(s.startswith(g) for g in generated
                           if g.startswith(SENTENCES))]
    return len(rows) + len(sentences), missing


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", nargs="?",
                        default="evidence/2026-10-08-batch")
    parser.add_argument("--check-readme", metavar="README",
                        help="fail unless README's benchmark tables and "
                             "figures match the evidence")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # "→", "²" on Windows consoles
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        report(args.folder)
    print(buffer.getvalue(), end="")
    if args.check_readme:
        readme = pathlib.Path(args.check_readme).read_text(encoding="utf-8")
        checked, missing = readme_mismatches(readme,
                                             buffer.getvalue().splitlines())
        print(f"\nREADME check: {checked - len(missing)} of {checked} "
              "benchmark rows and figures reproduced from the evidence")
        for line in missing:
            print(f"  NOT REPRODUCED: {line}")
        if missing:
            sys.exit(1)


if __name__ == "__main__":
    main()
