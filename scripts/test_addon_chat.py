"""Headless test for the addon Live Chat client (urllib + poll logic).

Runs inside Blender (the addon imports bpy), against a tiny mock web UI
in a thread. Covers: register/unregister + scene props, the default web
base when prefs are missing, chat create (JSON body), chat send
(urlencoded, target=live), fetch/apply, the poll timer stopping when the
turn is done, and the clean error when the web UI is unreachable.

What this does NOT prove (needs the laptop GUI): the panel drawing, the
operators firing from clicks, and the real web UI end to end.

Run:
    ~/workspace/tools/run-blender.sh -b --python \\
        scripts/test_addon_chat.py -- .
"""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

argv = sys.argv[sys.argv.index("--") + 1:]
ROOT = Path(argv[0]).resolve() if argv else Path.cwd()
sys.path.insert(0, str(ROOT / "addons"))

import bpy  # noqa: E402
import threedriver_live as addon  # noqa: E402

seen = {}


class Handler(BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/chats/chat-test":
            self._send({"id": "chat-test", "pending": None, "messages": [
                {"role": "user", "text": "halo"},
                {"role": "assistant",
                 "text": "v1 (live, langsung): plat",
                 "status": "done"}]})
        elif self.path == "/api/health":
            self._send({"ok": True, "version": "9.9.9"})
        else:
            self._send({"detail": "not found"}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        if self.path == "/api/chats":
            seen["create_ct"] = self.headers.get("Content-Type")
            seen["create_body"] = raw.decode()
            self._send({"id": "chat-test"})
        elif self.path == "/api/chats/chat-test/messages":
            seen["msg_fields"] = parse_qs(raw.decode())
            self._send({"ok": True})
        else:
            self._send({"detail": "not found"}, 404)

    def log_message(self, *a):
        pass


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    addon.register()
    try:
        assert hasattr(bpy.types.Scene, "td_chat_id")
        assert hasattr(bpy.types.Scene, "td_chat_input")
        print("scene props ok")
        assert addon._web_base() == addon.DEFAULT_WEB_URL
        print("web base default ok:", addon._web_base())
        created = addon._chat_http(base, "POST", "/api/chats", json_data={})
        assert created["id"] == "chat-test", created
        assert "application/json" in seen["create_ct"], seen
        assert json.loads(seen["create_body"]) == {}, seen
        print("chat create ok (JSON body)")
        addon._chat_http(base, "POST", "/api/chats/chat-test/messages",
                         data={"text": "halo", "target": "live"})
        assert seen["msg_fields"]["target"] == ["live"], seen
        assert seen["msg_fields"]["text"] == ["halo"], seen
        print("chat send ok (urlencoded, target=live)")
        session = addon._chat_fetch(base, "chat-test")
        addon._chat_apply_session(session)
        assert addon._chat_state["chat_id"] == "chat-test"
        assert len(addon._chat_state["messages"]) == 2
        assert addon._chat_state["messages"][1]["text"].startswith("v1"), \
            addon._chat_state
        print("chat fetch/apply ok")
        addon._chat_state["web_base"] = base
        addon._chat_state["polling"] = True
        assert addon._chat_poll() is None
        assert addon._chat_state["polling"] is False
        assert addon._chat_state["status"] == "selesai"
        print("chat poll ok (pending None -> stop)")
        try:
            addon._chat_http("http://127.0.0.1:9", "GET", "/api/health",
                             timeout=3)
        except addon.ChatWebError as exc:
            assert "start-windows.bat" in str(exc), exc
            print("unreachable web UI -> clean ChatWebError ok")
        else:
            raise AssertionError("unreachable did not raise")
    finally:
        addon.unregister()
        server.shutdown()
    assert not hasattr(bpy.types.Scene, "td_chat_id")
    print("unregister ok")
    print("ADDON CHAT TEST PASSED")


main()
