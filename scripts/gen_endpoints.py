#!/usr/bin/env python
"""Regenerate src/garmin_everything_mcp/data/endpoints.json.

Walks the installed ``garminconnect`` package with ``ast`` and collects every
Connect API path template the library touches (``*-service/...``), resolving
``self.garmin_connect_*_url`` attributes to their literal values and turning
f-string expressions into ``{placeholder}`` names. Run after upgrading the
library::

    uv run python scripts/gen_endpoints.py
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

import garminconnect

PKG = Path(garminconnect.__file__).parent
OUT = Path(__file__).resolve().parent.parent / "src" / "garmin_everything_mcp" / "data" / "endpoints.json"
from garmin_everything_mcp.registry import WRITE_PREFIXES


def _const_str(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        a, b = _const_str(node.left), _const_str(node.right)
        if a is not None and b is not None:
            return a + b
    return None


def collect_url_attrs(tree: ast.AST) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            t = node.targets[0]
            if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self":
                v = _const_str(node.value)
                if v is not None and t.attr.endswith("_url"):
                    attrs[t.attr] = v
    return attrs


def _placeholder(expr: ast.AST) -> str:
    src = ast.unparse(expr)
    if "display_name" in src:
        return "{dn}"
    src = re.sub(r"^self\.", "", src)
    src = re.sub(r"[^A-Za-z0-9_]+", "_", src).strip("_") or "x"
    low = src.lower()
    for needle, name in (("start", "start"), ("end", "end"), ("cdate", "date"), ("date", "date"), ("uuid", "uuid"), ("id", "id")):
        if needle in low:
            return "{" + name + "}"
    return "{" + src + "}"


def render(node: ast.AST, attrs: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                parts.append(str(v.value))
            elif isinstance(v, ast.FormattedValue):
                inner = v.value
                if isinstance(inner, ast.Attribute) and isinstance(inner.value, ast.Name) and inner.value.id == "self" and inner.attr in attrs:
                    parts.append(attrs[inner.attr])
                else:
                    parts.append(_placeholder(inner))
        return "".join(parts)
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self" and node.attr in attrs:
        return attrs[node.attr]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        a, b = render(node.left, attrs), render(node.right, attrs)
        if a is not None and b is not None:
            return a + b
    return None


def main() -> None:
    trees = {p: ast.parse(p.read_text(), filename=str(p)) for p in sorted(PKG.rglob("*.py"))}
    attrs: dict[str, str] = {}
    for tree in trees.values():
        attrs.update(collect_url_attrs(tree))
    found: dict[str, dict] = {}
    for path, tree in trees.items():
        for func in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            http = "write (POST/PUT/DELETE)" if func.name.startswith(WRITE_PREFIXES) else "GET"
            for node in ast.walk(func):
                s = render(node, attrs) if isinstance(node, (ast.JoinedStr, ast.BinOp)) else None
                if s is None and isinstance(node, ast.Name) and node.id == "url":
                    continue
                if s and "-service/" in s and s.startswith("/"):
                    s = re.sub(r"\{self_([a-z_]+)_url\}", lambda m: attrs.get(f"garmin_{m.group(1)}_url", m.group(0)), s)
                    entry = found.setdefault(s, {"path": s, "method": func.name, "module": path.stem, "http": http})
                    if entry["method"] != func.name and func.name not in entry.get("also", []):
                        entry.setdefault("also", []).append(func.name)
    # Standalone attribute URLs count too (prefixes used by many methods).
    for attr, val in attrs.items():
        if "-service/" in val and val not in found:
            found[val] = {"path": val, "method": None, "module": "base", "http": "GET", "note": f"prefix ({attr})"}
    entries = sorted(found.values(), key=lambda e: e["path"])
    OUT.write_text(json.dumps({"garminconnect": _version(), "count": len(entries), "endpoints": entries}, indent=1) + "\n")
    print(f"wrote {OUT} ({len(entries)} paths, garminconnect {_version()})", file=sys.stderr)


def _version() -> str:
    from importlib import metadata
    return metadata.version("garminconnect")


if __name__ == "__main__":
    main()
