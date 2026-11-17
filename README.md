# 3Driver — AI-driven 3D modelling in Blender

3Driver turns a plain-language prompt (plus photos or video) into a real
Blender model: an LLM API writes Blender Python, 3Driver runs it — headless
or live — and hands back a `.blend`, exports, a preview render, and a
measurement report. The name means "the thing that drives Blender", not a
hardware driver.

Status: **POC-1, headless executor only.** The live addon, AI provider
wiring, and CAD bridges come next. See `RESEARCH-HYBRID.md` for the full
design (in Indonesian).

## How it works

```
prompt + images/video
        |
[LLM API: 9router / Gemini / Claude / DeepSeek]  (coming in POC-2)
        |  spec JSON -> Blender Python script
        v
[Job queue + one directory per job: tmp/jobs/<job_id>/]
        |
[Headless executor]  one job = one fresh `blender -b` process
        |
[Verification]  geometry probes re-derived from the SAVED .blend
        |
.blend + .STL + preview + measurement report
```

## Quickstart

Requirements: Python 3.10+ and Blender 5.2 LTS.

```bash
git clone https://github.com/Reefoldfiveteen/threedriver.git
cd threedriver
python scripts/run_job.py examples/plate_with_holes.py --verify
```

Expected result: a 100x60x10 mm plate with 4x Ø10 through-holes, built
without booleans, saved as `plate.blend` + `plate.stl` with a preview render
and a JSON report — then re-verified from the saved file in a fresh Blender
process (bounding box, vertex/face counts, 0 boundary edges, through-hole
raycasts in both directions).

Blender is located in this order: `--blender`, the `THREEDRIVER_BLENDER`
environment variable, `PATH`, then the standard Windows install path
(`C:\Program Files\Blender Foundation\Blender 5.2\blender.exe`).

## Job contract

* Invocation is always an argument list with `shell=False`:
  `blender -b --python script.py -- --job-dir <dir> [script args]`.
* Every job has a mandatory timeout (default 600 s): terminate, grace
  period, then kill. A crashed or runaway script kills only its own job.
* Scripts are self-contained: they clear the scene, set millimetre units,
  and write `report.json` (objects, counts, bounding boxes, outputs).
  Output never goes to the project root — only the job directory.
* Never run Blender synchronously inside a web request thread. The future
  web UI will poll job status; jobs run in a worker/queue.
* bpy scripts must use the `bpy.data` / bmesh style, not UI-context
  `bpy.ops` where avoidable, so the same code works headless and live.

## Verification (the accuracy loop)

A render alone misses things — side-view problems have slipped past
front-view checks before. So every job is verified from the delivered file
itself: bounding box vs spec, through-hole raycasts in both directions,
boundary edges = 0 for closed solids, and key feature sizes. Later
iterations add six-side ortho renders compared by a vision model against
the reference, revising only the wrong part and proving the rest did not
move.

## Safety

This is a personal, single-operator tool, so full bpy code from the LLM is
allowed — a fixed command DSL cannot express technical-drawing models. The
compensations:

* Local only, no public endpoint. API keys live in `.env` (never committed,
  never in chat or logs). Copy `.env.example` to `.env` and fill it in.
* Headless: isolated process + timeout + separate job directory per job.
* A pre-execution text scan refuses scripts that touch the network, spawn
  subprocesses, or run shell commands. It is a tripwire, not a sandbox.
* Live mode (coming): localhost-only socket, per-session token, always
  save + work on a copy before running AI code.

## Project layout

```
threedriver/          Python package (stdlib only for now)
  headless.py         the executor: spawn, timeout, job dirs, reports
  scan.py             pre-execution text scan
  bpy/probe.py        re-probes a saved .blend in a fresh Blender process
examples/             bpy scripts (the shape LLM output will take)
scripts/run_job.py    CLI: run one job, optionally verify it
RESEARCH-HYBRID.md    architecture, contracts, POC plan (Indonesian)
```

## Roadmap

1. POC-1 headless executor — this release.
2. POC-2 provider wiring: prompt + image → spec JSON → bpy → headless →
   verify (9router first, Gemini/DeepSeek fallback).
3. POC-3 live socket addon for the laptop: watch the viewport change.
4. Optional: CadQuery executor for prismatic parts, local instant-mesh
   models (TripoSR), 3ds Max / Inventor bridges.
