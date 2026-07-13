import json
import os

_ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "*")

_HEADERS = {
    "Content-Type":                 "application/json",
    "Access-Control-Allow-Origin":  _ALLOWED_ORIGIN,
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "GET,OPTIONS",
}


def ok(body) -> dict:
    return {"statusCode": 200, "headers": _HEADERS, "body": json.dumps(body, default=str)}


def not_found(message: str) -> dict:
    return {"statusCode": 404, "headers": _HEADERS, "body": json.dumps({"error": message})}


def error(message: str) -> dict:
    return {"statusCode": 500, "headers": _HEADERS, "body": json.dumps({"error": message})}
