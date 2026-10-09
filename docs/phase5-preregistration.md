# Phase 5 pre-registration: forecast replay and hindcast band

Written 10 Oct 2026, before any Phase 5 computation was run. This file is committed before the runs, so the git history shows that the inputs and the pass/fail limits came first.

**The rules**
- Nothing below changes once this file is committed.
- Every result is published, pass or fail.
- If a bug is found, the fix and the rerun are disclosed next to the first result. A bug fix never changes an input or a limit.

## Setup shared by both runs

| Item | Value | Source |
|---|---|---|
| Flow path and cross-sections | `tests/fixtures/south_lhonak_channel.json` (SHA-256 `3bd8c74b…`) | Byte-identical to the deployed `terrain/south_lhonak/channel.json` |
| Routing code | `core/flood_routing.py` as of commit `2f038b2`, unchanged | |
| Manning's *n* | 0.175 | Calibrated on 8 Oct (brief §4.5). Not refitted here |
| Base flow; arrival rule | 50 m³/s; first rise past base + 10% of the peak rise | `core/flood_routing.py` |
| Release time | 22:12:20 IST, 3 Oct 2023 | Sattar et al. (2025) |
| Published values compared against | Peak at Chungthang **5,340 m³/s** (Sattar et al. 2025) and **about 7,355 m³/s** (*Natural Hazards* 2025). Arrival **about 00:30 IST**, so a travel time of **137.7 min**. Drained volume **50 ± 1.8 million m³** (Sattar et al. 2025) | Credits in the README |
| Percentiles | `numpy.percentile`, default linear method | |

The new code goes in `tools/science/`. Nothing in `core/` or the deployed image changes.

## R4: forecast replay ("what CASCADE would have said on 14 Sep 2023")

**Question:** CASCADE had measured the lake on 14 Sep 2023, three weeks before the burst. From that area alone, with no knowledge of the event, what flood would it have forecast at Teesta-III?

**Inputs, all fixed now:**

| Factor | Values | Source |
|---|---|---|
| Lake area | 1.6438 and 1.6543 km² | CASCADE's 14 Sep 2023 measurement (status "best"), in `evidence/2026-10-08-batch/executions.jsonl` |
| Area to volume | **Huggel et al. (2002):** V = 0.104·A^1.42 (A in m², V in m³)<br>**Fujita et al. (2013):** D = 55·A^0.25 (A in km², D in m), so V = D·A | Huggel, checked as Eq. 3 in Cook & Quincey (2015, *ESurf* 3:559). Fujita, checked as Eq. 1 in Fujita et al. (2013, *NHESS* 13:1827), fitted to Himalayan lake bathymetry; the authors call it "the maximum approximation", an upper envelope |
| Fraction drained | 0.5, 0.75, 1.0 | Brief §3.2, fixed 8 Oct |
| Peak outflow | **Evans (1986):** Qp = 0.72·V^0.53<br>**Popov (1991):** Qp = 0.0048·V^0.896<br>(V in m³, Qp in m³/s) | Both are confirmed only from quoted excerpts of secondary documents, not from the original papers.<br>Evans: quoted in a review table of a Research Square preprint (rs-364424; 39 cases, R² 0.836), which cites *Can. Geotech. J.* 23:385–387.<br>Popov: quoted in Table S2 of the supplement to EGUsphere preprint egusphere-2026-2011 |
| Hydrograph | A triangle holding the drained volume, rising over 0.1 or 0.3 of its length | Brief §3.2 |

That gives 2 × 2 × 3 × 2 × 2 = **48 members**, all routed at *n* = 0.175.

**Excluded before running:** the brief's third peak formula, "Huggel 2002: Qp = 0.00077·V^1.017". Its attribution couldn't be confirmed. The one secondary table found (Mergili & Schneider 2012, *NHESS* 12:393, Table 6) attributes a different moraine-dam formula to Huggel et al. (2002): Qp = 2V/t.

**Reported:** the minimum, p10, p50, p90 and maximum across the 48 members, for drained volume, source peak, peak at the dam, travel time and depth at the dam. The headline uses p10–p90: "As of 14 Sep 2023, CASCADE would have said: X–Y m³/s at Teesta-III, Z–W minutes after a burst."

**Checks (pass or fail as they come out):**

| Check | Passes if | Independent? |
|---|---|---|
| **F1** Volume | The forecast's drained-volume band (minimum to maximum) contains 50 million m³ | Yes |
| **F2** Peak at the dam | The p10–p90 peak band overlaps 5,340–7,355 m³/s | Partly: *n* shapes attenuation |
| **F3** Timing | The p10–p90 travel-time band overlaps 137.7 ± 10 min | **No.** *n* was calibrated on this arrival, so F3 is disclosed as dependent |

## R3: uncertainty band on the 2023 hindcast

**Question:** how much do the hindcast's numbers at the dam move within the inputs' own stated uncertainty?

**Inputs, all fixed now:**

| Factor | Values | Source |
|---|---|---|
| Lake outflow | Sattar et al. (2025) reconstruction, as deployed | Brief §3.2 |
| Drained volume | 48.2, 50.0, 51.8 million m³ | 50 ± 1.8, Sattar et al. (2025) |
| Manning's *n* | *n*₋, 0.175, *n*₊ | *n*₋ and *n*₊ come from the existing `calibrate_manning` search (5 s tolerance), aimed at travel times of 127.7 min and 147.7 min. That is the ±10-minute tolerance fixed on 8 Oct, so these are the roughness values that the calibration couldn't tell apart |

That gives 3 × 3 = **9 members**.

**Reported:** the minimum, median and maximum of the peak at the dam, the travel time and the depth.

**Checks:**

| Check | Passes if |
|---|---|
| **B0** Sanity | The member with 50.0 million m³ and *n* = 0.175 reproduces the deployed hindcast exactly: 10,541 m³/s, 137.7 min and 15.9 m. If it doesn't, that's a bug: stop and disclose |
| **B1** Peak | The minimum-to-maximum peak band overlaps 5,340–7,355 m³/s |

## Where results go

- `docs/phase5-results.md`, generated by the scripts with their raw member tables.
- A short section in the README.
- The headline line in the video, in the slot already in `docs/demo-video-script.md`.
