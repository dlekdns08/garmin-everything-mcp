"""Shared fixtures: a fake Garmin class with typed signatures, no network."""

from __future__ import annotations

import datetime
import enum
from typing import Any

import pytest

from garmin_everything_mcp.client import GarminSession
from garmin_everything_mcp.config import Settings


class FakeFormat(enum.Enum):
    TCX = 2
    GPX = 3


class FakeGarmin:
    """Mimics the surface of garminconnect.Garmin for the registry tests."""

    ActivityDownloadFormat = FakeFormat

    def __init__(self) -> None:
        self.display_name = "dn-123"
        self.calls: list[tuple[str, tuple, dict]] = []

    # --- read methods ----------------------------------------------------
    def get_sleep_data(self, cdate: str) -> dict[str, Any]:
        """Return sleep data for a date.

        Longer description that should not appear in the tool description.
        """
        self.calls.append(("get_sleep_data", (cdate,), {}))
        return {"dailySleepDTO": {"calendarDate": cdate, "sleepTimeSeconds": 25000}, "fullName": "Jane Doe"}

    def get_hrv_data(self, cdate: str) -> dict[str, Any] | None:
        """HRV for a date."""
        return {"hrvSummary": {"weeklyAvg": 50}, "userName": "jane@example.com"}

    def get_activities(self, start: int = 0, limit: int = 20, activitytype: str | None = None) -> list[dict[str, Any]]:
        """List activities."""
        return [{"activityId": i, "ownerFullName": "Jane Doe"} for i in range(limit)]

    def get_race_predictions(self, startdate: str | None = None, _type: str | None = None) -> dict[str, Any]:
        """Race predictions."""
        self.calls.append(("get_race_predictions", (), {"startdate": startdate, "_type": _type}))
        return {"ok": True, "_type": _type}

    def get_lactate_threshold(self, *, latest: bool = True, start_date: str | datetime.date | None = None) -> dict[str, Any]:
        """Lactate threshold."""
        return {"latest": latest, "start_date": start_date}

    def search_activities(self, start: int = 0, limit: int = 20, **filters: Any) -> list[dict[str, Any]]:
        """Search."""
        self.calls.append(("search_activities", (), {"start": start, "limit": limit, **filters}))
        return [filters]

    def get_full_name(self) -> str | None:
        """Full name."""
        return "Jane Doe"

    def get_unit_system(self) -> str | None:
        """Unit system."""
        return "metric"

    def get_big(self, n: int = 5000) -> list[dict[str, Any]]:
        """A large payload."""
        return [{"i": i, "value": i * 1.5, "label": "x" * 20} for i in range(n)]

    def get_last_activity(self) -> dict[str, Any] | None:
        """Last activity."""
        return {"activityId": 42}

    def download_activity(self, activity_id: str, dl_fmt: FakeFormat = FakeFormat.TCX) -> bytes:
        """Download bytes."""
        return b"<tcx/>" + dl_fmt.name.encode()

    def download_course(self, course_id: int | str, fmt: str = "gpx") -> bytes:
        return b"gpx"

    def download_workout(self, workout_id: int | str) -> bytes:
        return b"fit"

    def download_health_snapshot(self, requested_date: str) -> bytes:
        return b"fit"

    def download_health_snapshot_session(self, cdate: str, snapshot_uuid: str) -> bytes:
        return b"fit"

    def download_report(self, report_id: str) -> bytes:
        return b"bin"

    def get_report_blob(self) -> bytes:
        """Returns bytes with no override -> skipped."""
        return b""

    # --- write methods ---------------------------------------------------
    def add_weigh_in(self, weight: int | float, unitKey: str = "kg") -> dict[str, Any]:
        """Add a weigh-in."""
        self.calls.append(("add_weigh_in", (weight,), {"unitKey": unitKey}))
        return {"added": weight}

    def set_steps_goal(self, steps: int | None, cdate: str | None = None) -> dict[str, Any]:
        """Set steps goal."""
        return {}

    def delete_activity(self, activity_id: str) -> Any:
        """Delete."""
        return None

    # --- skipped ---------------------------------------------------------
    def login(self, tokenstore: str | None = None) -> tuple[str | None, Any]:
        return None, None

    def logout(self) -> None:
        return None

    def connectapi(self, path: str, **kwargs: Any) -> Any:
        self.calls.append(("connectapi", (path,), kwargs))
        return {"path": path, "params": kwargs.get("params"), "items": [{"a": 1}, {"a": 2}], "email": "x@y.z"}

    def today(self) -> datetime.date:
        return datetime.date(2026, 1, 1)


@pytest.fixture
def fake_garmin() -> FakeGarmin:
    return FakeGarmin()


@pytest.fixture
def session(fake_garmin: FakeGarmin) -> GarminSession:
    return GarminSession(factory=lambda: fake_garmin, timeout=0)


@pytest.fixture
def settings() -> Settings:
    return Settings(profile="all", redact=True, max_bytes=60_000)


@pytest.fixture
def wrapped() -> set[str]:
    return {"get_sleep_data", "get_activities"}


@pytest.fixture
def foreign() -> set[str]:
    return {"get_hrv_data"}
