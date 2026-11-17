"""3Driver Live: drive THIS Blender from another process on the same machine.

A TCP server on 127.0.0.1 (never exposed) accepts newline-delimited JSON
commands: ping, get_scene, execute_code, screenshot, shutdown. The socket
thread ONLY receives and parses; every command executes on Blender's main
thread via a bpy.app.timers pump, because bpy is NOT thread-safe and any
bpy access from another thread can crash Blender.

Security (this addon runs arbitrary Python, so these are mandatory, not
optional):
* bind 127.0.0.1 only, per-user secret token from ~/.threedriver/live_token
  (created on first start, mode 0600 on POSIX). No token, no execution.
* execute_code ALWAYS saves + writes a timestamped backup copy of the
  current .blend first (copy-first). Unsaved files get a warning instead.
* The server only runs while you explicitly start it (N-panel > 3Driver >
  Start Server). Stop it when done.
* Opening another .blend mid-session can cancel the timer pump: treat the
  live connection as broken after open_mainfile and restart the server.

This addon is standalone: it must not import the threedriver package (the
addon runs inside Blender; the package runs outside it). The token-file
logic here is intentionally duplicated in threedriver/live_client.py —
keep the two in sync.

Protocol (one JSON object per line, "\\n" terminated):
  {"id": "...", "token": "...", "type": "execute_code", "code": "..."}
  -> {"id": "...", "ok": true, "stdout": "...", "result": "...",
      "notes": [...], "warnings": [...]}
"""

bl_info = {
    "name": "3Driver Live",
    "author": "Reefoldfiveteen",
    "version": (0, 1, 0),
    "blender": (5, 2, 0),
    "location": "View3D > Sidebar > 3Driver",
    "description": "Local socket server so 3Driver can drive this Blender "
                   "(localhost + token, main-thread execution).",
    "category": "3D View",
}

import contextlib
import io
import json
import os
import queue
import secrets
import socket
import threading
import time
import traceback
from pathlib import Path

import bpy

DEFAULT_PORT = 9877
MAX_MESSAGE_BYTES = 1_000_000
MAX_OUTPUT_CHARS = 100_000
PUMP_INTERVAL = 0.05
PUMP_MAX_PER_TICK = 20

_state = {"server": None, "port": None, "token_file": None,
          "stop_requested": False}


class LiveServerError(RuntimeError):
    pass


# ---------------------------------------------------------------- token

def default_token_file() -> Path:
    return Path.home() / ".threedriver" / "live_token"


def load_or_create_token(path: str | Path | None = None) -> str:
    p = Path(path) if path else default_token_file()
    if p.is_file():
        token = p.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(token, encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return token


# ------------------------------------------------------------- plumbing

def _truncate(text: str) -> str:
    if len(text) > MAX_OUTPUT_CHARS:
        return text[:MAX_OUTPUT_CHARS] + "\n... [truncated]"
    return text


class _Conn:
    """One client connection; send is locked so the reader thread (error
    replies) and the pump thread (command replies) never interleave."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.lock = threading.Lock()

    def send(self, obj: dict) -> None:
        data = (json.dumps(obj, default=str) + "\n").encode("utf-8")
        with self.lock:
            self.sock.sendall(data)

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class _Server:
    def __init__(self, port: int, token: str) -> None:
        self.port = port
        self.token = token
        self.cmd_queue: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self._conns: list[_Conn] = []
        self._conns_lock = threading.Lock()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind(("127.0.0.1", port))
            self.sock.listen(5)
        except OSError as exc:
            self.sock.close()
            raise LiveServerError(
                f"cannot bind 127.0.0.1:{port}: {exc} "
                "(is another live server already running?)"
            ) from exc
        self.sock.settimeout(0.2)
        self.thread = threading.Thread(target=self._accept_loop, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        try:
            self.sock.close()
        except OSError:
            pass
        with self._conns_lock:
            conns = list(self._conns)
        for conn in conns:
            conn.close()
        if self.thread is not threading.current_thread():
            self.thread.join(timeout=2.0)

    def _accept_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                sock, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            conn = _Conn(sock)
            with self._conns_lock:
                self._conns.append(conn)
            threading.Thread(target=self._reader, args=(conn,),
                             daemon=True).start()

    def _reader(self, conn: _Conn) -> None:
        buf = b""
        try:
            while not self.stop_event.is_set():
                try:
                    chunk = conn.sock.recv(65536)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                if len(buf) > MAX_MESSAGE_BYTES:
                    conn.send({"id": None, "ok": False,
                               "error": "message too large"})
                    break
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    self._handle_line(conn, line)
        finally:
            with self._conns_lock:
                if conn in self._conns:
                    self._conns.remove(conn)
            conn.close()

    def _handle_line(self, conn: _Conn, line: bytes) -> None:
        try:
            cmd = json.loads(line.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            conn.send({"id": None, "ok": False, "error": "invalid JSON"})
            return
        if not isinstance(cmd, dict):
            conn.send({"id": None, "ok": False,
                       "error": "command must be a JSON object"})
            return
        if cmd.get("token") != self.token:
            conn.send({"id": cmd.get("id"), "ok": False,
                       "error": "bad token"})
            return
        self.cmd_queue.put((cmd, conn))


# -------------------------------------------------- command execution
# Everything below runs ONLY on Blender's main thread (via the pump).

def _scene_info() -> dict:
    objects = []
    for obj in bpy.data.objects:
        objects.append({
            "name": obj.name,
            "type": obj.type,
            "location": [round(v, 3) for v in obj.location],
            "dimensions": [round(v, 3) for v in obj.dimensions],
        })
    active = bpy.context.view_layer.objects.active
    return {
        "blender_version": ".".join(str(v) for v in bpy.app.version),
        "filepath": bpy.data.filepath,
        "is_dirty": bpy.data.is_dirty,
        "frame_current": bpy.context.scene.frame_current,
        "object_count": len(objects),
        "active_object": active.name if active else None,
        "objects": objects,
    }


def _backup_before_execute() -> tuple[list[str], list[str]]:
    """SAVE + copy-first. Returns (notes, warnings)."""
    notes: list[str] = []
    warnings: list[str] = []
    filepath = bpy.data.filepath
    if not filepath:
        warnings.append(
            "file is unsaved: no backup was made; save the .blend first "
            "for copy-first safety")
        return notes, warnings
    try:
        if bpy.data.is_dirty:
            bpy.ops.wm.save_mainfile()
            notes.append("saved current file before executing")
    except Exception as exc:  # noqa: BLE001 - backup problems are reported
        warnings.append(f"save failed: {exc}")
    try:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = f"{filepath}.3driver-backup-{stamp}.blend"
        bpy.ops.wm.save_as_mainfile(filepath=backup, copy=True)
        notes.append(f"backup copy: {backup}")
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"backup failed: {exc}")
    return notes, warnings


def _execute_code(code: str) -> dict:
    notes, warnings = _backup_before_execute()
    import bmesh  # noqa: PLC0415 - imported here: bpy modules only on main thread
    import mathutils  # noqa: PLC0415

    namespace: dict = {
        "bpy": bpy,
        "bmesh": bmesh,
        "mathutils": mathutils,
        "Vector": mathutils.Vector,
        "__name__": "__threedriver_live__",
    }
    stdout_buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout_buf), \
                contextlib.redirect_stderr(stdout_buf):
            exec(compile(code, "<3driver-live>", "exec"), namespace)  # noqa: S102
    except Exception:  # noqa: BLE001 - the error goes back to the client
        return {"ok": False, "stdout": _truncate(stdout_buf.getvalue()),
                "error": traceback.format_exc(),
                "notes": notes, "warnings": warnings}
    result = namespace.get("__result__")
    return {"ok": True, "stdout": _truncate(stdout_buf.getvalue()),
            "result": repr(result)[:2000] if result is not None else None,
            "notes": notes, "warnings": warnings}


def _screenshot(filepath: str | None) -> dict:
    if not filepath:
        return {"ok": False, "error": "screenshot needs a filepath"}
    scene = bpy.context.scene
    previous = scene.render.filepath
    try:
        scene.render.filepath = filepath
        bpy.ops.render.opengl(write_still=True)
    except Exception as exc:  # noqa: BLE001 - headless has no GL context
        return {"ok": False,
                "error": f"screenshot needs a Blender GUI viewport: {exc}"}
    finally:
        scene.render.filepath = previous
    size = Path(filepath).stat().st_size if Path(filepath).is_file() else 0
    return {"ok": True, "filepath": filepath, "bytes": size}


def _execute_command(cmd: dict) -> dict:
    ctype = cmd.get("type")
    if ctype == "ping":
        return {"ok": True, "result": "pong",
                "blender_version": ".".join(str(v) for v in bpy.app.version)}
    if ctype == "get_scene":
        return {"ok": True, "result": _scene_info()}
    if ctype == "execute_code":
        code = cmd.get("code")
        if not isinstance(code, str) or not code.strip():
            return {"ok": False, "error": "execute_code needs a code string"}
        return _execute_code(code)
    if ctype == "screenshot":
        return _screenshot(cmd.get("filepath"))
    if ctype == "shutdown":
        _state["stop_requested"] = True
        return {"ok": True, "result": "shutting down"}
    return {"ok": False, "error": f"unknown command type: {ctype!r}"}


def pump_once() -> float | None:
    """Drain the command queue on the main thread. Returns the next timer
    interval, or None when the server is gone. This is the function the
    bpy.app.timers callback AND headless test drivers call — one pump,
    one code path."""
    server = _state.get("server")
    if server is None:
        return None
    drained = 0
    while drained < PUMP_MAX_PER_TICK:
        try:
            cmd, conn = server.cmd_queue.get_nowait()
        except queue.Empty:
            break
        drained += 1
        try:
            resp = _execute_command(cmd)
        except Exception:  # noqa: BLE001 - one bad command must not kill the pump
            resp = {"ok": False, "error": traceback.format_exc()}
        resp["id"] = cmd.get("id")
        try:
            conn.send(resp)
        except OSError:
            pass
    if _state.get("stop_requested"):
        stop_server()
        return None
    return PUMP_INTERVAL


def _timer():
    return pump_once()


# ------------------------------------------------------- server control

def start_server(port: int | None = None, token_file: str | Path | None = None,
                 use_timer: bool = True) -> dict:
    if _state.get("server") is not None:
        raise LiveServerError("live server is already running")
    port = int(port or DEFAULT_PORT)
    token_path = Path(token_file) if token_file else default_token_file()
    token = load_or_create_token(token_path)
    server = _Server(port, token)
    _state["server"] = server
    _state["port"] = port
    _state["token_file"] = str(token_path)
    _state["stop_requested"] = False
    server.start()
    if use_timer and not bpy.app.timers.is_registered(_timer):
        bpy.app.timers.register(_timer, first_interval=0.0)
    print(f"3Driver Live: serving on 127.0.0.1:{port} "
          f"(token file: {token_path})")
    return {"port": port, "token_file": str(token_path)}


def stop_server() -> None:
    server = _state.get("server")
    _state["server"] = None
    _state["port"] = None
    _state["stop_requested"] = False
    try:
        if bpy.app.timers.is_registered(_timer):
            bpy.app.timers.unregister(_timer)
    except Exception:  # noqa: BLE001 - stopping must never raise
        pass
    if server is not None:
        server.stop()
        print("3Driver Live: server stopped")


def is_running() -> bool:
    return _state.get("server") is not None


# ----------------------------------------------------------------- UI

class ThreedriverLivePreferences(bpy.types.AddonPreferences):
    bl_idname = "threedriver_live"
    port: bpy.props.IntProperty(
        name="Port", default=DEFAULT_PORT, min=1024, max=65535,
        description="Localhost port for the live server")
    token_file: bpy.props.StringProperty(
        name="Token file", default="",
        description="Secret token file (empty = ~/.threedriver/live_token)",
        subtype="FILE_PATH")

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "port")
        layout.prop(self, "token_file")
        layout.label(text="Server binds 127.0.0.1 only. Stop it when done.")


class THREEDRIVER_OT_start_server(bpy.types.Operator):
    bl_idname = "threedriver.start_live_server"
    bl_label = "Start 3Driver Live Server"
    bl_description = "Start the localhost socket server (token protected)"

    def execute(self, context):
        prefs = context.preferences.addons["threedriver_live"].preferences
        try:
            info = start_server(port=prefs.port,
                                token_file=prefs.token_file or None)
        except LiveServerError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self.report({"INFO"},
                    f"3Driver Live on 127.0.0.1:{info['port']}")
        return {"FINISHED"}


class THREEDRIVER_OT_stop_server(bpy.types.Operator):
    bl_idname = "threedriver.stop_live_server"
    bl_label = "Stop 3Driver Live Server"
    bl_description = "Stop the localhost socket server"

    def execute(self, context):
        stop_server()
        self.report({"INFO"}, "3Driver Live stopped")
        return {"FINISHED"}


class THREEDRIVER_PT_panel(bpy.types.Panel):
    bl_label = "3Driver Live"
    bl_idname = "THREEDRIVER_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "3Driver"

    def draw(self, context):
        layout = self.layout
        if is_running():
            layout.label(text=f"Running on 127.0.0.1:{_state['port']}",
                         icon="LINKED")
            layout.operator("threedriver.stop_live_server")
        else:
            layout.label(text="Server stopped", icon="UNLINKED")
            layout.operator("threedriver.start_live_server")
        layout.label(text="Localhost + token only. Save + backup")
        layout.label(text="runs before every execute_code.")


_CLASSES = (ThreedriverLivePreferences, THREEDRIVER_OT_start_server,
            THREEDRIVER_OT_stop_server, THREEDRIVER_PT_panel)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    stop_server()
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
