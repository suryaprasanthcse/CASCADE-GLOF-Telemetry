"""Make the publishable evidence folder from the raw, private collection.

    python -m tools.benchmark.redact --raw <private dir> \
        --out evidence/2026-10-08-batch

Redaction rules:
  - every occurrence of the account ID becomes <ACCOUNT>;
  - CloudTrail records are rebuilt from an allowlist of fields. The caller's
    IP address, IAM user name, user ARN, principal ID and access key ID
    become <REDACTED>, and the user agent keeps only its product name. An
    unexpected field stops the run instead of passing through;
  - event IDs, request IDs, execution names and timestamps are kept.

Every output is then scanned for the account ID, the identity values found
in the raw CloudTrail records, access key IDs, IPv4 addresses and any other
free-standing 12-digit number. A hit stops the run. MANIFEST.sha256 lists the
published files and, under private-originals/, the raw files they came from.
"""
import argparse
import hashlib
import json
import pathlib
import re
import sys

FILES = ("collection.json", "executions.jsonl", "execution-history.jsonl",
         "lambda-reports.jsonl", "cloudtrail-start-execution.jsonl",
         "metrics.json", "runner-log.jsonl")
CLOUDTRAIL = "cloudtrail-start-execution.jsonl"
REDACTED = "<REDACTED>"
KEEP = {"awsRegion", "eventCategory", "eventID", "eventName", "eventSource",
        "eventTime", "eventType", "eventVersion", "managementEvent",
        "readOnly", "recipientAccountId", "requestID", "requestParameters",
        "responseElements", "tlsDetails"}
IDENTITY_KEEP = {"type", "accountId"}
IDENTITY_REDACT = {"userName", "arn", "principalId", "accessKeyId"}
PATTERNS = {
    # Free-standing, as in an ARN or a JSON string; runs of digits inside
    # request IDs, trace IDs and hashes are bordered by hex letters or "-".
    "12-digit number": re.compile(
        r"(?<![0-9A-Za-z-])[0-9]{12}(?![0-9A-Za-z-])"),
    "access key ID": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "IPv4 address": re.compile(r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b"),
}


def redact_cloudtrail(record):
    unexpected = set(record) - KEEP - {"userIdentity", "userAgent",
                                       "sourceIPAddress"}
    identity = record["userIdentity"]
    unexpected |= {f"userIdentity.{key}" for key in identity
                   if key not in IDENTITY_KEEP | IDENTITY_REDACT}
    if unexpected:
        sys.exit(f"unexpected CloudTrail fields: {sorted(unexpected)}")
    clean = {key: record[key] for key in KEEP if key in record}
    clean["userIdentity"] = {
        key: (REDACTED if key in IDENTITY_REDACT else value)
        for key, value in identity.items()}
    clean["sourceIPAddress"] = REDACTED
    product = record.get("userAgent", "").split(" ")[0].split("/")[0]
    clean["userAgent"] = f"{product}/{REDACTED}"
    return clean


def private_values(raw):
    """Identity values that must never reach the published copies."""
    values = set()
    with open(raw / CLOUDTRAIL, encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            values.add(record.get("sourceIPAddress"))
            for key in IDENTITY_REDACT:
                values.add(record["userIdentity"].get(key))
    return {value for value in values if value}


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    raw, out = pathlib.Path(args.raw), pathlib.Path(args.out)
    targets = [out / name for name in FILES + ("MANIFEST.sha256",)]
    if any(path.exists() for path in targets):
        sys.exit(f"refusing to overwrite existing files in {out}")
    out.mkdir(parents=True, exist_ok=True)

    account = json.loads((raw / "collection.json").read_text(
        encoding="utf-8"))["account"]
    for name in FILES:
        text = (raw / name).read_bytes().decode("utf-8")
        if name == CLOUDTRAIL:
            text = "".join(
                json.dumps(redact_cloudtrail(json.loads(line)),
                           sort_keys=True) + "\n"
                for line in text.splitlines())
        (out / name).write_bytes(
            text.replace(account, "<ACCOUNT>").encode("utf-8"))

    secrets = private_values(raw) | {account}
    leaks = []
    for name in FILES:
        text = (out / name).read_text(encoding="utf-8")
        if any(secret in text for secret in secrets):
            leaks.append((name, "private identity value"))
        for kind, pattern in PATTERNS.items():
            if pattern.search(text):
                leaks.append((name, kind))
    if leaks:
        for path in targets:
            path.unlink(missing_ok=True)
        sys.exit("leak check failed, outputs removed: "
                 + ", ".join(f"{name} ({kind})" for name, kind in leaks))

    lines = [f"{sha256(out / name)}  {name}" for name in sorted(FILES)]
    lines += [f"{sha256(raw / name)}  private-originals/{name}"
              for name in sorted(FILES)]
    (out / "MANIFEST.sha256").write_bytes(
        ("\n".join(lines) + "\n").encode("utf-8"))
    print(f"wrote {len(FILES)} redacted files and MANIFEST.sha256 to {out}; "
          f"leak check passed ({len(secrets)} private values, "
          f"{len(PATTERNS)} patterns)")


if __name__ == "__main__":
    main()
