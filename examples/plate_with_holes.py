"""POC-1 example: a 100x60x10 mm plate with 4 through-holes.

Built WITHOUT booleans. Boolean DIFFERENCE has silently failed on some
Blender builds (the build printed "done" but the holes were never cut), so
this script builds the solid directly:

* top/bottom faces: triangulated with mathutils.geometry.tessellate_polygon
  (outer outline + hole rings as holes),
* outer + hole walls: stitched as quads,
* normals recalculated, then verified: 0 boundary edges, positive volume,
  and through-hole raycasts in both directions before anything is saved.

Units: 1 Blender unit = 1 mm (metric, millimetres, scale 0.001), the same
convention used for technical-drawing models.

Run through the executor, never by hand:
    python scripts/run_job.py examples/plate_with_holes.py --verify

Job arguments (after `--`): --job-dir DIR [--width] [--depth] [--thickness]
[--hole-diameter] [--hole-offset-x] [--hole-offset-y] [--hole-segments]
"""

import argparse
import json
import math
import sys
import time
import traceback
from pathlib import Path

import bpy
import bmesh
from mathutils import Vector
from mathutils.bvhtree import BVHTree
from mathutils.geometry import tessellate_polygon

SPEC = {
    "name": "plate_with_holes",
    "width": 100.0,
    "depth": 60.0,
    "thickness": 10.0,
    "hole_diameter": 10.0,
    "hole_offset_x": 35.0,
    "hole_offset_y": 20.0,
    "hole_segments": 48,
}


def parse_args(argv):
    args = argv[argv.index("--") + 1:] if "--" in argv else []
    p = argparse.ArgumentParser()
    p.add_argument("--job-dir", required=True)
    p.add_argument("--width", type=float, default=SPEC["width"])
    p.add_argument("--depth", type=float, default=SPEC["depth"])
    p.add_argument("--thickness", type=float, default=SPEC["thickness"])
    p.add_argument("--hole-diameter", type=float, default=SPEC["hole_diameter"])
    p.add_argument("--hole-offset-x", type=float, default=SPEC["hole_offset_x"])
    p.add_argument("--hole-offset-y", type=float, default=SPEC["hole_offset_y"])
    p.add_argument("--hole-segments", type=int, default=SPEC["hole_segments"])
    return p.parse_args(args)


def clear_scene():
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for coll in (bpy.data.meshes, bpy.data.curves, bpy.data.cameras,
                 bpy.data.lights, bpy.data.materials):
        for block in list(coll):
            if block.users == 0:
                coll.remove(block)


def build_plate_mesh(name, width, depth, thickness, hole_d, ox, oy, segments):
    hw, hd = width / 2.0, depth / 2.0
    r = hole_d / 2.0
    # Outer outline CCW viewed from +Z; hole rings wind the opposite way.
    outer = [Vector((-hw, -hd, 0.0)), Vector((hw, -hd, 0.0)),
             Vector((hw, hd, 0.0)), Vector((-hw, hd, 0.0))]
    centers = [(ox, oy), (-ox, oy), (-ox, -oy), (ox, -oy)]
    rings = []
    for cx, cy in centers:
        ring = [Vector((cx + r * math.cos(-2.0 * math.pi * i / segments),
                        cy + r * math.sin(-2.0 * math.pi * i / segments), 0.0))
                for i in range(segments)]
        rings.append(ring)
    polys = [outer] + rings
    tris = tessellate_polygon(polys)
    flat = [v for poly in polys for v in poly]
    n = len(flat)
    verts = [(v.x, v.y, thickness) for v in flat] + [(v.x, v.y, 0.0) for v in flat]
    faces = []
    for a, b, c in tris:
        faces.append((a, b, c))              # top, +Z
        faces.append((n + c, n + b, n + a))  # bottom, -Z
    for i in range(4):  # outer walls
        j = (i + 1) % 4
        faces.append((i, j, n + j, n + i))
    for k in range(len(rings)):  # hole walls
        base = 4 + k * segments
        for i in range(segments):
            j = (i + 1) % segments
            faces.append((base + i, base + j, n + base + j, n + base + i))
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    boundary = sum(1 for e in bm.edges if len(e.link_faces) != 2)
    volume = bm.calc_volume(signed=True)
    if volume < 0:  # globally flipped: reverse so normals point outward
        bmesh.ops.reverse_faces(bm, faces=bm.faces)
        volume = -volume
    bm.to_mesh(mesh)
    bm.free()
    mesh.update()
    return mesh, boundary, volume, centers


def raycast_checks(obj, centers, z_top, z_bottom):
    bpy.context.view_layer.update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    bvh = BVHTree.FromObject(obj, depsgraph)
    checks = []
    for x, y in centers:
        down, *_ = bvh.ray_cast(Vector((x, y, z_top + 10.0)), Vector((0, 0, -1)), 1000.0)
        up, *_ = bvh.ray_cast(Vector((x, y, z_bottom - 10.0)), Vector((0, 0, 1)), 1000.0)
        checks.append({"point": [x, y], "kind": "through_hole",
                       "down_hit": down is not None, "up_hit": up is not None,
                       "pass": down is None and up is None})
    down, *_ = bvh.ray_cast(Vector((0, 0, z_top + 10.0)), Vector((0, 0, -1)), 1000.0)
    up, *_ = bvh.ray_cast(Vector((0, 0, z_bottom - 10.0)), Vector((0, 0, 1)), 1000.0)
    checks.append({"point": [0, 0], "kind": "solid_control",
                   "down_hit": down is not None, "up_hit": up is not None,
                   "pass": down is not None and up is not None})
    return checks


def look_at(obj, target):
    obj.rotation_euler = (Vector(target) - obj.location).to_track_quat("-Z", "Y").to_euler()


def setup_preview(scene, job_dir):
    world = bpy.data.worlds.new("PreviewWorld")
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs[0].default_value = (0.9, 0.9, 0.9, 1.0)
    world.node_tree.nodes["Background"].inputs[1].default_value = 1.0
    scene.world = world
    sun_data = bpy.data.lights.new("Sun", "SUN")
    sun_data.energy = 3.0
    sun = bpy.data.objects.new("Sun", sun_data)
    sun.rotation_euler = (math.radians(50), 0, math.radians(30))
    scene.collection.objects.link(sun)
    cam_data = bpy.data.cameras.new("PreviewCam")
    cam = bpy.data.objects.new("PreviewCam", cam_data)
    cam.location = (90.0, -110.0, 90.0)
    look_at(cam, (0, 0, 5))
    scene.collection.objects.link(cam)
    scene.camera = cam
    scene.render.engine = "CYCLES"  # EEVEE cannot run headless without libEGL
    scene.cycles.samples = 16
    scene.cycles.device = "CPU"
    scene.cycles.use_denoising = False
    scene.render.resolution_x = 480
    scene.render.resolution_y = 360
    scene.render.resolution_percentage = 100
    scene.render.filepath = str(job_dir / "preview.png")
    scene.render.image_settings.file_format = "PNG"
    bpy.ops.render.render(write_still=True)


def export_stl(obj, path):
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    try:
        bpy.ops.wm.stl_export(filepath=str(path), export_selected_objects=True,
                              ascii_format=False)
    except (AttributeError, TypeError):
        bpy.ops.export_mesh.stl(filepath=str(path), use_selection=True, ascii=False)


def main():
    args = parse_args(sys.argv)
    job_dir = Path(args.job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)
    report_path = job_dir / "report.json"
    started = time.monotonic()
    try:
        clear_scene()
        scene = bpy.context.scene
        scene.unit_settings.system = "METRIC"
        scene.unit_settings.length_unit = "MILLIMETERS"
        scene.unit_settings.scale_length = 0.001

        mesh, boundary, volume, centers = build_plate_mesh(
            "Plate", args.width, args.depth, args.thickness,
            args.hole_diameter, args.hole_offset_x, args.hole_offset_y,
            args.hole_segments)
        if boundary != 0:
            raise RuntimeError(f"mesh is not closed: {boundary} boundary edges")
        if volume <= 0:
            raise RuntimeError(f"mesh volume invalid: {volume}")

        mat = bpy.data.materials.new("PlateGray")
        mat.use_nodes = True
        bsdf = mat.node_tree.nodes["Principled BSDF"]
        bsdf.inputs["Base Color"].default_value = (0.75, 0.75, 0.78, 1.0)
        bsdf.inputs["Roughness"].default_value = 0.6
        obj = bpy.data.objects.new("Plate", mesh)
        scene.collection.objects.link(obj)
        obj.data.materials.append(mat)

        checks = raycast_checks(obj, centers, args.thickness, 0.0)
        if not all(c["pass"] for c in checks):
            raise RuntimeError(f"hole checks failed: {checks}")

        bpy.ops.wm.save_as_mainfile(filepath=str(job_dir / "plate.blend"))
        export_stl(obj, job_dir / "plate.stl")
        setup_preview(scene, job_dir)

        corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
        bbox = {"min": [min(c[i] for c in corners) for i in range(3)],
                "max": [max(c[i] for c in corners) for i in range(3)]}
        bbox["size"] = [bbox["max"][i] - bbox["min"][i] for i in range(3)]
        outputs = []
        for name in ("plate.blend", "plate.stl", "preview.png"):
            p = job_dir / name
            outputs.append({"file": name, "bytes": p.stat().st_size})
        report = {
            "status": "ok",
            "spec": {"width": args.width, "depth": args.depth,
                     "thickness": args.thickness,
                     "hole_diameter": args.hole_diameter,
                     "hole_offset_x": args.hole_offset_x,
                     "hole_offset_y": args.hole_offset_y,
                     "hole_segments": args.hole_segments},
            "blender_version": ".".join(str(v) for v in bpy.app.version),
            "objects": [{"name": obj.name, "vertices": len(mesh.vertices),
                         "faces": len(mesh.polygons), "bbox": bbox}],
            "boundary_edges": boundary,
            "volume_mm3": volume,
            "hole_centers": [list(c) for c in centers],
            "hole_axis": "Z",
            "solid_points": [[0, 0]],
            "build_checks": checks,
            "outputs": outputs,
            "elapsed_s": round(time.monotonic() - started, 2),
        }
    except Exception as exc:  # noqa: BLE001 - report must exist even on failure
        report = {"status": "error", "error": str(exc),
                  "traceback": traceback.format_exc(),
                  "elapsed_s": round(time.monotonic() - started, 2)}
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"REPORT {report_path} status=error: {exc}")
        raise
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"REPORT {report_path} status=ok")


if __name__ == "__main__":
    main()
