# 3Driver — AI-driven 3D modelling in Blender

3Driver turns a plain-language prompt (plus photos or video) into a real
Blender model: an LLM API writes Blender Python, 3Driver runs it — headless
or live — and hands back a `.blend`, exports, a preview render, and a
measurement report. The name means "the thing that drives Blender", not a
hardware driver.

Status: **POC-5 — chat mod, plus model-list fetch in Settings.** Pick a provider on the Settings page,
type a prompt, get a verified model — or drive the Blender already open on
your screen and watch the viewport change. The CAD bridges come next. See
`RESEARCH-HYBRID.md` for the full design (in Indonesian).

## How it works

```
prompt + images
        |
[LLM API: 9router / Gemini / DeepSeek / generic OpenAI-compatible / stub]
        |  1) spec JSON (dimensions + features, from prompt + images)
        |  2) Blender Python script from the spec
        v
[Job queue + one directory per job: tmp/jobs/<job_id>/]
        |
[Headless executor]  one job = one fresh `blender -b` process
        |
[Verification]  geometry probes re-derived from the SAVED .blend
        |
.blend + .STL + preview + measurement report
```

## Quickstart (web UI)

Requirements: Python 3.10+ and Blender 5.2 LTS.

Windows — double-click `start-windows.bat`. It creates a virtual
environment, installs dependencies, and opens the UI in your browser.

Linux/macOS:

```bash
git clone https://github.com/Reefoldfiveteen/threedriver.git
cd threedriver
bash start.sh   # or: pip install -r requirements.txt && python scripts/serve.py
```

Then open http://127.0.0.1:8765. The UI runs on localhost only and is
never exposed to the network.

No API key yet? The default **stub** provider runs the whole loop without
any key — it always builds the demo plate — so you can try the UI, the job
flow, and verification before connecting a real provider.

CLI without the UI:

```bash
python scripts/run_prompt.py "flat plate 100x60x10 with 4 holes" --image ref.jpg
python scripts/run_job.py examples/plate_with_holes.py --verify  # POC-1 executor directly
```

## Providers & settings

Everything is configured on the Settings page — no code or file editing:

* **Provider**: stub (demo), 9router, DeepSeek, Gemini, or any
  OpenAI-compatible endpoint. Switching provider is a dropdown + save.
* **Model / base URL**: prefilled with sane defaults where known; 9router
  needs its base URL (local `http://127.0.0.1:20128` or your tunnel URL)
  and defaults to the `HERMES2` combo model. The **Ambil daftar model
  dari API** button asks the configured provider what models it actually
  offers (OpenAI-compatible `GET /v1/models`, Gemini `GET /v1beta/models`)
  and fills a dropdown, so a model is picked from what is available
  instead of typed from memory; manual typing still works for names the
  list does not show. The fetch uses the form's current values, so
  unsaved edits can be tried before saving.
* **API key**: stored in local `settings.json` (gitignored, mode 0600 on
  POSIX), never committed, never logged, never shown in full. Leaving the
  key field blank keeps the stored key. Environment variables
  (`THREEDRIVER_API_KEY`, `NINE_ROUTER_API_KEY`, `GEMINI_API_KEY`,
  `DEEPSEEK_API_KEY`, …) override the file.
* **Test connection** sends one tiny completion to prove the provider
  answers before you run a real job.
* **Gemini note**: a Gemini app / Google AI Pro subscription does not grant
  API access — the Gemini provider needs a separate AI Studio API key.

9router robustness: the OpenAI-compatible client tolerates the quirks
routers actually return — SSE `data:` lines with a `[DONE]` terminator,
concatenated JSON, delta chunks, and HTTP 200 with an empty body (treated
as an error, with a hint to try another model).

## Live mode (POC-3): drive an open Blender

The headless executor builds in a fresh process. Live mode drives the
Blender already open on your screen, so you watch the viewport change.
Small nudges and interactive iteration go live; heavy builds stay headless.

Install once on the laptop:

```bash
python scripts/package_addon.py   # makes tmp/threedriver_live.zip
```

Or download the ready-to-install .zip from the [Releases page](https://github.com/Reefoldfiveteen/threedriver/releases) (rebuilt automatically by GitHub Actions on every push).

In Blender: Edit > Preferences > Add-ons > Install from Disk, pick the
.zip, enable "3Driver Live". Open the N-panel (N key) > 3Driver tab >
Start Server. The server binds 127.0.0.1:9877 and creates a secret token
at `~/.threedriver/live_token` (shared with the client tools on the same
machine; never commit or share it).

Drive it from a terminal:

```bash
python scripts/live_cmd.py ping
python scripts/live_cmd.py scene
python scripts/live_cmd.py exec my_nudge.py
python scripts/live_cmd.py shot C:\Users\you\view.png
python scripts/live_cmd.py shutdown
```

Or use the Live tab in the web UI: the Settings page holds the live
port/token, and the Live card shows connection status, scene objects, a
viewport screenshot button, and a code box that runs snippets in the open
Blender. The server must still be started from Blender first (same
N-panel button) — the web UI only talks to it, it cannot start it.

The Settings page also has a **Test Blender** button for headless jobs:
it resolves the Blender executable and reports its `--version` output, so
a missing or wrong Blender path shows up before any job runs.

Hard rules (enforced by the addon, not by convention):

* `bpy` is not thread-safe: the socket thread only receives/parses JSON;
  every command executes on Blender's main thread via a `bpy.app.timers`
  pump.
* Localhost + token only. No token, no execution.
* Every `execute_code` saves the file and writes a timestamped backup copy
  (`.3driver-backup-<time>.blend`) before running. Save the file first —
  unsaved files get a warning instead of a backup.
* Stop the server when done. Opening another .blend mid-session can cancel
  the pump: restart the server afterwards.

Tested headless here (`scripts/test_live_headless.py`: ping, execute,
scene, error isolation, bad-token rejection, backup file, CLI, clean
shutdown). Two things only the laptop GUI can prove: the timer pump firing
under the real event loop, and viewport screenshots — try `live_cmd.py
shot` once on the laptop and confirm the PNG shows the viewport.

Viewport shading (addon 0.2.0+): `live_cmd.py viewport WIREFRAME` (or the
Solid/Wireframe buttons in the Live tab) switches every 3D viewport's
shading without touching the scene — no save, no backup. Older addons
answer "unknown command type": reinstall the .zip.

## Chat mod (POC-5): revise a model across follow-up turns

One-shot generation is only half the job. The **Chat mod** tab (third tab
in the web UI) holds conversations anchored to one model each:

* The first message builds the model (full pipeline).
* Every later message revises it: the LLM revises the stored spec JSON,
  changing ONLY what was asked, writes a fresh script from the revised
  spec, and applies it. New reference images can be uploaded on any turn.
* Each turn targets **headless** (fresh process, probe-verified, a new
  `versions/v<n>/` directory) or **live** (the full script runs in the
  open Blender — the addon saves + backs up first — then a viewport
  screenshot is captured). A chat can mix targets turn by turn.
* A chat can also start from a finished one-shot job (pick it in the
  "Chat baru dari job" dropdown): the job's spec + script seed the chat.

Live rebuild semantics, stated plainly: a live turn runs the whole
script, which clears the scene first. The pre-mod file survives only as
the addon's timestamped backup copy, and after the turn the active file
in Blender is the new version's `model.blend`. Headless turns never
touch the open Blender.

## Job contract

* Invocation is always an argument list with `shell=False`:
  `blender -b --python script.py -- --job-dir <dir> [script args]`.
* Every job has a mandatory timeout (default 600 s): terminate, grace
  period, then kill. A crashed or runaway script kills only its own job.
* Scripts are self-contained: they clear the scene, set millimetre units,
  and write `report.json` (objects, counts, bounding boxes, outputs).
  Output never goes to the project root — only the job directory.
* Blender never runs inside a web request thread: jobs run in worker
  threads and the UI polls status.
* bpy scripts must use the `bpy.data` / bmesh style, not UI-context
  `bpy.ops` where avoidable, so the same code works headless and live.
  Generated scripts must not use booleans (they have silently failed to
  cut holes); holes are built directly via tessellation + wall stitching.

Blender is located per the Settings page mode: **automatic** (default)
checks `THREEDRIVER_BLENDER`, `PATH`, then the standard Windows install
path (`C:\Program Files\Blender Foundation\Blender 5.2\blender.exe`);
**custom** uses exactly the path you typed — an empty or missing custom
path is an error, never a silent fallback. The Test Blender button
reports the resolved executable and its `--version` output, and the
Settings page shows the currently detected Blender on load.

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

* Local only, no public endpoint. API keys live in `settings.json` or
  environment variables (never committed, never in chat or logs).
* Headless: isolated process + timeout + separate job directory per job.
* A pre-execution text scan refuses scripts that touch the network, spawn
  subprocesses, or run shell commands. It is a tripwire, not a sandbox.
* Live mode: localhost-only socket, per-session token, always save +
  write a timestamped backup copy before running code.

## Project layout

```
threedriver/
  headless.py         the executor: spawn, timeout, job dirs, reports
  scan.py             pre-execution text scan
  config.py           settings file + provider registry (keys masked)
  pipeline.py         prompt -> spec -> script -> build -> verify
  webapp.py           FastAPI app: settings, jobs, live tab, chat-mod
  web/index.html      the UI (single page, no build step)
  providers/          stub, OpenAI-compatible, Gemini clients
  live_client.py      client for the live addon socket (localhost + token)
  chat.py             chat-mod sessions: spec revision + headless/live turns
  bpy/probe.py        re-probes a saved .blend in a fresh Blender process
  bpy/live_test_driver.py  headless driver for the live addon test
examples/             bpy scripts (the shape LLM output takes)
scripts/
  run_job.py          CLI: run one bpy script, optionally verify it
  run_prompt.py       CLI: run the full prompt pipeline
  serve.py            CLI: start the web UI
  smoke_web.py        end-to-end web UI smoke test (stub provider)
  smoke_live_ui.py    end-to-end live-tab smoke test (headless live server)
  smoke_chat.py       end-to-end chat-mod smoke test (stub + live server)
  smoke_models.py     model-list fetch smoke test (mock provider servers)
  live_cmd.py         CLI: talk to a running live Blender
  test_live_headless.py  end-to-end live addon test (headless Blender)
  package_addon.py    zip the live addon for Blender Install from Disk
addons/threedriver_live/  Blender addon: localhost socket server (live mode)
RESEARCH-HYBRID.md    architecture, contracts, POC plan (Indonesian)
```

## Roadmap

1. POC-1 headless executor — done.
2. POC-2 provider wiring + local web UI — done.
3. POC-3 live socket addon — done.
4. POC-4 web UI live tab (headless test + live settings, viewport
   screenshot, code exec from the browser) — done.
5. POC-5 chat mod (follow-up revisions, headless/live targets, tab
   switcher, viewport wireframe toggle) — done (this release).
6. Next: vision check of renders vs reference (six-side ortho renders
   compared by a vision model, revising only what missed).
7. Optional: CadQuery executor for prismatic parts, local instant-mesh
   models (TripoSR), 3ds Max / Inventor bridges.
