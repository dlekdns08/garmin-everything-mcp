"""Hand-written high-value tools.

These are the tools LLMs actually want: compact ranges, downsampled series,
a raw endpoint escape hatch, and an offline export. All are read-only.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import time
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Callable

from .config import Settings
from .pathsel import pick_fields, resolve
from .results import budget, error_payload, redact, to_json

PACING_SECONDS = 0.3
CHUNK_DAYS = 28          # safe window for Garmin's daily stats endpoints
MAX_RANGE_DAYS = 400


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def parse_date(value: str) -> dt.date:
    return dt.date.fromisoformat(str(value).strip())


def date_range(start: str, end: str) -> list[dt.date]:
    s, e = parse_date(start), parse_date(end)
    if e < s:
        raise ValueError("end must be on or after start")
    if (e - s).days + 1 > MAX_RANGE_DAYS:
        raise ValueError(f"range too long (max {MAX_RANGE_DAYS} days)")
    return [s + dt.timedelta(days=i) for i in range((e - s).days + 1)]


def chunks(start: str, end: str, size: int = CHUNK_DAYS) -> list[tuple[str, str]]:
    days = date_range(start, end)
    out = []
    for i in range(0, len(days), size):
        out.append((days[i].isoformat(), days[min(i + size, len(days)) - 1].isoformat()))
    return out


def downsample(seq: list[Any], points: int) -> list[Any]:
    """Uniform-stride downsample keeping first and last element."""
    n = len(seq)
    if points <= 0 or n <= points:
        return list(seq)
    if points == 1:
        return [seq[0]]
    idx = sorted({round(i * (n - 1) / (points - 1)) for i in range(points)})
    return [seq[i] for i in idx]


def _round(v: Any) -> Any:
    if isinstance(v, float):
        return round(v, 3)
    return v


def flatten(d: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flatten(v, key + "."))
        else:
            out[key] = _round(v)
    return out


DATE_KEYS = ("calendarDate", "summaryDate", "date", "startDate", "calendarDateGmt", "startGMT", "week", "month")


def rows_from(payload: Any) -> list[dict[str, Any]]:
    """Turn a Garmin range payload into one compact row per period."""
    items: list[Any]
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        # Prefer the longest list-of-dicts value (hrvSummaries, dailyWeightSummaries, ...)
        candidates = [v for v in payload.values() if isinstance(v, list) and v and isinstance(v[0], dict)]
        items = max(candidates, key=len) if candidates else [payload]
    else:
        return []
    rows: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            rows.append({"value": item})
            continue
        date = next((item[k] for k in DATE_KEYS if k in item), None)
        body = item.get("values") if isinstance(item.get("values"), dict) else {
            k: v for k, v in item.items() if k not in DATE_KEYS
        }
        row: dict[str, Any] = {"date": date} if date is not None else {}
        row.update(flatten(body))
        rows.append(row)
    rows.sort(key=lambda r: str(r.get("date", "")))
    return rows


@dataclass
class ServerState:
    """What garmin_coverage reports; filled in by server.build_app."""

    settings: Settings
    library_version: str = "unknown"
    total_methods: int = 0
    read_methods: int = 0
    write_methods: int = 0
    skipped: dict[str, str] = field(default_factory=dict)
    wrapped_by_garmin_mcp: int = 0
    registered_auto: list[dict[str, str]] = field(default_factory=list)   # {name, group, kind}
    registered_extra: list[str] = field(default_factory=list)
    excluded: dict[str, str] = field(default_factory=dict)
    groups_total: dict[str, dict[str, int]] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Endpoint catalog (bundled data)
# --------------------------------------------------------------------------


def load_catalog() -> list[dict[str, Any]]:
    data = resources.files("garmin_everything_mcp.data")
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in data.joinpath("verified_endpoints.txt").read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        path, _, note = line.partition("\t")
        entries.append({"path": path.strip(), "verified": True, "source": "probed 2026-11-17", "note": note.strip()})
        seen.add(path.strip())
    try:
        generated = json.loads(data.joinpath("endpoints.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        generated = []
    if isinstance(generated, dict):
        generated = generated.get("endpoints", [])
    for e in generated:
        if e.get("path") in seen:
            continue
        seen.add(e["path"])
        entries.append({"path": e["path"], "verified": False, "source": "garminconnect source",
                        "method": e.get("method"), "http": e.get("http", "GET"), "note": e.get("note", "")})
    unavailable = []
    for line in data.joinpath("unavailable_endpoints.txt").read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            name, _, note = line.partition("\t")
            unavailable.append({"method": name.strip(), "note": note.strip()})
    for u in unavailable:
        entries.append({"path": f"(library method) {u['method']}", "verified": False,
                        "source": "probed 2026-11-17: 404/400 for the probing account", "note": u["note"]})
    return entries


# --------------------------------------------------------------------------
# Tool registration
# --------------------------------------------------------------------------


def register_tools(app: Any, session: Any, settings: Settings, state: ServerState) -> list[str]:
    """Register the hand-written tools on ``app``. Returns their names."""
    from mcp.types import ToolAnnotations

    names: list[str] = []
    ro = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)

    def finish(result: Any) -> str:
        if settings.redact:
            result = redact(result)
        return budget(result, settings.max_bytes)

    def guarded(fn: Callable[..., Any]) -> Callable[..., str]:
        def wrapper(**kwargs: Any) -> str:
            try:
                return finish(fn(**kwargs))
            except Exception as exc:  # noqa: BLE001 - tools never crash the server
                return to_json(error_payload(exc, tool=fn.__name__))
        wrapper.__name__ = fn.__name__
        wrapper.__doc__ = fn.__doc__
        import inspect
        wrapper.__signature__ = inspect.signature(fn)  # type: ignore[attr-defined]
        wrapper.__annotations__ = dict(getattr(fn, "__annotations__", {}))
        return wrapper

    def add(fn: Callable[..., Any]) -> None:
        app.add_tool(guarded(fn), name=fn.__name__, description=fn.__doc__, annotations=ro, structured_output=False)
        names.append(fn.__name__)

    # ---- garmin_raw_get --------------------------------------------------
    def garmin_raw_get(path: str, params: dict[str, Any] | None = None, fields: str | None = None,
                       jq_path: str | None = None) -> Any:
        """GET any Garmin Connect API path and return its JSON (read-only escape hatch).
        '{dn}' in the path is replaced with your display name. `fields` keeps only the listed
        top-level keys (comma list); `jq_path` picks a sub-tree with a tiny jq-like syntax
        (a.b[0].c, items[*].value, items[0:5]). See garmin_endpoint_catalog for known paths."""
        p = path.strip()
        if not p.startswith("/") or "://" in p:
            raise ValueError("path must be a Connect API path starting with '/' (e.g. /sleep-service/...)")
        if "{dn}" in p:
            p = p.replace("{dn}", session.display_name)
        kwargs: dict[str, Any] = {}
        if params:
            kwargs["params"] = params
        data = session.connectapi(p, **kwargs)
        data = resolve(data, jq_path)
        return pick_fields(data, fields)
    add(garmin_raw_get)

    # ---- garmin_endpoint_catalog ------------------------------------------
    def garmin_endpoint_catalog(search: str | None = None) -> Any:
        """List known Garmin Connect API paths for garmin_raw_get: the ones verified against a
        real account plus every path the garminconnect library source touches. Optional
        case-insensitive substring `search` (e.g. 'sleep', 'metrics-service')."""
        entries = load_catalog()
        if search:
            s = search.lower()
            entries = [e for e in entries if s in json.dumps(e).lower()]
        return {"count": len(entries), "verified": sum(1 for e in entries if e["verified"]), "endpoints": entries}
    add(garmin_endpoint_catalog)

    # ---- garmin_activity_timeseries --------------------------------------
    def garmin_activity_timeseries(activity_id: str, metrics: str = "directHeartRate,directSpeed,directPower",
                                   points: int = 200) -> Any:
        """Downsampled per-second metrics of an activity as compact columnar JSON (turns ~1 MB
        into a few KB). `metrics` is a comma list of metricDescriptor keys (directHeartRate,
        directSpeed, directPower, directRunCadence, directElevation, directLatitude,
        directLongitude, directBodyBattery, directVerticalOscillation, directGroundContactTime,
        directStrideLength, directPerformanceCondition, directGradeAdjustedSpeed, ...) or 'all'.
        Timestamps (directTimestamp ms, sumDuration s) are always included."""
        details = session.call("get_activity_details", str(activity_id))
        return shape_timeseries(details, metrics, points, activity_id)
    add(garmin_activity_timeseries)

    # ---- garmin_sleep_detail ---------------------------------------------
    def garmin_sleep_detail(date: str, include: str = "levels,hrv,movement,breathing,skin_temp,body_battery",
                            points: int = 120) -> Any:
        """Rich sleep detail for a night (date YYYY-MM-DD = wake-up day): sleep scores, sleep need,
        naps, skin-temperature deviation, breathing disruptions, HRV series, movement, restless
        moments, body battery, heart rate, stress, respiration. `include` is a comma list of
        sections: levels,hrv,movement,breathing,skin_temp,body_battery,heart_rate,stress,
        respiration,spo2,naps,all. Series are downsampled to `points`."""
        payload = session.call("get_sleep_data", date)
        naps = None
        if "naps" in include or include.strip() == "all":
            try:
                naps = session.call("get_naps", date)
            except Exception as exc:  # noqa: BLE001
                naps = {"error": str(exc)}
        return shape_sleep(payload, include, points, naps)
    add(garmin_sleep_detail)

    # ---- garmin_daily_range ----------------------------------------------
    def garmin_daily_range(metric: str, start: str, end: str) -> Any:
        """One compact row per day (or week/month) for a metric over a date range. metric is one of:
        sleep_score, sleep, body_battery, resting_hr, stress, steps, steps_monthly, floors,
        hydration, respiration, intensity_minutes, hrv, calories_weekly, vo2max_weekly, weight.
        Dates YYYY-MM-DD, max 400 days. Uses the verified range endpoints so it costs 1-15 calls
        instead of one call per day."""
        return daily_range(session, metric, start, end)
    add(garmin_daily_range)

    # ---- garmin_intraday -------------------------------------------------
    def garmin_intraday(date: str, kind: str = "heart_rate", points: int = 288) -> Any:
        """Intraday series for one day (YYYY-MM-DD), downsampled to `points`. kind: heart_rate
        (2-min bpm), steps_epochs (15-min steps + activity level), movement, respiration, stress
        (+ body battery), body_battery, spo2. Scalars in the payload are returned as `summary`."""
        return intraday(session, date, kind, points)
    add(garmin_intraday)

    # ---- garmin_export ---------------------------------------------------
    def garmin_export(start: str, end: str, out_dir: str | None = None,
                      metrics: str = "sleep,stress,heart_rate,body_battery,steps,hrv,respiration,spo2") -> Any:
        """Export raw daily payloads to local JSONL files (one file per metric, one line per day)
        with 0.3 s pacing; read-only against Garmin. metrics is a comma list of: sleep, stress,
        heart_rate, body_battery, steps, hrv, respiration, spo2, floors, hydration,
        intensity_minutes, stats, training_readiness, activities. Default out_dir:
        ~/.cache/garmin-everything-mcp/export. Returns a manifest."""
        return export(session, settings, start, end, out_dir, metrics)
    add(garmin_export)

    # ---- garmin_coverage -------------------------------------------------
    def garmin_coverage() -> Any:
        """Live coverage matrix: library version, active profile, how many tools are registered per
        group, what garmin_mcp already covers, and what was excluded and why."""
        per_group: dict[str, dict[str, int]] = {}
        for t in state.registered_auto:
            g = per_group.setdefault(t["group"], {"registered": 0})
            g["registered"] += 1
        for g, totals in state.groups_total.items():
            per_group.setdefault(g, {"registered": 0}).update(totals)
        return {
            "server": "garmin-everything-mcp",
            "garminconnect_version": state.library_version,
            "profile": state.settings.profile,
            "writes_enabled": state.settings.writes_enabled,
            "redact": state.settings.redact,
            "max_bytes": state.settings.max_bytes,
            "library_methods": state.total_methods,
            "library_read_methods": state.read_methods,
            "library_write_methods": state.write_methods,
            "wrapped_by_garmin_mcp": state.wrapped_by_garmin_mcp,
            "tools_registered": len(state.registered_auto) + len(state.registered_extra),
            "auto_generated_tools": len(state.registered_auto),
            "hand_written_tools": state.registered_extra,
            "per_group": dict(sorted(per_group.items())),
            "skipped_methods": state.skipped,
            "excluded_count": len(state.excluded),
            "excluded_reasons": _count(state.excluded.values()),
        }
    add(garmin_coverage)

    return names


def _count(values: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out


# --------------------------------------------------------------------------
# Shaping functions (pure, unit-tested)
# --------------------------------------------------------------------------

TIME_KEYS = ("directTimestamp", "sumDuration", "sumElapsedDuration")


def shape_timeseries(details: dict[str, Any], metrics: str, points: int, activity_id: Any) -> dict[str, Any]:
    descriptors = details.get("metricDescriptors") or []
    index = {d["key"]: d.get("metricsIndex") for d in descriptors if "key" in d}
    units = {d["key"]: (d.get("unit") or {}).get("key") for d in descriptors if "key" in d}
    rows = [r.get("metrics", []) for r in details.get("activityDetailMetrics") or []]
    wanted = list(index) if metrics.strip().lower() == "all" else [m.strip() for m in metrics.split(",") if m.strip()]
    missing = [m for m in wanted if m not in index]
    columns = [k for k in TIME_KEYS if k in index] + [m for m in wanted if m in index and m not in TIME_KEYS]
    sampled = downsample(rows, points)
    series: dict[str, list[Any]] = {}
    for col in columns:
        i = index[col]
        series[col] = [_round(r[i]) if i is not None and i < len(r) else None for r in sampled]
    out: dict[str, Any] = {
        "activityId": activity_id,
        "totalPoints": len(rows),
        "returnedPoints": len(sampled),
        "columns": columns,
        "units": {c: units.get(c) for c in columns},
        "series": series,
        "availableMetrics": sorted(index),
    }
    if missing:
        out["missingMetrics"] = missing
    return out


SLEEP_SECTIONS: dict[str, list[str]] = {
    "levels": ["sleepLevels"],
    "hrv": ["hrvData", "avgOvernightHrv", "hrvStatus"],
    "movement": ["sleepMovement", "sleepRestlessMoments", "restlessMomentsCount"],
    "breathing": ["breathingDisruptionData", "breathingDisruptionSeverity", "avgBreathingDisruption"],
    "skin_temp": ["avgSkinTempDeviationC", "avgSkinTempDeviationF", "skinTempDataDTOList", "skinTempDataExists"],
    "body_battery": ["sleepBodyBattery", "bodyBatteryChange"],
    "heart_rate": ["sleepHeartRate", "restingHeartRate"],
    "stress": ["sleepStress", "avgSleepStress"],
    "respiration": ["wellnessEpochRespirationDataDTOList", "wellnessEpochRespirationAveragesList",
                    "avgRespiration", "lowestRespiration", "highestRespiration"],
    "spo2": ["wellnessEpochSPO2DataDTOList", "wellnessSpO2SleepSummaryDTO"],
}
SUMMARY_DROP = {"id", "userProfilePK"}


def _series_pairs(seq: list[Any], points: int) -> list[Any]:
    out = []
    for item in downsample(seq, points):
        if isinstance(item, dict):
            ts = next((item[k] for k in ("startGMT", "startTimestampGMT", "startGmt") if item.get(k) is not None), None)
            val = item.get("value", item.get("activityLevel"))
            end = item.get("endGMT")
            if ts is not None and val is not None:
                out.append([ts, _round(val)] + ([end] if end is not None and "activityLevel" in item else []))
            else:
                out.append({k: _round(v) for k, v in item.items()})
        else:
            out.append(item)
    return out


def shape_sleep(payload: dict[str, Any], include: str, points: int, naps: Any = None) -> dict[str, Any]:
    inc = {s.strip().lower() for s in include.split(",") if s.strip()}
    if "all" in inc:
        inc = set(SLEEP_SECTIONS) | {"naps"}
    daily = payload.get("dailySleepDTO") or {}
    summary = {k: v for k, v in daily.items() if k not in SUMMARY_DROP}
    out: dict[str, Any] = {"date": daily.get("calendarDate"), "summary": summary}
    for section, keys in SLEEP_SECTIONS.items():
        if section not in inc:
            continue
        block: dict[str, Any] = {}
        for k in keys:
            if k not in payload or payload[k] is None:
                continue
            v = payload[k]
            if isinstance(v, list):
                block[k] = {"count": len(v), "returned": min(len(v), points), "values": _series_pairs(v, points)}
            else:
                block[k] = v
        out[section] = block if block else None
    if "naps" in inc:
        out["naps"] = naps if naps is not None else {"napTimeSeconds": daily.get("napTimeSeconds")}
    out["sections_available"] = sorted(k for k, v in payload.items() if v not in (None, [], {}))
    return out


RANGE_METRICS: dict[str, dict[str, Any]] = {
    "sleep_score": {"path": "/wellness-service/stats/daily/sleep/score/{start}/{end}", "span": "daily"},
    "sleep": {"path": "/sleep-service/stats/sleep/daily/{start}/{end}", "span": "daily"},
    "body_battery": {"path": "/usersummary-service/stats/bodybattery/daily/{start}/{end}", "span": "daily"},
    "resting_hr": {"path": "/usersummary-service/stats/heartRate/daily/{start}/{end}", "span": "daily"},
    "stress": {"path": "/usersummary-service/stats/stress/daily/{start}/{end}", "span": "daily"},
    "steps": {"path": "/usersummary-service/stats/steps/daily/{start}/{end}", "span": "daily"},
    "floors": {"path": "/usersummary-service/stats/floors/daily/{start}/{end}", "span": "daily"},
    "hydration": {"path": "/usersummary-service/stats/hydration/daily/{start}/{end}", "span": "daily"},
    "respiration": {"path": "/usersummary-service/stats/respiration/daily/{start}/{end}", "span": "daily"},
    "intensity_minutes": {"path": "/usersummary-service/stats/im/daily/{start}/{end}", "span": "daily"},
    "hrv": {"path": "/hrv-service/hrv/daily/{start}/{end}", "span": "daily"},
    "steps_monthly": {"path": "/usersummary-service/stats/steps/monthly/{month_start}/{months}", "span": "monthly"},
    "calories_weekly": {"path": "/usersummary-service/stats/calories/weekly/{end}/{weeks}", "span": "weekly"},
    "vo2max_weekly": {"path": "/metrics-service/metrics/maxmet/weekly/{start}/{end}", "span": "weekly"},
    "weight": {"path": "/weight-service/weight/range/{start}/{end}", "span": "range", "params": {"includeAll": "true"}},
}


def range_requests(metric: str, start: str, end: str) -> list[tuple[str, dict[str, Any] | None]]:
    """Build the (path, params) calls needed for a metric over a range."""
    if metric not in RANGE_METRICS:
        raise ValueError(f"unknown metric {metric!r}; choose one of {', '.join(RANGE_METRICS)}")
    spec = RANGE_METRICS[metric]
    s, e = parse_date(start), parse_date(end)
    days = (e - s).days + 1
    if days < 1:
        raise ValueError("end must be on or after start")
    if days > MAX_RANGE_DAYS:
        raise ValueError(f"range too long (max {MAX_RANGE_DAYS} days)")
    params = spec.get("params")
    if spec["span"] == "daily":
        return [(spec["path"].format(start=a, end=b), params) for a, b in chunks(start, end)]
    if spec["span"] == "monthly":
        months = (e.year - s.year) * 12 + (e.month - s.month) + 1
        return [(spec["path"].format(month_start=s.replace(day=1).isoformat(), months=months), params)]
    if spec["span"] == "weekly" and "{weeks}" in spec["path"]:
        return [(spec["path"].format(end=e.isoformat(), weeks=max(1, math.ceil(days / 7))), params)]
    return [(spec["path"].format(start=s.isoformat(), end=e.isoformat()), params)]


def daily_range(session: Any, metric: str, start: str, end: str) -> dict[str, Any]:
    calls = range_requests(metric, start, end)
    rows: dict[str, dict[str, Any]] = {}
    unkeyed: list[dict[str, Any]] = []
    for i, (path, params) in enumerate(calls):
        if i:
            time.sleep(PACING_SECONDS)
        payload = session.connectapi(path, **({"params": params} if params else {}))
        for row in rows_from(payload):
            key = str(row.get("date"))
            if row.get("date") is None:
                unkeyed.append(row)
            else:
                rows[key] = row
    ordered = [rows[k] for k in sorted(rows)] + unkeyed
    return {"metric": metric, "start": start, "end": end, "calls": len(calls), "rows": len(ordered), "data": ordered}


INTRADAY_KINDS: dict[str, tuple[str, Callable[[str], tuple[tuple[Any, ...], dict[str, Any]]]]] = {
    "heart_rate": ("get_heart_rates", lambda d: ((d,), {})),
    "steps_epochs": ("get_steps_data", lambda d: ((d,), {})),
    "movement": ("get_daily_movement", lambda d: ((d,), {})),
    "respiration": ("get_respiration_data", lambda d: ((d,), {})),
    "stress": ("get_stress_data", lambda d: ((d,), {})),
    "body_battery": ("get_body_battery", lambda d: ((d, d), {})),
    "spo2": ("get_spo2_data", lambda d: ((d,), {})),
}


def shape_intraday(payload: Any, points: int, series_min: int = 8) -> dict[str, Any]:
    """Split a payload into scalar `summary` and downsampled `series`."""
    if isinstance(payload, list) and payload and isinstance(payload[0], dict) and len(payload) <= 3:
        payload = payload[0]            # e.g. get_body_battery returns [day]
    summary: dict[str, Any] = {}
    series: dict[str, Any] = {}
    if isinstance(payload, list):
        series["values"] = {"count": len(payload), "returned": min(len(payload), points), "values": downsample(payload, points)}
        return {"summary": summary, "series": series}
    for k, v in (payload or {}).items():
        if isinstance(v, list) and len(v) >= series_min and v and isinstance(v[0], (list, dict)):
            sampled = downsample(v, points)
            series[k] = {"count": len(v), "returned": len(sampled),
                         "values": [[_round(x) for x in s] if isinstance(s, list) else s for s in sampled]}
        elif isinstance(v, dict):
            summary[k] = flatten(v)
        else:
            summary[k] = _round(v)
    return {"summary": summary, "series": series}


def intraday(session: Any, date: str, kind: str, points: int) -> dict[str, Any]:
    if kind not in INTRADAY_KINDS:
        raise ValueError(f"unknown kind {kind!r}; choose one of {', '.join(INTRADAY_KINDS)}")
    method, argf = INTRADAY_KINDS[kind]
    args, kwargs = argf(date)
    payload = session.call(method, *args, **kwargs)
    out = {"date": date, "kind": kind, "method": method}
    out.update(shape_intraday(payload, points))
    return out


EXPORT_METRICS: dict[str, tuple[str, Callable[[str], tuple[Any, ...]]]] = {
    "sleep": ("get_sleep_data", lambda d: (d,)),
    "stress": ("get_stress_data", lambda d: (d,)),
    "heart_rate": ("get_heart_rates", lambda d: (d,)),
    "body_battery": ("get_body_battery", lambda d: (d, d)),
    "steps": ("get_steps_data", lambda d: (d,)),
    "hrv": ("get_hrv_data", lambda d: (d,)),
    "respiration": ("get_respiration_data", lambda d: (d,)),
    "spo2": ("get_spo2_data", lambda d: (d,)),
    "floors": ("get_floors", lambda d: (d,)),
    "hydration": ("get_hydration_data", lambda d: (d,)),
    "intensity_minutes": ("get_intensity_minutes_data", lambda d: (d,)),
    "stats": ("get_stats", lambda d: (d,)),
    "training_readiness": ("get_training_readiness", lambda d: (d,)),
    "activities": ("get_activities_fordate", lambda d: (d,)),
}


def export(session: Any, settings: Settings, start: str, end: str, out_dir: str | None, metrics: str,
           pacing: float = PACING_SECONDS) -> dict[str, Any]:
    wanted = [m.strip() for m in metrics.split(",") if m.strip()]
    unknown = [m for m in wanted if m not in EXPORT_METRICS]
    if unknown:
        raise ValueError(f"unknown metrics {unknown}; choose from {', '.join(EXPORT_METRICS)}")
    days = date_range(start, end)
    base = Path(out_dir or settings.export_dir).expanduser()
    base.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {"out_dir": str(base), "start": start, "end": end, "days": len(days), "files": []}
    t0 = time.time()
    calls = 0
    for metric in wanted:
        method, argf = EXPORT_METRICS[metric]
        path = base / f"{metric}.jsonl"
        errors: list[dict[str, str]] = []
        written = 0
        with path.open("w", encoding="utf-8") as fh:
            for day in days:
                d = day.isoformat()
                if calls:
                    time.sleep(pacing)
                calls += 1
                try:
                    data = session.call(method, *argf(d))
                except Exception as exc:  # noqa: BLE001
                    errors.append({"date": d, "error": str(exc)})
                    continue
                if settings.redact:
                    data = redact(data)
                fh.write(json.dumps({"date": d, "metric": metric, "method": method, "data": data}, default=str) + "\n")
                written += 1
        manifest["files"].append({"metric": metric, "method": method, "path": str(path), "days_written": written,
                                  "bytes": path.stat().st_size, "errors": errors[:10], "error_count": len(errors)})
    manifest["calls"] = calls
    manifest["duration_s"] = round(time.time() - t0, 1)
    return manifest
