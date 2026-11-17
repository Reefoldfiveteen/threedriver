#!/usr/bin/env python3
"""Test the auto-fix retry loop end to end with a fake provider.

The fake provider answers the SPEC step with the plate spec, the FIRST
codegen with a script that uses the wrong bmesh kwarg (cap_end — the
exact error a real model produced: "create_cone: keyword 'cap_end' is
invalid for this operator"), and the FIX call with a working script.
Both paths run against real Blender: the headless path via
run_pipeline, the live path via run_live_build against a real headless
live addon (the open scene itself must change, no new .blend).

Usage:  python scripts/test_retry.py
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

from threedriver.live_client import LiveClient  # noqa: E402
from threedriver.providers.stub import PLATE_SPEC  # noqa: E402

LIVE_PORT = 19881
TEST_TOKEN = "test-retry-token"

_BAD_SCRIPT = '''"""Fails with the exact error a real model produced."""
import bmesh

bm = bmesh.new()
bmesh.ops.create_cone(bm, cap_end=True, segments=16,
                      radius1=10.0, radius2=5.0, depth=20.0)
'''

_GOOD_MOD = '''"""Good mod script: scale the scene's Cube x2, in place."""
import bpy

cube = bpy.data.objects.get("Cube")
if cube is None:
    raise RuntimeError("no Cube in scene")
cube.scale = (2.0, 2.0, 2.0)
bpy.context.view_layer.update()
__result__ = {"scaled": cube.name, "dimensions": list(cube.dimensions)}
'''

_GOOD_HEADLESS = (ROOT / "examples" / "plate_with_holes.py").read_text(
    encoding="utf-8")


class FailOnceProvider:
    """SPEC -> plate spec; first codegen -> bad script; fix -> good."""

    def __init__(self) -> None:
        self.fix_calls = 0

    def complete(self, messages, *, model="stub", temperature=0.2,
                 max_tokens=4096, json_mode=False):
        system_text = " ".join(
            part.get("text", "")
            for msg in messages if msg["role"] == "system"
            for part in msg["parts"] if part["type"] == "text"
        )
        if json_mode:
            return json.dumps(PLATE_SPEC, indent=2)
        if "that FAILED when Blender ran it" in system_text:
            self.fix_calls += 1
            good = _GOOD_MOD if "live addon" in system_text else _GOOD_HEADLESS
            return "```python\n" + good + "\n```"
        return "```python\n" + _BAD_SCRIPT + "\n```"

    def list_models(self):
        return ["stub"]


def test_headless(tmp: Path) -> None:
    import threedriver.pipeline as pipeline

    fake = FailOnceProvider()
    orig = pipeline.get_provider
    pipeline.get_provider = lambda settings: fake
    try:
        result = pipeline.run_pipeline(
            prompt="plat datar 100x60x10 dengan 4 lubang",
            settings={"provider": "stub", "model": "stub", "timeout": 300},
            jobs_root=tmp / "jobs")
    finally:
        pipeline.get_provider = orig
    assert result["status"] == "done", result
    assert result["attempts"] == 2, result
    assert fake.fix_calls == 1
    job_dir = Path(result["job_dir"])
    failed = job_dir / "generated_attempt1.py"
    assert failed.is_file(), sorted(p.name for p in job_dir.iterdir())
    assert "cap_end" in failed.read_text(encoding="utf-8")
    assert (job_dir / "plate.blend").is_file()
    assert (job_dir / "generated.py").is_file()
    print("headless retry ok: attempts=2, failed script kept, build done")


def test_live(tmp: Path, blender_exe: Path) -> None:
    token_file = tmp / "live_token"
    token_file.write_text(TEST_TOKEN, encoding="utf-8")
    cmd = [str(blender_exe), "-b", "--python",
           str(ROOT / "threedriver" / "bpy" / "live_test_driver.py"),
           "--", "--addon-dir", str(ROOT / "addons"),
           "--port", str(LIVE_PORT), "--token-file", str(token_file),
           "--blend", str(tmp / "scene.blend"), "--max-seconds", "180"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)
    line_queue: queue.Queue = queue.Queue()

    def _reader() -> None:
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
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
            if proc.poll() is not None:
                break
            continue
        if line is None:
            break
        if "LIVE READY" in line:
            ready = True
            break
    assert ready, "live server did not report LIVE READY"
    try:
        import threedriver.chat as chat

        fake = FailOnceProvider()
        orig = chat.get_provider
        chat.get_provider = lambda settings: fake
        try:
            result = chat.run_live_build(
                prompt="buat kerucut di atas kubus",
                images=[],
                settings={"provider": "stub", "model": "stub",
                          "live_port": LIVE_PORT, "live_token": TEST_TOKEN},
                job_dir=tmp / "livejob")
        finally:
            chat.get_provider = orig
        assert result["status"] == "done", result
        assert result["attempts"] == 2, result
        assert fake.fix_calls == 1
        job_dir = Path(result["job_dir"])
        assert (job_dir / "generated.py").is_file()
        assert "cap_end" not in (job_dir / "generated.py").read_text(
            encoding="utf-8")
        assert not list(job_dir.glob("*.blend")), "live must not write a file"
        with LiveClient(port=LIVE_PORT, token=TEST_TOKEN) as lc:
            scene = lc.get_scene()["result"]
        cube = next(o for o in scene["objects"] if o["name"] == "Cube")
        assert cube["dimensions"] == [4.0, 4.0, 4.0], cube
        print("live retry ok: attempts=2, open scene modified, no new file")
    finally:
        try:
            with LiveClient(port=LIVE_PORT, token=TEST_TOKEN) as lc:
                lc.shutdown()
        except Exception:
            pass
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> int:
    blender_exe = Path.home() / "workspace" / "tools" / "blender-current" / "blender"
    if blender_exe.is_file():
        os.environ.setdefault("THREEDRIVER_BLENDER", str(blender_exe))
    else:
        blender_exe = Path("blender")
    tmp = Path(tempfile.mkdtemp(prefix="threedriver-retry-"))
    test_headless(tmp)
    test_live(tmp, blender_exe)
    print("RETRY TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
