import json

import pytest

from garmin_everything_mcp import tools_extra as T


def _details(n=1000):
    return {
        "metricDescriptors": [
            {"metricsIndex": 0, "key": "directTimestamp", "unit": {"key": "gmt"}},
            {"metricsIndex": 1, "key": "sumDuration", "unit": {"key": "s"}},
            {"metricsIndex": 2, "key": "directHeartRate", "unit": {"key": "bpm"}},
            {"metricsIndex": 3, "key": "directSpeed", "unit": {"key": "mps"}},
        ],
        "activityDetailMetrics": [{"metrics": [1_700_000_000_000 + i * 1000, float(i), 100 + i % 50, 3.123456 + i]} for i in range(n)],
    }


def test_timeseries_downsamples_to_points_and_keeps_timestamps():
    out = T.shape_timeseries(_details(1000), "directHeartRate,directSpeed,directPower", 100, 42)
    assert out["totalPoints"] == 1000 and out["returnedPoints"] == 100
    assert out["columns"] == ["directTimestamp", "sumDuration", "directHeartRate", "directSpeed"]
    assert out["series"]["sumDuration"][0] == 0.0 and out["series"]["sumDuration"][-1] == 999.0
    assert out["series"]["directSpeed"][0] == 3.123  # rounded
    assert out["missingMetrics"] == ["directPower"]
    assert out["units"]["directHeartRate"] == "bpm"
    assert len(json.dumps(out)) < 8000


def test_timeseries_all_and_small():
    out = T.shape_timeseries(_details(10), "all", 200, 1)
    assert out["returnedPoints"] == 10 and set(out["columns"]) == {"directTimestamp", "sumDuration", "directHeartRate", "directSpeed"}


def test_downsample_keeps_ends():
    seq = list(range(1000))
    out = T.downsample(seq, 10)
    assert out[0] == 0 and out[-1] == 999 and len(out) == 10
    assert T.downsample([1, 2], 10) == [1, 2]
    assert T.downsample(seq, 1) == [0]


def test_daily_range_row_shaping():
    payload = [
        {"calendarDate": "2026-01-02", "values": {"restingHR": 50, "wellnessMaxAvgHR": 120}},
        {"calendarDate": "2026-01-01", "values": {"restingHR": 52, "wellnessMaxAvgHR": 118}},
    ]
    rows = T.rows_from(payload)
    assert rows == [{"date": "2026-01-01", "restingHR": 52, "wellnessMaxAvgHR": 118},
                    {"date": "2026-01-02", "restingHR": 50, "wellnessMaxAvgHR": 120}]
    hrv = {"userProfilePk": 1, "hrvSummaries": [{"calendarDate": "2026-01-01", "weeklyAvg": 50, "baseline": {"lowUpper": 40, "balancedLow": 45}}]}
    rows = T.rows_from(hrv)
    assert rows == [{"date": "2026-01-01", "weeklyAvg": 50, "baseline.lowUpper": 40, "baseline.balancedLow": 45}]
    assert T.rows_from([{"calendarDate": "2026-01-01", "value": 83}]) == [{"date": "2026-01-01", "value": 83}]


def test_range_requests_chunking_and_spans():
    daily = T.range_requests("sleep_score", "2026-01-01", "2026-03-01")   # 60 days -> 3 chunks of 28
    assert len(daily) == 3 and daily[0][0].endswith("/2026-01-01/2026-01-28") and daily[-1][0].endswith("/2026-02-26/2026-03-01")
    monthly = T.range_requests("steps_monthly", "2026-01-15", "2026-03-02")
    assert monthly == [("/usersummary-service/stats/steps/monthly/2026-01-01/3", None)]
    weekly = T.range_requests("calories_weekly", "2026-01-01", "2026-01-20")
    assert weekly == [("/usersummary-service/stats/calories/weekly/2026-01-20/3", None)]
    weight = T.range_requests("weight", "2026-01-01", "2026-01-20")
    assert weight[0][1] == {"includeAll": "true"}
    with pytest.raises(ValueError):
        T.range_requests("nope", "2026-01-01", "2026-01-02")
    with pytest.raises(ValueError):
        T.range_requests("steps", "2026-01-02", "2026-01-01")


def test_daily_range_merges_chunks(session, fake_garmin):
    fake_garmin.connectapi = lambda path, **kw: [{"calendarDate": path.rsplit("/", 1)[-1], "value": 1}]
    T.PACING_SECONDS = 0
    out = T.daily_range(session, "sleep_score", "2026-01-01", "2026-03-01")
    assert out["calls"] == 3 and out["rows"] == 3 and out["data"][0]["date"] == "2026-01-28"


def test_sleep_detail_shaping():
    payload = {
        "dailySleepDTO": {"id": 1, "userProfilePK": 2, "calendarDate": "2026-01-01", "sleepTimeSeconds": 25000,
                          "sleepScores": {"overall": {"value": 80}}, "sleepNeed": {"actual": 480}, "napTimeSeconds": 0},
        "sleepLevels": [{"startGMT": f"2026-01-01T0{i % 10}:00", "endGMT": "x", "activityLevel": i % 3} for i in range(300)],
        "hrvData": [{"startGMT": i, "value": 40 + i} for i in range(200)],
        "avgOvernightHrv": 45.0,
        "sleepMovement": [{"startGMT": i, "endGMT": i + 1, "activityLevel": 0.5} for i in range(500)],
        "sleepRestlessMoments": [{"startGMT": 1, "value": 1}],
        "breathingDisruptionData": [{"startGMT": 1, "value": 2}],
        "avgSkinTempDeviationC": -0.3,
        "sleepBodyBattery": [{"startGMT": i, "value": i} for i in range(100)],
        "sleepHeartRate": None,
        "wellnessEpochRespirationDataDTOList": [{"startGMT": 1, "respirationValue": 14}],
    }
    out = T.shape_sleep(payload, "levels,hrv,movement,breathing,skin_temp,body_battery", 50)
    assert out["date"] == "2026-01-01"
    assert "id" not in out["summary"] and out["summary"]["sleepNeed"] == {"actual": 480}
    assert out["levels"]["sleepLevels"]["count"] == 300 and out["levels"]["sleepLevels"]["returned"] == 50
    assert out["hrv"]["avgOvernightHrv"] == 45.0 and out["hrv"]["hrvData"]["values"][0] == [0, 40]
    assert out["movement"]["sleepMovement"]["returned"] == 50
    assert out["skin_temp"] == {"avgSkinTempDeviationC": -0.3}
    assert "respiration" not in out
    assert "wellnessEpochRespirationDataDTOList" in out["sections_available"]
    everything = T.shape_sleep(payload, "all", 10, naps=[{"x": 1}])
    assert everything["respiration"]["wellnessEpochRespirationDataDTOList"]["values"] == [{"startGMT": 1, "respirationValue": 14}]
    assert everything["naps"] == [{"x": 1}]


def test_intraday_shaping():
    payload = {"calendarDate": "2026-01-01", "restingHeartRate": 50, "heartRateValues": [[i, 60 + i] for i in range(720)],
               "small": [1, 2, 3], "nested": {"a": 1}}
    out = T.shape_intraday(payload, 100)
    assert out["summary"]["restingHeartRate"] == 50 and out["summary"]["small"] == [1, 2, 3] and out["summary"]["nested"] == {"a": 1}
    assert out["series"]["heartRateValues"]["count"] == 720 and out["series"]["heartRateValues"]["returned"] == 100
    out2 = T.shape_intraday([{"date": "x", "bodyBatteryValuesArray": [[i, i] for i in range(50)]}], 10)
    assert out2["series"]["bodyBatteryValuesArray"]["returned"] == 10


def test_export_writes_jsonl(session, fake_garmin, settings, tmp_path):
    manifest = T.export(session, settings, "2026-01-01", "2026-01-03", str(tmp_path), "sleep", pacing=0)
    f = manifest["files"][0]
    assert f["days_written"] == 3 and f["error_count"] == 0 and manifest["calls"] == 3
    lines = (tmp_path / "sleep.jsonl").read_text().splitlines()
    assert len(lines) == 3
    rec = json.loads(lines[0])
    assert rec["date"] == "2026-01-01" and rec["data"]["fullName"] == "[redacted]"
    with pytest.raises(ValueError):
        T.export(session, settings, "2026-01-01", "2026-01-03", str(tmp_path), "bogus")


def test_catalog_loads_and_searches():
    entries = T.load_catalog()
    assert any(e["verified"] and "sleep/score" in e["path"] for e in entries)
    assert any(not e["verified"] and e.get("method") == "get_naps" for e in entries)
