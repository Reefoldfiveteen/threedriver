"""Chat-mod sessions: follow-up prompts that revise one model.

A chat session is anchored to a single model. The first message builds it
(full pipeline: spec -> script -> build -> verify); every later message
revises it: the LLM revises the stored spec JSON (changing ONLY what the
user asked), writes a fresh script from the revised spec, and applies it
to the chosen target:

* headless — a fresh `blender -b` process per version, probe-verified,
  exactly like a one-shot job. Safe default. Builds a NEW file per
  version.
* live — DIRECT MODIFICATION of the open Blender file: the turn first
  reads the live scene inventory (object names, types, dimensions),
  then the LLM writes a script that edits those objects in place — no
  scene clear, no new file, no save. The addon still saves + writes a
  timestamped backup BEFORE executing, and a viewport screenshot is
  captured after. The user saves the modified file with Ctrl+S when
  happy. If the scene holds no matching objects, the script builds the
  part in the current scene instead.

`run_live_build()` is the same live path as a one-shot job (the Buat
model form's Live checkbox), without a chat session.

Sessions live on disk under tmp/chats/<chat_id>/ (session.json plus one
versions/v<n>/ directory per turn), so they survive a server restart.
One turn per session at a time; a second message while a turn runs is
rejected so two builds never fight over the same session.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path

from .config import load_settings
from .headless import (
    BlenderNotFound,
    blender_arg_from_settings,
    run_job,
)
from .live_client import LiveClient, LiveError
from .pipeline import (
    CODEGEN_SYSTEM,
    MAX_CODE_BYTES,
    MAX_FIX_ATTEMPTS,
    SPEC_SYSTEM,
    PipelineError,
    extract_code,
    extract_json,
    fix_script,
)
from .providers import ProviderError, get_provider, text_msg
from .providers.base import image_msg
from .scan import scan_text

SPEC_REVISE_SYSTEM = """You revise a 3D modelling specification (JSON) from a follow-up \
request. You are given the CURRENT spec, the user's change request, and \
optionally new reference images. Change ONLY what the user asked for: \
keep every other dimension, feature, count, and position byte-identical \
to the current spec. Shape follows the new references when given; sizes \
follow the dimensions the user states. If the request is ambiguous, keep \
the current value and note the assumption in "notes" — never invent extra \
features the user did not ask for, and never drop a feature silently.

Reply with ONLY the full revised JSON object, no prose, no markdown \
fences, using the same schema as the current spec."""

LIVE_MOD_SYSTEM = """You write a Blender Python (bpy) script for direct modification \
of the objects already in the open Blender scene, implementing the change \
from the OLD spec to the NEW spec. The script runs inside a GUI Blender \
via a socket addon, on the main thread, with `bpy`, `bmesh`, `mathutils`, \
and `Vector` already available. It takes NO arguments and runs with NO \
headless context.

Rules:
1. DO NOT clear the scene. DO NOT delete all objects. Edit the existing \
objects listed in the SCENE INVENTORY, found by exact name via \
bpy.data.objects.get("name"). If a named object is missing, build that \
part of the NEW spec fresh in the current scene instead of failing, and \
say so in __result__.
2. UNITS: millimetres. Coordinates are plain millimetre numbers.
3. STYLE: bpy.data / bmesh only. Do NOT use bpy.ops for modelling \
(UI-context dependent). No render calls, no file writes.
4. FORBIDDEN: saving or opening files (no save_mainfile, \
save_as_mainfile, open_mainfile, read_factory_settings), no network, \
subprocess, shell commands, eval/exec. The addon already saved + backed \
up the open file before running this script; the user saves afterwards \
with Ctrl+S.
5. IMPLEMENT THE DELTA: change only what differs between OLD and NEW \
spec (or what the user asked). Keep every other object, dimension, and \
position untouched. Prefer editing the existing mesh (move vertices, \
rebuild one object's mesh in place with bmesh) over deleting and \
recreating objects; keep object names stable so later turns can find \
them again.
6. NO BOOLEANS: never use boolean modifiers or operations — they have \
silently failed to cut holes on some builds. Rebuild meshes with holes \
directly (tessellate_polygon + stitched walls, or vertex by vertex).
7. Set __result__ to a one-line summary of what changed (object names + \
what was done). Do not print long output.
8. If the scene inventory is empty or holds only default objects \
(Cube/Camera/Light), build the NEW spec's part in the current scene \
from scratch, using the object name from the spec.
9. BMESH PRIMITIVES: bmesh.ops parameter names are exact — never guess \
them. The primitives you may use, with their real signatures (Blender \
5.2): create_cube(size, matrix, calc_uvs); create_cone(cap_ends, \
cap_tris, segments, radius1, radius2, depth, matrix, calc_uvs); \
create_uvsphere(u_segments, v_segments, radius, matrix, calc_uvs); \
create_icosphere(subdivisions, radius, matrix, calc_uvs); \
create_circle(cap_ends, cap_tris, segments, radius, matrix, calc_uvs); \
create_grid(x_segments, y_segments, size, matrix, calc_uvs). Note \
cap_ends (plural) and cap_tris — NOT cap_end or cap_triangles. When in \
doubt, build the mesh from vertices and faces directly.

Reply with ONLY the Python code in one ```python fenced block, no prose."""

MAX_IMAGES = 5
MAX_IMAGE_BYTES = 10 * 1024 * 1024

_lock = threading.Lock()

_MIME_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}


def chats_root(settings: dict | None = None) -> Path:
    settings = settings or load_settings()
    jobs_root = settings.get("jobs_root") or ""
    if jobs_root:
        return Path(jobs_root).parent / "chats"
    return Path.cwd() / "tmp" / "chats"


def _session_dir(root: str | Path, chat_id: str) -> Path:
    if not chat_id or any(c not in "abcdefghijklmnopqrstuvwxyz"
                          "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
                          for c in chat_id):
        raise ValueError(f"bad chat id: {chat_id!r}")
    return Path(root) / chat_id


def new_chat_id() -> str:
    return f"chat-{int(time.time())}-{uuid.uuid4().hex[:6]}"


def create_session(root: str | Path, base_job_dir: str | Path | None = None,
                   title: str = "") -> dict:
    """Create a session, optionally seeded from a finished job's files."""
    chat_id = new_chat_id()
    session = {
        "id": chat_id,
        "created": time.time(),
        "title": title or "Chat mod",
        "base_job_id": "",
        "spec": None,
        "script": None,
        "messages": [],
        "versions": [],
        "pending": None,
    }
    if base_job_dir:
        job_dir = Path(base_job_dir)
        spec_file, script_file = job_dir / "spec.json", job_dir / "generated.py"
        if spec_file.is_file() and script_file.is_file():
            try:
                session["spec"] = json.loads(spec_file.read_text(encoding="utf-8"))
                session["script"] = script_file.read_text(encoding="utf-8")
                session["base_job_id"] = job_dir.name
            except (json.JSONDecodeError, OSError):
                pass
    save_session(root, session)
    return session


def load_session(root: str | Path, chat_id: str) -> dict | None:
    path = _session_dir(root, chat_id) / "session.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def save_session(root: str | Path, session: dict) -> None:
    """Atomic write (temp + replace): the UI polls while turns save, so a
    torn read must never surface as a corrupt/missing session."""
    path = _session_dir(root, session["id"]) / "session.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(session, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def list_sessions(root: str | Path) -> list[dict]:
    root = Path(root)
    out = []
    if not root.is_dir():
        return out
    for path in root.glob("*/session.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, dict):
            out.append({
                "id": data.get("id", path.parent.name),
                "created": data.get("created", 0),
                "title": data.get("title", ""),
                "messages": len(data.get("messages", [])),
                "versions": len(data.get("versions", [])),
                "pending": bool(data.get("pending")),
                "has_model": data.get("spec") is not None,
            })
    return sorted(out, key=lambda s: s["created"], reverse=True)


def delete_session(root: str | Path, chat_id: str) -> bool:
    """Delete one session directory (history + versions + uploads).
    Returns True when something was deleted. Raises ValueError on a
    bad id (same guard as load)."""
    path = _session_dir(root, chat_id)
    if not path.is_dir():
        return False
    shutil.rmtree(path)
    return True


def delete_all_sessions(root: str | Path) -> int:
    """Delete every session directory under root. Only directories that
    contain a session.json are touched. Returns the number deleted."""
    root = Path(root)
    count = 0
    if not root.is_dir():
        return 0
    for path in root.iterdir():
        if path.is_dir() and (path / "session.json").is_file():
            shutil.rmtree(path)
            count += 1
    return count


def _version_dir(root: str | Path, session: dict, n: int) -> Path:
    path = _session_dir(root, session["id"]) / "versions" / f"v{n}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _save_images(version_dir: Path, images) -> list[str]:
    names = []
    for i, (data, mime) in enumerate(images or [], start=1):
        ext = _MIME_EXT.get((mime or "").lower(), "png")
        name = f"ref_{i}.{ext}"
        (version_dir / name).write_bytes(data)
        names.append(name)
    return names


def _complete_json(provider, messages, model: str) -> dict:
    """One completion + one bare-JSON repair attempt, like the pipeline."""
    try:
        return extract_json(provider.complete(
            messages, model=model, temperature=0.2,
            max_tokens=2048, json_mode=True))
    except (ProviderError, PipelineError, json.JSONDecodeError):
        repair = messages + [text_msg(
            "user", "Your previous reply was not valid JSON. Reply with "
                    "ONLY the JSON object, no other text.")]
        return extract_json(provider.complete(
            repair, model=model, temperature=0.0,
            max_tokens=2048, json_mode=True))


def _summarize(spec: dict, target: str, n: int, verified) -> str:
    dims = (spec or {}).get("dimensions") or {}
    dim_txt = ""
    if dims:
        dim_txt = " — " + " × ".join(
            f"{dims[k]}" for k in ("width", "depth", "height") if k in dims) + " mm"
    ver = ""
    if verified is True:
        ver = " · terverifikasi dari file .blend"
    elif verified is False:
        ver = " · verifikasi GAGAL, cek laporannya"
    name = (spec or {}).get("name") or "model"
    desc = (spec or {}).get("description") or ""
    label = "live, langsung" if target == "live" else target
    return f"v{n} ({label}): {name}{dim_txt}{ver}" + (f"\n{desc}" if desc else "")


def _apply_headless(script: str, version_dir: Path, job_id: str,
                    settings: dict, blender_exe, timeout: float,
                    on_status) -> tuple[dict | None, bool | None, list[str]]:
    """Run the script in a fresh headless Blender + probe-verify it."""
    script_path = version_dir / "generated.py"
    script_path.write_text(script, encoding="utf-8")
    if on_status:
        on_status("build")
    result = run_job(script_path, version_dir, job_id=job_id,
                     blender=blender_exe, timeout=timeout, scan=False)
    if not result.ok:
        tail = "\n".join(result.stdout_tail[-10:])
        raise PipelineError(
            f"Blender exited {result.returncode} "
            f"(timed out: {result.timed_out}). Log tail:\n{tail}")
    report = result.report or {}
    verified: bool | None = None
    probe_report = None
    blend = version_dir / "model.blend"
    if not blend.is_file():
        # Example/legacy scripts name their outputs differently (plate.*).
        blends = sorted(version_dir.glob("*.blend"))
        blend = blends[0] if blends else None
    if blend is not None and (version_dir / "report.json").is_file():
        if on_status:
            on_status("verify")
        probe = run_job(
            Path(__file__).resolve().parent / "bpy" / "probe.py",
            version_dir, job_id=job_id, blender=blender_exe,
            timeout=timeout,
            extra_args=["--blend", str(blend),
                        "--report", str(version_dir / "report.json")],
            report_name="probe.json", scan=False)
        probe_report = probe.report
        verified = bool(probe_report and probe_report.get("pass"))
    files = sorted(p.name for p in version_dir.iterdir() if p.is_file())
    return report, verified, files


def _live_port(settings: dict) -> int:
    try:
        return int(settings.get("live_port") or 9877)
    except (TypeError, ValueError):
        return 9877


def _live_inventory(settings: dict) -> dict:
    """Read the open Blender's scene via get_scene (a read-only command:
    no save, no backup — unlike execute_code)."""
    with LiveClient(port=_live_port(settings),
                    token=settings.get("live_token") or None,
                    timeout=30.0) as client:
        resp = client.get_scene()
    if not resp.get("ok"):
        raise PipelineError(
            f"could not read live scene: {resp.get('error', 'unknown')}")
    scene = resp.get("result")
    if not isinstance(scene, dict):
        raise PipelineError("live scene reply was not an object")
    return scene


def _inventory_summary(inventory: dict) -> dict:
    """Trim the inventory for the prompt: at most 50 objects."""
    objects = inventory.get("objects") or []
    return {
        "filepath": inventory.get("filepath") or "(unsaved file)",
        "object_count": inventory.get("object_count", len(objects)),
        "active_object": inventory.get("active_object"),
        "objects": objects[:50],
        "truncated": len(objects) > 50,
    }


_FORBIDDEN_IN_MOD = ("save_as_mainfile", "save_mainfile", "open_mainfile",
                     "read_factory_settings", "read_homefile")


def check_mod_script(script: str) -> None:
    """Refuse live-mod scripts that save or open files. The addon saves +
    backs up before executing; the user saves afterwards. A mod script
    must only touch the in-memory scene."""
    for token in _FORBIDDEN_IN_MOD:
        if token in script:
            raise PipelineError(
                f"live-mod script refused: it must not call {token} "
                "(the addon saves + backs up before executing; "
                "the user saves afterwards with Ctrl+S)")


def _capture_viewport(version_dir: Path, settings: dict) -> str:
    """Screenshot the live viewport into the version dir. Returns "" on
    success, else a user-facing note (the mod already applied; a failed
    screenshot must not fail the turn)."""
    shot = version_dir / "viewport.png"
    try:
        with LiveClient(port=_live_port(settings),
                        token=settings.get("live_token") or None,
                        timeout=30.0) as client:
            shot_resp = client.screenshot(str(shot))
        if not shot_resp.get("ok") or not shot.is_file():
            return ("Model sudah diubah di live Blender, tapi screenshot "
                    f"viewport gagal: {shot_resp.get('error', 'unknown')}")
    except LiveError as exc:
        return ("Model sudah diubah di live Blender, tapi screenshot "
                f"gagal: {exc}")
    return ""


def _apply_live_direct(script: str, version_dir: Path, settings: dict,
                       on_status) -> tuple[list[str], str]:
    """Execute a direct-modification script in the open Blender, then
    screenshot the viewport. The script takes no arguments and writes
    no files — it edits the in-memory scene only."""
    if on_status:
        on_status("live")
    with LiveClient(port=_live_port(settings),
                    token=settings.get("live_token") or None,
                    timeout=120.0) as client:
        resp = client.execute(script, timeout=120.0)
    if not resp.get("ok"):
        detail = resp.get("error") or "live execution failed"
        tail = (resp.get("stderr") or resp.get("stdout") or "").strip()
        if tail:
            detail += "\n" + "\n".join(tail.splitlines()[-10:])
        raise PipelineError(detail)
    note = _capture_viewport(version_dir, settings)
    files = sorted(p.name for p in version_dir.iterdir() if p.is_file())
    return files, note


def _codegen_live_mod(provider, model: str, old_spec, spec: dict,
                      inventory: dict, prompt: str, image_data) -> str:
    """Generate a direct-modification script for the live scene: the LLM
    sees the old spec, the new spec, and the live inventory, and writes
    a script that edits the named objects in place. The script is
    scanned and guarded before it is returned. Raises PipelineError on
    refusal."""
    mod_user = "\n".join([
        "OLD SPEC (what the scene should currently match):",
        json.dumps(old_spec, indent=2) if old_spec else
        "(none — the scene may be empty or hold only default objects)",
        "",
        "NEW SPEC (what the scene must match after this turn):",
        json.dumps(spec, indent=2),
        "",
        "SCENE INVENTORY (live objects right now):",
        json.dumps(_inventory_summary(inventory), indent=2),
        "",
        "USER REQUEST:",
        prompt,
    ])
    messages = [text_msg("system", LIVE_MOD_SYSTEM),
                text_msg("user", mod_user)]
    if image_data:
        messages.append(image_msg(
            "user", "Reference images for this change:", image_data))
    code_reply = provider.complete(
        messages, model=model, temperature=0.2,
        max_tokens=8192, json_mode=False)
    script = extract_code(code_reply)
    if len(script.encode("utf-8")) > MAX_CODE_BYTES:
        raise PipelineError("generated script larger than 100 KB")
    findings = scan_text(script)
    if findings:
        detail = "; ".join(f"line {ln}: {d}" for ln, d in findings)
        raise PipelineError(f"generated script refused: {detail}")
    check_mod_script(script)
    return script


def _live_mod_with_retries(provider, model: str, old_spec, spec: dict,
                           inventory: dict, prompt: str, image_data,
                           out_dir: Path, settings: dict,
                           on_status) -> tuple[str, list[str], str, int]:
    """First mod script + execute, with up to MAX_FIX_ATTEMPTS repairs
    when execution fails in Blender. Returns
    (script, files, note, attempts). Connection problems (LiveError)
    are NOT retried — only execution errors reach the fix loop."""
    script = _codegen_live_mod(provider, model, old_spec, spec, inventory,
                               prompt, image_data)
    script_path = out_dir / "generated.py"
    script_path.write_text(script, encoding="utf-8")
    attempts = 1
    while True:
        try:
            files, note = _apply_live_direct(script, out_dir, settings,
                                             on_status)
            return script, files, note, attempts
        except PipelineError as exc:
            if attempts > MAX_FIX_ATTEMPTS:
                raise
            if on_status:
                on_status("fix")
            script = fix_script(provider, model, script, str(exc),
                                live=True)
            check_mod_script(script)
            script_path.write_text(script, encoding="utf-8")
            attempts += 1


def run_live_build(*, prompt: str, images, settings: dict,
                   job_dir: str | Path, on_status=None) -> dict:
    """One-shot live build: spec -> inventory -> direct-mod script ->
    execute in the open Blender -> viewport screenshot. Writes spec.json,
    generated.py, viewport.png and report.json into job_dir and returns
    a pipeline-style result dict. Raises PipelineError on failure."""
    job_dir = Path(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)
    provider = get_provider(settings)
    model = settings.get("model") or "stub"
    image_data = list(images or [])

    def stage(name: str) -> None:
        if on_status:
            on_status(name)

    stage("spec")
    spec = _complete_json(provider, [
        text_msg("system", SPEC_SYSTEM),
        image_msg("user", prompt, image_data),
    ], model)
    (job_dir / "spec.json").write_text(
        json.dumps(spec, indent=2), encoding="utf-8")

    stage("inventory")
    inventory = _live_inventory(settings)
    stage("codegen")
    script, files, note, attempts = _live_mod_with_retries(
        provider, model, None, spec, inventory, prompt, image_data,
        job_dir, settings, stage)
    report = {
        "job_dir": str(job_dir),
        "target": "live",
        "spec": spec,
        "files": files,
        "verified": None,
        "note": note,
        "attempts": attempts,
        "summary": _summarize(spec, "live", 1, None),
    }
    (job_dir / "report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8")
    return {"status": "done", "job_dir": str(job_dir), "target": "live",
            "report": report, "files": files, "verified": None,
            "note": note, "attempts": attempts}


def run_mod_turn(*, settings: dict, root: str | Path, chat_id: str,
                 prompt: str, images=None, target: str = "headless",
                 on_status=None) -> dict:
    """Run one chat turn and persist everything. Never raises: failures
    become an assistant error message on the session."""
    session = load_session(root, chat_id)
    if session is None:
        raise ValueError(f"unknown chat: {chat_id}")
    n = len(session["versions"]) + 1
    version_dir = _version_dir(root, session, n)
    ref_names = _save_images(version_dir, images)
    user_msg = {"role": "user", "text": prompt, "images": ref_names,
                "target": target, "version": n}
    with _lock:
        session = load_session(root, chat_id) or session
        session["messages"].append(user_msg)
        session["pending"] = {"turn": n, "target": target, "stage": "spec"}
        save_session(root, session)

    def stage(name: str) -> None:
        with _lock:
            current = load_session(root, chat_id)
            if current is not None and current.get("pending"):
                current["pending"]["stage"] = name
                save_session(root, current)
        if on_status:
            on_status(name)

    def finish_ok(assistant_msg: dict) -> dict:
        with _lock:
            current = load_session(root, chat_id) or session
            current["pending"] = None
            current["messages"].append(assistant_msg)
            save_session(root, current)
        return assistant_msg

    def finish_err(error: str) -> dict:
        return finish_ok({"role": "assistant", "text": "",
                          "status": "error", "error": error, "version": n,
                          "target": target})

    try:
        if target not in ("headless", "live"):
            raise PipelineError(f"unknown target: {target!r}")
        provider = get_provider(settings)
        model = settings.get("model") or "stub"
        blender_exe = None
        if target == "headless":
            # Fail fast, before any provider call is spent.
            blender_exe = blender_arg_from_settings(settings)
        image_data = list(images or [])

        # 1. SPEC (fresh) or REVISE (follow-up).
        stage("spec")
        if session.get("spec") is None:
            messages = [text_msg("system", SPEC_SYSTEM),
                        image_msg("user", prompt, image_data)]
            spec = _complete_json(provider, messages, model)
        else:
            messages = [
                text_msg("system", SPEC_REVISE_SYSTEM),
                text_msg("user", "CURRENT SPEC:\n"
                         + json.dumps(session["spec"], indent=2)
                         + "\n\nCHANGE REQUEST:\n" + prompt),
            ]
            if image_data:
                messages.append(image_msg(
                    "user", "New reference images for this change:",
                    image_data))
            spec = _complete_json(provider, messages, model)
        (version_dir / "spec.json").write_text(
            json.dumps(spec, indent=2), encoding="utf-8")

        # 2-3. CODEGEN + APPLY, per target. Headless rebuilds a new file
        # from the spec; live modifies the open file directly.
        timeout = float(settings.get("timeout") or 600)
        job_id = f"{chat_id}-v{n}"
        verified: bool | None = None
        note = ""
        report = None
        attempts = 1
        if target == "live":
            stage("inventory")
            inventory = _live_inventory(settings)
            stage("codegen")
            script, files, note, attempts = _live_mod_with_retries(
                provider, model, session.get("spec"), spec, inventory,
                prompt, image_data, version_dir, settings, stage)
        else:
            stage("codegen")
            code_reply = provider.complete(
                [text_msg("system", CODEGEN_SYSTEM),
                 text_msg("user", "SPEC:\n" + json.dumps(spec, indent=2)
                          + "\n\nUSER PROMPT:\n" + prompt)],
                model=model, temperature=0.2, max_tokens=8192,
                json_mode=False)
            script = extract_code(code_reply)
            if len(script.encode("utf-8")) > MAX_CODE_BYTES:
                raise PipelineError("generated script larger than 100 KB")
            findings = scan_text(script)
            if findings:
                detail = "; ".join(f"line {ln}: {d}" for ln, d in findings)
                raise PipelineError(f"generated script refused: {detail}")
            (version_dir / "generated.py").write_text(script, encoding="utf-8")
            script_path = version_dir / "generated.py"
            attempts = 1
            while True:
                try:
                    report, verified, files = _apply_headless(
                        script, version_dir, job_id, settings, blender_exe,
                        timeout, stage)
                    break
                except PipelineError as exc:
                    if attempts > MAX_FIX_ATTEMPTS:
                        raise
                    stage("fix")
                    script = fix_script(provider, model, script, str(exc),
                                        live=False)
                    script_path.write_text(script, encoding="utf-8")
                    attempts += 1
        preview = None
        for candidate in ("preview.png", "viewport.png"):
            if candidate in files:
                preview = candidate
                break

        with _lock:
            current = load_session(root, chat_id) or session
            current["spec"] = spec
            current["script"] = script
            current["versions"].append({
                "n": n, "target": target,
                "mode": "direct" if target == "live" else "rebuild",
                "dir": f"versions/v{n}",
                "files": files, "verified": verified,
                "preview": preview, "attempts": attempts,
            })
            if len(current["messages"]) <= 1 and not current.get("title_set"):
                current["title"] = (prompt[:60] or "Chat mod")
            save_session(root, current)

        text = _summarize(spec, target, n, verified)
        if target == "live":
            text += ("\nSudah tampil di viewport Blender yang terbuka — "
                     "tekan Ctrl+S di Blender untuk menyimpan file.")
        if attempts > 1:
            text += (f"\nScript awal gagal {attempts - 1}x di Blender dan "
                     "diperbaiki otomatis dari errornya.")
        if note:
            text += "\n" + note
        return finish_ok({
            "role": "assistant", "text": text, "status": "done",
            "version": n, "target": target, "files": files,
            "preview": preview, "verified": verified,
            "report": report,
        })
    except (ProviderError, PipelineError, BlenderNotFound, LiveError,
            OSError, ValueError) as exc:
        return finish_err(f"{type(exc).__name__}: {exc}")


__all__ = [
    "chats_root",
    "check_mod_script",
    "create_session",
    "delete_all_sessions",
    "delete_session",
    "load_session",
    "save_session",
    "list_sessions",
    "run_live_build",
    "run_mod_turn",
]
