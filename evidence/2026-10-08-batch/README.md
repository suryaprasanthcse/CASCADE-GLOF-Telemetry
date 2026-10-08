# Evidence: the 8 Oct 2026 hindcast runs on AWS

These files are the AWS records behind the "Cloud performance and determinism" section of the main [README](../../README.md). They cover all **62 executions** of the `CascadePipeline` state machine on 8 Oct 2026, between 14:46 and 15:46 UTC: 2 manual runs, then a batch of 60. Times in the files are UTC.

## Check it yourself

```bash
cd evidence/2026-10-08-batch
sha256sum -c --ignore-missing MANIFEST.sha256
```

```bash
python -m tools.benchmark.analyze evidence/2026-10-08-batch
```

The first command checks the files against the manifest. The second, run from the repo root, checks the manifest again, cross-checks the sources against each other and prints the README's tables. It needs Python 3.11 or newer and nothing else: no AWS account and no packages.

## Files

| File | Source (AWS API) | What it holds |
|---|---|---|
| `executions.jsonl` | Step Functions `DescribeExecution` | 62 executions: name, status, start and stop times, input, full pipeline output |
| `execution-history.jsonl` | Step Functions `GetExecutionHistory` | Every state and task event of every execution, including each Lambda call's request ID |
| `lambda-reports.jsonl` | CloudWatch Logs `FilterLogEvents` | 310 Lambda `REPORT` lines (duration, billed duration, memory, init time) and 4 `INIT_REPORT` lines |
| `cloudtrail-start-execution.jsonl` | CloudTrail `LookupEvents` | 62 `StartExecution` records, redacted |
| `metrics.json` | CloudWatch `GetMetricData` | 1-minute Lambda invocations, errors, throttles and concurrency; Step Functions execution counts |
| `runner-log.jsonl` | The batch runner (`tools/benchmark/run_batch.py`), on the client | 60 batch runs: timing, full output and footprint geometry hashes |
| `collection.json` | `tools/benchmark/collect.py` | Window, resource names and counts for this collection |
| `MANIFEST.sha256` | `tools/benchmark/redact.py` | SHA-256 of every published file and of the private, unredacted originals |

## What `analyze` cross-checks

- **Executions:** 62 executions, all `SUCCEEDED`, 62 distinct names, and every history ends in `ExecutionSucceeded`.
- **CloudTrail:** 62 `StartExecution` records with the same 62 names and the same start times.
- **Lambda logs:** all 310 `REPORT` lines are tied to an execution by request ID through the execution history, and each was logged by the function that the history says was called. The 4 init timeouts are paired with their invocations by log stream.
- **Invocation counts:** CloudWatch counts 62 plan, 124 measure, 62 route and 62 report invocations, equal to the `REPORT` lines.
- **Runner log:** the runner's 60 runs match Step Functions exactly, in timing and in output.

## How it was made

1. `tools/benchmark/run_batch.py` ran the batch: 15 runs, then 45.
2. `tools/benchmark/collect.py` pulled the raw records read-only into a private folder outside the repo.
3. `tools/benchmark/redact.py` wrote this folder.

The collection was re-run once more, and every raw file came out byte-identical apart from its collection timestamp.

## Redaction

- **Account ID:** the AWS account ID is `<ACCOUNT>` everywhere.
- **CloudTrail records:** rebuilt from an allowlist of fields. The caller's IP address, IAM user name, user ARN, principal ID and access key ID are `<REDACTED>`, and the user agent keeps only its product name (`Boto3`). The execution input in these records was already hidden by AWS.
- **Kept:** event IDs, request IDs, execution names and timestamps.
- **Leak check:** before writing the manifest, `redact.py` scans the output for the account ID, the redacted identity values, access key IDs, IPv4 addresses and free-standing 12-digit numbers.

`MANIFEST.sha256` also lists the hashes of the unredacted originals under `private-originals/`. They are kept private, and `--ignore-missing` skips them. Publishing their hashes fixes their content without revealing it.

## Limits of this evidence

- **CloudTrail records aren't signed.** There was no CloudTrail trail, so the records come from CloudTrail's 90-day event history, not from signed log files. The tamper evidence here is the git history and the manifest.
- **The runner log comes from the client.** It's the only source for the footprint geometry hashes. Its timings and outputs match Step Functions exactly.
- **Footprint hashes cover only 45 runs.** Batch runs 1–15 logged the S3 ETag of the footprint file, using an earlier version of the runner. Runs 16–60 logged the SHA-256 of the footprint's geometry, which is what `run_batch.py` does now. "45 of 45" in the main README counts only those 45.
- **CloudWatch misses one success.** Step Functions' `ExecutionsSucceeded` metric shows **61** successes against 62 succeeded executions, at 1-minute, 5-minute and 1-hour periods. The missing count falls in the 15:42 UTC minute, where 6 executions finished and the metric shows 5. The metric shows 0 failed, aborted or timed-out executions. The execution records, the histories, CloudTrail and the Lambda metrics all agree on 62, so this table reports the metric as found.
- **Some README claims aren't covered here.**
  - The footprint's size (449 vertices, 17.9 KiB) and its ~330 m² growth after the first run were measured on the S3 object, which every run overwrote; the bucket has no versioning.
  - The Lambda concurrency limit of 10 is an account setting.
- **Collecting again won't reproduce everything.** AWS keeps execution records, CloudTrail event history and (since 8 Oct) these log groups for about 90 days. CloudWatch keeps 1-minute metric data for only 15 days, so a later collection can't reproduce `metrics.json` exactly.
