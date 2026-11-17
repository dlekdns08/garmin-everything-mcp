"""Lazy, lock-protected Garmin session reusing garmin_mcp's token conventions.

Environment (identical to garmin_mcp so one `garmin-mcp-auth` serves both):

* ``GARMINTOKENS``         token directory (default ``~/.garminconnect``)
* ``GARMINTOKENS_BASE64``  base64 token file (default ``~/.garminconnect_base64``),
                           used only when the directory is missing
* ``GARMIN_IS_CN``         use garmin.cn

This server never prompts for credentials.
"""

from __future__ import annotations

import base64
import contextlib
import io
import logging
import os
import sys
import threading
from typing import Any, Callable

DEFAULT_CALL_TIMEOUT = 90.0
AUTH_HELP = (
    "No usable Garmin tokens. Authenticate once with "
    "'uvx --python 3.12 --from git+https://github.com/Taxuspt/garmin_mcp garmin-mcp-auth' "
    "(saves tokens to ~/.garminconnect), then restart this server."
)


def resolve_token_path(path: str) -> str:
    expanded = os.path.expandvars(path).replace("${HOME}", os.path.expanduser("~"))
    return os.path.expanduser(expanded)


def token_dir() -> str:
    return resolve_token_path(os.getenv("GARMINTOKENS") or "~/.garminconnect")


def token_base64_path() -> str:
    return resolve_token_path(os.getenv("GARMINTOKENS_BASE64") or "~/.garminconnect_base64")


def is_cn() -> bool:
    return os.getenv("GARMIN_IS_CN", "false").strip().lower() in ("1", "true", "yes")


def call_timeout() -> float:
    raw = os.getenv("GARMIN_EVERYTHING_CALL_TIMEOUT")
    if not raw or not raw.strip():
        return DEFAULT_CALL_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_CALL_TIMEOUT
    return value if value > 0 else 0.0


def _default_factory() -> Any:
    """Log in with saved tokens only. Raises if that is impossible."""
    from garminconnect import Garmin

    logging.getLogger("garminconnect").setLevel(logging.ERROR)
    directory = token_dir()
    b64 = token_base64_path()
    garmin = Garmin(is_cn=is_cn())
    # Library warnings must never reach stdout (JSON-RPC framing); keep stderr quiet too.
    with contextlib.redirect_stderr(io.StringIO()):
        if os.path.isdir(directory):
            garmin.login(directory)
            return garmin
        if os.path.isfile(b64):
            with open(b64, "r", encoding="utf-8") as fh:
                token_json = base64.b64decode(fh.read().strip()).decode("utf-8")
            garmin.login(token_json)
            return garmin
    raise FileNotFoundError(f"{AUTH_HELP} (looked in {directory} and {b64})")


class GarminSession:
    """Owns the Garmin client; logs in lazily on first use under a lock."""

    def __init__(self, factory: Callable[[], Any] | None = None, timeout: float | None = None):
        self._factory = factory or _default_factory
        self._timeout = call_timeout() if timeout is None else timeout
        self._lock = threading.RLock()
        self._client: Any = None
        self._login_error: BaseException | None = None

    # -- login ---------------------------------------------------------------
    @property
    def ready(self) -> bool:
        return self._client is not None

    def get(self) -> Any:
        with self._lock:
            if self._client is not None:
                return self._client
            try:
                self._client = self._factory()
            except BaseException as exc:
                self._login_error = exc
                raise
            print("garmin-everything-mcp: Garmin session ready.", file=sys.stderr)
            return self._client

    @property
    def display_name(self) -> str:
        client = self.get()
        name = getattr(client, "display_name", None)
        if not name and hasattr(client, "_require_display_name"):
            return client._require_display_name()
        from urllib.parse import quote
        return quote(name or "", safe="")

    # -- calls ---------------------------------------------------------------
    def call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """Call ``method`` on the client, bounded by the configured timeout."""
        client = self.get()
        fn = getattr(client, method)
        if not self._timeout:
            return fn(*args, **kwargs)
        outcome: dict[str, Any] = {}

        def _worker() -> None:
            try:
                outcome["value"] = fn(*args, **kwargs)
            except BaseException as exc:  # noqa: BLE001 - replayed below
                outcome["error"] = exc

        worker = threading.Thread(target=_worker, name=f"garmin-call:{method}", daemon=True)
        worker.start()
        worker.join(self._timeout)
        if worker.is_alive():
            raise TimeoutError(f"Garmin request '{method}' did not return within {self._timeout:g}s")
        if "error" in outcome:
            raise outcome["error"]
        return outcome.get("value")

    def connectapi(self, path: str, **kwargs: Any) -> Any:
        return self.call("connectapi", path, **kwargs)
