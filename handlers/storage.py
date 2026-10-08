"""S3 and DynamoDB access for the Lambda handlers.

Bucket and table names come from the environment (see template.yaml).
Clients are created once per container and reused across invocations.
"""
import functools
import json
import os
from decimal import Decimal

import boto3


@functools.cache
def _s3():
    return boto3.client("s3")


@functools.cache
def _table():
    return boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])


def put_json(key, document):
    _s3().put_object(Bucket=os.environ["BUCKET_NAME"], Key=key,
                     Body=json.dumps(document).encode("utf-8"),
                     ContentType="application/json")


def get_json(key, default=None):
    """The JSON document stored at `key`, or `default` if there is none.

    A missing key reads as NoSuchKey only because the functions may list
    the bucket; without s3:ListBucket, S3 answers AccessDenied instead.
    """
    s3 = _s3()
    try:
        response = s3.get_object(Bucket=os.environ["BUCKET_NAME"], Key=key)
    except s3.exceptions.NoSuchKey:
        return default
    with response["Body"] as body:
        return json.loads(body.read())


def put_item(item):
    """Write one item; floats become Decimal, as DynamoDB requires."""
    _table().put_item(Item=json.loads(json.dumps(item), parse_float=Decimal))
