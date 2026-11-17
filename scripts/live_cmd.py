#!/usr/bin/env python3
"""Talk to a running 3Driver Live server from the terminal.

Usage (start the server in Blender first: N-panel > 3Driver > Start Server):
    python scripts/live_cmd.py ping
    python scripts/live_cmd.py scene
    python scripts/live_cmd.py exec my_code.py
    python scripts/live_cmd.py exec - < my_code.py
    python scripts/live_cmd.py shot /tmp/view.png
    python scripts/live_cmd.py viewport WIREFRAME
    python scripts/live_cmd.py shutdown

Prints the JSON response. Exit code 1 when the server reports ok=false.
Token: --token, THREEDRIVER_LIVE_TOKEN, or ~/.threedriver/live_token.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from threedriver.live_client import DEFAULT_PORT, LiveClient, LiveError  # noqa: E402


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--token", default=None)
    p.add_argument("--token-file", default=None)
    p.add_argument("--timeout", type=float, default=60.0)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("ping")
    sub.add_parser("scene")
    p_exec = sub.add_parser("exec", help="run a Python file (or - for stdin)")
    p_exec.add_argument("path")
    p_shot = sub.add_parser("shot", help="viewport screenshot to a file")
    p_shot.add_argument("filepath")
    p_view = sub.add_parser(
        "viewport", help="set 3D viewport shading (WIREFRAME/SOLID/MATERIAL)")
    p_view.add_argument("shading")
    sub.add_parser("shutdown")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        client = LiveClient(host=args.host, port=args.port, token=args.token,
                            token_file=args.token_file, timeout=args.timeout)
    except LiveError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        with client:
            if args.command == "ping":
                resp = client.ping()
            elif args.command == "scene":
                resp = client.get_scene()
            elif args.command == "exec":
                code = (sys.stdin.read() if args.path == "-"
                        else Path(args.path).read_text(encoding="utf-8"))
                resp = client.execute(code)
            elif args.command == "shot":
                resp = client.screenshot(args.filepath)
            elif args.command == "viewport":
                resp = client.set_viewport(args.shading.upper())
            elif args.command == "shutdown":
                resp = client.shutdown()
            else:
                raise AssertionError(args.command)
    except LiveError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(resp, indent=2, default=str))
    return 0 if resp.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
