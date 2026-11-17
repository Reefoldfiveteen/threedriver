#!/usr/bin/env python3
"""End-to-end test of the 3Driver Live addon, driven headless.

Starts real Blender 5.2 in background mode with the addon loaded, pumps its
command queue on the main thread (the same pump_once() the GUI timer calls),
and drives it over the socket with the real client library + CLI:

  ping, execute_code (stdout + __result__), get_scene, error isolation,
  bad-token rejection, screenshot failure mode (no viewport headless),
  backup-before-execute file, CLI ping, clean shutdown.

What this does NOT prove (needs the laptop GUI): the bpy.app.timers event
loop firing the pump, and viewport screenshots. Those two steps are manual
checks in README.md.

Usage:  python scripts/test_live_headless.py [--blender PATH] [--port N]
Exit 0 = all checks passed.
"""

from __future__ import annotations

import argparse
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from threedriver.headless import resolve_blender  # noqa: E402
from threedriver.live_client import LiveClient  # noqa: E402

TEST_TOKEN = "live-test-token-do-not-use-in-production"
PORT_DEFAULT = 19877


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--blender", default=None)
    p.add_argument("--port", type=int, default=PORT_DEFAULT)
    return p.parse_args(argv)


def _reader(proc: subprocess.Popen, out_queue: queue.Queue) -> None:
    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            out_queue.put(line)
    finally:
        out_queue.put(None)


def main(argv=None) -> int:
    args = parse_args(argv)
    blender = resolve_blender(args.blender)
    tmpdir = Path(tempfile.mkdtemp(prefix="threedriver-live-test-"))
    token_file = tmpdir / "live_token"
    token_file.write_text(TEST_TOKEN, encoding="utf-8")
    blend = tmpdir / "scene.blend"
    cmd = [blender, "-b", "--python",
           str(ROOT / "threedriver" / "bpy" / "live_test_driver.py"),
           "--", "--addon-dir", str(ROOT / "addons"), "--port", str(args.port),
           "--token-file", str(token_file), "--blend", str(blend),
           "--max-seconds", "120"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)
    lines: list[str] = []
    line_queue: queue.Queue = queue.Queue()
    threading.Thread(target=_reader, args=(proc, line_queue),
                     daemon=True).start()

    def wait_for(marker: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = line_queue.get(timeout=0.5)
            except queue.Empty:
                if proc.poll() is not None:
                    return False
                continue
            if line is None:
                return False
            lines.append(line)
            if marker in line:
                return True
        return False

    def drain() -> None:
        while True:
            try:
                line = line_queue.get_nowait()
            except queue.Empty:
                return
            if line is None:
                return
            lines.append(line)

    def fail(message: str) -> int:
        drain()
        print(f"FAIL: {message}")
        print("--- blender output tail ---")
        print("".join(lines[-40:]))
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        return 1

    try:
        if not wait_for("LIVE READY", 60):
            return fail("server did not report LIVE READY")
        print("server up (headless Blender + addon)")

        with LiveClient(port=args.port, token=TEST_TOKEN,
                        timeout=30.0) as client:
            resp = client.ping()
            assert resp["ok"] and resp["result"] == "pong", resp
            print("ping ok, Blender", resp.get("blender_version"))

            resp = client.execute(
                "import bpy\n"
                "mesh = bpy.data.meshes.new('TestCube')\n"
                "mesh.from_pydata([(-1,-1,0),(1,-1,0),(1,1,0),(-1,1,0),"
                "(-1,-1,2),(1,-1,2),(1,1,2),(-1,1,2)], [], "
                "[(0,3,2,1),(4,5,6,7),(0,1,5,4),(1,2,6,5),(2,3,7,6),(3,0,4,7)])\n"
                "mesh.update()\n"
                "obj = bpy.data.objects.new('TestCube', mesh)\n"
                "bpy.context.scene.collection.objects.link(obj)\n"
                "print('HELLO-CUBE')\n"
                "__result__ = 40 + 2\n")
            assert resp["ok"], resp
            assert "HELLO-CUBE" in resp.get("stdout", ""), resp
            assert resp.get("result") == "42", resp
            print("execute_code ok (stdout + __result__ captured)")

            backups = list(tmpdir.glob("*.3driver-backup-*.blend"))
            assert backups, "no backup .blend was written before execute"
            print("backup-before-execute ok:", backups[0].name)

            resp = client.get_scene()
            assert resp["ok"], resp
            names = [o["name"] for o in resp["result"]["objects"]]
            assert "TestCube" in names, names
            print("get_scene ok, objects:", names)

            resp = client.execute("raise ValueError('boom-test')")
            assert not resp["ok"] and "boom-test" in resp.get("error", ""), resp
            resp = client.ping()
            assert resp["ok"], "server died after a failing command"
            print("error isolation ok (bad code reported, server survived)")

            resp = client.screenshot(str(tmpdir / "shot.png"))
            assert not resp["ok"] and "viewport" in resp.get("error", ""), resp
            print("screenshot headless failure mode ok:",
                  resp["error"][:60])

        with LiveClient(port=args.port, token="wrong-token",
                        timeout=10.0) as bad:
            resp = bad.ping()
            assert not resp["ok"] and "token" in resp.get("error", ""), resp
        print("bad-token rejection ok")

        cli = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "live_cmd.py"),
             "--port", str(args.port), "--token-file", str(token_file), "ping"],
            capture_output=True, text=True, timeout=30)
        assert cli.returncode == 0 and "pong" in cli.stdout, cli.stdout + cli.stderr
        print("CLI ping ok")

        with LiveClient(port=args.port, token=TEST_TOKEN,
                        timeout=10.0) as client:
            resp = client.shutdown()
            assert resp["ok"], resp
        try:
            rc = proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            return fail("Blender did not exit after shutdown")
        drain()
        assert rc == 0, f"Blender exit code {rc}"
        assert any("LIVE DONE" in line for line in lines), "no LIVE DONE"
        print("clean shutdown ok")
    except AssertionError as exc:
        return fail(str(exc) or "assertion failed")
    except Exception as exc:  # noqa: BLE001 - report + cleanup, exit 1
        return fail(f"{type(exc).__name__}: {exc}")
    print("LIVE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
