"""Local web UI: settings page, job submission, status, file downloads.

Runs on 127.0.0.1 only — never exposed. Blender jobs run in worker threads,
never in a request thread (see the headless contract in README.md); the UI
polls job status. Job state is in-memory; job outputs live on disk under
tmp/jobs/<job_id>/ and survive a server restart.

The Buat model tab's Live checkbox sends one-shot builds to the
3Driver Live addon in an open Blender (status, viewport screenshot,
viewport shading, execute code) over localhost + token. A stopped live
server is a normal state: live endpoints report ok:false, never crash.

Chat-mod sessions (see chat.py) revise one model across follow-up turns;
turns also run in worker threads and the UI polls the session.

Start:  python scripts/serve.py   (then open http://127.0.0.1:8765)
"""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from . import __version__
from .chat import (
    chats_root,
    create_session,
    delete_all_sessions,
    delete_session,
    list_sessions,
    load_session,
    run_live_build,
    run_mod_turn,
    save_session,
)
from .config import (
    default_settings_path,
    load_settings,
    providers_info,
    redact,
    save_settings,
)
from .headless import (
    BlenderNotFound,
    blender_arg_from_settings,
    resolve_blender,
)
from .live_client import LiveClient, LiveError
from .pipeline import run_pipeline
from .providers import ProviderError, get_provider, list_models, text_msg

MAX_IMAGES = 5
MAX_IMAGE_BYTES = 10 * 1024 * 1024

app = FastAPI(title="3Driver")
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
ROOT = Path(__file__).resolve().parent


def _jobs_root(settings: dict) -> Path:
    configured = settings.get("jobs_root") or ""
    return Path(configured) if configured else Path.cwd() / "tmp" / "jobs"


def _update_job(job_id: str, **fields) -> None:
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(fields)


def _live_client(settings: dict, timeout: float = 30.0) -> LiveClient:
    """Client for the live addon, from settings. An empty settings token
    falls back to THREEDRIVER_LIVE_TOKEN / the shared token file."""
    try:
        port = int(settings.get("live_port") or 9877)
    except (TypeError, ValueError):
        port = 9877
    return LiveClient(port=port, token=settings.get("live_token") or None,
                      timeout=timeout)


def _live_shot_path() -> Path:
    shot = Path.cwd() / "tmp" / "live" / "viewport.png"
    shot.parent.mkdir(parents=True, exist_ok=True)
    return shot


def _run_job_thread(job_id: str, prompt: str, images, settings: dict,
                    target: str = "headless") -> None:
    def on_status(stage: str) -> None:
        _update_job(job_id, status=stage)

    try:
        if target == "live":
            result = run_live_build(
                prompt=prompt, images=images, settings=settings,
                job_dir=_jobs_root(settings) / job_id,
                on_status=on_status)
        else:
            result = run_pipeline(
                prompt=prompt, images=images, settings=settings,
                jobs_root=_jobs_root(settings), job_id=job_id,
                on_status=on_status)
    except Exception as exc:  # noqa: BLE001 - a failed thread must report
        _update_job(job_id, status="error", error=str(exc))
        return
    if result["status"] == "done":
        _update_job(job_id, status="done", result=result)
    else:
        _update_job(job_id, status="error", error=result.get("error", "unknown"),
                    result=result)


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "version": __version__}


@app.get("/api/providers")
def list_providers() -> dict:
    return {"providers": providers_info()}


@app.get("/api/settings")
def get_settings() -> dict:
    return redact(load_settings())


@app.post("/api/settings")
def post_settings(payload: dict) -> dict:
    allowed = {"provider", "model", "base_url", "api_key", "blender",
               "blender_mode", "timeout", "jobs_root", "live_port",
               "live_token"}
    updates = {k: v for k, v in payload.items() if k in allowed}
    if ("blender_mode" in updates
            and updates["blender_mode"] not in ("auto", "custom")):
        raise HTTPException(400, "blender_mode must be 'auto' or 'custom'")
    for num_key in ("timeout", "live_port"):
        if num_key in updates:
            try:
                updates[num_key] = int(updates[num_key])
            except (TypeError, ValueError):
                raise HTTPException(400, f"{num_key} must be a number")
    return redact(save_settings(updates))


@app.post("/api/test-provider")
def test_provider(payload: dict | None = None) -> dict:
    """Tiny completion call to prove the configured provider works."""
    settings = load_settings()
    for key in ("provider", "model", "base_url", "api_key"):
        value = (payload or {}).get(key)
        if value:
            settings[key] = value
    try:
        provider = get_provider(settings)
        reply = provider.complete(
            [text_msg("user", "Reply with exactly: OK")],
            model=settings.get("model") or "stub",
            temperature=0.0, max_tokens=16)
    except ProviderError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "reply": reply.strip()[:200]}


@app.post("/api/models")
def list_models_endpoint(payload: dict | None = None) -> dict:
    """Fetch the model list from the configured provider's API.

    Payload overrides (provider, base_url, api_key) win over saved
    settings when non-empty, so unsaved form values can be tried first.
    An empty api_key means "use the saved key", as everywhere else.
    """
    settings = load_settings()
    for key in ("provider", "base_url", "api_key"):
        value = (payload or {}).get(key)
        if value:
            settings[key] = value
    try:
        models = list_models(settings)
    except ProviderError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "provider": settings.get("provider"),
            "models": models, "count": len(models)}


@app.post("/api/test-blender")
def test_blender(payload: dict | None = None) -> dict:
    """Resolve the Blender executable per the settings mode and prove it
    runs (--version). Accepts unsaved field overrides, like test-provider."""
    settings = load_settings()
    payload = payload or {}
    if "blender" in payload:
        settings["blender"] = payload["blender"] or ""
    if "blender_mode" in payload:
        settings["blender_mode"] = payload["blender_mode"] or "auto"
    try:
        exe = resolve_blender(blender_arg_from_settings(settings))
    except BlenderNotFound as exc:
        return {"ok": False, "error": str(exc)}
    try:
        proc = subprocess.run([exe, "--version"], capture_output=True,
                              text=True, timeout=30)
    except OSError as exc:
        return {"ok": False, "error": f"could not run {exe}: {exc}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"{exe} did not answer --version in 30s"}
    first = (proc.stdout or "").splitlines()[:1]
    return {"ok": True, "path": exe,
            "mode": settings.get("blender_mode") or "auto",
            "version": first[0].strip() if first else "(no version output)"}


@app.get("/api/live/status")
def live_status() -> dict:
    """Ping the live addon + scene summary. A stopped server is reported
    as ok:false, never as an exception."""
    try:
        with _live_client(load_settings()) as client:
            ping = client.ping()
            if not ping.get("ok"):
                return {"ok": False, "error": ping.get("error", "ping failed")}
            scene = client.get_scene()
    except LiveError as exc:
        return {"ok": False, "error": str(exc)}
    if not scene.get("ok"):
        return {"ok": False, "error": scene.get("error", "get_scene failed")}
    return {"ok": True, "blender_version": ping.get("blender_version"),
            "scene": scene.get("result", {})}


@app.post("/api/live/screenshot")
def live_screenshot() -> dict:
    """Ask live Blender for a viewport screenshot; the PNG lands in
    tmp/live/viewport.png and is served by GET /api/live/viewport.png."""
    shot = _live_shot_path()
    try:
        with _live_client(load_settings()) as client:
            resp = client.screenshot(str(shot))
    except LiveError as exc:
        return {"ok": False, "error": str(exc)}
    if not resp.get("ok"):
        return {"ok": False, "error": resp.get("error", "screenshot failed")}
    if not shot.is_file():
        return {"ok": False,
                "error": "live Blender reported ok but no PNG was written"}
    return {"ok": True, "bytes": shot.stat().st_size}


@app.get("/api/live/viewport.png")
def live_viewport_png():
    shot = _live_shot_path()
    if not shot.is_file():
        raise HTTPException(404, "no viewport screenshot yet — take one first")
    return FileResponse(shot, media_type="image/png", filename="viewport.png")


@app.post("/api/live/exec")
def live_exec(payload: dict) -> dict:
    """Run one bpy snippet in live Blender. The addon saves + backs up
    the .blend first (its own safety contract)."""
    code = (payload or {}).get("code")
    if not isinstance(code, str) or not code.strip():
        raise HTTPException(400, "code must be a non-empty string")
    if len(code) > 100_000:
        raise HTTPException(400, "code larger than 100 KB")
    try:
        with _live_client(load_settings(), timeout=120.0) as client:
            return client.execute(code, timeout=120.0)
    except LiveError as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/api/live/viewport")
def live_viewport(payload: dict) -> dict:
    """Switch the live 3D viewport shading (view-only: no save, no scene
    change). Needs addon 0.2.0+; older addons answer 'unknown command'."""
    shading = str((payload or {}).get("shading") or "").upper()
    if shading not in ("WIREFRAME", "SOLID", "MATERIAL", "RENDERED"):
        raise HTTPException(
            400, "shading must be WIREFRAME, SOLID, MATERIAL or RENDERED")
    try:
        with _live_client(load_settings()) as client:
            return client.set_viewport(shading)
    except LiveError as exc:
        return {"ok": False, "error": str(exc)}


def _get_session_or_404(chat_id: str) -> tuple[dict, Path]:
    root = chats_root(load_settings())
    try:
        session = load_session(root, chat_id)
    except ValueError:
        raise HTTPException(400, "bad chat id")
    if session is None:
        raise HTTPException(404, "unknown chat")
    return session, root


def _run_turn_thread(chat_id: str, prompt: str, image_data, target: str,
                     settings: dict) -> None:
    try:
        run_mod_turn(settings=settings, root=chats_root(settings),
                     chat_id=chat_id, prompt=prompt, images=image_data,
                     target=target)
    except Exception as exc:  # noqa: BLE001 - never strand a pending turn
        try:
            root = chats_root(settings)
            session = load_session(root, chat_id)
            if session is not None:
                session["pending"] = None
                session["messages"].append(
                    {"role": "assistant", "text": "", "status": "error",
                     "error": f"turn crashed: {exc}", "target": target})
                save_session(root, session)
        except Exception:
            pass


@app.post("/api/chats")
def create_chat(payload: dict | None = None) -> dict:
    settings = load_settings()
    root = chats_root(settings)
    base_job_id = (payload or {}).get("base_job_id") or ""
    base_job_dir = None
    if base_job_id:
        with _jobs_lock:
            job = _jobs.get(base_job_id)
        if job is not None and "result" in job:
            base_job_dir = job["result"].get("job_dir")
    session = create_session(root, base_job_dir=base_job_dir)
    if base_job_id and base_job_dir:
        session["base_job_id"] = base_job_id
        save_session(root, session)
    return {"id": session["id"], "title": session["title"],
            "has_model": session["spec"] is not None}


@app.get("/api/chats")
def list_chats() -> dict:
    return {"chats": list_sessions(chats_root(load_settings()))}


@app.delete("/api/chats")
def delete_all_chats() -> dict:
    count = delete_all_sessions(chats_root(load_settings()))
    return {"ok": True, "deleted": count}


@app.get("/api/chats/{chat_id}")
def get_chat(chat_id: str) -> dict:
    session, _root = _get_session_or_404(chat_id)
    return session


@app.delete("/api/chats/{chat_id}")
def delete_chat(chat_id: str) -> dict:
    _session, root = _get_session_or_404(chat_id)
    delete_session(root, chat_id)
    return {"ok": True, "deleted": chat_id}


@app.post("/api/chats/{chat_id}/messages")
async def post_chat_message(
    chat_id: str,
    text: str = Form(...),
    target: str = Form("headless"),
    images: list[UploadFile] = File(default=[]),
) -> dict:
    settings = load_settings()
    session, root = _get_session_or_404(chat_id)
    if session.get("pending"):
        raise HTTPException(409, "a turn is already running in this chat")
    text = (text or "").strip()
    if not text:
        raise HTTPException(400, "message text must not be empty")
    if target not in ("headless", "live"):
        raise HTTPException(400, "target must be 'headless' or 'live'")
    if len(images) > MAX_IMAGES:
        raise HTTPException(400, f"at most {MAX_IMAGES} images")
    image_data = []
    for upload in images:
        data = await upload.read()
        if len(data) > MAX_IMAGE_BYTES:
            raise HTTPException(400, f"{upload.filename}: larger than 10 MB")
        if not (upload.content_type or "").startswith("image/"):
            raise HTTPException(400, f"{upload.filename}: not an image")
        image_data.append((data, upload.content_type))
    thread = threading.Thread(
        target=_run_turn_thread,
        args=(chat_id, text, image_data, target, settings), daemon=True)
    thread.start()
    return {"status": "queued", "chat_id": chat_id}


@app.get("/api/chats/{chat_id}/files/{version}/{filename}")
def get_chat_file(chat_id: str, version: str, filename: str):
    if ("/" in filename or "\\" in filename or filename in ("", ".", "..")
            or not (version.startswith("v") and version[1:].isdigit())):
        raise HTTPException(400, "bad filename")
    _session, root = _get_session_or_404(chat_id)
    version_dir = (Path(root) / chat_id / "versions" / version).resolve()
    path = (version_dir / filename).resolve()
    if path.parent != version_dir or not path.is_file():
        raise HTTPException(404, "file not found")
    media = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".json": "application/json", ".log": "text/plain",
        ".py": "text/plain", ".stl": "application/octet-stream",
        ".blend": "application/octet-stream",
    }.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=media, filename=filename)


@app.post("/api/jobs")
async def create_job(
    prompt: str = Form(...),
    target: str = Form("headless"),
    images: list[UploadFile] = File(default=[]),
) -> dict:
    prompt = (prompt or "").strip()
    if not prompt:
        raise HTTPException(400, "prompt must not be empty")
    if target not in ("headless", "live"):
        raise HTTPException(400, f"unknown target: {target!r}")
    if len(images) > MAX_IMAGES:
        raise HTTPException(400, f"at most {MAX_IMAGES} images")
    image_data = []
    for upload in images:
        data = await upload.read()
        if len(data) > MAX_IMAGE_BYTES:
            raise HTTPException(400, f"{upload.filename}: larger than 10 MB")
        if not (upload.content_type or "").startswith("image/"):
            raise HTTPException(400, f"{upload.filename}: not an image")
        image_data.append((data, upload.content_type))
    settings = load_settings()
    job_id = f"web-{int(time.time())}-{len(_jobs) + 1}"
    with _jobs_lock:
        _jobs[job_id] = {"job_id": job_id, "prompt": prompt,
                         "target": target,
                         "status": "queued", "created": time.time(),
                         "provider": settings.get("provider"),
                         "model": settings.get("model")}
    thread = threading.Thread(
        target=_run_job_thread,
        args=(job_id, prompt, image_data, settings, target),
        daemon=True)
    thread.start()
    return {"job_id": job_id, "status": "queued"}


@app.get("/api/jobs")
def list_jobs() -> dict:
    with _jobs_lock:
        jobs = sorted(_jobs.values(), key=lambda j: j["created"], reverse=True)
        return {"jobs": [{k: v for k, v in j.items() if k != "result"}
                         for j in jobs]}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")
    return job


@app.get("/api/jobs/{job_id}/files/{filename}")
def get_job_file(job_id: str, filename: str):
    if "/" in filename or "\\" in filename or filename in ("", ".", ".."):
        raise HTTPException(400, "bad filename")
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None or "result" not in job:
        raise HTTPException(404, "job or result not found")
    job_dir = Path(job["result"]["job_dir"]).resolve()
    path = (job_dir / filename).resolve()
    if path.parent != job_dir or not path.is_file():
        raise HTTPException(404, "file not found")
    media = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".json": "application/json", ".log": "text/plain",
        ".py": "text/plain", ".stl": "application/octet-stream",
        ".blend": "application/octet-stream",
    }.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=media, filename=filename)


@app.get("/")
def index():
    return FileResponse(ROOT / "web" / "index.html", media_type="text/html")
