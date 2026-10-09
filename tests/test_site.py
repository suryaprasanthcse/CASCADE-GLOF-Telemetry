"""The dossier page's Lambda function URL handler, with S3 stubbed."""
import base64
import io

import pytest
from botocore.exceptions import ClientError

from site_function import app

OBJECTS = {
    ("site", "index.html"): b"<!doctype html>",
    ("site", "img/lake.jpg"): b"\xff\xd8\xff\xe0jpeg",
    ("results", "reports/teesta_iii/dossier.json"): b'{"dam_id": "x"}',
}


class FakeS3:
    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in OBJECTS:
            raise ClientError({"Error": {"Code": "AccessDenied"}},
                              "GetObject")
        return {"Body": io.BytesIO(OBJECTS[(Bucket, Key)])}


@pytest.fixture(autouse=True)
def stub_s3(monkeypatch):
    monkeypatch.setenv("SITE_BUCKET", "site")
    monkeypatch.setenv("BUCKET_NAME", "results")
    monkeypatch.setattr(app, "_s3", FakeS3)


def request(path, method="GET"):
    return app.handler({"rawPath": path,
                        "requestContext": {"http": {"method": method}}},
                       None)


def test_root_serves_the_page():
    result = request("/")
    assert result["statusCode"] == 200
    assert result["headers"]["Content-Type"].startswith("text/html")
    assert result["body"] == "<!doctype html>"
    assert result["headers"]["X-Content-Type-Options"] == "nosniff"


def test_published_results_come_from_the_results_bucket_uncached():
    result = request("/reports/teesta_iii/dossier.json")
    assert result["statusCode"] == 200
    assert result["headers"]["Cache-Control"] == "no-store"
    assert result["body"] == '{"dam_id": "x"}'


def test_images_are_base64_encoded():
    result = request("/img/lake.jpg")
    assert result["isBase64Encoded"]
    expected = OBJECTS[("site", "img/lake.jpg")]
    assert base64.b64decode(result["body"]) == expected


@pytest.mark.parametrize("path", [
    "/footprints/south_lhonak.geojson",   # unpublished results prefix
    "/scenarios/south_lhonak/hindcast.json",
    "/../footprints/south_lhonak.geojson",
    "/reports/../footprints/x.geojson",
    "/template.yaml",                     # not a page file type
    "/img/missing.jpg",
])
def test_anything_else_is_not_found(path):
    assert request(path)["statusCode"] == 404


def test_paths_cannot_escape_into_other_prefixes():
    assert app.resolve("/reports/../footprints/x.geojson") == (
        "site", "footprints/x.geojson", "public, max-age=300")


def test_only_reads_are_allowed():
    assert request("/", method="POST")["statusCode"] == 405


def test_head_returns_headers_without_a_body():
    result = request("/reports/teesta_iii/dossier.json", method="HEAD")
    assert result["statusCode"] == 200
    assert result["body"] == ""
