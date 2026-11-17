import json

import pytest

from garmin_everything_mcp import pathsel
from garmin_everything_mcp.results import budget, error_payload, redact, summarize


def test_redact_masks_personal_keys_recursively():
    data = {
        "email": "a@b.c", "userName": "a@b.c", "firstName": "A", "lastName": "B", "fullName": "A B",
        "displayName": "ab", "birthDate": "1990-01-01", "address": {"x": 1}, "phoneNumber": "1",
        "ownerFullName": "A B", "emailAddress": "z",
        "steps": 100, "nested": [{"displayName": "x", "value": 2}], "empty": None,
    }
    out = redact(data)
    for k in ("email", "userName", "firstName", "lastName", "fullName", "displayName", "birthDate",
              "address", "phoneNumber", "ownerFullName", "emailAddress"):
        assert out[k] == "[redacted]", k
    assert out["steps"] == 100 and out["nested"][0]["value"] == 2 and out["nested"][0]["displayName"] == "[redacted]"
    assert out["empty"] is None
    assert data["email"] == "a@b.c"  # input untouched
    assert redact({"activityName": "Run"})["activityName"] == "Run"


def test_budget_passes_small_and_summarises_large():
    small = {"a": [1, 2, 3]}
    assert json.loads(budget(small, 1000)) == small
    big = {"rows": [{"i": i, "label": "x" * 30} for i in range(2000)], "meta": {"k": "v"}}
    out = json.loads(budget(big, 5000))
    assert out["truncated"] is True and out["original_bytes"] > 5000
    assert out["summary"]["rows"]["len"] == 2000 and out["summary"]["rows"]["first"][0]["i"] == 0
    assert out["summary"]["meta"] == {"k": "v"}
    assert "garmin_raw_get" in out["hint"]
    assert len(budget(big, 5000)) <= 5000


def test_summarize_shapes():
    s = summarize({"a": list(range(10)), "b": {"c": "d" * 200}})
    assert s["a"]["_type"] == "list" and s["a"]["len"] == 10
    assert s["b"]["c"].endswith("...")


def test_error_payload_hints():
    e = error_payload(Exception("HTTP 404 Not Found"))
    assert e["error"].startswith("HTTP 404") and "404" in e["hint"]
    assert "garmin-mcp-auth" in error_payload(Exception("401 Unauthorized"))["hint"]
    assert "stalled" in error_payload(TimeoutError("x"))["hint"]


DOC = {"a": {"b": [{"c": 1, "d": "x"}, {"c": 2, "d": "y"}, {"c": 3}]}, "weird key": {"z": 9}, "list": [10, 20, 30]}


@pytest.mark.parametrize("path,expected", [
    ("a.b[0].c", 1),
    (".a.b[-1].c", 3),
    ("a.b[*].c", [1, 2, 3]),
    ("a.b[0:2]", [{"c": 1, "d": "x"}, {"c": 2, "d": "y"}]),
    ('"weird key".z', 9),
    ("list[1]", 20),
    ("", DOC),
    (None, DOC),
])
def test_jq_path_resolver(path, expected):
    assert pathsel.resolve(DOC, path) == expected


def test_jq_path_errors():
    with pytest.raises(pathsel.PathError):
        pathsel.resolve(DOC, "a.missing")
    with pytest.raises(pathsel.PathError):
        pathsel.resolve(DOC, "list[99]")
    with pytest.raises(pathsel.PathError):
        pathsel.resolve(DOC, "a[*]")


def test_pick_fields():
    assert pathsel.pick_fields({"a": 1, "b": 2, "c": 3}, "a, c") == {"a": 1, "c": 3}
    assert pathsel.pick_fields([{"a": 1, "b": 2}], ["b"]) == [{"b": 2}]
    assert pathsel.pick_fields({"a": 1}, None) == {"a": 1}
