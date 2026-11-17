"""Stub provider: deterministic demo/test backend, no network, no key.

The spec step always returns the plate specification; the codegen step
returns the proven plate example script (``examples/plate_with_holes.py``
from the repo checkout, or a small built-in cube script if the file cannot
be found). Use it to try the whole loop — prompt, job, build, verify —
before any API key exists, and in automated smoke tests.
"""

from __future__ import annotations

import json
from pathlib import Path

PLATE_SPEC = {
    "name": "plate_with_holes",
    "description": "Flat rectangular plate with 4 through-holes (demo spec).",
    "units": "mm",
    "dimensions": {"width": 100, "depth": 60, "thickness": 10},
    "features": [
        {"type": "through_hole", "diameter": 10, "count": 4,
         "positions": [[35, 20], [-35, 20], [-35, -20], [35, -20]]}
    ],
    "notes": "Stub provider demo spec; replace with a real provider.",
}

_FALLBACK_CUBE = '''"""Fallback stub script: 20 mm cube (used only if the example file is missing)."""
import argparse, json, sys, time, traceback
from pathlib import Path
import bpy
from mathutils import Vector

def main():
    argv = sys.argv[sys.argv.index("--") + 1:]
    p = argparse.ArgumentParser()
    p.add_argument("--job-dir", required=True)
    args = p.parse_args(argv)
    job_dir = Path(args.job_dir)
    started = time.monotonic()
    try:
        for obj in list(bpy.data.objects):
            bpy.data.objects.remove(obj, do_unlink=True)
        scene = bpy.context.scene
        scene.unit_settings.system = "METRIC"
        scene.unit_settings.length_unit = "MILLIMETERS"
        scene.unit_settings.scale_length = 0.001
        mesh = bpy.data.meshes.new("Cube")
        vs = [(-10, -10, 0), (10, -10, 0), (10, 10, 0), (-10, 10, 0),
              (-10, -10, 20), (10, -10, 20), (10, 10, 20), (-10, 10, 20)]
        fs = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4),
              (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
        mesh.from_pydata(vs, [], fs)
        mesh.update()
        obj = bpy.data.objects.new("Cube", mesh)
        scene.collection.objects.link(obj)
        bpy.ops.wm.save_as_mainfile(filepath=str(job_dir / "model.blend"))
        bpy.ops.object.select_all(action="DESELECT")
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        try:
            bpy.ops.wm.stl_export(filepath=str(job_dir / "model.stl"),
                                  export_selected_objects=True, ascii_format=False)
        except (AttributeError, TypeError):
            bpy.ops.export_mesh.stl(filepath=str(job_dir / "model.stl"),
                                    use_selection=True, ascii=False)
        corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
        bbox = {"min": [min(c[i] for c in corners) for i in range(3)],
                "max": [max(c[i] for c in corners) for i in range(3)]}
        bbox["size"] = [bbox["max"][i] - bbox["min"][i] for i in range(3)]
        report = {"status": "ok", "objects": [{"name": obj.name,
                    "vertices": len(mesh.vertices), "faces": len(mesh.polygons),
                    "bbox": bbox}],
                  "boundary_edges": 0, "hole_axis": "Z",
                  "outputs": [{"file": "model.blend",
                               "bytes": (job_dir / "model.blend").stat().st_size},
                              {"file": "model.stl",
                               "bytes": (job_dir / "model.stl").stat().st_size}],
                  "elapsed_s": round(time.monotonic() - started, 2)}
    except Exception as exc:
        report = {"status": "error", "error": str(exc),
                  "traceback": traceback.format_exc(),
                  "elapsed_s": round(time.monotonic() - started, 2)}
        (job_dir / "report.json").write_text(json.dumps(report, indent=2))
        raise
    (job_dir / "report.json").write_text(json.dumps(report, indent=2))

if __name__ == "__main__":
    main()
'''


def _find_example_script() -> str | None:
    here = Path(__file__).resolve()
    for parent in [here.parent, *here.parents]:
        candidate = parent / "examples" / "plate_with_holes.py"
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    return None


class StubProvider:
    def complete(
        self,
        messages: list[dict],
        *,
        model: str = "stub",
        temperature: float = 0.2,
        max_tokens: int = 4096,
        json_mode: bool = False,
    ) -> str:
        system_text = " ".join(
            part.get("text", "")
            for msg in messages if msg["role"] == "system"
            for part in msg["parts"] if part["type"] == "text"
        ).lower()
        if "specification" in system_text or json_mode:
            return json.dumps(PLATE_SPEC, indent=2)
        script = _find_example_script()
        if script is None:
            return _FALLBACK_CUBE
        return "```python\n" + script + "\n```"
