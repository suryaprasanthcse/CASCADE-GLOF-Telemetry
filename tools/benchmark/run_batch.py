"""Run the CASCADE 2023 hindcast back to back and log one JSON line per run.

    python -m tools.benchmark.run_batch --runs 15 --first 1 --log runs.jsonl

Each record holds the execution timing, the full pipeline output, and a hash
of the stored lake footprint's geometry before and after the run (the
pipeline grows the footprint, so outputs can only be compared run to run
once it settles). The state machine and bucket are looked up from the stack.
"""
import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

from tools.benchmark import stack

FOOTPRINT_KEY = "footprints/south_lhonak.geojson"
EVENT = {"start": "2023-09-01", "end": "2023-10-31", "lakes": ["south_lhonak"]}


def footprint_hash(s3, bucket):
    """SHA-256 of the footprint *geometry*. The file also carries an
    "updated" timestamp that changes every run, so its ETag says nothing."""
    with s3.get_object(Bucket=bucket, Key=FOOTPRINT_KEY)["Body"] as body:
        geometry = json.loads(body.read())["geometry"]
    canonical = json.dumps(geometry, sort_keys=True).encode()
    return hashlib.sha256(canonical).hexdigest()


def run_once(sfn, s3, deployed, name):
    before = footprint_hash(s3, deployed.bucket)
    arn = sfn.start_execution(stateMachineArn=deployed.state_machine_arn,
                              name=name, input=json.dumps(EVENT))[
        "executionArn"]
    while True:
        execution = sfn.describe_execution(executionArn=arn)
        if execution["status"] != "RUNNING":
            break
        time.sleep(2)
    return {
        "name": name,
        "status": execution["status"],
        "start": execution["startDate"].isoformat(),
        "stop": execution["stopDate"].isoformat(),
        "seconds": (execution["stopDate"]
                    - execution["startDate"]).total_seconds(),
        "footprint_before": before,
        "footprint_after": footprint_hash(s3, deployed.bucket),
        "output": json.loads(execution.get("output") or "null"),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, required=True)
    parser.add_argument("--first", type=int, required=True)
    parser.add_argument("--log", required=True)
    parser.add_argument("--stack", default=stack.STACK)
    parser.add_argument("--region", default=stack.REGION)
    args = parser.parse_args()
    deployed = stack.lookup(args.stack, args.region)
    sfn = boto3.client("stepfunctions", region_name=args.region)
    s3 = boto3.client("s3", region_name=args.region)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M")
    try:
        for index in range(args.first, args.first + args.runs):
            record = run_once(sfn, s3, deployed, f"batch-{stamp}-{index:03d}")
            with open(args.log, "a", encoding="utf-8") as log:
                log.write(json.dumps(record) + "\n")
            print(f"run {index:3d}: {record['status']} "
                  f"{record['seconds']:6.1f} s footprint "
                  f"{record['footprint_before'][:8]}->"
                  f"{record['footprint_after'][:8]}", flush=True)
            if record["status"] != "SUCCEEDED":
                print("stopping: execution did not succeed")
                return 1
        return 0
    except ClientError as error:
        print(f"AWS error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
