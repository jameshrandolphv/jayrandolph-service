"""GET /albums: list the photo bucket and return album/photo metadata with presigned URLs.

Bucket layout (written by scripts/upload-photos.mjs):

    albums/<album-id>/album.json            optional: {"title": "...", ...extra metadata}
    albums/<album-id>/originals/<id>.<ext>  full-size original; user metadata: width, height, title
    albums/<album-id>/thumbs/<id>.webp      thumbnail

Objects whose file name starts with "private-" (case-insensitive) are never listed or presigned.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote

import boto3
from botocore.config import Config

logger = logging.getLogger()
logger.setLevel(logging.INFO)

PREFIX = "albums/"
PRIVATE_PREFIX = "private-"
KEY_RE = re.compile(r"^albums/(?P<album>[^/]+)/(?P<kind>originals|thumbs)/(?P<file>[^/]+)$")
ALBUM_META_RE = re.compile(r"^albums/(?P<album>[^/]+)/album\.json$")
MAX_ALBUM_JSON_BYTES = 64 * 1024

s3 = boto3.client(
    "s3",
    config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}, max_pool_connections=32),
)

# Warm-container cache of per-object lookups, keyed by S3 key and validated by ETag.
_cache: dict[str, tuple[str, object]] = {}


def _bucket() -> str:
    return os.environ["PHOTOS_BUCKET_NAME"]


def _ttl() -> int:
    return int(os.environ.get("PRESIGNED_URL_TTL", "3600"))


def _cached(key: str, etag: str, load):
    hit = _cache.get(key)
    if hit and hit[0] == etag:
        return hit[1]
    value = load()
    _cache[key] = (etag, value)
    return value


def _read_image_meta(key: str) -> dict | None:
    head = s3.head_object(Bucket=_bucket(), Key=key)
    meta = head.get("Metadata", {})
    try:
        width, height = int(meta["width"]), int(meta["height"])
    except (KeyError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    title = unquote(meta["title"]) if meta.get("title") else None
    return {"width": width, "height": height, "title": title}


def _read_album_json(key: str) -> dict:
    body = s3.get_object(Bucket=_bucket(), Key=key)["Body"].read(MAX_ALBUM_JSON_BYTES + 1)
    if len(body) > MAX_ALBUM_JSON_BYTES:
        raise ValueError("album.json too large")
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError("album.json must be an object")
    return data


def _safe(fn, *args):
    """Run a lookup, returning None (and logging) rather than failing the whole listing."""
    try:
        return fn(*args)
    except Exception:
        logger.exception("lookup failed for %s", args[0] if args else fn)
        return None


def _natural(value: str):
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", value.lower())]


def _title_case(value: str) -> str:
    return re.sub(r"\b\w", lambda m: m.group(0).upper(), re.sub(r"[-_]+", " ", value).strip())


def _presign(key: str) -> str:
    return s3.generate_presigned_url(
        "get_object", Params={"Bucket": _bucket(), "Key": key}, ExpiresIn=_ttl()
    )


def _list_objects() -> list[dict]:
    objects: list[dict] = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=_bucket(), Prefix=PREFIX):
        objects.extend(page.get("Contents", []))
    return objects


def build_albums() -> list[dict]:
    albums: dict[str, dict] = {}
    for obj in _list_objects():
        key = obj["Key"]
        if key.rsplit("/", 1)[-1].lower().startswith(PRIVATE_PREFIX):
            continue
        if m := KEY_RE.match(key):
            album_id = m["album"]
            slot = albums.setdefault(album_id, {"originals": {}, "thumbs": {}, "meta": None})
            stem = m["file"].rsplit(".", 1)[0]
            slot[m["kind"]][stem] = obj
        elif m := ALBUM_META_RE.match(key):
            slot = albums.setdefault(m["album"], {"originals": {}, "thumbs": {}, "meta": None})
            slot["meta"] = obj

    with ThreadPoolExecutor(max_workers=16) as pool:
        image_futures = {
            (album_id, stem): pool.submit(
                _safe, _cached, obj["Key"], obj["ETag"], lambda k=obj["Key"]: _read_image_meta(k)
            )
            for album_id, slot in albums.items()
            for stem, obj in slot["originals"].items()
            if stem in slot["thumbs"]
        }
        meta_futures = {
            album_id: pool.submit(
                _safe, _cached, slot["meta"]["Key"], slot["meta"]["ETag"],
                lambda k=slot["meta"]["Key"]: _read_album_json(k),
            )
            for album_id, slot in albums.items()
            if slot["meta"]
        }

        result = []
        for album_id, slot in albums.items():
            album_meta = dict(meta_futures[album_id].result() or {}) if album_id in meta_futures else {}
            title = album_meta.pop("title", None)
            images = []
            for stem in sorted(slot["originals"], key=_natural):
                future = image_futures.get((album_id, stem))
                if future is None:
                    logger.warning("no thumbnail for %s/%s; skipping", album_id, stem)
                    continue
                info = future.result()
                if info is None:
                    logger.warning("missing or invalid width/height metadata for %s/%s; skipping", album_id, stem)
                    continue
                original, thumb = slot["originals"][stem], slot["thumbs"][stem]
                images.append(
                    {
                        "id": stem,
                        "name": info["title"] or _title_case(stem),
                        "thumb": _presign(thumb["Key"]),
                        "src": _presign(original["Key"]),
                        "width": info["width"],
                        "height": info["height"],
                        "size": original["Size"],
                        "lastModified": original["LastModified"].isoformat(),
                    }
                )
            if images:
                result.append(
                    {
                        "id": album_id,
                        "title": title if isinstance(title, str) and title.strip() else _title_case(album_id),
                        "metadata": album_meta,
                        "images": images,
                    }
                )
    return sorted(result, key=lambda a: _natural(a["id"]))


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def handler(event, context):
    try:
        ttl = _ttl()
        albums = build_albums()
        return _response(200, {"albums": albums, "expiresIn": ttl, "expiresAt": int(time.time()) + ttl})
    except Exception:
        logger.exception("failed to list photos")
        return _response(500, {"message": "Failed to load photos"})
