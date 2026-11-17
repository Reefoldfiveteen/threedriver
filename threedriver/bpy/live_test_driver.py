"""Headless driver for the 3Driver Live addon test.

Runs inside `blender -b`: opens (or creates) a .blend so backup-before-
execute has a real filepath, registers the addon, starts the server WITHOUT
the GUI timer, and pumps the command queue manually — the same pump_once()
the bpy.app.timers callback calls in GUI Blender, on the same main thread.
Exits when a shutdown command arrives or --max-seconds elapses.

Usage (from scripts/test_live_headless.py, not by hand):
    blender -b --python live_test_driver.py -- --addon-dir DIR --port N
        --token-file FILE --blend FILE.blend [--max-seconds N]
"""

import argparse
import sys
import time
from pathlib import Path


def parse_args(argv):
    args = argv[argv.index("--") + 1:] if "--" in argv else []
    p = argparse.ArgumentParser()
    p.add_argument("--addon-dir", required=True)
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--token-file", required=True)
    p.add_argument("--blend", default=None)
    p.add_argument("--max-seconds", type=float, default=120.0)
    return p.parse_args(args)


def main():
    import bpy  # noqa: PLC0415 - only exists inside Blender

    args = parse_args(sys.argv)
    sys.path.insert(0, args.addon_dir)
    import threedriver_live

    if args.blend:
        blend = Path(args.blend)
        if blend.is_file():
            bpy.ops.wm.open_mainfile(filepath=str(blend))
        else:
            blend.parent.mkdir(parents=True, exist_ok=True)
            bpy.ops.wm.save_as_mainfile(filepath=str(blend))

    threedriver_live.register()
    try:
        threedriver_live.start_server(port=args.port,
                                      token_file=args.token_file,
                                      use_timer=False)
    except Exception as exc:  # noqa: BLE001 - test reads the failure line
        print(f"LIVE FAILED TO START: {exc}", flush=True)
        raise
    print("LIVE READY", flush=True)
    deadline = time.monotonic() + args.max_seconds
    while time.monotonic() < deadline:
        nxt = threedriver_live.pump_once()
        if nxt is None:
            break
        time.sleep(0.05)
    threedriver_live.stop_server()
    print("LIVE DONE", flush=True)


if __name__ == "__main__":
    main()
