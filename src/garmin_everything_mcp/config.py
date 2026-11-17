"""Settings read from the environment (all optional)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .registry import PROFILES
from .results import DEFAULT_MAX_BYTES


def _csv(value: str | None) -> set[str]:
    return {v.strip() for v in (value or "").split(",") if v.strip()}


def _flag(value: str | None, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    profile: str = "extras"
    writes_enabled: bool = False
    redact: bool = True
    max_bytes: int = DEFAULT_MAX_BYTES
    enabled_tools: set[str] = field(default_factory=set)
    disabled_tools: set[str] = field(default_factory=set)
    groups: set[str] = field(default_factory=set)
    transport: str = "stdio"
    host: str = "127.0.0.1"
    port: int = 8001
    export_dir: str = "~/.cache/garmin-everything-mcp/export"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        profile = (env.get("GARMIN_EVERYTHING_PROFILE") or "extras").strip().lower()
        if profile not in PROFILES:
            raise ValueError(f"GARMIN_EVERYTHING_PROFILE={profile!r}; expected one of {', '.join(PROFILES)}")
        try:
            max_bytes = int(env.get("GARMIN_EVERYTHING_MAX_BYTES") or DEFAULT_MAX_BYTES)
        except ValueError:
            max_bytes = DEFAULT_MAX_BYTES
        return cls(
            profile=profile,
            writes_enabled=_flag(env.get("GARMIN_EVERYTHING_WRITES"), False),
            redact=_flag(env.get("GARMIN_EVERYTHING_REDACT"), True),
            max_bytes=max_bytes,
            enabled_tools=_csv(env.get("GARMIN_ENABLED_TOOLS")),
            disabled_tools=_csv(env.get("GARMIN_DISABLED_TOOLS")),
            groups={g.lower() for g in _csv(env.get("GARMIN_EVERYTHING_GROUPS"))},
            transport=(env.get("GARMIN_EVERYTHING_TRANSPORT") or "stdio").strip().lower(),
            host=env.get("GARMIN_EVERYTHING_HOST") or "127.0.0.1",
            port=int(env.get("GARMIN_EVERYTHING_PORT") or 8001),
            export_dir=env.get("GARMIN_EVERYTHING_EXPORT_DIR") or "~/.cache/garmin-everything-mcp/export",
        )
