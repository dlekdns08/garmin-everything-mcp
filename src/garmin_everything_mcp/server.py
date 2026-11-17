"""FastMCP server: reflection-generated tools + hand-written extras."""

from __future__ import annotations

import argparse
import os
import sys
from importlib import metadata
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import __version__, registry, tools_extra
from .client import GarminSession
from .config import Settings
from .results import budget, error_payload, redact, to_json
from .tools_extra import ServerState

VALID_TRANSPORTS = ("stdio", "streamable-http", "sse")


def library_version() -> str:
    try:
        return metadata.version("garminconnect")
    except metadata.PackageNotFoundError:  # pragma: no cover
        return "unknown"


def make_invoker(session: GarminSession, settings: Settings):
    """Return the function that executes an auto-generated ToolSpec."""

    def invoke(spec: registry.ToolSpec, kwargs: dict[str, Any]) -> str:
        try:
            args, kw = registry.bind_call_args(spec, kwargs)
            if spec.override_fn is not None:
                result = spec.override_fn(session.get(), *args, **kw)
            else:
                result = session.call(spec.method, *args, **kw)
            if not isinstance(result, (dict, list)):
                result = {"result": result}
            if settings.redact:
                result = redact(result)
            return budget(result, settings.max_bytes)
        except Exception as exc:  # noqa: BLE001 - tools never crash the server
            return to_json(error_payload(exc, tool=spec.name))

    return invoke


def build_app(settings: Settings | None = None, session: GarminSession | None = None,
              garmin_cls: type | None = None) -> tuple[FastMCP, ServerState]:
    """Create the FastMCP app with all tools registered. Pure apart from imports."""
    settings = settings or Settings.from_env()
    session = session or GarminSession()
    if garmin_cls is None:
        from garminconnect import Garmin as garmin_cls  # noqa: N813

    infos = registry.introspect(garmin_cls)
    specs = registry.build_specs(garmin_cls, infos)
    selection = registry.select_tools(
        specs, settings.profile, writes_enabled=settings.writes_enabled,
        enabled=settings.enabled_tools, disabled=settings.disabled_tools, groups=settings.groups,
    )

    app = FastMCP("Garmin Everything", host=settings.host, port=settings.port)
    invoke = make_invoker(session, settings)
    state = ServerState(settings=settings, library_version=library_version())
    state.total_methods = len(infos)
    state.read_methods = sum(1 for i in infos.values() if i.kind == "read")
    state.write_methods = len(infos) - state.read_methods
    state.skipped = {n: i.skip_reason for n, i in infos.items() if i.skip_reason}
    state.wrapped_by_garmin_mcp = len(registry.garmin_mcp_wrapped() & set(infos))
    state.excluded = selection.excluded
    for info in infos.values():
        g = state.groups_total.setdefault(info.group, {"library_read": 0, "library_write": 0})
        g["library_read" if info.kind == "read" else "library_write"] += 1

    for spec in selection.selected:
        fn = registry.make_tool_function(spec, invoke)
        ann = ToolAnnotations(readOnlyHint=spec.read_only, destructiveHint=not spec.read_only, openWorldHint=True)
        app.add_tool(fn, name=spec.name, description=spec.description, annotations=ann, structured_output=False)
        state.registered_auto.append({"name": spec.name, "group": spec.group, "kind": spec.kind})

    state.registered_extra = tools_extra.register_tools(app, session, settings, state)
    return app, state


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="garmin-everything-mcp", description="The other 70% of your Garmin, as MCP tools.")
    p.add_argument("--transport", choices=VALID_TRANSPORTS, default=None,
                   help="stdio (default) | streamable-http | sse (env GARMIN_EVERYTHING_TRANSPORT)")
    p.add_argument("--host", default=None, help="bind address for HTTP transports (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=None, help="port for HTTP transports (default 8001)")
    p.add_argument("--profile", choices=registry.PROFILES, default=None,
                   help="extras (default) | all | full (env GARMIN_EVERYTHING_PROFILE)")
    p.add_argument("--version", action="version", version=f"garmin-everything-mcp {__version__}")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    if sys.platform == "win32":  # keep JSON-RPC newlines intact
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, newline="\n")
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    if args.profile:
        settings.profile = args.profile
    if args.transport:
        settings.transport = args.transport
    if args.host:
        settings.host = args.host
    if args.port:
        settings.port = args.port
    if settings.transport not in VALID_TRANSPORTS:
        print(f"Invalid transport {settings.transport!r}; expected one of {VALID_TRANSPORTS}", file=sys.stderr)
        sys.exit(1)
    if settings.profile == "full" and not settings.writes_enabled:
        print("Profile 'full' selected but GARMIN_EVERYTHING_WRITES is not set: write tools stay hidden.", file=sys.stderr)

    app, state = build_app(settings)
    n_auto, n_extra = len(state.registered_auto), len(state.registered_extra)
    print(
        f"garmin-everything-mcp {__version__} | garminconnect {state.library_version} | profile={settings.profile} | "
        f"{n_auto} auto-generated + {n_extra} hand-written tools | redact={'on' if settings.redact else 'off'} | "
        f"tokens={os.getenv('GARMINTOKENS') or '~/.garminconnect'}",
        file=sys.stderr,
    )
    if settings.enabled_tools or settings.disabled_tools:
        known = {s.lower() for s in state.excluded} | {t["name"].lower() for t in state.registered_auto}
        known |= {n.lower() for n in state.registered_extra}
        unknown = sorted({n.lower() for n in settings.enabled_tools | settings.disabled_tools} - known)
        if unknown:
            print(f"Tool filter: name(s) not found and ignored: {', '.join(unknown)}", file=sys.stderr)
    if settings.transport != "stdio":
        print(f"Serving MCP over {settings.transport} on {settings.host}:{settings.port}", file=sys.stderr)
    app.run(transport=settings.transport)


if __name__ == "__main__":
    main()
