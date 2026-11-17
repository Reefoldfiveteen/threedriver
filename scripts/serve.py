#!/usr/bin/env python3
"""Start the 3Driver local web UI (127.0.0.1 only, never exposed).

Usage:
    python scripts/serve.py [--host 127.0.0.1] [--port 8765]

Then open http://127.0.0.1:8765 in a browser. Run from the repo root so
settings.json and tmp/jobs land next to the code.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"WARNING: binding to {args.host} exposes the UI beyond this "
              "machine. Only do this on a trusted network.", file=sys.stderr)
    import uvicorn

    from threedriver.webapp import app

    print(f"3Driver UI: http://{args.host}:{args.port}  (Ctrl+C untuk berhenti)")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
