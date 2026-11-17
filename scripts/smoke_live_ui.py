#!/usr/bin/env python3
"""Smoke-test the web UI live tab + headless test end to end.

Starts the server on a scratch port with a scratch settings file, checks
/api/test-blender, live settings save/redaction, and the live endpoints
first with no server (graceful ok:false), then against a real headless
Blender running the live addon (status, exec, error isolation, screenshot
headless failure mode, backup-before-execute through the web UI path).

What this does NOT prove (needs the laptop GUI): viewport screenshots
with a real GL context. Headless, screenshot must fail with the clean
"needs a Blender GUI viewport" error, not a crash.

Usage:  python scripts/smoke_live_ui.py
Exit 0 = all checks passed.
"""

from __future__ import annotations

import json
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

from threedriver.headless import (  # noqa: E402
    BlenderNotFound,
    blender_arg_from_settings,
    resolve_blender,
)
from threedriver.live_client import LiveClient  # noqa: E402

PORT = 18766
BASE = f"http://127.0.0.1:{PORT}"
LIVE_PORT = 19878
TEST_TOKEN = "live-ui-test-token-do-not-use-in-production"


def main() -> int:
    import httpx

    settings_path = ROOT / "tmp" / "smoke-live-settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    if settings_path.exists():
        settings_path.unlink()
    # The runtime Blender is not on PATH; point the server at it explicitly
    # (on the laptop this is the Settings-page value / standard install).
    blender_exe = Path.home() / "workspace" / "tools" / "blender-current" / "blender"
    env = dict(os.environ, THREEDRIVER_SETTINGS=str(settings_path))
    if blender_exe.is_file():
        env["THREEDRIVER_BLENDER"] = str(blender_exe)

    # Unit checks: mode helper + legacy-settings migration (no server).
    from threedriver.config import load_settings

    assert blender_arg_from_settings(
        {"blender_mode": "auto", "blender": str(blender_exe)}) is None
    assert blender_arg_from_settings(
        {"blender_mode": "custom", "blender": str(blender_exe)}) == str(blender_exe)
    for bad in ({"blender_mode": "custom", "blender": ""},
                {"blender_mode": "custom", "blender": "/no/such/x.exe"}):
        try:
            blender_arg_from_settings(bad)
        except BlenderNotFound:
            pass
        else:
            raise AssertionError(f"should have raised for {bad}")
    legacy = ROOT / "tmp" / "legacy-settings.json"
    legacy.write_text(json.dumps({"blender": str(blender_exe)}), encoding="utf-8")
    assert load_settings(legacy)["blender_mode"] == "custom", "migration failed"
    fresh = ROOT / "tmp" / "fresh-settings.json"
    fresh.write_text(json.dumps({"provider": "stub"}), encoding="utf-8")
    assert load_settings(fresh)["blender_mode"] == "auto"
    print("mode helper + legacy migration ok")
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

            tb = client.post(f"{BASE}/api/test-blender", json={}).json()
            assert tb["ok"], tb
            assert tb["version"].startswith("Blender"), tb
            print("test-blender ok:", tb["path"], "|", tb["version"])

            tb2 = client.post(
                f"{BASE}/api/test-blender",
                json={"blender": str(blender_exe)}).json()
            assert tb2["ok"] and tb2["path"] == str(blender_exe), tb2
            print("test-blender explicit path override ok")

            mode = client.post(
                f"{BASE}/api/settings",
                json={"blender_mode": "custom",
                      "blender": str(blender_exe)}).json()
            assert mode["blender_mode"] == "custom", mode
            # Payload overrides beat the server env var, so these prove the
            # mode logic itself (env THREEDRIVER_BLENDER would otherwise
            # override the saved file value by design).
            tb3 = client.post(
                f"{BASE}/api/test-blender",
                json={"blender": str(blender_exe),
                      "blender_mode": "custom"}).json()
            assert (tb3["ok"] and tb3["path"] == str(blender_exe)
                    and tb3["mode"] == "custom"), tb3
            print("blender_mode custom ok:", tb3["path"])

            tb4 = client.post(
                f"{BASE}/api/test-blender",
                json={"blender": "/no/such/blender.exe",
                      "blender_mode": "custom"}).json()
            assert not tb4["ok"] and "/no/such/blender.exe" in tb4["error"], tb4
            print("custom missing path -> ok:false, no silent fallback")

            tb5 = client.post(
                f"{BASE}/api/test-blender",
                json={"blender": "", "blender_mode": "custom"}).json()
            assert not tb5["ok"] and "no path is set" in tb5["error"], tb5
            print("custom empty path -> ok:false")

            badmode = client.post(f"{BASE}/api/settings",
                                  json={"blender_mode": "bogus"})
            assert badmode.status_code == 400, badmode.status_code
            print("blender_mode bogus -> 400")

            client.post(f"{BASE}/api/settings", json={"blender_mode": "auto"})
            tb6 = client.post(f"{BASE}/api/test-blender", json={}).json()
            assert tb6["ok"] and tb6["mode"] == "auto", tb6
            print("blender_mode auto ok (env fallback)")

            saved = client.post(
                f"{BASE}/api/settings",
                json={"live_port": LIVE_PORT, "live_token": TEST_TOKEN}).json()
            assert saved["live_port"] == LIVE_PORT, saved
            assert saved["has_live_token"] is True, saved
            assert TEST_TOKEN not in str(saved), "token leaked in response"
            assert "••••" in saved["live_token"], saved
            print("live settings saved, token masked")

            kept = client.post(
                f"{BASE}/api/settings", json={"live_token": ""}).json()
            assert kept["has_live_token"] is True, kept
            print("blank live_token keeps the stored token")

            st = client.get(f"{BASE}/api/live/status").json()
            assert not st["ok"], st
            print("live status with no server -> ok:false:",
                  str(st["error"])[:60])

            shot404 = client.get(f"{BASE}/api/live/viewport.png")
            assert shot404.status_code == 404, shot404.status_code
            print("viewport.png with no shot -> 404")

            empty = client.post(f"{BASE}/api/live/exec",
                                json={"code": "  "})
            assert empty.status_code == 400, empty.status_code
            print("live exec empty code -> 400")

        # --- real headless Blender + live addon, driven via the web UI ---
        tmpdir = Path(tempfile.mkdtemp(prefix="threedriver-live-ui-"))
        token_file = tmpdir / "live_token"
        token_file.write_text(TEST_TOKEN, encoding="utf-8")
        blender = str(blender_exe) if blender_exe.is_file() else resolve_blender()
        cmd = [blender, "-b", "--python",
               str(ROOT / "threedriver" / "bpy" / "live_test_driver.py"),
               "--", "--addon-dir", str(ROOT / "addons"),
               "--port", str(LIVE_PORT), "--token-file", str(token_file),
               "--blend", str(tmpdir / "scene.blend"),
               "--max-seconds", "120"]
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
            st = client.get(f"{BASE}/api/live/status").json()
            assert st["ok"], st
            assert st["blender_version"].startswith("5.2"), st
            names = [o["name"] for o in st["scene"]["objects"]]
            assert "Cube" in names, names
            print("live status ok, Blender", st["blender_version"],
                  "objects:", names)

            ex = client.post(
                f"{BASE}/api/live/exec",
                json={"code": "print('HELLO-LIVE-UI')\n__result__ = 40 + 2"},
                timeout=60).json()
            assert ex["ok"], ex
            assert "HELLO-LIVE-UI" in ex.get("stdout", ""), ex
            assert ex.get("result") == "42", ex
            print("live exec ok (stdout + __result__ via web UI)")

            backups = list(tmpdir.glob("*.3driver-backup-*.blend"))
            assert backups, "no backup .blend written before web UI exec"
            print("backup-before-execute ok:", backups[0].name)

            boom = client.post(
                f"{BASE}/api/live/exec",
                json={"code": "raise ValueError('boom-ui-test')"},
                timeout=60).json()
            assert not boom["ok"] and "boom-ui-test" in boom.get("error", ""), boom
            st2 = client.get(f"{BASE}/api/live/status").json()
            assert st2["ok"], "server died after a failing exec"
            print("error isolation ok (bad code via web UI, server survived)")

            shot = client.post(f"{BASE}/api/live/screenshot").json()
            assert not shot["ok"] and "viewport" in shot.get("error", ""), shot
            print("screenshot headless failure mode ok:",
                  shot["error"][:60])

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
        print("LIVE UI SMOKE PASSED")
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
