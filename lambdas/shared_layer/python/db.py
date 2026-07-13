import os
import boto3

_client = None
DB_CLUSTER_ARN = os.environ["DB_CLUSTER_ARN"]
DB_SECRET_ARN  = os.environ["DB_SECRET_ARN"]
DB_NAME        = os.environ.get("DB_NAME", "photography")


def _get_client():
    global _client
    if _client is None:
        _client = boto3.client("rds-data")
    return _client


def _parse_field(field: dict):
    if field.get("isNull"):
        return None
    if "stringValue"  in field: return field["stringValue"]
    if "longValue"    in field: return field["longValue"]
    if "doubleValue"  in field: return field["doubleValue"]
    if "booleanValue" in field: return field["booleanValue"]
    if "blobValue"    in field: return field["blobValue"]
    return None


def query(sql: str, parameters: dict | None = None) -> list[dict]:
    """Execute a read-only statement and return rows as dicts."""
    kwargs: dict = {
        "resourceArn":           DB_CLUSTER_ARN,
        "secretArn":             DB_SECRET_ARN,
        "database":              DB_NAME,
        "sql":                   sql,
        "includeResultMetadata": True,
    }
    if parameters:
        kwargs["parameters"] = [
            {"name": k, "value": v} for k, v in parameters.items()
        ]

    response = _get_client().execute_statement(**kwargs)
    columns  = [col["name"] for col in response["columnMetadata"]]
    return [
        {col: _parse_field(field) for col, field in zip(columns, record)}
        for record in response["records"]
    ]
