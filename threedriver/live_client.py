"""Client for the 3Driver Live addon socket (same machine, localhost only).

Speaks the addon's newline-delimited JSON protocol. The token is resolved
in this order: explicit argument, THREEDRIVER_LIVE_TOKEN, then the shared
token file ~/.threedriver/live_token (created by the addon on first start).
The token-file logic is duplicated in the addon itself (it cannot import
this package) — keep the two in sync.

Never point this at a remote host: the server binds 127.0.0.1 and the
protocol has no encryption.
"""

from __future__ import annotations

import json
import os
import socket
import uuid
from pathlib import Path

DEFAULT_PORT = 9877
MAX_LINE_BYTES = 2_000_000


class LiveError(RuntimeError):
    """Connection, protocol, or token problem talking to live Blender."""


def default_token_file() -> Path:
    return Path.home() / ".threedriver" / "live_token"


def resolve_token(token: str | None = None,
                  token_file: str | Path | None = None) -> str:
    if token:
        return token
    env = os.environ.get("THREEDRIVER_LIVE_TOKEN")
    if env:
        return env
    path = Path(token_file) if token_file else default_token_file()
    if path.is_file():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    raise LiveError(
        f"live token not found: start the 3Driver Live server in Blender "
        f"first (it creates {path}), or pass --token / set "
        "THREEDRIVER_LIVE_TOKEN."
    )


class LiveClient:
    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
                 token: str | None = None, token_file: str | Path | None = None,
                 timeout: float = 60.0) -> None:
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise LiveError("live client only connects to localhost")
        self.host = host
        self.port = port
        self.token = resolve_token(token, token_file)
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._reader = None

    def connect(self) -> "LiveClient":
        if self._sock is not None:
            return self
        try:
            sock = socket.create_connection(
                (self.host, self.port), timeout=self.timeout)
        except OSError as exc:
            raise LiveError(
                f"cannot connect to live Blender at {self.host}:{self.port}: "
                f"{exc} (is the 3Driver Live server started?)"
            ) from exc
        sock.settimeout(self.timeout)
        self._sock = sock
        self._reader = sock.makefile("rb")
        return self

    def close(self) -> None:
        try:
            if self._reader is not None:
                self._reader.close()
        except OSError:
            pass
        try:
            if self._sock is not None:
                self._sock.close()
        except OSError:
            pass
        self._sock = None
        self._reader = None

    def __enter__(self) -> "LiveClient":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    def call(self, command_type: str, timeout: float | None = None,
             **fields) -> dict:
        """Send one command, return the parsed response dict."""
        self.connect()
        assert self._sock is not None and self._reader is not None
        payload = {"id": uuid.uuid4().hex[:8], "token": self.token,
                   "type": command_type, **fields}
        data = (json.dumps(payload) + "\n").encode("utf-8")
        if timeout is not None:
            self._sock.settimeout(timeout)
        try:
            self._sock.sendall(data)
            line = self._reader.readline(MAX_LINE_BYTES + 1)
        except (OSError, socket.timeout) as exc:
            raise LiveError(f"live call failed: {exc}") from exc
        finally:
            if timeout is not None:
                self._sock.settimeout(self.timeout)
        if not line:
            raise LiveError("live server closed the connection")
        if len(line) > MAX_LINE_BYTES:
            raise LiveError("live response too large")
        try:
            return json.loads(line.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise LiveError(f"invalid response from live server: {exc}") from exc

    def ping(self) -> dict:
        return self.call("ping")

    def execute(self, code: str, timeout: float | None = None) -> dict:
        return self.call("execute_code", timeout=timeout, code=code)

    def get_scene(self) -> dict:
        return self.call("get_scene")

    def screenshot(self, filepath: str) -> dict:
        return self.call("screenshot", filepath=filepath)

    def shutdown(self) -> dict:
        return self.call("shutdown")
