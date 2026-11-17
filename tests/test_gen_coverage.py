import importlib.util
import pathlib

from tests.conftest import FakeGarmin

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "gen_coverage.py"


def _load():
    spec = importlib.util.spec_from_file_location("gen_coverage", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_coverage_generator_on_fake_class(monkeypatch):
    mod = _load()
    monkeypatch.setattr("garmin_everything_mcp.registry.garmin_mcp_wrapped", lambda: {"get_sleep_data"})
    monkeypatch.setattr("garmin_everything_mcp.registry.garmin_mcp_tool_names", lambda: {"get_hrv_data"})
    c = mod.compute(FakeGarmin)
    rows = {r["method"]: r for r in c["rows"]}
    assert rows["get_sleep_data"]["garmin_mcp"] and not rows["get_sleep_data"]["extras"] and rows["get_sleep_data"]["all"]
    assert rows["add_weigh_in"]["full"] and not rows["add_weigh_in"]["all"]
    assert rows["login"]["skip"]
    md = mod.render(c)
    assert "| `get_sleep_data` | sleep | read | yes |  | yes | yes |" in md
    assert "## By group" in md and "garmin_mcp covers 2" in mod.badge_line(c)


def test_committed_coverage_is_current():
    mod = _load()
    assert mod.render(mod.compute()) == mod.DOCS.read_text()
