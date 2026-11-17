"""Pre-execution text scan for AI-generated bpy scripts.

This is NOT a sandbox. It is a cheap tripwire for a personal, single-operator
tool: code the LLM writes must not touch the network, spawn subprocesses, or
run shell commands. Anything that looks like that fails closed before Blender
starts. The real isolation is the per-job Blender process, the timeout, and
the separate job directory (see headless.py).
"""

from __future__ import annotations

import re
from pathlib import Path

# (pattern, human-readable reason)
FORBIDDEN: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bimport\s+(urllib|requests|httpx|socket|ftplib|smtplib)\b"),
     "network import"),
    (re.compile(r"\bfrom\s+(urllib|requests|httpx|socket|http)\s+import\b"),
     "network import"),
    (re.compile(r"\bsubprocess\b"), "subprocess use"),
    (re.compile(r"\bos\.system\b|\bos\.popen\b|\bcommands\b"), "shell execution"),
    (re.compile(r"\beval\s*\(|\bexec\s*\("), "dynamic code execution"),
    (re.compile(r"__import__\s*\("), "dynamic import"),
    (re.compile(r"\bctypes\b"), "ctypes / native code"),
]


def scan_text(text: str) -> list[tuple[int, str]]:
    """Return [(line_number, reason)] for every forbidden pattern found."""
    findings: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.split("#", 1)[0]  # ignore trailing comments
        for pattern, reason in FORBIDDEN:
            if pattern.search(stripped):
                findings.append((lineno, reason))
                break
    return findings


def scan_file(path: str | Path) -> list[tuple[int, str]]:
    return scan_text(Path(path).read_text(encoding="utf-8"))
