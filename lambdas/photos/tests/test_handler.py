import json
import os
import sys
from urllib.parse import parse_qs, urlparse

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

BUCKET = "test-photos"


@pytest.fixture
def handler(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("PHOTOS_BUCKET_NAME", BUCKET)
    monkeypatch.setenv("PRESIGNED_URL_TTL", "600")
    with mock_aws():
        import importlib

        import handler as module

        module = importlib.reload(module)
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        yield module


def put(key, body=b"x", **metadata):
    boto3.client("s3", region_name="us-east-1").put_object(Bucket=BUCKET, Key=key, Body=body, Metadata=metadata)


def add_photo(album, stem, ext="jpg", width="300", height="200", **extra):
    put(f"albums/{album}/originals/{stem}.{ext}", b"original", width=width, height=height, **extra)
    put(f"albums/{album}/thumbs/{stem}.webp", b"thumb")


def call(handler):
    res = handler.handler({}, None)
    return res["statusCode"], json.loads(res["body"])


def test_empty_bucket(handler):
    status, body = call(handler)
    assert status == 200
    assert body["albums"] == []
    assert body["expiresIn"] == 600


def test_lists_albums_with_metadata_and_presigned_urls(handler):
    put("albums/2026-09-01-portra/album.json", json.dumps({"title": "Kodak Portra 400", "camera": "Nikon F3"}).encode())
    add_photo("2026-09-01-portra", "scan-2", title="Scan%20%282%29")
    add_photo("2026-09-01-portra", "scan-10", width="200", height="300")
    add_photo("other", "a", ext="png")

    status, body = call(handler)
    assert status == 200
    portra, other = body["albums"]

    assert portra["id"] == "2026-09-01-portra"
    assert portra["title"] == "Kodak Portra 400"
    assert portra["metadata"] == {"camera": "Nikon F3"}
    assert [i["id"] for i in portra["images"]] == ["scan-2", "scan-10"]

    first = portra["images"][0]
    assert first["name"] == "Scan (2)"
    assert (first["width"], first["height"], first["size"]) == (300, 200, len(b"original"))
    assert portra["images"][1]["name"] == "Scan 10"

    src, thumb = urlparse(first["src"]), urlparse(first["thumb"])
    assert src.path == "/albums/2026-09-01-portra/originals/scan-2.jpg"
    assert thumb.path == "/albums/2026-09-01-portra/thumbs/scan-2.webp"
    assert parse_qs(src.query)["X-Amz-Expires"] == ["600"]

    assert other["title"] == "Other"
    assert other["metadata"] == {}


def test_skips_photos_without_thumbnail_or_dimensions(handler):
    add_photo("album", "good")
    put("albums/album/originals/no-thumb.jpg", b"x", width="1", height="1")
    put("albums/album/originals/no-dims.jpg", b"x")
    put("albums/album/thumbs/no-dims.webp", b"x")
    put("albums/empty/album.json", b"{}")
    put("unrelated/file.jpg", b"x")

    _, body = call(handler)
    assert [a["id"] for a in body["albums"]] == ["album"]
    assert [i["id"] for i in body["albums"][0]["images"]] == ["good"]


def test_bad_album_json_falls_back_to_derived_title(handler):
    put("albums/my-trip/album.json", b"not json")
    add_photo("my-trip", "a")
    _, body = call(handler)
    assert body["albums"][0]["title"] == "My Trip"


def test_returns_500_when_bucket_is_unreadable(handler, monkeypatch):
    monkeypatch.setenv("PHOTOS_BUCKET_NAME", "does-not-exist")
    status, body = call(handler)
    assert status == 500
    assert body == {"message": "Failed to load photos"}


def test_excludes_files_prefixed_private(handler):
    add_photo("album", "public")
    add_photo("album", "private-secret")
    add_photo("album", "Private-Mixed-Case", ext="JPG")
    put("albums/only-private/album.json", b"{}")
    add_photo("only-private", "private-one")
    put("albums/album/private-album.json", b"{}")

    _, body = call(handler)
    assert [a["id"] for a in body["albums"]] == ["album"]
    assert [i["id"] for i in body["albums"][0]["images"]] == ["public"]
    assert "private" not in json.dumps(body).lower()


def test_returns_folder_path_for_nested_albums(handler):
    put("albums/trip/album.json", json.dumps({"title": "Trip", "path": ["trip"]}).encode())
    add_photo("trip", "a")
    put("albums/trip-day-1/album.json", json.dumps({"title": "Day 1", "path": ["trip", "Day 1"], "camera": "F3"}).encode())
    add_photo("trip-day-1", "b")
    put("albums/loose/album.json", json.dumps({"title": "Pics", "path": []}).encode())
    add_photo("loose", "c")
    put("albums/legacy/album.json", json.dumps({"title": "Old One"}).encode())
    add_photo("legacy", "d")
    put("albums/bad/album.json", json.dumps({"title": "Bad", "path": ["ok", 3]}).encode())
    add_photo("bad", "e")

    _, body = call(handler)
    albums = {a["id"]: a for a in body["albums"]}
    assert albums["trip"]["path"] == ["trip"]
    assert albums["trip-day-1"]["path"] == ["trip", "Day 1"]
    assert albums["trip-day-1"]["metadata"] == {"camera": "F3"}
    assert albums["loose"]["path"] == []
    assert albums["legacy"]["path"] == ["Old One"]
    assert albums["bad"]["path"] == ["Bad"]


def test_uses_images_json_instead_of_per_photo_metadata(handler, monkeypatch):
    # Originals carry no width/height metadata, so listing them proves the index was used.
    for stem in ("a", "b", "c"):
        put(f"albums/album/originals/{stem}.jpg", b"x")
        put(f"albums/album/thumbs/{stem}.webp", b"x")
    index = {"a": {"width": 30, "height": 20, "title": "Alpha"}, "b": {"width": 0, "height": 5}, "c": "nope"}
    put("albums/album/images.json", json.dumps(index).encode())

    _, body = call(handler)
    images = body["albums"][0]["images"]
    assert [(i["id"], i["name"], i["width"], i["height"]) for i in images] == [("a", "Alpha", 30, 20)]


def test_falls_back_to_object_metadata_for_photos_missing_from_index(handler):
    add_photo("album", "indexed", width="1", height="1")
    add_photo("album", "legacy", width="40", height="30")
    put("albums/album/images.json", json.dumps({"indexed": {"width": 10, "height": 5}}).encode())

    _, body = call(handler)
    assert [(i["id"], i["width"], i["height"]) for i in body["albums"][0]["images"]] == [("indexed", 10, 5), ("legacy", 40, 30)]


def test_bad_images_json_falls_back_to_object_metadata(handler):
    add_photo("album", "a", width="7", height="8")
    put("albums/album/images.json", b"not json")

    _, body = call(handler)
    assert [(i["width"], i["height"]) for i in body["albums"][0]["images"]] == [(7, 8)]


def test_gzips_response_when_client_accepts_it(handler):
    import base64
    import gzip

    add_photo("album", "a")
    plain = handler.handler({}, None)
    zipped = handler.handler({"headers": {"accept-encoding": "gzip, deflate, br"}}, None)

    assert zipped["isBase64Encoded"] is True
    assert zipped["headers"]["Content-Encoding"] == "gzip"
    body = json.loads(gzip.decompress(base64.b64decode(zipped["body"])))
    assert [a["id"] for a in body["albums"]] == [json.loads(plain["body"])["albums"][0]["id"]]
    assert "isBase64Encoded" not in plain


@pytest.mark.parametrize("token", [None, "sess/ion+token=="])
def test_hand_signed_urls_match_botocore(handler, monkeypatch, token):
    if token:
        monkeypatch.setenv("AWS_SESSION_TOKEN", token)
    handler._session = boto3.session.Session()
    handler.s3 = handler._session.client("s3", region_name="us-east-1", config=handler.s3.meta.config)
    keys = ["albums/a/originals/x.jpg", "albums/trip day/thumbs/we ird&name+(1)~é.webp"]

    for _ in range(5):  # retry if the two signers straddle a second boundary
        ours = handler._make_presigner()
        theirs = [handler.s3.generate_presigned_url("get_object", Params={"Bucket": BUCKET, "Key": k}, ExpiresIn=600) for k in keys]
        ours_urls = [ours(k) for k in keys]
        if all(parse_qs(urlparse(a).query)["X-Amz-Date"] == parse_qs(urlparse(b).query)["X-Amz-Date"] for a, b in zip(ours_urls, theirs)):
            break
    for a, b in zip(ours_urls, theirs):
        assert urlparse(a)[:3] == urlparse(b)[:3]
        assert parse_qs(urlparse(a).query) == parse_qs(urlparse(b).query)
