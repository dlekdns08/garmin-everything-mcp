import json

import pytest

from garmin_everything_mcp.client import GarminSession
from garmin_everything_mcp.config import Settings
from garmin_everything_mcp.server import build_app
from tests.conftest import FakeGarmin


async def call(app, name, **args):
    res = await app.call_tool(name, args)
    return json.loads(res[0].text)


@pytest.fixture
def built(fake_garmin, session, wrapped, foreign, monkeypatch):
    monkeypatch.setattr("garmin_everything_mcp.registry.garmin_mcp_wrapped", lambda: wrapped)
    monkeypatch.setattr("garmin_everything_mcp.registry.garmin_mcp_tool_names", lambda: foreign)
    def make(**kw):
        settings = Settings(**({"profile": "all"} | kw))
        return build_app(settings, session, garmin_cls=FakeGarmin)
    return make


async def test_tool_counts_per_profile(built):
    app, state = built(profile="extras")
    names = {t.name for t in await app.list_tools()}
    assert "get_sleep_data" not in names and "get_race_predictions" in names and "garmin_coverage" in names
    app, state = built(profile="all")
    assert "get_sleep_data" in {t.name for t in await app.list_tools()}
    app, state = built(profile="full", writes_enabled=True)
    names = {t.name for t in await app.list_tools()}
    assert "add_weigh_in" in names
    tool = next(t for t in await app.list_tools() if t.name == "add_weigh_in")
    assert tool.annotations.readOnlyHint is False and tool.annotations.destructiveHint is True
    ro = next(t for t in await app.list_tools() if t.name == "get_sleep_data")
    assert ro.annotations.readOnlyHint is True


async def test_auto_tool_call_redacts_and_returns_json(built, fake_garmin):
    app, _ = built()
    out = await call(app, "get_sleep_data", cdate="2026-01-01")
    assert out["dailySleepDTO"]["sleepTimeSeconds"] == 25000 and out["fullName"] == "[redacted]"
    out = await call(app, "get_unit_system")
    assert out == {"result": "metric"}
    out = await call(app, "get_full_name")
    assert out == {"fullName": "[redacted]"}
    out = await call(app, "get_race_predictions", type="5K")
    assert out["_type"] == "5K" and fake_garmin.calls[-1] == ("get_race_predictions", (), {"startdate": None, "_type": "5K"})
    out = await call(app, "search_activities", filters={"activityType": "running"})
    assert out == [{"activityType": "running"}]


async def test_redaction_can_be_disabled(built):
    app, _ = built(redact=False)
    out = await call(app, "get_full_name")
    assert out == {"fullName": "Jane Doe"}


async def test_size_budget_truncates(built):
    app, _ = built(max_bytes=4000)
    out = await call(app, "get_big", n=3000)
    assert out["truncated"] is True and out["summary"]["_type"] == "list" and out["summary"]["len"] == 3000


async def test_errors_never_crash(built, fake_garmin):
    app, _ = built()
    fake_garmin.get_sleep_data = lambda cdate: (_ for _ in ()).throw(RuntimeError("HTTP 404 Not Found"))
    out = await call(app, "get_sleep_data", cdate="2026-01-01")
    assert out["error"] == "HTTP 404 Not Found" and "404" in out["hint"] and out["tool"] == "get_sleep_data"


async def test_login_failure_is_reported_not_raised(wrapped, foreign, monkeypatch):
    monkeypatch.setattr("garmin_everything_mcp.registry.garmin_mcp_wrapped", lambda: wrapped)
    monkeypatch.setattr("garmin_everything_mcp.registry.garmin_mcp_tool_names", lambda: foreign)
    def boom():
        raise FileNotFoundError("No usable Garmin tokens")
    app, _ = build_app(Settings(profile="all"), GarminSession(factory=boom, timeout=0), garmin_cls=FakeGarmin)
    out = await call(app, "get_unit_system")
    assert "No usable Garmin tokens" in out["error"]


async def test_override_download_writes_file(built, tmp_path):
    app, _ = built()
    out = await call(app, "download_activity", activity_id="1", fmt="gpx", out_dir=str(tmp_path))
    assert out["path"].endswith("activity_1.gpx") and (tmp_path / "activity_1.gpx").read_bytes() == b"<tcx/>GPX"


async def test_raw_get_with_dn_fields_and_jq(built, fake_garmin):
    app, _ = built()
    out = await call(app, "garmin_raw_get", path="/x-service/{dn}/y", params={"a": "1"}, jq_path="items[*].a")
    assert out == [1, 2]
    assert fake_garmin.calls[-1] == ("connectapi", ("/x-service/dn-123/y",), {"params": {"a": "1"}})
    out = await call(app, "garmin_raw_get", path="/x-service/y", fields="path,email")
    assert out == {"path": "/x-service/y", "email": "[redacted]"}
    out = await call(app, "garmin_raw_get", path="https://evil", fields=None)
    assert "error" in out


async def test_coverage_tool(built):
    app, state = built(profile="extras")
    out = await call(app, "garmin_coverage")
    assert out["profile"] == "extras" and out["tools_registered"] == len(state.registered_auto) + 8
    assert out["per_group"]["sleep"]["library_read"] == 1
    assert out["excluded_reasons"]["already provided by garmin_mcp"] == 3


def test_settings_from_env():
    s = Settings.from_env({"GARMIN_EVERYTHING_PROFILE": "FULL", "GARMIN_EVERYTHING_WRITES": "1", "GARMIN_EVERYTHING_REDACT": "0",
                           "GARMIN_EVERYTHING_MAX_BYTES": "123", "GARMIN_ENABLED_TOOLS": "a, b", "GARMIN_EVERYTHING_GROUPS": "Sleep"})
    assert s.profile == "full" and s.writes_enabled and not s.redact and s.max_bytes == 123
    assert s.enabled_tools == {"a", "b"} and s.groups == {"sleep"}
    with pytest.raises(ValueError):
        Settings.from_env({"GARMIN_EVERYTHING_PROFILE": "bogus"})
