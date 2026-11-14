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


def make_splash() -> Path | None:
    """Shown by the onefile bootloader *while* it unpacks (before Python starts)."""
    dst = ROOT / "splash.png"
    try:
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new("RGB", (560, 300), (247, 249, 252))
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, 560, 8], fill=(37, 99, 235))
        d.rounded_rectangle([210, 44, 350, 184], radius=22, fill=(37, 99, 235))
        d.rectangle([250, 82, 310, 142], outline="white", width=7)
        d.ellipse([271, 103, 289, 121], fill="white")
        font_paths = [r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf"]
        def font(size):
            for fp in font_paths:
                try:
                    return ImageFont.truetype(fp, size)
                except Exception:
                    continue
            return ImageFont.load_default()
        d.text((280, 214), "YOLO 工坊", font=font(30), fill=(15, 23, 42), anchor="mm")
        d.text((280, 254), "正在准备安装程序，请稍候…",
               font=font(16), fill=(100, 116, 139), anchor="mm")
        img.save(dst)
        return dst
    except Exception as exc:
        print("splash skipped:", exc)
        return None


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
    splash = make_splash()
    cmd = [sys.executable, "-m", "PyInstaller", "--onefile", "--windowed", "--clean",
           "--noconfirm", "--name", "YOLOStudioSetup",
           "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build"),
           "--specpath", str(ROOT / "build"),
           "--add-data", f"{PAYLOAD}{os.pathsep}."]
    if splash:
        cmd += ["--splash", str(splash)]
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
