# -*- coding: utf-8 -*-
"""Build the installer: YOLO工坊-安装版.exe

Separate-EXE layout:
  1. build_exe.py  -> YOLO工坊.exe  (independent launcher, from launcher.py)
  2. make_release.py -> portable ZIP (code + web + the launcher EXE)
  3. that ZIP becomes payload.zip, embedded into the installer
  4. PyInstaller onefile/windowed on installer.py -> the setup EXE

The setup EXE only installs / uninstalls. The app is launched by the separate
YOLO工坊.exe copied into the install directory (see installer.py).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORTABLE = ROOT.parent / "YOLO工坊-绿色版.zip"
PAYLOAD = ROOT / "payload.zip"
ICO = ROOT / "app.ico"
LAUNCHER_EXE = ROOT / "YOLO工坊.exe"       # built by build_exe.py
OUT = ROOT.parent / "YOLO工坊-安装版.exe"
SETUP_NAME = "YOLOStudioSetup"


def _has_module(py: str, mod: str) -> bool:
    try:
        return subprocess.run([py, "-c", "import " + mod],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=30).returncode == 0
    except Exception:
        return False


def ensure_gui_python() -> None:
    """The installer GUI wizard AND the standalone launcher both import tkinter.
    Some slim/embedded Pythons (e.g. the WorkBuddy 3.13 runtime) ship without it:
    a setup EXE built with such an interpreter crashes on launch with
    "No module named 'tkinter'". Restart the build with a full Python that has
    tkinter so the frozen EXE is actually usable."""
    if _has_module(sys.executable, "tkinter"):
        return
    candidates = []
    home = Path.home()
    candidates += sorted((home / "AppData" / "Local" / "Programs" / "Python").glob(
        "Python*/python.exe"), reverse=True)
    candidates += sorted(Path("C:/").glob("Python*/python.exe"), reverse=True)
    candidates.append(ROOT / ".venv" / "Scripts" / "python.exe")
    for py in candidates:
        if py.is_file() and _has_module(str(py), "tkinter"):
            print("Current Python lacks tkinter; rebuilding with:", py, flush=True)
            code = subprocess.call([str(py), str(Path(__file__).resolve())] + sys.argv[1:],
                                   cwd=str(ROOT))
            sys.exit(code)
    raise RuntimeError(
        "No Python with tkinter was found. The installer GUI needs tkinter; "
        "please build with a full python.org Python (e.g. Python 3.10).")


def ensure_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa
    except ImportError:
        print("Installing PyInstaller (one time)...", flush=True)
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])


def make_splash() -> Path | None:
    """Shown by the onefile bootloader *while* it unpacks (before Python starts)."""
    dst = ROOT / "splash.png"
    try:
        import tkinter  # noqa: F401  (PyInstaller --splash requires tkinter at build time)
    except ImportError:
        print("splash skipped: this Python build has no tkinter; "
              "the installer still builds, just without a splash image.")
        return None
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


def build_launcher(ico: Path | None) -> None:
    """Step 1: build the standalone app launcher (launcher.py -> YOLO工坊.exe)."""
    print("\n[1/3] Building standalone launcher (YOLO工坊.exe)...", flush=True)
    subprocess.check_call([sys.executable, str(ROOT / "build_exe.py")], cwd=str(ROOT))
    if not LAUNCHER_EXE.is_file():
        raise RuntimeError("launcher build did not produce %s" % LAUNCHER_EXE)
    print("  launcher ready: %.1f MB" % (LAUNCHER_EXE.stat().st_size / 1048576))


def build_payload() -> None:
    """Step 2: portable ZIP (now includes the launcher) -> payload.zip."""
    print("\n[2/3] Building payload (code + web + launcher)...", flush=True)
    subprocess.check_call([sys.executable, str(ROOT / "make_release.py")], cwd=str(ROOT))
    shutil.copy2(PORTABLE, PAYLOAD)
    # Sanity check: the installer refuses to build if the launcher is missing.
    with zipfile.ZipFile(PAYLOAD) as zf:
        names = set(zf.namelist())
    want = "YOLOStudio/" + LAUNCHER_EXE.name
    if want not in names:
        raise RuntimeError("payload is missing the launcher (%s)" % want)
    print("  payload ready: %.1f MB" % (PAYLOAD.stat().st_size / 1048576))


def build_installer(ico: Path | None, splash: Path | None) -> Path:
    """Step 3: PyInstaller onefile on installer.py -> setup EXE."""
    print("\n[3/3] Building installer EXE...", flush=True)
    cmd = [sys.executable, "-m", "PyInstaller", "--onefile", "--windowed", "--clean",
           "--noconfirm", "--name", SETUP_NAME,
           "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build"),
           "--specpath", str(ROOT / "build"),
           "--hidden-import", "tkinter", "--hidden-import", "tkinter.ttk",
           "--hidden-import", "tkinter.filedialog", "--hidden-import", "tkinter.messagebox",
           "--add-data", f"{PAYLOAD}{os.pathsep}."]
    if splash:
        cmd += ["--splash", str(splash)]
    if ico:
        cmd += ["--icon", str(ico), "--add-data", f"{ico}{os.pathsep}."]
    cmd.append(str(ROOT / "installer.py"))
    print("Building installer:", " ".join(cmd), flush=True)
    code = subprocess.call(cmd, cwd=str(ROOT))
    if code:
        raise RuntimeError("PyInstaller failed with code %d" % code)
    src = ROOT / "dist" / (SETUP_NAME + ".exe")
    if not src.is_file():
        raise RuntimeError("built exe not found: %s" % src)
    shutil.copy2(src, OUT)
    return src


def main() -> int:
    try:
        ensure_gui_python()
        ensure_pyinstaller()
        ico = make_icon()
        splash = make_splash()
        build_launcher(ico)
        build_payload()
        build_installer(ico, splash)
        print("\nDone: %s  (%.1f MB)" % (OUT, OUT.stat().st_size / 1048576))
        print("It contains TWO programs: this setup installs/uninstalls; the")
        print("installed YOLO工坊.exe is the standalone app launcher.")
        return 0
    except Exception as exc:
        print("\nBuild failed: %s" % exc, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
