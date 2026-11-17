"""Reflection-based tool registry.

Introspects ``garminconnect.Garmin`` and builds one MCP tool per public method:

* tool name   = method name
* parameters  = from the method signature (type hints, defaults)
* description = first docstring paragraph + the signature
* kind        = READ or WRITE by name prefix
* group       = a coarse topic tag by name heuristics

Everything here is pure: no network, no FastMCP. ``server.py`` turns the
resulting :class:`ToolSpec` objects into registered tools.
"""

from __future__ import annotations

import datetime as _dt
import enum
import inspect
import re
import types
import typing
from dataclasses import dataclass, field
from importlib import resources
from typing import Annotated, Any, Callable

from pydantic import Field

# --------------------------------------------------------------------------
# Classification tables
# --------------------------------------------------------------------------

WRITE_PREFIXES: tuple[str, ...] = (
    "add_", "create_", "delete_", "update_", "set_", "upload_", "schedule_",
    "push_", "send_", "import_", "reschedule_", "move_", "confirm_", "init_",
    "request_", "copy_", "convert_", "unschedule_", "remove_",
)

# Methods that are never turned into auto-generated tools. Value = reason.
SKIP: dict[str, str] = {
    "login": "session management",
    "logout": "session management",
    "resume_login": "session management (MFA)",
    "request_reload": "triggers a device sync request (write) and is wrapped by garmin_mcp",
    "connectapi": "raw escape hatch; use garmin_raw_get instead",
    "connectwebproxy": "raw escape hatch; use garmin_raw_get instead",
    "download": "raw bytes escape hatch",
    "query_garmin_graphql": "raw GraphQL POST; wrapped by garmin_mcp",
    "now": "clock helper, not an API call",
    "today": "clock helper, not an API call",
}

# Ordered (regex, group) heuristics. First match wins.
GROUP_RULES: list[tuple[str, str]] = [
    (r"sleep|naps?$|_naps", "sleep"),
    (r"hrv", "hrv"),
    (r"stress", "stress"),
    (r"body_battery", "body_battery"),
    (r"heart_rate|_rhr|resting_hr", "heart_rate"),
    (r"steps|daily_movement", "steps"),
    (r"floors", "floors"),
    (r"respiration", "respiration"),
    (r"spo2", "spo2"),
    (r"hydration", "hydration"),
    (r"weight|weigh_in|body_composition", "weight"),
    (r"nutrition|food|calorie", "nutrition"),
    (r"blood_pressure", "blood_pressure"),
    (r"glucose", "glucose"),
    (r"ecg", "ecg"),
    (r"health_snapshot", "health_snapshot"),
    (r"jetlag", "jetlag"),
    (r"menstrual|pregnancy", "menstrual"),
    (r"golf", "golf"),
    (r"badge", "badges"),
    (r"challenge", "challenges"),
    (r"report", "reports"),
    (r"personal_record|max_metrics|vo2|fitness_?age|lactate|running_economy|power_curve|"
     r"cycling_ability|critical_swim|stamina|one_rep_max|power_to_weight|"
     r"functional_threshold|cycling_ftp|hill_score|endurance_score|race_predictions|"
     r"biometric|power_zones|heat_altitude|acclimation|running_tolerance|"
     r"strength_benchmarks|fastest_splits|available_metrics|lifetime_totals", "metrics"),
    (r"device|sensors|solar", "devices"),
    (r"training|readiness", "training"),
    (r"activit", "activities"),
    (r"gear", "gear"),
    (r"course|pace_bands", "courses"),
    (r"workout", "workouts"),
    (r"calendar|upcoming_events|primary_event", "calendar"),
    (r"goal", "goals"),
    (r"profile|user_settings|personal_information|full_name|unit_system|timezone|"
     r"privacy|usage_indicators|sync_timestamp", "profile"),
]

DATE_PARAM_NAMES = {
    "cdate", "date", "start", "end", "startdate", "enddate", "fordate", "start_date",
    "end_date", "date_str", "calendar_date", "requested_date", "first_use_date",
    "period_start_date", "today_date", "today_calendar_date", "current_date",
}

ALL_GROUPS = sorted({g for _, g in GROUP_RULES} | {"other"})


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------


@dataclass
class ParamSpec:
    name: str                 # public (MCP) name
    py_name: str              # name used when calling the library
    annotation: Any           # python type used for the MCP schema
    default: Any = inspect.Parameter.empty
    kind: inspect._ParameterKind = inspect.Parameter.POSITIONAL_OR_KEYWORD
    converter: Callable[[Any], Any] | None = None
    description: str | None = None
    splat: bool = False       # True for a **kwargs stand-in (dict is splatted)

    @property
    def required(self) -> bool:
        return self.default is inspect.Parameter.empty


@dataclass
class MethodInfo:
    """Everything we know about one public library method (even skipped ones)."""

    name: str
    kind: str                     # "read" | "write"
    group: str
    signature: str
    doc: str
    returns_bytes: bool
    skip_reason: str | None = None
    params: list[ParamSpec] = field(default_factory=list)


@dataclass
class ToolSpec:
    """A method that will become an MCP tool."""

    name: str
    method: str
    kind: str
    group: str
    description: str
    params: list[ParamSpec]
    overridden: bool = False
    override_fn: Callable[..., Any] | None = None

    @property
    def read_only(self) -> bool:
        return self.kind == "read"


# --------------------------------------------------------------------------
# Classification helpers
# --------------------------------------------------------------------------


def classify_kind(name: str) -> str:
    return "write" if name.startswith(WRITE_PREFIXES) else "read"


def classify_group(name: str) -> str:
    for pattern, group in GROUP_RULES:
        if re.search(pattern, name):
            return group
    return "other"


def first_doc_paragraph(doc: str | None) -> str:
    if not doc:
        return ""
    text = inspect.cleandoc(doc)
    para = text.split("\n\n", 1)[0]
    return " ".join(line.strip() for line in para.splitlines()).strip()


def _is_bytes_return(ann: Any) -> bool:
    return ann is bytes or (isinstance(ann, str) and ann.strip() == "bytes")


# --------------------------------------------------------------------------
# Annotation mapping
# --------------------------------------------------------------------------

_NONE = type(None)


def _union_members(ann: Any) -> list[Any] | None:
    origin = typing.get_origin(ann)
    if origin is typing.Union or origin is types.UnionType:
        return list(typing.get_args(ann))
    return None


def map_annotation(ann: Any) -> tuple[Any, bool, Callable[[Any], Any] | None]:
    """Map a library annotation to (mcp_type, optional, converter).

    The MCP side only needs JSON-friendly types: str/int/float/bool, lists and
    dicts of those, and Optional wrappers. Anything exotic becomes ``str``.
    """
    if ann is inspect.Parameter.empty or ann is None:
        return str, False, None
    if isinstance(ann, str):
        # Unresolvable forward ref: be permissive.
        lowered = ann.lower()
        if "int" in lowered and "str" not in lowered:
            return int, "none" in lowered, None
        if "float" in lowered:
            return float, "none" in lowered, None
        if "bool" in lowered:
            return bool, "none" in lowered, None
        if lowered.startswith("list"):
            return list[Any], "none" in lowered, None
        if lowered.startswith("dict"):
            return dict[str, Any], "none" in lowered, None
        return str, "none" in lowered, None

    members = _union_members(ann)
    if members is not None:
        optional = _NONE in members
        rest = [m for m in members if m is not _NONE]
        if not rest:
            return str, True, None
        mapped = [map_annotation(m)[0] for m in rest]
        if str in mapped:
            return str, optional, None
        if int in mapped and float in mapped:
            return float, optional, None
        if len(mapped) == 1:
            return mapped[0], optional, None
        # e.g. dict | list -> accept anything JSON
        return Any, optional, None

    if ann is Any:
        return Any, False, None
    if ann in (str, int, float, bool):
        return ann, False, None
    if ann in (_dt.date, _dt.datetime):
        return str, False, None
    if inspect.isclass(ann) and issubclass(ann, enum.Enum):
        enum_cls = ann

        def _to_enum(value: Any, _cls=enum_cls) -> Any:
            if isinstance(value, _cls):
                return value
            if isinstance(value, str):
                if value in _cls.__members__:
                    return _cls[value]
                for member in _cls:
                    if str(member.value) == value:
                        return member
            return _cls(value)

        return str, False, _to_enum

    origin = typing.get_origin(ann)
    if origin in (list, typing.List, tuple, set):
        args = typing.get_args(ann)
        inner = map_annotation(args[0])[0] if args else Any
        if inner not in (str, int, float, bool):
            inner = Any
        return list[inner], False, None  # type: ignore[valid-type]
    if origin in (dict, typing.Dict):
        return dict[str, Any], False, None
    if inspect.isclass(ann) and issubclass(ann, dict):
        return dict[str, Any], False, None
    if inspect.isclass(ann) and issubclass(ann, (list, tuple)):
        return list[Any], False, None
    return str, False, None


def _public_param_name(name: str, taken: set[str]) -> str:
    public = name.lstrip("_") or "arg"
    if public in taken:
        public = f"{public}_"
    return public


def _resolve_hints(method: Any) -> dict[str, Any]:
    try:
        return typing.get_type_hints(method)
    except Exception:  # pragma: no cover - string annotations that fail to eval
        return {}


def build_params(method: Any) -> list[ParamSpec]:
    """Translate a bound/unbound method signature into ParamSpecs."""
    sig = inspect.signature(method)
    hints = _resolve_hints(method)
    out: list[ParamSpec] = []
    taken: set[str] = set()
    for p in sig.parameters.values():
        if p.name == "self":
            continue
        ann = hints.get(p.name, p.annotation)
        if p.kind is inspect.Parameter.VAR_POSITIONAL:
            continue  # nothing sensible to expose
        if p.kind is inspect.Parameter.VAR_KEYWORD:
            public = _public_param_name(p.name, taken)
            taken.add(public)
            out.append(ParamSpec(
                name=public, py_name=p.name, annotation=dict[str, Any] | None,
                default=None, kind=p.kind, splat=True,
                description="Extra keyword arguments passed through as-is (JSON object).",
            ))
            continue
        mcp_type, optional, converter = map_annotation(ann)
        default = p.default
        if optional and default is inspect.Parameter.empty:
            default = None
        if optional or default is None:
            mcp_type = typing.Optional[mcp_type]  # noqa: UP007 - explicit for schema
        if isinstance(default, enum.Enum):
            default = default.name
        public = _public_param_name(p.name, taken)
        taken.add(public)
        desc = None
        base_type = typing.get_args(mcp_type)[0] if typing.get_origin(mcp_type) is typing.Union else mcp_type
        if base_type is str and (p.name.lower() in DATE_PARAM_NAMES or p.name.lower().endswith("date")):
            desc = "Date as YYYY-MM-DD."
        out.append(ParamSpec(
            name=public, py_name=p.name, annotation=mcp_type, default=default,
            kind=p.kind, converter=converter, description=desc,
        ))
    return out


def _signature_text(name: str, method: Any) -> str:
    try:
        sig = str(inspect.signature(method, eval_str=True))
    except (TypeError, ValueError, NameError):
        try:
            sig = str(inspect.signature(method))
        except (TypeError, ValueError):  # pragma: no cover
            return f"{name}(...)"
    sig = sig.replace("(self, ", "(").replace("(self)", "()")
    sig = re.sub(r"typing\.", "", sig)
    sig = re.sub(r"garminconnect\.[\w.]+\.(\w+)", r"\1", sig)
    return f"{name}{sig}"


# --------------------------------------------------------------------------
# Introspection
# --------------------------------------------------------------------------


def introspect(cls: type) -> dict[str, MethodInfo]:
    """Return MethodInfo for every public method of ``cls`` (sorted by name)."""
    infos: dict[str, MethodInfo] = {}
    for name, member in inspect.getmembers(cls):
        if name.startswith("_"):
            continue
        if not (inspect.isfunction(member) or inspect.ismethod(member)):
            continue
        hints = _resolve_hints(member)
        ret = hints.get("return", inspect.signature(member).return_annotation)
        returns_bytes = _is_bytes_return(ret)
        skip = SKIP.get(name)
        if skip is None and returns_bytes and name not in OVERRIDES:
            skip = "returns bytes (use the download override if available)"
        try:
            params = build_params(member)
        except Exception as exc:  # pragma: no cover - defensive
            params = []
            skip = skip or f"unsupported signature: {exc}"
        infos[name] = MethodInfo(
            name=name,
            kind=classify_kind(name),
            group=classify_group(name),
            signature=_signature_text(name, member),
            doc=first_doc_paragraph(inspect.getdoc(member)),
            returns_bytes=returns_bytes,
            skip_reason=skip,
            params=params,
        )
    return dict(sorted(infos.items()))


def describe(info: MethodInfo) -> str:
    tag = "[read-only]" if info.kind == "read" else "[WRITE: modifies your Garmin account]"
    doc = info.doc or f"Call garminconnect.Garmin.{info.name}."
    parts = [f"{tag} {doc}", f"Signature: {info.signature}"]
    if any(p.description for p in info.params):
        parts.append("Dates are YYYY-MM-DD.")
    return "\n".join(parts)


def build_specs(cls: type, infos: dict[str, MethodInfo] | None = None) -> list[ToolSpec]:
    """Build ToolSpecs for every non-skipped method (overrides applied)."""
    infos = infos if infos is not None else introspect(cls)
    specs: list[ToolSpec] = []
    for name, info in infos.items():
        override = OVERRIDES.get(name)
        if override is not None:
            specs.append(ToolSpec(
                name=name, method=name, kind=info.kind, group=info.group,
                description=inspect.cleandoc(override.__doc__) if override.__doc__ else describe(info),
                params=build_override_params(override), overridden=True, override_fn=override,
            ))
            continue
        if info.skip_reason:
            continue
        specs.append(ToolSpec(
            name=name, method=name, kind=info.kind, group=info.group,
            description=describe(info), params=info.params,
        ))
    return specs


# --------------------------------------------------------------------------
# Callable construction
# --------------------------------------------------------------------------


def make_signature(spec: ToolSpec) -> inspect.Signature:
    params: list[inspect.Parameter] = []
    for p in spec.params:
        ann = p.annotation
        if p.description:
            ann = Annotated[ann, Field(description=p.description)]
        default = inspect.Parameter.empty if p.required else p.default
        kind = inspect.Parameter.KEYWORD_ONLY if p.kind is inspect.Parameter.VAR_KEYWORD else (
            inspect.Parameter.POSITIONAL_OR_KEYWORD if p.kind is inspect.Parameter.POSITIONAL_ONLY else p.kind
        )
        params.append(inspect.Parameter(p.name, kind, default=default, annotation=ann))
    # Required params must precede optional ones for a valid signature.
    params.sort(key=lambda q: (q.kind is inspect.Parameter.KEYWORD_ONLY, q.default is not inspect.Parameter.empty))
    return inspect.Signature(params, return_annotation=str)


def bind_call_args(spec: ToolSpec, mcp_kwargs: dict[str, Any]) -> tuple[list[Any], dict[str, Any]]:
    """Translate MCP kwargs (public names) to library call args."""
    args: list[Any] = []
    kwargs: dict[str, Any] = {}
    for p in spec.params:
        if p.name not in mcp_kwargs:
            continue
        value = mcp_kwargs[p.name]
        if p.splat:
            if value:
                kwargs.update(dict(value))
            continue
        if value is not None and p.converter is not None:
            value = p.converter(value)
        if p.kind is inspect.Parameter.POSITIONAL_ONLY:
            args.append(value)
        else:
            kwargs[p.py_name] = value
    return args, kwargs


def make_tool_function(spec: ToolSpec, invoke: Callable[[ToolSpec, dict[str, Any]], str]) -> Callable[..., str]:
    """Create a function object with a real signature for FastMCP to inspect."""

    def _tool(**kwargs: Any) -> str:
        return invoke(spec, kwargs)

    _tool.__name__ = spec.name
    _tool.__qualname__ = spec.name
    _tool.__doc__ = spec.description
    _tool.__signature__ = make_signature(spec)  # type: ignore[attr-defined]
    _tool.__annotations__ = {p.name: p.annotation for p in spec.params} | {"return": str}
    return _tool


# --------------------------------------------------------------------------
# Profiles and filtering
# --------------------------------------------------------------------------

PROFILES = ("extras", "all", "full")


def load_name_list(filename: str) -> set[str]:
    """Load one of the bundled newline-separated name lists."""
    text = resources.files("garmin_everything_mcp.data").joinpath(filename).read_text()
    return {line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")}


def garmin_mcp_wrapped() -> set[str]:
    return load_name_list("garmin_mcp_wrapped_methods.txt")


def garmin_mcp_tool_names() -> set[str]:
    return load_name_list("garmin_mcp_tool_names.txt")


@dataclass
class Selection:
    profile: str
    writes_enabled: bool
    selected: list[ToolSpec]
    excluded: dict[str, str]   # tool name -> reason


def select_tools(
    specs: list[ToolSpec],
    profile: str = "extras",
    *,
    writes_enabled: bool = False,
    wrapped: set[str] | None = None,
    foreign_tool_names: set[str] | None = None,
    enabled: set[str] | None = None,
    disabled: set[str] | None = None,
    groups: set[str] | None = None,
) -> Selection:
    if profile not in PROFILES:
        raise ValueError(f"Unknown profile {profile!r}; expected one of {PROFILES}")
    wrapped = garmin_mcp_wrapped() if wrapped is None else wrapped
    foreign = garmin_mcp_tool_names() if foreign_tool_names is None else foreign_tool_names
    enabled = {n.lower() for n in (enabled or set())}
    disabled = {n.lower() for n in (disabled or set())}
    groups = {g.lower() for g in (groups or set())}

    chosen: list[ToolSpec] = []
    excluded: dict[str, str] = {}
    for spec in specs:
        lname = spec.name.lower()
        if spec.kind == "write":
            if profile != "full":
                excluded[spec.name] = f"write method (profile={profile})"
                continue
            if not writes_enabled:
                excluded[spec.name] = "write method (GARMIN_EVERYTHING_WRITES not set)"
                continue
        if profile == "extras" and (spec.name in wrapped or spec.name in foreign):
            excluded[spec.name] = "already provided by garmin_mcp"
            continue
        if enabled and lname not in enabled:
            excluded[spec.name] = "not in GARMIN_ENABLED_TOOLS"
            continue
        if not enabled and lname in disabled:
            excluded[spec.name] = "in GARMIN_DISABLED_TOOLS"
            continue
        if groups and spec.group not in groups:
            excluded[spec.name] = "group not in GARMIN_EVERYTHING_GROUPS"
            continue
        chosen.append(spec)
    return Selection(profile=profile, writes_enabled=writes_enabled, selected=chosen, excluded=excluded)


# --------------------------------------------------------------------------
# Overrides: hand-written replacements for methods whose auto-generation is
# wrong (binary return, needs post-processing). Each override receives the
# Garmin client as its first positional argument and returns JSON-able data.
# They are registered under the *method name* so coverage stays honest.
# --------------------------------------------------------------------------


def _save_bytes(data: bytes, filename: str, out_dir: str | None) -> dict[str, Any]:
    import os
    from pathlib import Path

    base = Path(out_dir).expanduser() if out_dir else Path(
        os.environ.get("GARMIN_EVERYTHING_DOWNLOAD_DIR", "~/.cache/garmin-everything-mcp/downloads")
    ).expanduser()
    base.mkdir(parents=True, exist_ok=True)
    path = base / filename
    path.write_bytes(data)
    return {"path": str(path), "bytes": len(data)}


def download_activity(garmin: Any, activity_id: str, fmt: str = "TCX", out_dir: str | None = None) -> dict[str, Any]:
    """[read-only] Download an activity file (fmt: ORIGINAL|TCX|GPX|KML|CSV) to a local
    directory and return its path. ORIGINAL is a zip containing the FIT file.
    Default directory: ~/.cache/garmin-everything-mcp/downloads."""
    fmt_enum = garmin.ActivityDownloadFormat[fmt.upper()]
    data = garmin.download_activity(str(activity_id), dl_fmt=fmt_enum)
    ext = {"ORIGINAL": "zip", "TCX": "tcx", "GPX": "gpx", "KML": "kml", "CSV": "csv"}[fmt.upper()]
    return _save_bytes(data, f"activity_{activity_id}.{ext}", out_dir)


def download_course(garmin: Any, course_id: str, fmt: str = "gpx", out_dir: str | None = None) -> dict[str, Any]:
    """[read-only] Download a course (gpx or fit) to a local directory and return its path."""
    data = garmin.download_course(course_id, fmt=fmt)
    return _save_bytes(data, f"course_{course_id}.{fmt.lower()}", out_dir)


def download_workout(garmin: Any, workout_id: str, out_dir: str | None = None) -> dict[str, Any]:
    """[read-only] Download a workout as a FIT file to a local directory and return its path."""
    data = garmin.download_workout(workout_id)
    return _save_bytes(data, f"workout_{workout_id}.fit", out_dir)


def download_health_snapshot(garmin: Any, requested_date: str, out_dir: str | None = None) -> dict[str, Any]:
    """[read-only] Download the health snapshot FIT file for a date (YYYY-MM-DD) and return its path."""
    data = garmin.download_health_snapshot(requested_date)
    return _save_bytes(data, f"health_snapshot_{requested_date}.fit", out_dir)


def download_health_snapshot_session(garmin: Any, cdate: str, snapshot_uuid: str, out_dir: str | None = None) -> dict[str, Any]:
    """[read-only] Download one health snapshot session (date YYYY-MM-DD + uuid) as FIT and return its path."""
    data = garmin.download_health_snapshot_session(cdate, snapshot_uuid)
    return _save_bytes(data, f"health_snapshot_{cdate}_{snapshot_uuid}.fit", out_dir)


def download_report(garmin: Any, report_id: str, out_dir: str | None = None) -> dict[str, Any]:
    """[read-only] Download a generated report (see request_report / get_report_status) and return its path."""
    data = garmin.download_report(report_id)
    return _save_bytes(data, f"report_{report_id}.bin", out_dir)


def get_full_name(garmin: Any) -> dict[str, Any]:
    """[read-only] Return the account's full name. Redacted unless GARMIN_EVERYTHING_REDACT=0."""
    return {"fullName": garmin.get_full_name()}


OVERRIDES: dict[str, Callable[..., Any]] = {
    "download_activity": download_activity,
    "download_course": download_course,
    "download_workout": download_workout,
    "download_health_snapshot": download_health_snapshot,
    "download_health_snapshot_session": download_health_snapshot_session,
    "download_report": download_report,
    "get_full_name": get_full_name,
}


def build_override_params(fn: Callable[..., Any]) -> list[ParamSpec]:
    """Params for an override function, dropping the leading ``garmin`` argument."""
    return [p for p in build_params(fn) if p.py_name != "garmin"]
