"""GET /albums: list the photo bucket and return album/photo metadata with presigned URLs.

Bucket layout (written by scripts/upload-photos.mjs):

    albums/<album-id>/album.json            optional: {"title": "...", "path": ["folder", ...], ...extra metadata}
    albums/<album-id>/images.json           optional: {"<id>": {"width": n, "height": n, "title": "..."}, ...}
    albums/<album-id>/originals/<id>.<ext>  full-size original; user metadata: width, height, title
    albums/<album-id>/thumbs/<id>.webp      thumbnail

images.json saves a HEAD request per photo; photos missing from it fall back to their object metadata.
"path" is the album's folder path relative to the uploaded source directory, so clients can rebuild the
folder tree from the flat album list. Albums without a valid path are treated as top-level.

Objects whose file name starts with "private-" (case-insensitive) are never listed or presigned.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import quote, unquote, urlsplit

import boto3
from botocore.config import Config

logger = logging.getLogger()
logger.setLevel(logging.INFO)

PREFIX = "albums/"
PRIVATE_PREFIX = "private-"
KEY_RE = re.compile(r"^albums/(?P<album>[^/]+)/(?P<kind>originals|thumbs)/(?P<file>[^/]+)$")
ALBUM_META_RE = re.compile(r"^albums/(?P<album>[^/]+)/album\.json$")
IMAGE_INDEX_RE = re.compile(r"^albums/(?P<album>[^/]+)/images\.json$")
MAX_ALBUM_JSON_BYTES = 64 * 1024
MAX_IMAGE_INDEX_BYTES = 8 * 1024 * 1024

_session = boto3.session.Session()
s3 = _session.client(
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


def _read_json_object(key: str, max_bytes: int) -> dict:
    body = s3.get_object(Bucket=_bucket(), Key=key)["Body"].read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ValueError(f"{key} too large")
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError(f"{key} must be an object")
    return data


def _index_info(index: dict | None, stem: str) -> dict | None:
    entry = index.get(stem) if index else None
    if not isinstance(entry, dict):
        return None
    width, height, title = entry.get("width"), entry.get("height"), entry.get("title")
    if not all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in (width, height)):
        return None
    return {"width": width, "height": height, "title": title if isinstance(title, str) and title else None}


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


def _album_path(value, title: str) -> list[str]:
    if isinstance(value, list) and all(isinstance(p, str) and p.strip() for p in value):
        return value
    return [title]


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _make_presigner():
    """Return a function that presigns GETs for this bucket.

    Equivalent to s3.generate_presigned_url("get_object", ...) but signs by hand: botocore's per-URL request
    pipeline costs ~1.4 ms on a small Lambda, which adds up to seconds for thousands of photos. The signing
    key, scope and shared query string are computed once per request.
    """
    ttl = _ttl()
    creds = _session.get_credentials().get_frozen_credentials()
    # Borrow host and region from botocore so addressing style and endpoint config stay in one place.
    host = urlsplit(s3.generate_presigned_url("get_object", Params={"Bucket": _bucket(), "Key": "x"}, ExpiresIn=ttl)).netloc
    region = s3.meta.region_name
    amz_date = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    day = amz_date[:8]
    scope = f"{day}/{region}/s3/aws4_request"
    signing_key = _hmac(_hmac(_hmac(_hmac(f"AWS4{creds.secret_key}".encode(), day), region), "s3"), "aws4_request")
    params = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{creds.access_key}/{scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(ttl),
        "X-Amz-SignedHeaders": "host",
    }
    if creds.token:
        params["X-Amz-Security-Token"] = creds.token
    query = "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in sorted(params.items()))

    def presign(key: str) -> str:
        path = "/" + quote(key, safe="/~")
        canonical = f"GET\n{path}\n{query}\nhost:{host}\n\nhost\nUNSIGNED-PAYLOAD"
        to_sign = f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}"
        signature = hmac.new(signing_key, to_sign.encode(), hashlib.sha256).hexdigest()
        return f"https://{host}{path}?{query}&X-Amz-Signature={signature}"

    return presign


def _list_objects() -> list[dict]:
    objects: list[dict] = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=_bucket(), Prefix=PREFIX):
        objects.extend(page.get("Contents", []))
    return objects


class _Timer:
    """Logs how long each phase of a request takes, to spot what dominates large libraries."""

    def __init__(self):
        self.last = time.monotonic()
        self.phases: list[str] = []

    def lap(self, name: str) -> None:
        now = time.monotonic()
        self.phases.append(f"{name}={now - self.last:.2f}s")
        self.last = now


def build_albums(timer: _Timer | None = None) -> list[dict]:
    timer = timer or _Timer()
    presign = _make_presigner()
    albums: dict[str, dict] = {}
    objects = _list_objects()
    timer.lap("list")
    for obj in objects:
        key = obj["Key"]
        if key.rsplit("/", 1)[-1].lower().startswith(PRIVATE_PREFIX):
            continue
        if m := KEY_RE.match(key):
            album_id = m["album"]
            slot = albums.setdefault(album_id, {"originals": {}, "thumbs": {}, "meta": None, "index": None})
            stem = m["file"].rsplit(".", 1)[0]
            slot[m["kind"]][stem] = obj
        elif m := ALBUM_META_RE.match(key):
            slot = albums.setdefault(m["album"], {"originals": {}, "thumbs": {}, "meta": None, "index": None})
            slot["meta"] = obj
        elif m := IMAGE_INDEX_RE.match(key):
            slot = albums.setdefault(m["album"], {"originals": {}, "thumbs": {}, "meta": None, "index": None})
            slot["index"] = obj

    with ThreadPoolExecutor(max_workers=16) as pool:
        meta_futures = {
            album_id: pool.submit(
                _safe, _cached, slot["meta"]["Key"], slot["meta"]["ETag"],
                lambda k=slot["meta"]["Key"]: _read_json_object(k, MAX_ALBUM_JSON_BYTES),
            )
            for album_id, slot in albums.items()
            if slot["meta"]
        }
        index_futures = {
            album_id: pool.submit(
                _safe, _cached, slot["index"]["Key"], slot["index"]["ETag"],
                lambda k=slot["index"]["Key"]: _read_json_object(k, MAX_IMAGE_INDEX_BYTES),
            )
            for album_id, slot in albums.items()
            if slot["index"]
        }
        indexes = {album_id: f.result() for album_id, f in index_futures.items()}

        # Only photos missing from their album's index cost a HEAD request.
        infos = {
            (album_id, stem): _index_info(indexes.get(album_id), stem)
            for album_id, slot in albums.items()
            for stem in slot["originals"]
            if stem in slot["thumbs"]
        }
        image_futures = {
            (album_id, stem): pool.submit(
                _safe, _cached, slot["originals"][stem]["Key"], slot["originals"][stem]["ETag"],
                lambda k=slot["originals"][stem]["Key"]: _read_image_meta(k),
            )
            for album_id, slot in albums.items()
            for stem in slot["originals"]
            if (album_id, stem) in infos and infos[(album_id, stem)] is None
        }

        timer.lap("index")
        result = []
        for album_id, slot in albums.items():
            album_meta = dict(meta_futures[album_id].result() or {}) if album_id in meta_futures else {}
            title = album_meta.pop("title", None)
            raw_path = album_meta.pop("path", None)
            images = []
            for stem in sorted(slot["originals"], key=_natural):
                if (album_id, stem) not in infos:
                    logger.warning("no thumbnail for %s/%s; skipping", album_id, stem)
                    continue
                info = infos[(album_id, stem)]
                if info is None:
                    info = image_futures[(album_id, stem)].result()
                if info is None:
                    logger.warning("missing or invalid width/height metadata for %s/%s; skipping", album_id, stem)
                    continue
                original, thumb = slot["originals"][stem], slot["thumbs"][stem]
                images.append(
                    {
                        "id": stem,
                        "name": info["title"] or _title_case(stem),
                        "thumb": presign(thumb["Key"]),
                        "src": presign(original["Key"]),
                        "width": info["width"],
                        "height": info["height"],
                        "size": original["Size"],
                        "lastModified": original["LastModified"].isoformat(),
                    }
                )
            if images:
                album_title = title if isinstance(title, str) and title.strip() else _title_case(album_id)
                result.append(
                    {
                        "id": album_id,
                        "title": album_title,
                        "path": _album_path(raw_path, album_title),
                        "metadata": album_meta,
                        "images": images,
                    }
                )
    timer.lap("build")
    return sorted(result, key=lambda a: _natural(a["id"]))


def _response(status: int, body: dict, accept_encoding: str = "") -> dict:
    payload = json.dumps(body, separators=(",", ":")).encode()
    # Lambda responses are capped at 6 MB; presigned URLs are repetitive, so gzip shrinks them several-fold.
    if "gzip" in accept_encoding.lower():
        return {
            "statusCode": status,
            "headers": {"Content-Type": "application/json", "Content-Encoding": "gzip"},
            "body": base64.b64encode(gzip.compress(payload, compresslevel=5)).decode(),
            "isBase64Encoded": True,
        }
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": payload.decode()}


def handler(event, context):
    accept_encoding = ((event or {}).get("headers") or {}).get("accept-encoding", "")
    try:
        ttl = _ttl()
        timer = _Timer()
        albums = build_albums(timer)
        response = _response(200, {"albums": albums, "expiresIn": ttl, "expiresAt": int(time.time()) + ttl}, accept_encoding)
        timer.lap("encode")
        logger.info("listed %d albums: %s", len(albums), " ".join(timer.phases))
        return response
    except Exception:
        logger.exception("failed to list photos")
        return _response(500, {"message": "Failed to load photos"})
