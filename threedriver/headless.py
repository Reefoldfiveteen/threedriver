"""Headless Blender executor.

One job = one fresh Blender process + one isolated job directory. The executor
never builds a shell string: Blender is always spawned with an argument list
and ``shell=False``. Every job has a mandatory timeout: terminate, a grace
period, then kill.

This module is stdlib-only and Windows-ready. The only platform differences
are how Blender is located and how a timed-out process is killed, both handled
here so callers never branch on the OS.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .scan import scan_file

DEFAULT_TIMEOUT = 600
GRACE_SECONDS = 10
LOG_TAIL_LINES = 30


class BlenderNotFound(RuntimeError):
    """Raised when no Blender executable can be located."""


class UnsafeScript(RuntimeError):
    """Raised when the pre-execution text scan finds forbidden patterns."""


def resolve_blender(explicit: str | Path | None = None) -> str:
    """Locate the Blender executable.

    Order: explicit argument, THREEDRIVER_BLENDER env var, PATH, then the
    standard Windows install locations for Blender 5.2.
    """
    candidates: list[str] = []
    if explicit:
        candidates.append(str(explicit))
    env_path = os.environ.get("THREEDRIVER_BLENDER")
    if env_path:
        candidates.append(env_path)
    on_path = shutil.which("blender")
    if on_path:
        candidates.append(on_path)
    if platform.system() == "Windows":
        for base in (
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        ):
            candidates.append(
                os.path.join(base, r"Blender Foundation\Blender 5.2\blender.exe")
            )
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise BlenderNotFound(
        "Blender executable not found. Install Blender 5.2 LTS, put it on "
        "PATH, pass --blender, or set THREEDRIVER_BLENDER."
    )


def new_job_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"job-{stamp}-{uuid.uuid4().hex[:6]}"


def new_job_dir(jobs_root: str | Path, job_id: str | None = None) -> tuple[str, Path]:
    """Create and return (job_id, isolated job directory)."""
    job_id = job_id or new_job_id()
    job_dir = Path(jobs_root) / job_id
    job_dir.mkdir(parents=True, exist_ok=False)
    return job_id, job_dir


@dataclass
class JobResult:
    job_id: str
    job_dir: Path
    returncode: int | None
    timed_out: bool
    elapsed_s: float
    report: dict | None
    log_path: Path
    report_path: Path
    stdout_tail: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and self.report is not None


def _stop(proc: subprocess.Popen, grace: float = GRACE_SECONDS) -> None:
    """Terminate, wait a grace period, then kill. POSIX kills the whole group."""
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
    except (ProcessLookupError, PermissionError):
        return
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait()


def run_job(
    script: str | Path,
    job_dir: str | Path,
    *,
    job_id: str | None = None,
    blender: str | Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    extra_args: list[str] | None = None,
    report_name: str = "report.json",
    scan: bool = True,
) -> JobResult:
    """Run one bpy script in a fresh headless Blender process.

    ``script`` receives ``--job-dir <dir>`` plus ``extra_args`` after the
    ``--`` separator. The script is expected to write ``report_name`` into the
    job directory. Returns a JobResult; never raises for a Blender failure
    (check ``.ok`` / ``.returncode``), only for local setup problems.
    """
    script = Path(script).resolve()
    job_dir = Path(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)
    if not script.is_file():
        raise FileNotFoundError(f"bpy script not found: {script}")
    if scan:
        findings = scan_file(script)
        if findings:
            detail = "; ".join(f"line {n}: {d}" for n, d in findings)
            raise UnsafeScript(f"refusing to run {script.name}: {detail}")

    blender_exe = resolve_blender(blender)
    log_path = job_dir / f"{script.stem}.log"
    report_path = job_dir / report_name
    cmd = [
        blender_exe,
        "-b",
        "--python",
        str(script),
        "--",
        "--job-dir",
        str(job_dir),
        *(extra_args or []),
    ]
    started = time.monotonic()
    timed_out = False
    with open(log_path, "w", encoding="utf-8") as log:
        log.write(f"$ {' '.join(cmd)}\n\n")
        log.flush()
        proc = subprocess.Popen(
            cmd,
            stdout=log,
            stderr=subprocess.STDOUT,
            shell=False,
            start_new_session=(os.name == "posix"),
        )
        try:
            returncode = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _stop(proc)
            returncode = proc.returncode
    elapsed = time.monotonic() - started

    report = None
    if report_path.is_file():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            report = None
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    return JobResult(
        job_id=job_id or job_dir.name,
        job_dir=job_dir,
        returncode=returncode,
        timed_out=timed_out,
        elapsed_s=elapsed,
        report=report,
        log_path=log_path,
        report_path=report_path,
        stdout_tail=lines[-LOG_TAIL_LINES:],
    )
