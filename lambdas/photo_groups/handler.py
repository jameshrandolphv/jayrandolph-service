from db import query
from response import ok, error


def handler(event, context):
    try:
        rows = query(
            "SELECT id, name, type, film_stock, iso, camera, date_developed "
            "FROM photo_group "
            "ORDER BY date_developed DESC NULLS LAST"
        )
        groups = [
            {
                "id":     row["id"],
                "name":   row["name"],
                "photos": None,
            }
            for row in rows
        ]
        return ok(groups)
    except Exception as exc:
        print(f"[list-groups] error: {exc}")
        return error("Failed to fetch photo groups")
