"""A tiny dotted/bracket path resolver (no jq dependency).

Supported syntax::

    a.b.c          nested keys
    items[0]       list index (negative ok)
    items[-1].x    index then key
    items[*].x     map over a list
    items[0:5]     slice
    a."weird key"  quoted key (dots allowed inside quotes)
"""

from __future__ import annotations

import re
from typing import Any

_TOKEN = re.compile(r'"([^"]*)"|([^.\[\]"]+)|\[([^\]]*)\]')


class PathError(ValueError):
    pass


def tokenize(path: str) -> list[Any]:
    tokens: list[Any] = []
    pos = 0
    path = path.strip()
    if path.startswith("."):
        path = path[1:]
    while pos < len(path):
        if path[pos] == ".":
            pos += 1
            continue
        m = _TOKEN.match(path, pos)
        if not m:
            raise PathError(f"cannot parse path near {path[pos:]!r}")
        quoted, bare, bracket = m.groups()
        if quoted is not None:
            tokens.append(("key", quoted))
        elif bare is not None:
            tokens.append(("key", bare))
        else:
            b = bracket.strip()
            if b == "*":
                tokens.append(("star", None))
            elif ":" in b:
                lo, _, hi = b.partition(":")
                tokens.append(("slice", (int(lo) if lo.strip() else None, int(hi) if hi.strip() else None)))
            elif re.fullmatch(r"-?\d+", b):
                tokens.append(("index", int(b)))
            else:
                tokens.append(("key", b.strip("'\"")))
        pos = m.end()
    return tokens


def resolve(obj: Any, path: str | None) -> Any:
    if not path or path.strip() in ("", "."):
        return obj
    return _walk(obj, tokenize(path))


def _walk(obj: Any, tokens: list[Any]) -> Any:
    for i, (kind, arg) in enumerate(tokens):
        if kind == "star":
            if not isinstance(obj, list):
                raise PathError("[*] applied to a non-list")
            rest = tokens[i + 1:]
            return [_walk(item, rest) for item in obj]
        if kind == "key":
            if isinstance(obj, dict):
                if arg not in obj:
                    raise PathError(f"key {arg!r} not found; available: {list(obj.keys())[:30]}")
                obj = obj[arg]
            elif isinstance(obj, list) and arg.lstrip("-").isdigit():
                obj = obj[int(arg)]
            else:
                raise PathError(f"cannot take key {arg!r} from {type(obj).__name__}")
        elif kind == "index":
            if not isinstance(obj, list):
                raise PathError(f"index [{arg}] applied to {type(obj).__name__}")
            try:
                obj = obj[arg]
            except IndexError as e:
                raise PathError(f"index {arg} out of range (len={len(obj)})") from e
        elif kind == "slice":
            if not isinstance(obj, list):
                raise PathError("slice applied to a non-list")
            obj = obj[arg[0]:arg[1]]
    return obj


def pick_fields(obj: Any, fields: str | list[str] | None) -> Any:
    """Keep only the given top-level keys (on a dict, or on each dict in a list)."""
    if not fields:
        return obj
    names = [f.strip() for f in (fields.split(",") if isinstance(fields, str) else fields) if f.strip()]
    if isinstance(obj, dict):
        return {k: obj[k] for k in names if k in obj}
    if isinstance(obj, list):
        return [{k: d[k] for k in names if k in d} if isinstance(d, dict) else d for d in obj]
    return obj
