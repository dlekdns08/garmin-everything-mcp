<h1 align="center">garmin-everything-mcp</h1>

<p align="center"><strong>The other 70% of your Garmin.</strong></p>

<p align="center">
An MCP server that exposes <em>everything</em> in Garmin Connect that
<a href="https://github.com/Taxuspt/garmin_mcp">garmin_mcp</a> does not.
Run it <strong>side by side</strong> with garmin_mcp: it adds hundreds of tools without duplicating a single name.
</p>

<p align="center">
<code>garminconnect 0.3.20: 348 methods | garmin_mcp covers 109 | garmin-everything-mcp adds 178 (extras) / 265 (all) / 338 (full) auto-generated tools + 8 hand-written</code>
</p>

---

[garmin_mcp](https://github.com/Taxuspt/garmin_mcp) is excellent, but it hand-wraps **105** calls of the
[python-garminconnect](https://github.com/cyberjunky/python-garminconnect) library, pinned at 0.3.2.
The library is now at **0.3.20 with 348 public methods**: sleep need and skin temperature, HRV baselines,
training load balance, ECG, glucose, health snapshots, jet lag, race predictions, power curves, one-rep max,
gear, courses, calendars, reports, golf, menstrual health, and dozens of daily/weekly/monthly *range* endpoints.

This server does not hand-wrap anything. It **introspects `garminconnect.Garmin` at startup** and turns every
public method into an MCP tool: name, typed parameters, defaults and docstring come straight from the library.
When python-garminconnect gains a method, this server gains a tool. A shipped list of what garmin_mcp already
provides keeps the two servers disjoint, and eight hand-written tools do what reflection cannot: compact ranges,
downsampled time series, a raw endpoint escape hatch, and an offline export.

## Highlights

- **Reflection-generated tools**: 178 read-only tools in the default `extras` profile, 265 in `all`, 338 with writes.
  See [docs/COVERAGE.md](docs/COVERAGE.md) for the full 348-row matrix.
- **Zero duplicates with garmin_mcp**: the `extras` profile excludes every method and tool name garmin_mcp registers.
- **8 hand-written power tools**: `garmin_daily_range`, `garmin_activity_timeseries`, `garmin_sleep_detail`,
  `garmin_intraday`, `garmin_raw_get`, `garmin_endpoint_catalog`, `garmin_export`, `garmin_coverage`.
- **Context-safe by default**: a 60 KB size budget summarises huge payloads instead of flooding the model;
  a 1 MB activity detail becomes a 4 KB columnar series.
- **Privacy by default**: email, names, birth date, address and phone fields are redacted recursively.
- **Write-safe by default**: every `add_/create_/delete_/update_/set_/upload_/...` method stays hidden unless you
  opt in twice (`profile=full` **and** `GARMIN_EVERYTHING_WRITES=1`). Tools carry MCP `readOnlyHint` annotations.
- **Same auth as garmin_mcp**: authenticate once with `garmin-mcp-auth`; both servers read `~/.garminconnect`.

## Quick start

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
# 1. Authenticate once (this is garmin_mcp's auth tool; tokens land in ~/.garminconnect)
uvx --python 3.12 --from git+https://github.com/Taxuspt/garmin_mcp garmin-mcp-auth

# 2. Run the server (stdio)
uvx --python 3.12 --from git+https://github.com/dlekdns08/garmin-everything-mcp garmin-everything-mcp
```

The server never prompts for credentials and never stores anything but what the library already keeps.

## The composition recipe: run both servers

Claude Desktop (`~/Library/Application Support/Claude/claude_desktop_config.json`) or any MCP client:

```json
{
  "mcpServers": {
    "garmin": {
      "command": "uvx",
      "args": ["--python", "3.12", "--from", "git+https://github.com/Taxuspt/garmin_mcp", "garmin-mcp"]
    },
    "garmin-everything": {
      "command": "uvx",
      "args": ["--python", "3.12", "--from", "git+https://github.com/dlekdns08/garmin-everything-mcp", "garmin-everything-mcp"],
      "env": {
        "GARMIN_EVERYTHING_PROFILE": "extras"
      }
    }
  }
}
```

Claude Code:

```bash
claude mcp add garmin -- uvx --python 3.12 --from git+https://github.com/Taxuspt/garmin_mcp garmin-mcp
claude mcp add garmin-everything -e GARMIN_EVERYTHING_PROFILE=extras -- \
  uvx --python 3.12 --from git+https://github.com/dlekdns08/garmin-everything-mcp garmin-everything-mcp
```

`extras` is the default, so the `env` block is optional. With it, no tool name exists in both servers.
Prefer a single server? Use `GARMIN_EVERYTHING_PROFILE=all` and drop garmin_mcp (you lose its workout builders
and FIT parsing, which are not library methods).

## The hand-written tools

| Tool | What it does |
|---|---|
| `garmin_daily_range(metric, start, end)` | One compact row per day for `sleep_score, sleep, body_battery, resting_hr, stress, steps, steps_monthly, floors, hydration, respiration, intensity_minutes, hrv, calories_weekly, vo2max_weekly, weight`. Uses the verified range endpoints, chunked in 28-day windows: 60 days of resting HR is 3 calls and 5 KB, not 60 calls. |
| `garmin_activity_timeseries(activity_id, metrics, points)` | Maps `metricDescriptors` to columns and downsamples `activityDetailMetrics` to N points (uniform stride, timestamps kept). 1,188 samples / 990 KB becomes 50 points / 3.7 KB. `metrics="all"` for every descriptor. |
| `garmin_sleep_detail(date, include, points)` | The sleep sub-objects garmin_mcp drops: sleep need and next-night need, naps, skin-temperature deviation, breathing disruptions, HRV series + status, movement, restless moments, body battery, heart rate, stress, respiration. |
| `garmin_intraday(date, kind, points)` | Downsampled intraday series: `heart_rate` (2-min bpm), `steps_epochs` (15-min), `movement` (1-min), `respiration`, `stress`, `body_battery`, `spo2`. Scalars come back as `summary`. |
| `garmin_raw_get(path, params, fields, jq_path)` | GET any Connect API path. `{dn}` becomes your display name. `fields` keeps top-level keys, `jq_path` picks a sub-tree (`a.b[0].c`, `items[*].value`, `items[0:5]`), no jq binary needed. |
| `garmin_endpoint_catalog(search)` | 54 paths verified against a real account plus ~270 path templates extracted from the library source by `scripts/gen_endpoints.py`, with notes on which were verified and which returned 404 for the probing account. |
| `garmin_export(start, end, out_dir, metrics)` | Writes one JSONL file per metric (one line per day) to `~/.cache/garmin-everything-mcp/export` with 0.3 s pacing. Read-only against Garmin; redaction applies. Returns a manifest. |
| `garmin_coverage()` | Live matrix: library version, profile, tools per group, what garmin_mcp covers, what was excluded and why. |

Examples (as the model would call them):

```text
garmin_daily_range(metric="sleep_score", start="2026-11-11", end="2026-11-17")
-> {"metric":"sleep_score","calls":1,"rows":7,"data":[{"date":"2026-11-11","value":65}, ...]}

garmin_activity_timeseries(activity_id="12345678901", metrics="directHeartRate,directPower", points=100)
-> {"totalPoints":1188,"returnedPoints":100,"columns":["directTimestamp","sumDuration","directHeartRate","directPower"],
    "units":{"directHeartRate":"bpm","directPower":"watt"},"series":{"directHeartRate":[127,135,...],...}}

garmin_sleep_detail(date="2026-11-16", include="hrv,skin_temp,breathing")
-> {"summary":{"sleepTimeSeconds":24462,"sleepNeed":{"actual":500,"feedback":"INCREASED",...},"sleepScores":{...}},
    "hrv":{"hrvData":{"count":86,"returned":120,"values":[[1794746901000,32.0],...]},"avgOvernightHrv":48.0,"hrvStatus":"UNBALANCED"},
    "skin_temp":{"avgSkinTempDeviationC":0.3},"breathing":{"breathingDisruptionData":{...}}}

garmin_raw_get(path="/wellness-service/stats/daily/sleep/score/2026-11-11/2026-11-17", jq_path="[*].value")
-> [65,54,48,69,86,73,76]
```

Everything else is auto-generated. A few of the 178 extras: `get_sleep_daily`, `get_sleep_stats_weekly`,
`get_hrv_data_range`, `get_daily_body_battery_stats`, `get_training_four_week_load_balance`,
`get_training_readiness_range`, `get_heart_rate_zones_v2`, `get_power_zones`, `get_sensors`, `get_ecg_list`,
`get_glucose_data`, `get_health_snapshots`, `get_jetlag_status`, `get_personal_record_candidates`,
`get_fastest_splits`, `get_activity_laps`, `get_activity_power_curve`, `get_gear_list`, `get_calendar_week`,
`get_upcoming_events`, `get_golf_summary`, `get_menstrual_cycle_summary`, `search_activities`, ...

## Profiles and environment

| Variable | Default | Effect |
|---|---|---|
| `GARMIN_EVERYTHING_PROFILE` | `extras` | `extras`: read methods garmin_mcp lacks. `all`: every read method. `full`: read + write. CLI: `--profile`. |
| `GARMIN_EVERYTHING_WRITES` | unset | Must be `1` for `full` to actually register write tools. |
| `GARMIN_EVERYTHING_GROUPS` | all | Comma list of groups to keep: `sleep,hrv,stress,body_battery,heart_rate,steps,floors,respiration,spo2,hydration,weight,nutrition,blood_pressure,glucose,ecg,health_snapshot,training,metrics,activities,workouts,courses,gear,devices,badges,challenges,calendar,goals,jetlag,menstrual,golf,profile,reports,other`. |
| `GARMIN_ENABLED_TOOLS` | unset | Comma allowlist of auto-generated tool names (case-insensitive), same semantics as garmin_mcp. |
| `GARMIN_DISABLED_TOOLS` | unset | Comma denylist; ignored when the allowlist is set. |
| `GARMIN_EVERYTHING_MAX_BYTES` | `60000` | Size budget per result; larger payloads are summarised. `0` disables. |
| `GARMIN_EVERYTHING_REDACT` | `1` | Recursive redaction of personal keys. Set `0` to disable. |
| `GARMIN_EVERYTHING_CALL_TIMEOUT` | `90` | Seconds before a stalled Garmin request is abandoned. `0` disables. |
| `GARMIN_EVERYTHING_EXPORT_DIR` | `~/.cache/garmin-everything-mcp/export` | Default directory for `garmin_export`. |
| `GARMIN_EVERYTHING_DOWNLOAD_DIR` | `~/.cache/garmin-everything-mcp/downloads` | Where the `download_*` tools save files. |
| `GARMIN_EVERYTHING_TRANSPORT` / `HOST` / `PORT` | `stdio` / `127.0.0.1` / `8001` | Also `--transport streamable-http --port 8001` on the CLI. |
| `GARMINTOKENS` | `~/.garminconnect` | Token directory, identical to garmin_mcp. |
| `GARMINTOKENS_BASE64` | `~/.garminconnect_base64` | Base64 token file fallback, identical to garmin_mcp. |
| `GARMIN_IS_CN` | `false` | Use garmin.cn. |

The hand-written `garmin_*` tools are always registered.

## Size budget and redaction

Every tool returns a JSON string. If it exceeds `GARMIN_EVERYTHING_MAX_BYTES`, you get a structural summary
(keys, list lengths, first items) with `"truncated": true`, the original size, and a hint pointing at
`garmin_raw_get(fields=, jq_path=)` or the downsampling tools. Errors come back as
`{"error": ..., "type": ..., "hint": ...}` with actionable hints (expired tokens, rate limit, 404 for a feature your
device lacks, bad date format). The server never crashes on a tool call.

Redaction masks, recursively and case-insensitively, any key matching `email`, `userName`, `firstName`,
`lastName`, `fullName`, `displayName`, `birthDate`, `address`, `phone` (including prefixed variants such as
`ownerFullName`) with `"[redacted]"`. The display name is still used internally for URL paths. Numeric ids
(profile, device, activity) are not redacted because tools need them as parameters.

## Raw endpoint escape hatch

`garmin_endpoint_catalog(search="metrics-service")` lists what is known; `garmin_raw_get` calls it. The catalog
bundles two sources: paths **verified** on 2026-11-17 against a real account (shapes noted), and every
`*-service/` path template the installed library touches, extracted by walking its AST
(`scripts/gen_endpoints.py`). Fifteen library methods returned 404/400 for the probing account (naps, lifetime
totals, acclimation, running economy, power curve, one-rep max, ECG, health snapshots, ...) and are flagged; they
may work for you if your device records that data.

## Write safety

Methods are classified by name prefix: `add_ create_ delete_ update_ set_ upload_ schedule_ unschedule_ push_ send_
import_ reschedule_ move_ confirm_ init_ request_ copy_ convert_ remove_` are WRITE, everything else is READ.
Write tools only exist when `GARMIN_EVERYTHING_PROFILE=full` **and** `GARMIN_EVERYTHING_WRITES=1`, are described
with a `[WRITE: modifies your Garmin account]` prefix, and carry `readOnlyHint=false, destructiveHint=true`.
`login`, `logout`, `resume_login`, `request_reload`, the raw `connectapi`/`download` helpers and the GraphQL
escape hatch are never auto-generated. Methods that return bytes (`download_*`) are wrapped by small overrides that
save the file locally and return its path.

## Privacy

This repository contains no personal data: the bundled endpoint lists only hold path templates with `{dn}`
placeholders, tests use a fake Garmin class, and the smoke tests are run locally. Tokens live where garmin_mcp
puts them and are never copied. `garmin_export` writes to your machine only, with redaction applied, and the
`.gitignore` refuses token files and exports by default.

## Updating when python-garminconnect releases

```bash
uv lock --upgrade-package garminconnect
uv run python scripts/gen_endpoints.py     # refresh the endpoint catalog from the new source
uv run python scripts/gen_coverage.py      # refresh docs/COVERAGE.md and the badge line
uv run pytest                              # the committed coverage file is checked against the live registry
```

New methods appear as tools automatically. If one needs special handling (odd signature, bytes return), add it to
`OVERRIDES` or `SKIP` in `src/garmin_everything_mcp/registry.py`.

## Development

```bash
git clone https://github.com/dlekdns08/garmin-everything-mcp && cd garmin-everything-mcp
uv sync
uv run pytest                               # 45 tests, no network
uv run garmin-everything-mcp --profile all  # stdio
npx @modelcontextprotocol/inspector uv run garmin-everything-mcp
```

Layout: `registry.py` (introspection, classification, profiles, overrides), `results.py` (JSON, redaction,
size budget, error hints), `pathsel.py` (jq-like resolver), `client.py` (lazy token login with a lock),
`tools_extra.py` (the eight hand-written tools), `server.py` (FastMCP wiring and CLI),
`data/` (garmin_mcp method and tool name lists, verified and generated endpoint catalogs).

## How the garmin_mcp list was derived

`data/garmin_mcp_wrapped_methods.txt` is the set of `garmin_client.<method>(` calls grepped from garmin_mcp's
`src/garmin_mcp/*.py` (105 names, 99 of which are `Garmin` methods in 0.3.20; the rest are HTTP helpers such as
`post`/`put`). `data/garmin_mcp_tool_names.txt` is every `@app.tool`-decorated function name in the same tree
(153 names), because a few garmin_mcp tools shadow library method names without calling them
(`get_heart_rate_zones`, `get_courses`, `get_calendar_events`, the `get_nutrition_daily_*` trio, ...). The
`extras` profile excludes the union of both lists.

## Credits

- [Taxuspt/garmin_mcp](https://github.com/Taxuspt/garmin_mcp) for the server this one is designed to sit next to,
  and for the token conventions it established.
- [cyberjunky/python-garminconnect](https://github.com/cyberjunky/python-garminconnect) for the library that does
  all the real work. Every tool here is one of its methods.

MIT licensed. Not affiliated with Garmin.
