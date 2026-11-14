# -*- coding: utf-8 -*-
"""Build the single-file installer: YOLO工坊-安装版.exe

Pipeline: make the clean portable ZIP -> embed it as payload.zip ->
PyInstaller onefile/windowed EXE. The EXE is an installer, the app launcher
and the uninstaller in one file (see installer.py).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORTABLE = ROOT.parent / "YOLO工坊-绿色版.zip"
PAYLOAD = ROOT / "payload.zip"
ICO = ROOT / "app.ico"
OUT = ROOT.parent / "YOLO工坊-安装版.exe"


def make_icon() -> Path | None:
    try:
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([6, 6, 250, 250], radius=54, fill=(37, 99, 235, 255))
        d.rounded_rectangle([6, 6, 250, 250], radius=54,
                            outline=(56, 189, 248, 255), width=5)
        d.rectangle([62, 62, 194, 194], outline="white", width=12)
        d.ellipse([112, 112, 144, 144], fill="white")
        img.save(ICO, sizes=[(16, 16), (32, 32), (48, 48), (64, 64),
                             (128, 128), (256, 256)])
        return ICO
    except Exception as exc:
        print("icon skipped:", exc)
        return None


def main() -> int:
    try:
        import PyInstaller  # noqa
    except ImportError:
        print("Installing PyInstaller (one time)...", flush=True)
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller",
                               "-i", "https://pypi.tuna.tsinghua.edu.cn/simple"])
    subprocess.check_call([sys.executable, str(ROOT / "make_release.py")], cwd=str(ROOT))
    shutil.copy2(PORTABLE, PAYLOAD)
    ico = make_icon()
    cmd = [sys.executable, "-m", "PyInstaller", "--onefile", "--windowed", "--clean",
           "--name", "YOLOStudioSetup",
           "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build"),
           "--specpath", str(ROOT / "build"),
           "--add-data", f"{PAYLOAD}{os.pathsep}."]
    if ico:
        cmd += ["--icon", str(ico), "--add-data", f"{ico}{os.pathsep}."]
    cmd.append(str(ROOT / "installer.py"))
    print("Building installer:", " ".join(cmd), flush=True)
    code = subprocess.call(cmd, cwd=str(ROOT))
    if code:
        print("PyInstaller failed with code", code)
        return code
    src = ROOT / "dist" / "YOLOStudioSetup.exe"
    if not src.is_file():
        print("built exe not found:", src)
        return 1
    shutil.copy2(src, OUT)
    print("\nDone: %s  (%.1f MB)" % (OUT, OUT.stat().st_size / 1048576))
    print("Double-click it to install. Desktop + Start Menu shortcuts and an "
          "uninstall entry are created automatically.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
