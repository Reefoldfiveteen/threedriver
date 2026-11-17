"""3Driver Live: drive THIS Blender from another process on the same machine.

A TCP server on 127.0.0.1 (never exposed) accepts newline-delimited JSON
commands: ping, get_scene, execute_code, screenshot, set_viewport, shutdown. The socket
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

The N-panel also has a Live Chat panel: a thin client for the local web
UI's chat API (same endpoints the browser uses), so live modification
works without opening a browser. The brain stays in the web app — it runs
the LLM calls and drives this Blender back through the socket above.
Blender's Python has no httpx, so the chat client uses urllib (stdlib).
"""

bl_info = {
    "name": "3Driver Live",
    "author": "Reefoldfiveteen",
    "version": (0, 4, 0),
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
import textwrap
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
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
    """Viewport screenshot via render.opengl, with a VIEW_3D context.

    The pump runs on a timer, where bpy.context has no area — calling
    render.opengl bare fails its poll even in the GUI ("context is
    incorrect"). Overriding the context with a real 3D viewport area
    (and its WINDOW region) is what makes the operator run. Headless
    Blender still fails, but with the real cause attached.
    """
    if not filepath:
        return {"ok": False, "error": "screenshot needs a filepath"}
    screen = bpy.context.screen
    if screen is None:
        return {"ok": False,
                "error": "screenshot needs a Blender GUI viewport: "
                         "no screen (headless?)"}
    area = next((a for a in screen.areas if a.type == "VIEW_3D"), None)
    if area is None:
        return {"ok": False,
                "error": "screenshot needs a Blender GUI viewport: "
                         "no 3D viewport open"}
    region = next((r for r in area.regions if r.type == "WINDOW"), None)
    scene = bpy.context.scene
    previous = scene.render.filepath
    try:
        scene.render.filepath = filepath
        override: dict = {"area": area}
        if region is not None:
            override["region"] = region
        with bpy.context.temp_override(**override):
            bpy.ops.render.opengl(write_still=True)
    except Exception:
        detail = traceback.format_exc(limit=3).strip().splitlines()
        cause = detail[-1] if detail else "unknown error"
        return {"ok": False,
                "error": "screenshot failed (needs a Blender GUI viewport "
                         f"with a GL context): {cause}"}
    finally:
        scene.render.filepath = previous
    path = Path(filepath)
    size = path.stat().st_size if path.is_file() else 0
    if size == 0:
        return {"ok": False,
                "error": "screenshot produced no file at the given path"}
    return {"ok": True, "filepath": filepath, "bytes": size}


def _set_viewport(shading: str | None) -> dict:
    """Switch every 3D viewport's shading (SOLID/WIREFRAME/MATERIAL).

    View-only: no save, no backup, no scene change. This only sets a
    property, so it can succeed even headless (background Blender still
    exposes a screen); the screenshot is what proves the viewport
    visually. Returns ok:false only when there is no screen or no 3D
    viewport at all.
    """
    if shading not in ("WIREFRAME", "SOLID", "MATERIAL", "RENDERED"):
        return {"ok": False, "error": f"unknown shading: {shading!r}"}
    screen = bpy.context.screen
    if screen is None:
        return {"ok": False,
                "error": "no screen: viewport shading needs the Blender GUI"}
    changed = 0
    for area in screen.areas:
        if area.type == "VIEW_3D":
            area.spaces.active.shading.type = shading
            changed += 1
    if not changed:
        return {"ok": False, "error": "no 3D viewport open"}
    return {"ok": True, "result": shading, "viewports": changed}


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
    if ctype == "set_viewport":
        return _set_viewport(cmd.get("shading"))
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


# ------------------------------------------------- web UI chat client
# Thin client for the local web UI's chat API. The web app runs the LLM
# calls and drives this Blender back through the socket; this side only
# sends text and polls for the turn result. No auth: the web UI binds
# 127.0.0.1 only, same as the live socket.

DEFAULT_WEB_URL = "http://127.0.0.1:8765"

_chat_state = {
    "chat_id": None,
    "messages": [],   # [{role, text}] — last few, for the panel log
    "status": "",
    "polling": False,
    "web_base": None,
}


class ChatWebError(Exception):
    """The web UI could not be reached or answered badly."""


def _web_base() -> str:
    try:
        prefs = bpy.context.preferences.addons["threedriver_live"].preferences
        url = (getattr(prefs, "web_url", "") or "").strip()
        if url:
            return url.rstrip("/")
    except Exception:  # noqa: BLE001 - prefs missing (headless test): default
        pass
    return DEFAULT_WEB_URL


def _chat_http(base: str, method: str, path: str, data=None,
               json_data=None, timeout: float = 15.0) -> dict:
    """One JSON request to the web UI. `data` is urlencoded form fields,
    `json_data` is a JSON body. Raises ChatWebError, plainly worded."""
    url = base.rstrip("/") + path
    body = None
    headers = {"Accept": "application/json"}
    if json_data is not None:
        body = json.dumps(json_data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif data is not None:
        body = urllib.parse.urlencode(data).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise ChatWebError(
            f"web UI menjawab HTTP {exc.code}: "
            f"{exc.read().decode('utf-8', 'replace')[:200]}")
    except urllib.error.URLError as exc:
        raise ChatWebError(
            "web UI tidak terjangkau di " + base +
            " — nyalakan start-windows.bat dulu "
            f"({exc.reason})")
    except (OSError, ValueError) as exc:
        raise ChatWebError(f"web UI tidak terjangkau: {exc}")
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        raise ChatWebError("web UI menjawab bukan JSON")
    if not isinstance(parsed, dict):
        raise ChatWebError("web UI menjawab bentuk yang tidak dikenal")
    return parsed


def _chat_fetch(base: str, chat_id: str) -> dict:
    return _chat_http(base, "GET", f"/api/chats/{chat_id}")


def _chat_apply_session(session: dict) -> None:
    msgs = []
    for m in session.get("messages", [])[-8:]:
        text = (m.get("text") or "").strip()
        if m.get("status") == "error":
            text = f"ERROR: {m.get('error', 'unknown')}"
        elif not text and m.get("status"):
            text = f"({m['status']})"
        msgs.append({"role": m.get("role", "?"), "text": text})
    _chat_state["messages"] = msgs
    _chat_state["chat_id"] = session.get("id")


def _chat_redraw() -> None:
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                area.tag_redraw()
    except Exception:  # noqa: BLE001 - headless has no windows
        pass


def _chat_poll() -> float | None:
    """Timer: wait for the pending turn, then show the result. Returns
    the next interval, or None to stop."""
    base = _chat_state.get("web_base") or _web_base()
    chat_id = _chat_state.get("chat_id")
    if not chat_id:
        _chat_state["polling"] = False
        return None
    try:
        session = _chat_fetch(base, chat_id)
    except ChatWebError as exc:
        _chat_state["status"] = str(exc)
        _chat_state["polling"] = False
        _chat_redraw()
        return None
    _chat_apply_session(session)
    if session.get("pending"):
        _chat_state["status"] = (
            "giliran jalan… (" + str(session["pending"].get("stage", "")) + ")")
        _chat_redraw()
        return 1.0
    _chat_state["status"] = "selesai"
    _chat_state["polling"] = False
    _chat_redraw()
    return None


def _wrap_lines(text: str, width: int = 52) -> list[str]:
    lines = []
    for para in text.splitlines() or [""]:
        lines.extend(textwrap.wrap(para, width=width) or [""])
    return lines


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
    web_url: bpy.props.StringProperty(
        name="Web UI URL", default=DEFAULT_WEB_URL,
        description="Alamat web UI lokal untuk Live Chat")

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "port")
        layout.prop(self, "token_file")
        layout.prop(self, "web_url")
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
    bl_order = 0

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


class THREEDRIVER_OT_chat_new(bpy.types.Operator):
    bl_idname = "threedriver.chat_new"
    bl_label = "Chat Live Baru"
    bl_description = "Buat chat live baru di web UI"

    def execute(self, context):
        base = _web_base()
        try:
            created = _chat_http(base, "POST", "/api/chats", json_data={})
            session = _chat_fetch(base, created["id"])
        except (ChatWebError, KeyError) as exc:
            self.report({"ERROR"}, str(exc))
            _chat_state["status"] = str(exc)
            return {"CANCELLED"}
        _chat_state["web_base"] = base
        _chat_apply_session(session)
        context.scene.td_chat_id = session["id"]
        _chat_state["status"] = f"chat baru: {session['id']}"
        return {"FINISHED"}


class THREEDRIVER_OT_chat_load(bpy.types.Operator):
    bl_idname = "threedriver.chat_load"
    bl_label = "Muat Chat Live"
    bl_description = "Muat chat live dari web UI berdasarkan ID"

    def execute(self, context):
        chat_id = (context.scene.td_chat_id or "").strip()
        if not chat_id:
            self.report({"ERROR"}, "ID chat masih kosong")
            return {"CANCELLED"}
        base = _web_base()
        try:
            session = _chat_fetch(base, chat_id)
        except ChatWebError as exc:
            self.report({"ERROR"}, str(exc))
            _chat_state["status"] = str(exc)
            return {"CANCELLED"}
        _chat_state["web_base"] = base
        _chat_apply_session(session)
        _chat_state["status"] = f"chat dimuat: {chat_id}"
        return {"FINISHED"}


class THREEDRIVER_OT_chat_send(bpy.types.Operator):
    bl_idname = "threedriver.chat_send"
    bl_label = "Kirim ke Chat Live"
    bl_description = "Kirim pesan ke chat live (target: file yang terbuka)"

    def execute(self, context):
        scene = context.scene
        text = (scene.td_chat_input or "").strip()
        if not text:
            self.report({"ERROR"}, "Pesan masih kosong")
            return {"CANCELLED"}
        if _chat_state.get("polling"):
            self.report({"WARNING"}, "Masih menunggu giliran sebelumnya")
            return {"CANCELLED"}
        base = _web_base()
        chat_id = (scene.td_chat_id or "").strip()
        try:
            if not chat_id:
                created = _chat_http(base, "POST", "/api/chats", json_data={})
                chat_id = created["id"]
                scene.td_chat_id = chat_id
            _chat_http(base, "POST", f"/api/chats/{chat_id}/messages",
                       data={"text": text, "target": "live"})
            session = _chat_fetch(base, chat_id)
        except (ChatWebError, KeyError) as exc:
            self.report({"ERROR"}, str(exc))
            _chat_state["status"] = str(exc)
            return {"CANCELLED"}
        _chat_state["web_base"] = base
        _chat_apply_session(session)
        scene.td_chat_input = ""
        _chat_state["status"] = "giliran jalan…"
        _chat_state["polling"] = True
        if not bpy.app.timers.is_registered(_chat_poll):
            bpy.app.timers.register(_chat_poll, first_interval=1.0)
        return {"FINISHED"}


class THREEDRIVER_OT_chat_refresh(bpy.types.Operator):
    bl_idname = "threedriver.chat_refresh"
    bl_label = "Refresh Chat Live"
    bl_description = "Muat ulang pesan chat live dari web UI"

    def execute(self, context):
        chat_id = (context.scene.td_chat_id or "").strip()
        if not chat_id:
            self.report({"ERROR"}, "Belum ada chat — pencet Chat baru dulu")
            return {"CANCELLED"}
        try:
            session = _chat_fetch(_web_base(), chat_id)
        except ChatWebError as exc:
            self.report({"ERROR"}, str(exc))
            _chat_state["status"] = str(exc)
            return {"CANCELLED"}
        _chat_apply_session(session)
        _chat_state["status"] = "diperbarui"
        return {"FINISHED"}


class THREEDRIVER_PT_chat(bpy.types.Panel):
    bl_label = "Live Chat"
    bl_idname = "THREEDRIVER_PT_chat"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "3Driver"
    bl_order = 1

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        row = layout.row()
        row.prop(scene, "td_chat_id", text="ID chat")
        row.operator("threedriver.chat_load", text="Muat")
        layout.operator("threedriver.chat_new", text="Chat baru")
        for msg in _chat_state.get("messages", []):
            who = "Anda" if msg["role"] == "user" else "3Driver"
            for line in _wrap_lines(f"{who}: {msg['text']}")[:6]:
                layout.label(text=line)
        layout.separator()
        layout.prop(scene, "td_chat_input", text="Pesan")
        row = layout.row()
        row.enabled = not _chat_state.get("polling")
        row.operator("threedriver.chat_send", text="Kirim (Live)")
        layout.operator("threedriver.chat_refresh", text="Refresh")
        status = _chat_state.get("status") or ""
        if status:
            layout.label(text=status)
        layout.label(text="Mengubah file yang terbuka, langsung.")
        layout.label(text="Butuh server web (start-windows.bat).")
        layout.label(text="Upload gambar tetap lewat browser.")


_CLASSES = (ThreedriverLivePreferences, THREEDRIVER_OT_start_server,
            THREEDRIVER_OT_stop_server, THREEDRIVER_PT_panel,
            THREEDRIVER_OT_chat_new, THREEDRIVER_OT_chat_load,
            THREEDRIVER_OT_chat_send, THREEDRIVER_OT_chat_refresh,
            THREEDRIVER_PT_chat)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.td_chat_id = bpy.props.StringProperty(
        name="ID chat", default="",
        description="ID chat live di web UI (kosong = buat baru saat kirim)")
    bpy.types.Scene.td_chat_input = bpy.props.StringProperty(
        name="Pesan", default="",
        description="Pesan untuk chat live")


def unregister():
    stop_server()
    try:
        if bpy.app.timers.is_registered(_chat_poll):
            bpy.app.timers.unregister(_chat_poll)
    except Exception:  # noqa: BLE001 - stopping must never raise
        pass
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
    for prop in ("td_chat_id", "td_chat_input"):
        if hasattr(bpy.types.Scene, prop):
            delattr(bpy.types.Scene, prop)


if __name__ == "__main__":
    register()
