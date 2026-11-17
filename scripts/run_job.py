#!/usr/bin/env python3
"""Run one bpy script as an isolated headless Blender job.

Usage:
    python scripts/run_job.py examples/plate_with_holes.py --verify

Creates tmp/jobs/<job_id>/, runs the script in a fresh `blender -b`
process, and prints the report summary. With --verify, a second headless
process re-opens the saved .blend and re-derives the geometry facts
(bounding box, counts, closed-solid check, through-hole raycasts) from
the file itself; the command exits non-zero if verification fails.

Blender is located via --blender, THREEDRIVER_BLENDER, PATH, or the
standard Windows install path. See README.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from threedriver.headless import (  # noqa: E402
    DEFAULT_TIMEOUT,
    new_job_dir,
    resolve_blender,
    run_job,
)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("script", help="bpy script to run (receives --job-dir)")
    p.add_argument("--jobs-root", default=str(ROOT / "tmp" / "jobs"))
    p.add_argument("--job-id", default=None)
    p.add_argument("--blender", default=None, help="path to blender executable")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    p.add_argument("--verify", action="store_true",
                   help="re-probe the saved .blend in a fresh process")
    p.add_argument("--blend-name", default="plate.blend",
                   help="blend file the script saves (for --verify)")
    p.add_argument("--no-scan", action="store_true",
                   help="skip the pre-execution text scan (not recommended)")
    p.add_argument("--script-arg", action="append", default=[],
                   help="extra argument passed to the script after --")
    return p.parse_args(argv)


def print_report(report: dict) -> None:
    for obj in report.get("objects", []):
        bbox = obj["bbox"]
        print(f"  {obj['name']}: {obj['vertices']} verts, {obj['faces']} faces, "
              f"bbox {bbox['size'][0]:g} x {bbox['size'][1]:g} x {bbox['size'][2]:g} mm")
    for out in report.get("outputs", []):
        print(f"  output: {out['file']} ({out['bytes']} bytes)")
    checks = report.get("build_checks", [])
    if checks:
        print(f"  build checks: {sum(c['pass'] for c in checks)}/{len(checks)} passed")


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        blender = resolve_blender(args.blender)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"blender: {blender}")
    job_id, job_dir = new_job_dir(args.jobs_root, args.job_id)
    print(f"job: {job_id}\njob dir: {job_dir}")
    try:
        result = run_job(
            args.script,
            job_dir,
            job_id=job_id,
            blender=blender,
            timeout=args.timeout,
            extra_args=args.script_arg,
            scan=not args.no_scan,
        )
    except Exception as exc:  # noqa: BLE001 - setup errors (missing script, unsafe code)
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"exit: {result.returncode}  timed_out: {result.timed_out}  "
          f"elapsed: {result.elapsed_s:.1f}s")
    print(f"log: {result.log_path}")
    if not result.ok:
        print("job FAILED", file=sys.stderr)
        for line in result.stdout_tail:
            print(f"  | {line}", file=sys.stderr)
        return 1
    print(f"report: {result.report_path}")
    print_report(result.report)

    if args.verify:
        blend_path = job_dir / args.blend_name
        if not blend_path.is_file():
            print(f"error: --verify needs {blend_path}, not produced by the script",
                  file=sys.stderr)
            return 2
        probe = run_job(
            ROOT / "threedriver" / "bpy" / "probe.py",
            job_dir,
            job_id=job_id,
            blender=blender,
            timeout=args.timeout,
            extra_args=["--blend", str(blend_path), "--report", str(result.report_path)],
            report_name="probe.json",
            scan=not args.no_scan,
        )
        if probe.report is None:
            print("verify FAILED: probe produced no probe.json", file=sys.stderr)
            return 1
        print(f"probe: {probe.report_path} pass={probe.report.get('pass')}")
        print(f"  boundary edges: {probe.report.get('boundary_edges_total')}, "
              f"matches build report: {probe.report.get('matches_build_report')}")
        for c in probe.report.get("hole_checks", []):
            print(f"  {c['kind']} {c['point']}: {'pass' if c['pass'] else 'FAIL'}")
        if not probe.report.get("pass"):
            print("VERIFY FAILED", file=sys.stderr)
            return 1
        print("VERIFY PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
