#!/usr/bin/env python3
"""Package the 3Driver Live addon as a .zip for Blender's Install from Disk.

Usage:
    python scripts/package_addon.py [--out tmp/threedriver_live.zip]

Then in Blender: Edit > Preferences > Add-ons > Install from Disk, pick the
.zip, enable "3Driver Live", and use the N-panel > 3Driver tab.
"""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--src", default=str(ROOT / "addons" / "threedriver_live"))
    p.add_argument("--out", default=str(ROOT / "tmp" / "threedriver_live.zip"))
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    src = Path(args.src)
    out = Path(args.out)
    if not (src / "__init__.py").is_file():
        print(f"error: no addon at {src}", file=__import__("sys").stderr)
        return 2
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(src.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                zf.write(path, f"threedriver_live/{path.relative_to(src)}")
    print(f"wrote {out} ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
