"""Serves the dam dossier page through a Lambda function URL.

The page's own files come from the site bucket. Requests under the three
published prefixes come from the results bucket, which this function's
role can read nowhere else. Everything is read-only: GET and HEAD only.
"""
import base64
import functools
import os
import posixpath

import boto3
from botocore.exceptions import ClientError

RESULT_PREFIXES = ("reports/", "obs/", "terrain/")
TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".geojson": "application/geo+json",
    ".jpg": "image/jpeg",
    ".png": "image/png",
}
SECURITY_HEADERS = {
    "Strict-Transport-Security": "max-age=31536000",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}


@functools.cache
def _s3():
    return boto3.client("s3")


def resolve(raw_path):
    """(bucket, key, cache policy) for a request path, or None.

    Paths are normalised first, so "..", "//" and the like can never
    reach outside the page files or the three published prefixes.
    """
    key = posixpath.normpath("/" + raw_path).lstrip("/") or "index.html"
    if key.startswith(RESULT_PREFIXES):
        return os.environ["BUCKET_NAME"], key, "no-store"
    if posixpath.splitext(key)[1] not in TYPES:
        return None
    return os.environ["SITE_BUCKET"], key, "public, max-age=300"


def response(status, body=b"", content_type="text/plain; charset=utf-8",
             cache="no-store", head=False):
    text = content_type.startswith("text/") or "json" in content_type
    payload = b"" if head else body
    return {
        "statusCode": status,
        "headers": {"Content-Type": content_type, "Cache-Control": cache,
                    **SECURITY_HEADERS},
        "body": (payload.decode("utf-8") if text
                 else base64.b64encode(payload).decode("ascii")),
        "isBase64Encoded": not text,
    }


def handler(event, context):
    method = event["requestContext"]["http"]["method"]
    if method not in ("GET", "HEAD"):
        return response(405, b"Method not allowed")
    target = resolve(event.get("rawPath", "/"))
    if target is None:
        return response(404, b"Not found")
    bucket, key, cache = target
    try:
        body = _s3().get_object(Bucket=bucket, Key=key)["Body"].read()
    except ClientError as error:
        # No ListBucket permission, so a missing key reads as AccessDenied.
        if error.response["Error"]["Code"] in ("NoSuchKey", "AccessDenied"):
            return response(404, b"Not found")
        raise
    content_type = TYPES.get(posixpath.splitext(key)[1],
                             "application/octet-stream")
    return response(200, body, content_type, cache, head=method == "HEAD")
