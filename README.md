# CASCADE: Glacial Lake Outburst Flood Telemetry & Early Warning System
*Built during WeMakeDevs x AWS Environmental Hacks — Track 02: Heat and Water*

## Cloud performance and determinism: 2023 South Lhonak hindcast on AWS

Raw AWS records and the scripts that rebuild these tables: [`evidence/2026-10-08-batch/`](evidence/2026-10-08-batch/).

**Test setup:**
- **Deployment:** stack `cascade-glof` (us-west-2), Lambda container images (Python 3.14, x86_64), orchestrated by a Step Functions Standard workflow.
- **Input:** every run used the same input, `events/hindcast-2023.json`, covering September–October 2023 for South Lhonak Lake.
- **Runs:** on 8 Oct 2026 we ran **60 executions back to back**, after 2 earlier manual executions. All 62 succeeded.

### End to end (one Step Functions execution)

| Condition | Runs | Median | 95% CI of median | IQR | p95 | Max |
|---|---:|---:|---|---|---:|---:|
| **Warm** (environments reused) | 59 | **7.1 s** | 7.0–7.2 s | 6.8–7.5 s | 8.0 s | 8.2 s |
| Cold, image already cached | 2 | 18.6 s and 23.4 s | – | – | – | – |
| Cold, first load after deploy | 1 | 81.2 s | – | – | – | – |

The warm median held steady across the four blocks of 15 runs: 7.2 s, 7.2 s, 7.1 s and 6.8 s. The coefficient of variation was 6.7%.

### Per Lambda function (batch runs)

| Function | Memory | Warm invocations | Warm median (95% CI) | Warm p95 | Warm max | Cold-start invoke time | Peak memory used |
|---|---:|---:|---|---:|---:|---|---:|
| plan | 512 MB | 59 | 0.002 s (0.002–0.002) | 0.002 s | 0.003 s | 0.002 s | 161 MB |
| measure (one lake-month) | 3008 MB | 118 | **5.55 s** (5.47–5.73) | 6.74 s | 7.03 s | 10.0–11.7 s | 405 MB |
| route (lake → dam) | 2048 MB | 59 | 0.473 s (0.471–0.475) | 0.519 s | 0.545 s | 0.81–0.97 s | 194 MB |
| report | 512 MB | 59 | 0.179 s (0.175–0.185) | 0.234 s | 0.252 s | 0.73–0.77 s | 184 MB |

CloudWatch recorded 60 plan, 120 measure, 60 route and 60 report invocations over the batch, with **0 errors and 0 throttles**. The account's Lambda concurrency limit is 10, and each run uses at most 2 functions at once.

### Cold starts

| Kind | Observations | Startup (Lambda init) |
|---|---|---|
| First load of a newly deployed image | 1 run, 5 invocations | **4 of 5 hit Lambda's 10 s init limit.** Lambda re-ran the startup inside the invocation; nothing failed. |
| Image already cached by Lambda | 3 runs, 11 invocations | 1.0–3.3 s, with no timeouts |

### Physics parity across all 60 batch runs

| Output | Reference (local run) | All 60 batch runs | Distinct values |
|---|---|---|---:|
| September scene, status | 2023-09-14, best | 2023-09-14, best | 1 |
| September lake area | 1.6438 km² (high 1.6543) | 1.6438 km² (high 1.6543) | 1 |
| October scene, status | 2023-10-29, range | 2023-10-29, range | 1 |
| October lake area range | 1.2063–1.4636 km² | 1.2063–1.464 km² | 1 |
| Flood arrival at Teesta-III | 00:30:00 IST (benchmark ~00:30) | 00:30:00 IST | 1 |
| Peak flow / depth at the dam | 10,541 m³/s / 15.9 m | 10,541 m³/s / 15.9 m | 1 |
| Warning time | 137.7 min | 137.7 min | 1 |

The October high end differs from the local run by 0.0004 km². The first cloud run merged a ~330 m² sliver into the stored lake footprint. Every run since then has produced identical outputs.

### What these numbers do and don't show

- **Determinism.** Zero divergent outputs in 60 runs puts the probability of any one run diverging below **4.9%** at 95% confidence (1 − 0.05^(1/60)). The code has no random inputs. Repeated runs can't *prove* determinism; they can only fail to find a counterexample.
- **Tail latency.** With 59 warm runs, the slowest run (8.2 s) is a 95% upper bound for warm p95 (1 − 0.95⁵⁹ = 0.952). That's why the batch had 60 runs rather than 20: with 20, the slowest run would be only a 64% bound. Medians use distribution-free confidence intervals from order statistics.
- **Warm runs flatter real use.** Each warm run re-reads the same satellite image windows, and GDAL's in-memory HTTP cache probably serves part of them. A real run on new scenes will sit closer to the cold invoke times, about 10–12 s per lake-month.
- **Cold starts are few.** Three cold runs are enough to show the pattern, not a distribution. Sampling more needs runs spaced 20–30 minutes or more apart, or a redeploy.
- **The stored footprint drifts.** Its outline changes at the floating-point level on every run (45 of 45 hashed runs), without changing any output. Its size is stable at 449 vertices and 17.9 KiB.
- **One case study.** All of this is one lake and one event, the 2023 South Lhonak flood.

**Cost:** 2,094 GB-s of Lambda time, 300 requests and about 7 state transitions per run comes to **$0.0455 for the 60 runs** ($0.00076 per run) at on-demand prices, inside the AWS free tier.

## AI tools used

As the hackathon rules require, these are the AI coding tools used to build this project:

- **Claude Code** (Anthropic): used throughout the build to write and review code, run the AWS deployment and benchmark commands, analyse results and draft documentation.
