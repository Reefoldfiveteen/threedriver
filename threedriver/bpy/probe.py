"""Probe a SAVED .blend file: geometry facts re-derived from the file itself.

Runs inside headless Blender (fresh process, via the executor). Opens the
.blend, and for every mesh object reports vertex/face counts, world bounding
box, and boundary-edge count (0 = closed solid). If the build report lists
hole centres / solid control points, the through-hole raycasts are re-run
from the saved file in both directions.

Usage (via the executor, never by hand):
    blender -b --python probe.py -- --job-dir DIR --blend FILE.blend
        [--report report.json]

Writes probe.json into the job directory and prints PROBE <path>.
Exit code is 0 when the probe completed (pass/fail lives in the JSON);
non-zero only when the probe itself crashed.
"""

import argparse
import json
import sys
import traceback
from pathlib import Path

import bpy
import bmesh
from mathutils import Vector
from mathutils.bvhtree import BVHTree


def parse_args(argv):
    args = argv[argv.index("--") + 1:] if "--" in argv else []
    p = argparse.ArgumentParser()
    p.add_argument("--job-dir", required=True)
    p.add_argument("--blend", required=True)
    p.add_argument("--report", default=None)
    return p.parse_args(args)


def world_bbox(obj):
    corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    mins = [min(c[i] for c in corners) for i in range(3)]
    maxs = [max(c[i] for c in corners) for i in range(3)]
    return {"min": mins, "max": maxs, "size": [maxs[i] - mins[i] for i in range(3)]}


def probe_object(obj, depsgraph):
    eval_obj = obj.evaluated_get(depsgraph)
    mesh = eval_obj.to_mesh()
    try:
        bm = bmesh.new()
        bm.from_mesh(mesh)
        try:
            boundary = sum(1 for e in bm.edges if len(e.link_faces) != 2)
        finally:
            bm.free()
        return {
            "name": obj.name,
            "vertices": len(mesh.vertices),
            "faces": len(mesh.polygons),
            "bbox": world_bbox(eval_obj),
            "boundary_edges": boundary,
        }
    finally:
        eval_obj.to_mesh_clear()


def raycast_checks(objects, depsgraph, report, overall_bbox):
    """Through-hole checks re-run from the saved file. Z axis only (POC)."""
    if not report or report.get("hole_axis", "Z") != "Z":
        return []
    holes = report.get("hole_centers") or []
    solids = report.get("solid_points") or []
    if not holes and not solids:
        return []
    z_top = overall_bbox["max"][2]
    z_bottom = overall_bbox["min"][2]
    # One BVH per mesh object; a ray "hits" if any object is hit.
    bvhs = [BVHTree.FromObject(o, depsgraph) for o in objects]

    def hit(origin, direction):
        for bvh in bvhs:
            loc, *_ = bvh.ray_cast(Vector(origin), Vector(direction), 10000.0)
            if loc is not None:
                return True
        return False

    checks = []
    for x, y in holes:
        down = hit((x, y, z_top + 10.0), (0, 0, -1))
        up = hit((x, y, z_bottom - 10.0), (0, 0, 1))
        checks.append({"point": [x, y], "kind": "through_hole",
                       "down_hit": down, "up_hit": up,
                       "pass": (not down) and (not up)})
    for x, y in solids:
        down = hit((x, y, z_top + 10.0), (0, 0, -1))
        up = hit((x, y, z_bottom - 10.0), (0, 0, 1))
        checks.append({"point": [x, y], "kind": "solid_control",
                       "down_hit": down, "up_hit": up,
                       "pass": down and up})
    return checks


def main():
    args = parse_args(sys.argv)
    job_dir = Path(args.job_dir)
    probe_path = job_dir / "probe.json"
    try:
        bpy.ops.wm.open_mainfile(filepath=args.blend)
        bpy.context.view_layer.update()
        depsgraph = bpy.context.evaluated_depsgraph_get()
        meshes = [o for o in bpy.data.objects if o.type == "MESH"]
        probed = [probe_object(o, depsgraph) for o in meshes]
        if probed:
            overall = {
                "min": [min(o["bbox"]["min"][i] for o in probed) for i in range(3)],
                "max": [max(o["bbox"]["max"][i] for o in probed) for i in range(3)],
            }
            overall["size"] = [overall["max"][i] - overall["min"][i] for i in range(3)]
        else:
            overall = {"min": [0, 0, 0], "max": [0, 0, 0], "size": [0, 0, 0]}

        report = None
        if args.report:
            report = json.loads(Path(args.report).read_text(encoding="utf-8"))
        checks = raycast_checks(meshes, depsgraph, report, overall)

        matches = None
        if report:
            rb = (report.get("objects") or [{}])[0].get("bbox") if report.get("objects") else None
            pb = probed[0]["bbox"] if probed else None
            matches = bool(rb and pb) and all(
                abs(rb["size"][i] - pb["size"][i]) < 1e-3 for i in range(3)
            ) and (report["objects"][0]["vertices"] == probed[0]["vertices"]
                   if report.get("objects") and probed else False)

        boundary_total = sum(o["boundary_edges"] for o in probed)
        result = {
            "status": "ok",
            "blend": args.blend,
            "blender_version": ".".join(str(v) for v in bpy.app.version),
            "objects": probed,
            "overall_bbox": overall,
            "boundary_edges_total": boundary_total,
            "hole_checks": checks,
            "matches_build_report": matches,
            "pass": boundary_total == 0
                    and all(c["pass"] for c in checks)
                    and (matches is not False),
        }
    except Exception as exc:  # noqa: BLE001 - probe must report, not die silently
        result = {"status": "error", "error": str(exc),
                  "traceback": traceback.format_exc(), "pass": False}
        probe_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"PROBE {probe_path} status=error")
        raise
    probe_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"PROBE {probe_path} pass={result['pass']}")


if __name__ == "__main__":
    main()
