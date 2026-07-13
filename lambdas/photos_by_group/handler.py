import os
import boto3
from db import query
from response import ok, not_found, error

BUCKET_NAME       = os.environ["PHOTOS_BUCKET_NAME"]
PRESIGNED_URL_TTL = int(os.environ.get("PRESIGNED_URL_TTL", "900"))

_s3 = boto3.client("s3")


def _presign(s3_key: str) -> str:
    return _s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": BUCKET_NAME, "Key": s3_key},
        ExpiresIn=PRESIGNED_URL_TTL,
    )


def handler(event, context):
    try:
        raw_id = (event.get("pathParameters") or {}).get("groupId")
        if not raw_id or not raw_id.isdigit():
            return not_found("Invalid group ID")

        group_id = int(raw_id)

        groups = query(
            "SELECT id, name, type, film_stock, iso, camera, lens, developer, fixer, "
            "dev_number, roll_number, size, number_of_exposures, expiration, dates_shot, "
            "date_developed, dev_time_mins, dev_temp_f, fixer_time_mins, dev_adjustments, "
            "scanner, notes "
            "FROM photo_group WHERE id = :id",
            {"id": {"longValue": group_id}},
        )
        if not groups:
            return not_found(f"Group {group_id} not found")

        g = groups[0]

        photo_rows = query(
            "SELECT id, s3_key, sequence_in_roll, width, height, title "
            "FROM photo "
            "WHERE group_id = :group_id "
            "ORDER BY sequence_in_roll",
            {"group_id": {"longValue": group_id}},
        )

        # NUMERIC columns come back as strings from the Data API
        def _float(v) -> float | None:
            return float(v) if v is not None else None

        photos = [
            {
                "id":     row["id"],
                "src":    _presign(row["s3_key"]),
                "width":  row["width"],
                "height": row["height"],
                "metadata": {
                    "title":               row["title"] or "",
                    "type":                g["type"],
                    "roll_number":         g["roll_number"],
                    "developer":           g["developer"] or "",
                    "fixer":               g["fixer"] or "",
                    "dev_number":          g["dev_number"],
                    "film_stock":          g["film_stock"] or "",
                    "iso":                 g["iso"],
                    "expiration":          g["expiration"] or "",
                    "size":                g["size"],
                    "number_of_exposures": g["number_of_exposures"],
                    "dates_shot":          g["dates_shot"] or "",
                    "date_developed":      g["date_developed"] or "",
                    "camera":              g["camera"] or "",
                    "lens":                g["lens"] or "",
                    "dev_time_mins":       _float(g["dev_time_mins"]),
                    "dev_temp_f":          _float(g["dev_temp_f"]),
                    "fixer_time_mins":     _float(g["fixer_time_mins"]),
                    "dev_adjustments":     g["dev_adjustments"] or "",
                    "scanner":             g["scanner"] or "",
                    "notes":               g["notes"] or "",
                },
            }
            for row in photo_rows
        ]
        return ok(photos)

    except Exception as exc:
        print(f"[photos-by-group] error: {exc}")
        return error("Failed to fetch photos")
