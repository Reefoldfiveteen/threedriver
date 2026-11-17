#!/usr/bin/env python3
"""Run the prompt -> spec -> script -> headless -> verify pipeline from CLI.

Usage:
    python scripts/run_prompt.py "flat plate 100x60x10 with 4 holes"
    python scripts/run_prompt.py "bracket from this photo" --image ref.jpg

Settings come from settings.json / environment (see threedriver/config.py).
With no API key configured, the default stub provider builds the demo plate.
Prints a JSON summary and exits 0 on success, 1 on job failure.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from threedriver.config import load_settings  # noqa: E402
from threedriver.pipeline import run_pipeline  # noqa: E402


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("prompt", help="plain-language description of the part")
    p.add_argument("--image", action="append", default=[],
                   help="reference image (repeatable, max 5)")
    p.add_argument("--settings", default=None, help="settings file path")
    p.add_argument("--provider", default=None, help="override provider id")
    p.add_argument("--jobs-root", default=None)
    p.add_argument("--timeout", type=float, default=None)
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if len(args.image) > 5:
        print("error: at most 5 reference images", file=sys.stderr)
        return 2
    images = []
    for path in args.image:
        data = Path(path).read_bytes()
        mime = mimetypes.guess_type(path)[0] or "image/png"
        images.append((data, mime))
    settings = load_settings(args.settings)
    if args.provider:
        settings["provider"] = args.provider
    if args.timeout:
        settings["timeout"] = args.timeout
    print(f"provider: {settings['provider']}  model: {settings.get('model')}",
          file=sys.stderr)

    def on_status(stage: str) -> None:
        print(f"... {stage}", file=sys.stderr)

    result = run_pipeline(
        prompt=args.prompt, images=images, settings=settings,
        jobs_root=args.jobs_root, on_status=on_status)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result["status"] == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
