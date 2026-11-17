#!/usr/bin/env python3
"""Smoke-test the web UI end to end with the stub provider.

Starts the server on a scratch port with a scratch settings file, submits
one job (prompt + a 1x1 PNG), polls until it finishes, checks the report,
the probe verdict, and the preview download, then stops the server.

Usage:  python scripts/smoke_web.py
Exit 0 = all checks passed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PORT = 18765
BASE = f"http://127.0.0.1:{PORT}"

# Smallest valid PNG (1x1, transparent).
TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6360000000020001e221bc330000000049454e44ae426082"
)


def main() -> int:
    import httpx

    settings_path = ROOT / "tmp" / "smoke-settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    if settings_path.exists():
        settings_path.unlink()
    env = dict(os.environ, THREEDRIVER_SETTINGS=str(settings_path))
    blender_exe = Path.home() / "workspace" / "tools" / "blender-current" / "blender"
    if blender_exe.is_file():
        # The runtime's Blender is not on PATH; point the server at it.
        env["THREEDRIVER_BLENDER"] = str(blender_exe)
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "serve.py"), "--port", str(PORT)],
        cwd=ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        with httpx.Client(timeout=10, trust_env=False) as client:
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

            providers = client.get(f"{BASE}/api/providers").json()["providers"]
            assert any(p["id"] == "stub" for p in providers), "stub missing"
            print("providers ok:", [p["id"] for p in providers])

            page = client.get(f"{BASE}/").text
            for marker in ("deleteChatBtn", "deleteAllChatsBtn", "chatCount",
                           "jobLive", "livePanel"):
                assert marker in page, marker
            print("page contains delete controls + merged-tab ids")

            saved = client.post(f"{BASE}/api/settings",
                                json={"provider": "stub"}).json()
            assert saved["provider"] == "stub", saved
            assert "api_key" not in saved or "••••" in saved["api_key"] or not saved["api_key"]
            print("settings ok (key masked)")

            test = client.post(f"{BASE}/api/test-provider", json={}).json()
            assert test["ok"], test
            print("test-provider ok:", test["reply"])

            resp = client.post(
                f"{BASE}/api/jobs",
                data={"prompt": "plat datar 100x60x10 dengan 4 lubang"},
                files={"images": ("tiny.png", TINY_PNG, "image/png")},
                timeout=30)
            assert resp.status_code == 200, resp.text
            job_id = resp.json()["job_id"]
            print("job:", job_id)

            deadline = time.monotonic() + 180
            job = {}
            while time.monotonic() < deadline:
                job = client.get(f"{BASE}/api/jobs/{job_id}").json()
                if job["status"] in ("done", "error"):
                    break
                time.sleep(2)
            assert job["status"] == "done", job.get("error", job)
            result = job["result"]
            assert result["verified"] is True, result.get("probe")
            assert result["report"]["status"] == "ok"
            bbox = result["report"]["objects"][0]["bbox"]["size"]
            assert bbox == [100.0, 60.0, 10.0], bbox
            print("job done, verified, bbox:", bbox)

            preview = client.get(f"{BASE}/api/jobs/{job_id}/files/preview.png")
            assert preview.status_code == 200, preview.status_code
            assert preview.headers["content-type"] == "image/png"
            assert len(preview.content) > 10000, len(preview.content)
            print("preview download ok:", len(preview.content), "bytes")

            report = client.get(f"{BASE}/api/jobs/{job_id}/files/report.json")
            assert report.status_code == 200
            assert report.json()["status"] == "ok"
            print("report download ok")
        print("SMOKE PASSED")
        return 0
    except AssertionError as exc:
        print("FAIL:", exc)
        return 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
