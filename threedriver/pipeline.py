"""Pipeline: prompt + images -> spec JSON -> bpy script -> headless -> verify.

Two LLM steps, then the POC-1 executor and probe do the rest:

1. SPEC: a vision-capable model reads the prompt and reference images and
   returns a dimensioned specification as JSON.
2. CODEGEN: a model writes a self-contained Blender Python script from that
   spec, following the bpy rules in CODEGEN_SYSTEM (bpy.data/bmesh style,
   no booleans, millimetre units, report.json + model.blend + model.stl +
   preview.png in the job directory).
3. BUILD: the script runs in a fresh headless Blender process (see
   headless.py). The generated code is text-scanned first.
4. VERIFY: a second headless process re-opens the saved .blend and
   re-derives the geometry facts from the file itself (see bpy/probe.py).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .config import load_settings
from .headless import (
    BlenderNotFound,
    blender_arg_from_settings,
    new_job_dir,
    run_job,
)
from .providers import ProviderError, get_provider, text_msg
from .providers.base import image_msg
from .scan import scan_text

SPEC_SYSTEM = """You extract a precise 3D modelling specification from the user's \
prompt and reference images. Shape follows the reference images; sizes follow \
any dimensions the user states. If the user gives no dimensions, infer \
sensible ones in millimetres and say so in "notes" — never leave dimensions \
empty, and never invent extra features the user did not ask for. Count \
features (holes, ribs, bosses) from the references, never from a guess.

Reply with ONLY a JSON object, no prose, no markdown fences:
{
  "name": "short_snake_case_name",
  "description": "one sentence, what the part is",
  "units": "mm",
  "dimensions": {"width": 0, "depth": 0, "height": 0},
  "features": [{"type": "through_hole", "diameter": 0, "count": 0,
                "positions": [[x, y]]}],
  "notes": "assumptions made, if any"
}
Use "features" for every hole, slot, rib, boss, thread, or label the part \
needs, with numbers. Keep the schema above; add keys only when needed."""

CODEGEN_SYSTEM = """You write a self-contained Blender Python (bpy) script that builds the \
3D part described by the SPEC JSON below. The script runs as \
`blender -b --python script.py -- --job-dir <dir>` in Blender 5.2 LTS, \
headless, with no UI. Follow these rules exactly:

1. UNITS: millimetres. Set scene.unit_settings.system = "METRIC", \
length_unit = "MILLIMETERS", scale_length = 0.001. Model coordinates are \
plain millimetre numbers (1 Blender unit = 1 mm).
2. STYLE: bpy.data / bmesh only. Do NOT use bpy.ops for modelling \
(bpy.ops is UI-context dependent and fails headless). Allowed ops: \
wm.save_as_mainfile, wm.stl_export (fall back to export_mesh.stl), \
render.render, object.select_all/select_set for export selection.
3. NO BOOLEANS: never use boolean modifiers or boolean operations — they \
have silently failed to cut holes on some builds. Build holes and slots \
directly: triangulate faces with holes via \
mathutils.geometry.tessellate_polygon and stitch the walls as quads, or \
construct the mesh vertex by vertex. Recalculate normals and check the \
mesh is closed (0 boundary edges) before saving.
4. ARGUMENTS: read everything after "--" from sys.argv with argparse. \
--job-dir is required; write ALL outputs into it, nothing elsewhere.
5. SCENE: delete all default objects first. Give the part a clear object \
name. Add one simple material.
6. VERIFY INSIDE THE SCRIPT: after building, raycast every through-hole \
in BOTH directions with mathutils.bvhtree.BVHTree and fail loudly (raise) \
if a hole does not pass through or a solid control point does not hit.
7. OUTPUTS (all in --job-dir, exact names): model.blend \
(wm.save_as_mainfile), model.stl, preview.png (Cycles CPU, 16 samples, \
480x360, one camera + one sun light; NEVER EEVEE — it cannot run \
headless), and report.json.
8. report.json schema (exact keys):
{"status": "ok", "spec": <the spec object>, "blender_version": "5.2.0",
 "objects": [{"name": ..., "vertices": n, "faces": n,
              "bbox": {"min": [x,y,z], "max": [x,y,z], "size": [x,y,z]}}],
 "boundary_edges": 0, "hole_centers": [[x, y]], "hole_axis": "Z",
 "solid_points": [[x, y]], "build_checks": [...],
 "outputs": [{"file": "model.blend", "bytes": n}], "elapsed_s": 0.0}
bbox must be in millimetres. hole_centers lists every through-hole centre \
(or []); solid_points lists control points that must hit solid.
9. ERRORS: on any failure, still write report.json with \
{"status": "error", "error": str, "traceback": ...} and re-raise.
10. FORBIDDEN: network access, subprocess, shell commands, eval/exec, \
writing outside --job-dir. The script is scanned and refused if it \
contains them.
11. BMESH PRIMITIVES: bmesh.ops parameter names are exact — never guess \
them. The primitives you may use, with their real signatures (Blender \
5.2): create_cube(size, matrix, calc_uvs); create_cone(cap_ends, \
cap_tris, segments, radius1, radius2, depth, matrix, calc_uvs); \
create_uvsphere(u_segments, v_segments, radius, matrix, calc_uvs); \
create_icosphere(subdivisions, radius, matrix, calc_uvs); \
create_circle(cap_ends, cap_tris, segments, radius, matrix, calc_uvs); \
create_grid(x_segments, y_segments, size, matrix, calc_uvs). Note \
cap_ends (plural) and cap_tris — NOT cap_end or cap_triangles. When in \
doubt, build the mesh from vertices and faces directly (bmesh \
verts/faces or from_pydata): that has no signature to get wrong.

Reply with ONLY the Python code in one ```python fenced block, no prose."""


class PipelineError(RuntimeError):
    """A pipeline stage failed (spec, codegen, scan, build, or verify)."""


def extract_json(text: str) -> dict:
    """Pull a JSON object out of model output (fenced or bare)."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise PipelineError("model reply contained no JSON object")
        data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise PipelineError("spec JSON is not an object")
    return data


def extract_code(text: str) -> str:
    """Pull the Python script out of model output (fenced or bare)."""
    fenced = re.search(r"```python\s*(.*?)\s*```", text, re.DOTALL)
    if fenced:
        return fenced.group(1).strip()
    fenced = re.search(r"```\s*(.*?)\s*```", text, re.DOTALL)
    if fenced and "import bpy" in fenced.group(1):
        return fenced.group(1).strip()
    if "import bpy" in text:
        return text.strip()
    raise PipelineError("model reply contained no Blender Python script")


MAX_CODE_BYTES = 100_000
MAX_FIX_ATTEMPTS = 2

FIX_SYSTEM = """You fix a Blender Python (bpy) script that FAILED when Blender ran it. \
You are given the failed script and the exact error Blender reported \
(usually a traceback naming the broken line and the wrong call).

Rules:
1. Return the COMPLETE corrected script — not a patch, not a fragment. \
Change ONLY what the error requires; keep everything else (shape, \
dimensions, structure) exactly as it was.
2. Read the traceback literally: if it says a keyword is invalid for an \
operator, use the operator's real parameter names (rule 4); if it says \
an attribute does not exist, use the correct attribute.
3. {CONTRACT}
4. bmesh.ops parameter names are exact — never guess them. The \
primitives you may use, with their real signatures (Blender 5.2): \
create_cube(size, matrix, calc_uvs); create_cone(cap_ends, cap_tris, \
segments, radius1, radius2, depth, matrix, calc_uvs); \
create_uvsphere(u_segments, v_segments, radius, matrix, calc_uvs); \
create_icosphere(subdivisions, radius, matrix, calc_uvs); \
create_circle(cap_ends, cap_tris, segments, radius, matrix, calc_uvs); \
create_grid(x_segments, y_segments, size, matrix, calc_uvs). Note \
cap_ends (plural) and cap_tris — NOT cap_end or cap_triangles. When in \
doubt, build the mesh from vertices and faces directly.
5. Same bans as always: no booleans, no network, subprocess, shell \
commands, eval/exec, no bpy.ops modelling calls.

Do NOT explain the fix in prose. Reply with ONLY the Python code in one \
```python fenced block."""


def fix_script(provider, model: str, failed_script: str, error: str,
               live: bool) -> str:
    """Ask the model to repair a script that failed at execution.
    Returns the corrected script (extracted + scanned). Raises
    PipelineError when the reply has no code or the repair is refused
    by the scan. Callers running live must additionally apply
    check_mod_script (save/open guard) to the result."""
    if live:
        contract = ("The script runs inside a GUI Blender via the live "
                    "addon: it takes NO arguments, writes NO files, and "
                    "must not save or open files.")
    else:
        contract = ("The script runs headless with --job-dir and must "
                    "write model.blend, model.stl, preview.png and "
                    "report.json into the job dir.")
    messages = [
        text_msg("system", FIX_SYSTEM.replace("{CONTRACT}", contract)),
        text_msg("user", "FAILED SCRIPT:\n```python\n" + failed_script +
                 "\n```\n\nBLENDER ERROR:\n" + error),
    ]
    reply = provider.complete(messages, model=model, temperature=0.0,
                              max_tokens=8192, json_mode=False)
    code = extract_code(reply)
    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        raise PipelineError("repaired script larger than 100 KB")
    findings = scan_text(code)
    if findings:
        detail = "; ".join(f"line {n}: {d}" for n, d in findings)
        raise PipelineError(f"repaired script refused: {detail}")
    return code


def _find_blend(job_dir: Path) -> Path | None:
    preferred = job_dir / "model.blend"
    if preferred.is_file():
        return preferred
    blends = sorted(job_dir.glob("*.blend"))
    return blends[0] if blends else None


def run_pipeline(
    *,
    prompt: str,
    images: list[tuple[bytes, str]] | None = None,
    settings: dict | None = None,
    jobs_root: str | Path | None = None,
    job_id: str | None = None,
    on_status=None,
) -> dict:
    """Run the full loop. Returns a summary dict; never raises for a job
    failure — check result["status"] ("done" / "error"). Raises only for
    local setup problems (missing provider key, missing Blender)."""
    settings = settings or load_settings()
    images = images or []
    provider = get_provider(settings)  # raises ProviderError if unusable
    model = settings.get("model") or "stub"
    root = Path(jobs_root or settings.get("jobs_root") or Path.cwd() / "tmp" / "jobs")
    job_id, job_dir = new_job_dir(root, job_id)

    def stage(name: str) -> None:
        if on_status:
            on_status(name)

    def fail(where: str, error: str, extra: dict | None = None) -> dict:
        result = {"job_id": job_id, "job_dir": str(job_dir),
                  "status": "error", "failed_at": where, "error": error}
        if extra:
            result.update(extra)
        return result

    try:
        # Resolve the Blender location first: a bad custom path must fail
        # before any provider call is spent.
        blender_exe = blender_arg_from_settings(settings)

        # 1. SPEC
        stage("spec")
        spec_messages = [
            text_msg("system", SPEC_SYSTEM),
            image_msg("user", prompt, images),
        ]
        try:
            spec_reply = provider.complete(
                spec_messages, model=model, temperature=0.2,
                max_tokens=2048, json_mode=True)
            spec = extract_json(spec_reply)
        except (ProviderError, PipelineError, json.JSONDecodeError):
            # One repair attempt: ask for bare JSON only.
            repair = spec_messages + [text_msg(
                "user", "Your previous reply was not valid JSON. Reply with "
                        "ONLY the JSON object, no other text.")]
            spec_reply = provider.complete(
                repair, model=model, temperature=0.0,
                max_tokens=2048, json_mode=True)
            spec = extract_json(spec_reply)
        (job_dir / "spec.json").write_text(
            json.dumps(spec, indent=2), encoding="utf-8")

        # 2. CODEGEN
        stage("codegen")
        codegen_messages = [
            text_msg("system", CODEGEN_SYSTEM),
            text_msg("user", "SPEC:\n" + json.dumps(spec, indent=2)
                     + "\n\nUSER PROMPT:\n" + prompt),
        ]
        code_reply = provider.complete(
            codegen_messages, model=model, temperature=0.2,
            max_tokens=8192, json_mode=False)
        code = extract_code(code_reply)
        script_path = job_dir / "generated.py"
        script_path.write_text(code, encoding="utf-8")
        findings = scan_text(code)
        if findings:
            detail = "; ".join(f"line {n}: {d}" for n, d in findings)
            return fail("scan", f"generated script refused: {detail}",
                        {"spec": spec})

        # 3. BUILD, with auto-fix retries: when the script crashes in
        # Blender (a wrong kwarg, a missing attribute), the traceback
        # goes back to the model for a repair instead of failing the
        # job outright. Timeouts are NOT retried — a fix prompt cannot
        # cure a job that ran out of time.
        attempts = 1
        while True:
            stage("build")
            result = run_job(
                script_path, job_dir, job_id=job_id,
                blender=blender_exe,
                timeout=float(settings.get("timeout") or 600),
                scan=False,  # scanned above / in fix_script; scan once
            )
            if result.ok:
                break
            tail = "\n".join(result.stdout_tail[-10:])
            error_text = (
                f"Blender exited {result.returncode} "
                f"(timed out: {result.timed_out}). Log tail:\n{tail}")
            if result.timed_out or attempts > MAX_FIX_ATTEMPTS:
                return fail("build", error_text,
                            {"spec": spec, "report": result.report,
                             "attempts": attempts})
            stage("fix")
            (job_dir / f"generated_attempt{attempts}.py").write_text(
                code, encoding="utf-8")
            code = fix_script(provider, model, code,
                              "\n".join(result.stdout_tail[-30:]),
                              live=False)
            script_path.write_text(code, encoding="utf-8")
            attempts += 1
        report = result.report or {}

        # 4. VERIFY
        stage("verify")
        blend = _find_blend(job_dir)
        probe_report = None
        if blend is not None and (job_dir / "report.json").is_file():
            probe = run_job(
                Path(__file__).resolve().parent / "bpy" / "probe.py",
                job_dir, job_id=job_id,
                blender=blender_exe,
                timeout=float(settings.get("timeout") or 600),
                extra_args=["--blend", str(blend),
                            "--report", str(job_dir / "report.json")],
                report_name="probe.json",
                scan=False,
            )
            probe_report = probe.report
        stage("done")
        files = sorted(p.name for p in job_dir.iterdir() if p.is_file())
        verified = bool(probe_report and probe_report.get("pass"))
        return {"job_id": job_id, "job_dir": str(job_dir), "status": "done",
                "spec": spec, "report": report, "probe": probe_report,
                "verified": verified, "files": files,
                "attempts": attempts}
    except BlenderNotFound as exc:
        return fail("build", str(exc))
    except ProviderError as exc:
        return fail("provider", str(exc))
    except PipelineError as exc:
        return fail("pipeline", str(exc))
