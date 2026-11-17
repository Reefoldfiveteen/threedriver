#!/usr/bin/env python3
"""Smoke-test chat-mod turns end to end with the stub provider.

Starts the server on a scratch port with scratch settings, creates a chat,
runs two headless turns (build + revise with an image upload), checks
version files and the probe verdict, then a live turn with no server
(clean error), then a live turn against a real headless live addon
(exec works, screenshot fails with the clean viewport message), plus the
set_viewport endpoint (a property set, provable even headless).

What this does NOT prove (needs the laptop GUI): viewport screenshots and
shading changes with a real GL context.

Usage:  python scripts/smoke_chat.py
Exit 0 = all checks passed.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from threedriver.live_client import LiveClient  # noqa: E402

PORT = 18767
BASE = f"http://127.0.0.1:{PORT}"
LIVE_PORT = 19879
TEST_TOKEN = "live-chat-test-token-do-not-use-in-production"

TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6360000000020001e221bc330000000049454e44ae426082"
)


def wait_for_turn(client, chat_id: str, prev_messages: int,
                  timeout: float = 180) -> dict:
    """Poll until the turn's messages land and pending clears."""
    deadline = time.monotonic() + timeout
    saw_pending = False
    while time.monotonic() < deadline:
        session = client.get(f"{BASE}/api/chats/{chat_id}").json()
        if "messages" not in session:
            time.sleep(1)  # transient read during a save; keep polling
            continue
        if session.get("pending"):
            saw_pending = True
        if len(session["messages"]) > prev_messages and not session.get("pending"):
            return session
        if saw_pending and not session.get("pending"):
            return session
        time.sleep(1)
    raise AssertionError(f"turn did not finish in {timeout}s: {session}")


def main() -> int:
    import httpx

    from threedriver.chat import check_mod_script
    from threedriver.pipeline import PipelineError
    check_mod_script("import bpy\nbpy.data.objects['Cube'].location.x = 1")
    try:
        check_mod_script("bpy.ops.wm.save_as_mainfile(filepath='x.blend')")
    except PipelineError:
        pass
    else:
        raise AssertionError("save_as_mainfile was not refused")
    print("mod-script guard ok")

    settings_path = ROOT / "tmp" / "smoke-chat-settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    if settings_path.exists():
        settings_path.unlink()
    blender_exe = Path.home() / "workspace" / "tools" / "blender-current" / "blender"
    env = dict(os.environ, THREEDRIVER_SETTINGS=str(settings_path))
    if blender_exe.is_file():
        env["THREEDRIVER_BLENDER"] = str(blender_exe)
    server = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "serve.py"), "--port", str(PORT)],
        cwd=ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    blender_proc = None
    try:
        with httpx.Client(timeout=15, trust_env=False) as client:
            deadline = time.monotonic() + 30
            while True:
                try:
                    if client.get(f"{BASE}/api/health").json().get("ok"):
                        break
                except httpx.RequestError:
                    pass
                if time.monotonic() > deadline:
                    print("FAIL: server did not come up")
                    return 1
                time.sleep(0.5)
            print("server up")

            assert client.get(f"{BASE}/api/chats/nope").status_code == 404
            print("unknown chat -> 404")

            created = client.post(f"{BASE}/api/chats", json={}).json()
            chat_id = created["id"]
            assert not created["has_model"], created
            print("chat created:", chat_id)

            bad_target = client.post(
                f"{BASE}/api/chats/{chat_id}/messages",
                data={"text": "x", "target": "bogus"})
            assert bad_target.status_code == 400, bad_target.status_code
            empty = client.post(
                f"{BASE}/api/chats/{chat_id}/messages",
                data={"text": "  ", "target": "headless"})
            assert empty.status_code == 400, empty.status_code
            print("bad target / empty text -> 400")

            resp = client.post(
                f"{BASE}/api/chats/{chat_id}/messages",
                data={"text": "plat datar 100x60x10 dengan 4 lubang",
                      "target": "headless"}, timeout=30)
            assert resp.status_code == 200, resp.text
            session = wait_for_turn(client, chat_id, 0)
            assert len(session["messages"]) == 2, session["messages"]
            assert session["spec"]["name"] == "plate_with_holes", session["spec"]
            v1 = session["versions"][0]
            assert v1["n"] == 1 and v1["target"] == "headless", v1
            assert v1["verified"] is True, v1
            assert (any(f.endswith(".blend") for f in v1["files"])
                    and "preview.png" in v1["files"]), v1
            msg = session["messages"][1]
            assert msg["status"] == "done" and "v1 (headless)" in msg["text"], msg
            print("turn 1 headless ok, verified, files:", v1["files"])

            preview = client.get(
                f"{BASE}/api/chats/{chat_id}/files/v1/preview.png")
            assert preview.status_code == 200, preview.status_code
            assert preview.headers["content-type"] == "image/png"
            assert len(preview.content) > 10000, len(preview.content)
            print("version preview download ok:", len(preview.content), "bytes")

            assert client.get(
                f"{BASE}/api/chats/{chat_id}/files/vx/preview.png").status_code == 400
            assert client.get(
                f"{BASE}/api/chats/{chat_id}/files/v1/..").status_code in (400, 404)
            print("bad version/filename rejected")

            resp = client.post(
                f"{BASE}/api/chats/{chat_id}/messages",
                data={"text": "tambah 2 lubang lagi", "target": "headless"},
                files={"images": ("ref.png", TINY_PNG, "image/png")},
                timeout=30)
            assert resp.status_code == 200, resp.text
            session = wait_for_turn(client, chat_id, 2)
            assert len(session["messages"]) == 4, len(session["messages"])
            v2 = session["versions"][1]
            assert v2["n"] == 2 and "ref_1.png" in v2["files"], v2
            assert session["messages"][3]["status"] == "done"
            print("turn 2 revise + image upload ok, ref saved:", v2["files"])

            resp = client.post(
                f"{BASE}/api/chats/{chat_id}/messages",
                data={"text": "ubah di live", "target": "live"}, timeout=30)
            assert resp.status_code == 200, resp.text
            session = wait_for_turn(client, chat_id, 4)
            err_msg = session["messages"][5]
            assert err_msg["status"] == "error", err_msg
            assert session.get("pending") is None
            print("live turn with no server -> clean error:",
                  str(err_msg["error"])[:70])

            chats = client.get(f"{BASE}/api/chats").json()["chats"]
            assert any(c["id"] == chat_id and c["versions"] == 2
                       for c in chats), chats
            print("chat list ok")

        # --- real headless live server, driven via a chat live turn ---
        tmpdir = Path(tempfile.mkdtemp(prefix="threedriver-chat-test-"))
        token_file = tmpdir / "live_token"
        token_file.write_text(TEST_TOKEN, encoding="utf-8")
        blender = str(blender_exe) if blender_exe.is_file() else "blender"
        cmd = [blender, "-b", "--python",
               str(ROOT / "threedriver" / "bpy" / "live_test_driver.py"),
               "--", "--addon-dir", str(ROOT / "addons"),
               "--port", str(LIVE_PORT), "--token-file", str(token_file),
               "--blend", str(tmpdir / "scene.blend"),
               "--max-seconds", "180"]
        blender_proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1)
        line_queue: queue.Queue = queue.Queue()

        def _reader() -> None:
            try:
                for line in blender_proc.stdout:  # type: ignore[union-attr]
                    line_queue.put(line)
            finally:
                line_queue.put(None)

        threading.Thread(target=_reader, daemon=True).start()
        ready = False
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                line = line_queue.get(timeout=0.5)
            except queue.Empty:
                if blender_proc.poll() is not None:
                    break
                continue
            if line is None:
                break
            if "LIVE READY" in line:
                ready = True
                break
        assert ready, "live server did not report LIVE READY"
        print("headless live server up")

        with httpx.Client(timeout=30, trust_env=False) as client:
            saved = client.post(
                f"{BASE}/api/settings",
                json={"live_port": LIVE_PORT, "live_token": TEST_TOKEN}).json()
            assert saved["live_port"] == LIVE_PORT, saved

            vp = client.post(f"{BASE}/api/live/viewport",
                             json={"shading": "WIREFRAME"}).json()
            assert vp["ok"] and vp.get("viewports", 0) >= 1, vp
            print("set_viewport ok (property set, viewports:",
                  vp["viewports"], ")")
            assert client.post(f"{BASE}/api/live/viewport",
                               json={"shading": "NOPE"}).status_code == 400

            session = client.get(f"{BASE}/api/chats/{chat_id}").json()
            prev = len(session["messages"])
            resp = client.post(
                f"{BASE}/api/chats/{chat_id}/messages",
                data={"text": "terapkan di live", "target": "live"},
                timeout=30)
            assert resp.status_code == 200, resp.text
            session = wait_for_turn(client, chat_id, prev)
            live_msg = session["messages"][-1]
            assert live_msg["status"] == "done", live_msg
            assert "screenshot" in live_msg["text"].lower(), live_msg
            assert "langsung" in live_msg["text"], live_msg
            assert "Ctrl+S" in live_msg["text"], live_msg
            live_ver = session["versions"][-1]
            assert live_ver["target"] == "live", live_ver
            assert live_ver["mode"] == "direct", live_ver
            assert "generated.py" in live_ver["files"], live_ver
            assert not any(f.endswith(".blend")
                           for f in live_ver["files"]), live_ver
            print("live direct-mod turn ok (no new .blend, "
                  "screenshot note kept)")

            # One-shot live job (Buat model + checkbox Live): same direct
            # modification, no chat session.
            resp = client.post(f"{BASE}/api/jobs",
                               data={"prompt": "x", "target": "bogus"})
            assert resp.status_code == 400, resp.text
            print("bad job target -> 400")
            resp = client.post(f"{BASE}/api/jobs",
                               data={"prompt": "plat 100x60x10",
                                     "target": "live"}, timeout=30)
            assert resp.status_code == 200, resp.text
            live_job_id = resp.json()["job_id"]
            deadline = time.time() + 60
            while True:
                job = client.get(f"{BASE}/api/jobs/{live_job_id}").json()
                if job["status"] in ("done", "error"):
                    break
                assert time.time() < deadline, job
                time.sleep(0.5)
            assert job["status"] == "done", job
            assert job["target"] == "live", job
            assert "generated.py" in job["result"]["files"], job
            assert not any(f.endswith(".blend")
                           for f in job["result"]["files"]), job
            print("one-shot live job ok:", live_job_id)

        # The stub mod script scaled the live Cube x2 in place: prove the
        # open scene itself changed, not a new file.
        with LiveClient(port=LIVE_PORT, token=TEST_TOKEN,
                        timeout=10.0) as lc:
            scene = lc.get_scene()["result"]
        cube = next(o for o in scene["objects"] if o["name"] == "Cube")
        assert cube["dimensions"] == [4.0, 4.0, 4.0], cube
        print("live scene actually modified: Cube dimensions",
              cube["dimensions"])

        with LiveClient(port=LIVE_PORT, token=TEST_TOKEN,
                        timeout=10.0) as lc:
            assert lc.shutdown()["ok"]
        try:
            rc = blender_proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            blender_proc.kill()
            print("FAIL: Blender did not exit after shutdown")
            return 1
        assert rc == 0, f"Blender exit code {rc}"
        print("clean shutdown ok")
        print("CHAT SMOKE PASSED")
        return 0
    except AssertionError as exc:
        print("FAIL:", exc)
        return 1
    finally:
        if blender_proc is not None and blender_proc.poll() is None:
            blender_proc.terminate()
            try:
                blender_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                blender_proc.kill()
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    raise SystemExit(main())
