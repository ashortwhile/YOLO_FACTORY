# -*- coding: utf-8 -*-
"""Build YOLO工坊.exe - the standalone app launcher (small windowed EXE).

PyTorch is NOT bundled - it stays in the venv managed by bootstrap.py, which is
what makes the "download and auto-configure" workflow possible.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"


def make_icon() -> str | None:
    try:
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([6, 6, 250, 250], radius=54, fill=(37, 99, 235, 255))
        d.rounded_rectangle([6, 6, 250, 250], radius=54, outline=(14, 165, 233, 255), width=4)
        d.line([(74, 76), (128, 150), (182, 76)], fill="white", width=22, joint="curve")
        d.line([(128, 144), (128, 196)], fill="white", width=22)
        ico = ROOT / "app.ico"
        img.save(ico, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
        return str(ico)
    except Exception as exc:
        print("icon skipped:", exc)
        return None


def main() -> int:
    try:
        import PyInstaller  # noqa
    except ImportError:
        print("Installing PyInstaller (one time)...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller", "-i", MIRROR])
    ico = make_icon()
    cmd = [sys.executable, "-m", "PyInstaller", "--onefile", "--windowed", "--clean",
           "--name", "YOLOStudio", "--distpath", str(ROOT / "dist"),
           "--workpath", str(ROOT / "build"), "--specpath", str(ROOT / "build"),
           str(ROOT / "launcher.py")]
    if ico:
        cmd[6:6] = ["--icon", ico]
    print("Building:", " ".join(cmd))
    code = subprocess.call(cmd, cwd=str(ROOT))
    if code:
        print("PyInstaller failed with code", code)
        return code
    src = ROOT / "dist" / "YOLOStudio.exe"
    dst = ROOT / "YOLO工坊.exe"
    if src.is_file():
        shutil.copy2(src, dst)
        print(f"\nDone: {dst}  ({dst.stat().st_size / 1048576:.1f} MB)")
        print("This is the standalone launcher. build_setup.py bundles it into the installer.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
