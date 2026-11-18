# -*- coding: utf-8 -*-
"""Build a portable ZIP that a friend can download, unzip and double-click.

It contains only code + launchers. No venv, no personal data, no machine-specific
config.json: the recipient's first run performs the full environment setup.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT.parent / "YOLO工坊-绿色版.zip"
TOP = "YOLOStudio"
SKIP_DIRS = {".venv", "logs", "__pycache__", "dist", "build", ".git", ".idea",
             ".appprofile", ".workbuddy", "workspace", "cache", "tests"}
SKIP_FILES = {"config.json", "app.ico", "splash.png", "make_release.py",
              "build_exe.py", "build_setup.py", "installer.py", "打包安装版.bat"}
# The standalone app launcher is intentionally shipped inside the package;
# all other EXEs (notably the setup EXE) must stay out.
KEEP_EXE = {"YOLO工坊.exe"}
EMPTY_DIRS = ["workspace/datasets", "workspace/models", "workspace/exports",
              "workspace/runs", "logs"]

import os

files = []
# prune skipped trees (notably the multi-GB .venv) up front
for dirpath, dirnames, filenames in os.walk(ROOT):
    dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
    base = Path(dirpath)
    for name in sorted(filenames):
        p = base / name
        rel = p.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if p.name in SKIP_FILES:
            continue
        if p.suffix.lower() in (".pyc", ".zip", ".spec"):
            continue
        if p.suffix.lower() == ".exe" and p.name not in KEEP_EXE:
            continue
        files.append((p, str(Path(TOP) / rel)))

with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    for src, arc in files:
        z.write(src, arc)
    for d in EMPTY_DIRS:
        zi = zipfile.ZipInfo(f"{TOP}/{d}/")
        zi.external_attr = 0o40775 << 16
        z.writestr(zi, "")

print(f"Portable package: {OUT}")
print(f"Size: {OUT.stat().st_size / 1048576:.2f} MB, {len(files)} files")
for _, arc in files:
    print("  " + arc)
print("\nRecipient: unzip -> double-click YOLO工坊.exe -> automatic setup.")
