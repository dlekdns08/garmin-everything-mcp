import inspect
import typing

from garmin_everything_mcp import registry as R


def test_introspect_finds_public_methods(fake_garmin):
    infos = R.introspect(type(fake_garmin))
    assert "get_sleep_data" in infos and "login" in infos
    assert infos["login"].skip_reason
    assert infos["today"].skip_reason
    assert infos["get_report_blob"].skip_reason and "bytes" in infos["get_report_blob"].skip_reason
    assert infos["download_activity"].skip_reason is None  # override exists


def test_kind_and_group_classification():
    assert R.classify_kind("add_weigh_in") == "write"
    assert R.classify_kind("get_sleep_data") == "read"
    assert R.classify_group("get_sleep_data") == "sleep"
    assert R.classify_group("get_hrv_data_range") == "hrv"
    assert R.classify_group("get_training_load_activities") == "training"
    assert R.classify_group("get_gear_activities") == "activities"
    assert R.classify_group("get_badges_for_activity") == "badges"
    assert R.classify_group("get_device_displayed_activities") == "devices"
    assert R.classify_group("get_stats") == "other"


def test_description_uses_first_paragraph_and_signature(fake_garmin):
    infos = R.introspect(type(fake_garmin))
    desc = infos["get_sleep_data"]
    assert desc.doc == "Return sleep data for a date."
    text = R.describe(desc)
    assert text.startswith("[read-only] Return sleep data for a date.")
    assert "Signature: get_sleep_data(cdate: str)" in text
    assert "YYYY-MM-DD" in text
    assert "[WRITE" in R.describe(infos["add_weigh_in"])


def test_param_mapping(fake_garmin):
    specs = {s.name: s for s in R.build_specs(type(fake_garmin))}
    p = {q.name: q for q in specs["get_activities"].params}
    assert p["start"].annotation is int and p["start"].default == 0
    assert p["activitytype"].annotation == typing.Optional[str] and p["activitytype"].default is None
    # leading underscore renamed, mapped back on call
    rp = {q.name: q for q in specs["get_race_predictions"].params}
    assert "type" in rp and rp["type"].py_name == "_type"
    # str | date | None -> Optional[str]; keyword-only preserved
    lt = {q.name: q for q in specs["get_lactate_threshold"].params}
    assert lt["start_date"].annotation == typing.Optional[str]
    assert lt["latest"].kind is inspect.Parameter.KEYWORD_ONLY
    # **filters -> optional dict that is splatted
    sa = {q.name: q for q in specs["search_activities"].params}
    assert sa["filters"].splat and sa["filters"].default is None
    # int | float -> float
    aw = {q.name: q for q in specs["add_weigh_in"].params}
    assert aw["weight"].annotation is float
    # enum param of override -> str default
    da = {q.name: q for q in specs["download_activity"].params}
    assert da["fmt"].annotation is str and da["fmt"].default == "TCX"
    assert "garmin" not in da


def test_bind_call_args_maps_names_and_splats(fake_garmin):
    specs = {s.name: s for s in R.build_specs(type(fake_garmin))}
    args, kw = R.bind_call_args(specs["get_race_predictions"], {"startdate": "2026-01-01", "type": "5K"})
    assert args == [] and kw == {"startdate": "2026-01-01", "_type": "5K"}
    args, kw = R.bind_call_args(specs["search_activities"], {"start": 1, "filters": {"activityType": "running"}})
    assert kw == {"start": 1, "activityType": "running"}


def test_make_tool_function_has_signature(fake_garmin):
    spec = {s.name: s for s in R.build_specs(type(fake_garmin))}["get_activities"]
    fn = R.make_tool_function(spec, lambda s, kw: str(kw))
    sig = inspect.signature(fn)
    assert list(sig.parameters) == ["start", "limit", "activitytype"]
    assert fn.__name__ == "get_activities"
    assert fn(start=1, limit=2) == "{'start': 1, 'limit': 2}"


def test_profiles_and_write_gating(fake_garmin, wrapped, foreign):
    specs = R.build_specs(type(fake_garmin))
    names = lambda sel: {s.name for s in sel.selected}  # noqa: E731
    extras = R.select_tools(specs, "extras", wrapped=wrapped, foreign_tool_names=foreign)
    assert "get_sleep_data" not in names(extras) and "get_hrv_data" not in names(extras)
    assert "get_race_predictions" in names(extras)
    assert "add_weigh_in" not in names(extras)
    assert extras.excluded["get_sleep_data"] == "already provided by garmin_mcp"
    all_ = R.select_tools(specs, "all", wrapped=wrapped, foreign_tool_names=foreign)
    assert "get_sleep_data" in names(all_) and "add_weigh_in" not in names(all_)
    full_off = R.select_tools(specs, "full", wrapped=wrapped, foreign_tool_names=foreign)
    assert "add_weigh_in" not in names(full_off)
    assert "GARMIN_EVERYTHING_WRITES" in full_off.excluded["add_weigh_in"]
    full_on = R.select_tools(specs, "full", writes_enabled=True, wrapped=wrapped, foreign_tool_names=foreign)
    assert {"add_weigh_in", "set_steps_goal", "delete_activity"} <= names(full_on)


def test_enable_disable_group_filters(fake_garmin, wrapped, foreign):
    specs = R.build_specs(type(fake_garmin))
    sel = R.select_tools(specs, "all", wrapped=wrapped, foreign_tool_names=foreign, enabled={"GET_SLEEP_DATA"})
    assert [s.name for s in sel.selected] == ["get_sleep_data"]
    sel = R.select_tools(specs, "all", wrapped=wrapped, foreign_tool_names=foreign, disabled={"get_sleep_data"})
    assert "get_sleep_data" not in {s.name for s in sel.selected}
    sel = R.select_tools(specs, "all", wrapped=wrapped, foreign_tool_names=foreign, groups={"sleep", "hrv"})
    assert {s.name for s in sel.selected} == {"get_sleep_data", "get_hrv_data"}


def test_bundled_lists_load():
    wrapped = R.garmin_mcp_wrapped()
    assert len(wrapped) == 105 and "get_sleep_data" in wrapped
    assert "get_heart_rate_zones" in R.garmin_mcp_tool_names()
