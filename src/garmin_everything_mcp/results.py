"""Result handling: JSON serialisation, redaction, size budget, error payloads."""

from __future__ import annotations

import json
import re
from typing import Any

DEFAULT_MAX_BYTES = 60_000

# Keys whose values are masked when redaction is on (case-insensitive, full match).
REDACT_KEY_PATTERN = re.compile(
    r"^(.*e-?mail.*|.*user_?name|.*first_?name|.*last_?name|.*full_?name|.*display_?name|"
    r".*birth_?date|.*date_?of_?birth|.*address.*|.*phone.*)$",
    re.IGNORECASE,
)
REDACTED = "[redacted]"


def redact(obj: Any) -> Any:
    """Recursively mask personal keys. Returns a new structure; input untouched."""
    if isinstance(obj, dict):
        return {
            k: (REDACTED if isinstance(k, str) and REDACT_KEY_PATTERN.match(k) and v is not None else redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def to_json(obj: Any) -> str:
    return json.dumps(obj, default=str, ensure_ascii=False, separators=(",", ":"))


# --------------------------------------------------------------------------
# Size budget
# --------------------------------------------------------------------------


def _short(value: Any, limit: int = 80) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "..."
    return value


def summarize(obj: Any, depth: int = 2, max_keys: int = 40, sample: int = 2) -> Any:
    """Compact structural summary: keys, list lengths, first items."""
    if isinstance(obj, dict):
        keys = list(obj.keys())
        out: dict[str, Any] = {}
        for k in keys[:max_keys]:
            v = obj[k]
            if isinstance(v, (dict, list)):
                out[str(k)] = summarize(v, depth - 1, max_keys, sample) if depth > 0 else _shape(v)
            else:
                out[str(k)] = _short(v)
        if len(keys) > max_keys:
            out["..."] = f"{len(keys) - max_keys} more keys"
        return out
    if isinstance(obj, list):
        first = [summarize(v, depth - 1, max_keys, sample) if depth > 0 else _shape(v) for v in obj[:sample]]
        return {"_type": "list", "len": len(obj), "first": first}
    return _short(obj)


def _shape(v: Any) -> Any:
    if isinstance(v, dict):
        return {"_type": "dict", "keys": [str(k) for k in list(v.keys())[:20]]}
    if isinstance(v, list):
        return {"_type": "list", "len": len(v)}
    return _short(v)


TRUNCATION_HINT = (
    "Result exceeded the size budget and was summarised. Narrow it with "
    "garmin_raw_get(path, fields=..., jq_path=...), or use garmin_daily_range / "
    "garmin_intraday / garmin_activity_timeseries for downsampled series. Raise the "
    "budget with GARMIN_EVERYTHING_MAX_BYTES if you really want the full payload."
)


def budget(obj: Any, max_bytes: int = DEFAULT_MAX_BYTES) -> str:
    """Serialise ``obj``; if it is too large return a summary instead."""
    text = to_json(obj)
    if max_bytes <= 0 or len(text) <= max_bytes:
        return text
    summary = summarize(obj)
    out = {
        "truncated": True,
        "original_bytes": len(text),
        "max_bytes": max_bytes,
        "summary": summary,
        "hint": TRUNCATION_HINT,
    }
    text = to_json(out)
    if len(text) > max_bytes:  # still too big: shrink the summary further
        out["summary"] = summarize(obj, depth=0, max_keys=20, sample=1)
        text = to_json(out)
    return text


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


def error_hint(exc: BaseException) -> str:
    name = type(exc).__name__
    msg = str(exc)
    if "Authentication" in name or "401" in msg or "Unauthorized" in msg:
        return ("Garmin rejected the session. Re-run "
                "'uvx --from git+https://github.com/Taxuspt/garmin_mcp garmin-mcp-auth' "
                "to refresh ~/.garminconnect, then restart the server.")
    if "TooManyRequests" in name or "429" in msg:
        return "Garmin rate limit hit. Wait a few minutes before retrying."
    if "404" in msg or "Not Found" in msg:
        return ("Garmin returned 404: this endpoint/feature is not available for this account "
                "or date (no data, unsupported device, or wrong id). See garmin_endpoint_catalog.")
    if "400" in msg or "Bad Request" in msg:
        return "Garmin returned 400: check parameter formats (dates are YYYY-MM-DD, ids are numeric)."
    if "403" in msg or "Forbidden" in msg:
        return "Garmin returned 403: the account cannot access this resource."
    if isinstance(exc, TimeoutError):
        return "The request stalled. Retry; adjust GARMIN_EVERYTHING_CALL_TIMEOUT if needed."
    if "Connection" in name or "ConnectionError" in name:
        return "Garmin Connect is unreachable. Check your network and retry."
    if isinstance(exc, (TypeError, ValueError, KeyError)):
        return "Argument error: check the tool's Signature line for parameter names and types."
    return "Unexpected error; retry or inspect the raw endpoint with garmin_raw_get."


def error_payload(exc: BaseException, **extra: Any) -> dict[str, Any]:
    return {"error": str(exc) or type(exc).__name__, "type": type(exc).__name__, "hint": error_hint(exc), **extra}
