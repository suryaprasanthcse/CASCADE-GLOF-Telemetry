"""Pull the raw evidence for a window of CASCADE runs from AWS.

    python -m tools.benchmark.collect --out <private dir> \
        --start 2026-10-08T14:40:00Z --end 2026-10-08T15:50:00Z

The files written here are UNREDACTED: they contain the account ID, and the
CloudTrail records carry the caller's IAM identity, IP address and access key
ID. Keep them outside the repo; tools/benchmark/redact.py makes the copies
that get published. Everything is read-only on the AWS side.

Files:
  collection.json                    what was collected, from where, counts
  executions.jsonl                   DescribeExecution for every execution
  execution-history.jsonl            full GetExecutionHistory per execution
  lambda-reports.jsonl               every Lambda REPORT / INIT_REPORT line
  cloudtrail-start-execution.jsonl   CloudTrail StartExecution records
  metrics.json                       1-minute Lambda and Step Functions metrics
"""
import argparse
import json
import pathlib
import re
import sys
from datetime import datetime, timezone

import boto3

from tools.benchmark import stack

REPORT_LINE = re.compile(r"^(REPORT RequestId:|INIT_REPORT )")
LAMBDA_METRICS = (("Invocations", "Sum"), ("Errors", "Sum"),
                  ("Throttles", "Sum"), ("ConcurrentExecutions", "Maximum"))
SFN_METRICS = (("ExecutionsStarted", "Sum"), ("ExecutionsSucceeded", "Sum"),
               ("ExecutionsFailed", "Sum"), ("ExecutionsTimedOut", "Sum"),
               ("ExecutionThrottled", "Sum"))


def utc(value):
    """JSON encoder hook: datetimes as UTC ISO 8601, whatever the local TZ."""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def write_jsonl(path, records):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, default=utc, sort_keys=True)
                         + "\n")


def executions(sfn, arn, start, end):
    found = []
    for page in sfn.get_paginator("list_executions").paginate(
            stateMachineArn=arn):
        for item in page["executions"]:
            if start <= item["startDate"] <= end:
                found.append(item["executionArn"])
    described = []
    for execution_arn in found:
        record = sfn.describe_execution(executionArn=execution_arn)
        record.pop("ResponseMetadata", None)
        described.append(record)
    return sorted(described, key=lambda r: r["startDate"])


def histories(sfn, described):
    for record in described:
        events = []
        for page in sfn.get_paginator("get_execution_history").paginate(
                executionArn=record["executionArn"]):
            events.extend(page["events"])
        yield {"executionArn": record["executionArn"], "events": events}


def report_lines(logs, log_groups, start, end):
    """Same row format as the first private export, so hashes compare."""
    rows = []
    for group in log_groups:
        for page in logs.get_paginator("filter_log_events").paginate(
                logGroupName=group, filterPattern="REPORT",
                startTime=int(start.timestamp() * 1000),
                endTime=int(end.timestamp() * 1000)):
            for event in page["events"]:
                message = event["message"]
                if not REPORT_LINE.match(message):
                    continue
                rows.append({
                    "logGroupName": group,
                    "logStreamName": event["logStreamName"],
                    "timestamp": event["timestamp"],
                    "ingestionTime": event.get("ingestionTime"),
                    "eventId": event["eventId"],
                    "type": ("INIT_REPORT" if message.startswith("INIT_REPORT")
                             else "REPORT"),
                    "message": message.rstrip("\n"),
                })
    rows.sort(key=lambda r: (r["timestamp"], r["logGroupName"],
                             r["eventId"]))
    return rows


def start_execution_events(cloudtrail, arn, start, end):
    records = []
    for page in cloudtrail.get_paginator("lookup_events").paginate(
            LookupAttributes=[{"AttributeKey": "EventName",
                               "AttributeValue": "StartExecution"}],
            StartTime=start, EndTime=end):
        for event in page["Events"]:
            record = json.loads(event["CloudTrailEvent"])
            parameters = record.get("requestParameters") or {}
            if parameters.get("stateMachineArn") == arn:
                records.append(record)
    return sorted(records, key=lambda r: (r["eventTime"], r["eventID"]))


def metrics(cloudwatch, deployed, start, end):
    queries = []
    for function in stack.FUNCTIONS:
        for metric, stat in LAMBDA_METRICS:
            queries.append((f"{function}_{metric.lower()}", "AWS/Lambda",
                            metric, stat,
                            [{"Name": "FunctionName",
                              "Value": deployed.functions[function]}]))
    for metric, stat in SFN_METRICS:
        queries.append((f"pipeline_{metric.lower()}", "AWS/States", metric,
                        stat, [{"Name": "StateMachineArn",
                                "Value": deployed.state_machine_arn}]))
    requests = [{"Id": query_id,
                 "MetricStat": {"Metric": {"Namespace": namespace,
                                           "MetricName": metric,
                                           "Dimensions": dimensions},
                                "Period": 60, "Stat": stat}}
                for query_id, namespace, metric, stat, dimensions in queries]
    results = {}
    for page in cloudwatch.get_paginator("get_metric_data").paginate(
            MetricDataQueries=requests, StartTime=start, EndTime=end,
            ScanBy="TimestampAscending"):
        for result in page["MetricDataResults"]:
            series = results.setdefault(result["Id"],
                                        {"timestamps": [], "values": []})
            series["timestamps"].extend(result["Timestamps"])
            series["values"].extend(result["Values"])
    return {
        "period_seconds": 60,
        "start": start,
        "end": end,
        "series": [{"id": query_id, "namespace": namespace,
                    "metric": metric, "stat": stat,
                    "dimensions": dimensions, **results.get(
                        query_id, {"timestamps": [], "values": []})}
                   for query_id, namespace, metric, stat, dimensions
                   in queries],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--start", required=True, type=datetime.fromisoformat)
    parser.add_argument("--end", required=True, type=datetime.fromisoformat)
    parser.add_argument("--stack", default=stack.STACK)
    parser.add_argument("--region", default=stack.REGION)
    args = parser.parse_args()
    out = pathlib.Path(args.out)
    if out.exists() and any(out.iterdir()):
        sys.exit(f"refusing to write into non-empty {out}")
    out.mkdir(parents=True, exist_ok=True)

    session = boto3.session.Session(region_name=args.region)
    account = session.client("sts").get_caller_identity()["Account"]
    deployed = stack.lookup(args.stack, args.region)
    sfn = session.client("stepfunctions")

    described = executions(sfn, deployed.state_machine_arn, args.start,
                           args.end)
    write_jsonl(out / "executions.jsonl", described)
    write_jsonl(out / "execution-history.jsonl", histories(sfn, described))
    reports = report_lines(session.client("logs"),
                           sorted(deployed.log_groups.values()),
                           args.start, args.end)
    with open(out / "lambda-reports.jsonl", "w", encoding="utf-8",
              newline="\n") as handle:
        for row in reports:
            handle.write(json.dumps(row, separators=(",", ":")) + "\n")
    trail = start_execution_events(session.client("cloudtrail"),
                                   deployed.state_machine_arn, args.start,
                                   args.end)
    write_jsonl(out / "cloudtrail-start-execution.jsonl", trail)
    with open(out / "metrics.json", "w", encoding="utf-8",
              newline="\n") as handle:
        json.dump(metrics(session.client("cloudwatch"), deployed, args.start,
                          args.end), handle, default=utc, indent=1,
                  sort_keys=True)
        handle.write("\n")

    collection = {
        "collected_at": datetime.now(timezone.utc),
        "account": account,
        "region": args.region,
        "stack": args.stack,
        "window": {"start": args.start, "end": args.end},
        "state_machine_arn": deployed.state_machine_arn,
        "functions": deployed.functions,
        "log_groups": deployed.log_groups,
        "counts": {
            "executions": len(described),
            "report_lines": sum(r["type"] == "REPORT" for r in reports),
            "init_report_lines": sum(r["type"] == "INIT_REPORT"
                                     for r in reports),
            "cloudtrail_start_execution": len(trail),
        },
    }
    with open(out / "collection.json", "w", encoding="utf-8",
              newline="\n") as handle:
        json.dump(collection, handle, default=utc, indent=1, sort_keys=True)
        handle.write("\n")
    print(json.dumps(collection["counts"]))


if __name__ == "__main__":
    main()
