# -*- coding: utf-8 -*-
"""YOLO 工坊 —— 独立启动器 (YOLO工坊.exe).

打包后的本 EXE 放在安装目录，双击即启动软件：找一个可用的 Python，
运行同目录的 bootstrap.py（由它配置环境、启动本地服务并打开窗口）。

本程序只负责「启动」；安装 / 卸载由安装程序（YOLOStudioSetup.exe）负责。
PyTorch / ultralytics 不打进本 EXE，而是留在 venv 里由 bootstrap 管理，
所以本 EXE 很小，且能自动适配不同显卡。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def app_root() -> Path:
    """Install dir when frozen; source dir when run as a plain script."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


ROOT = app_root()
IS_WIN = os.name == "nt"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WIN else 0
DETACHED = 0x00000008 if IS_WIN else 0


def log(msg: str) -> None:
    try:
        p = ROOT / "logs" / "launcher.log"
        p.parent.mkdir(exist_ok=True)
        with p.open("a", encoding="utf-8", errors="replace") as fh:
            fh.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def find_python() -> list[str] | None:
    cfg_path = ROOT / "config.json"
    if cfg_path.is_file():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            # Prefer the configured venv: it holds the GPU-specific torch build.
            for key in ("venv_python", "python"):
                p = cfg.get(key)
                if p and Path(p).is_file():
                    return [p]
        except Exception:
            pass
    for name in ("python", "python3"):
        found = shutil.which(name)
        if found:
            return [found]
    if shutil.which("py"):
        return ["py", "-3"]
    return None


def native_alert(msg: str, title: str = "YOLO 工坊", timeout_ms: int = 0) -> None:
    """Win32 MessageBox works even when Tk is unavailable (windowed build)."""
    if not IS_WIN:
        print(msg)
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        flags = 0x40 | 0x1000                      # info | topmost
        if timeout_ms:
            try:
                user32.MessageBoxTimeoutW(0, str(msg), str(title), flags, 0, timeout_ms)
                return
            except Exception:
                pass
        user32.MessageBoxW(0, str(msg), str(title), flags)
    except Exception:
        print(msg)


def single_instance() -> bool:
    """Avoid several launcher windows when the user double-clicks repeatedly."""
    if not IS_WIN:
        return True
    try:
        import ctypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        k.CreateMutexW.restype = ctypes.c_void_p
        handle = k.CreateMutexW(None, False, "Local\\YOLOStudioLauncherMutex")
        already = ctypes.get_last_error() == 183
        if not handle or already:
            native_alert("YOLO 工坊已经在运行了，请稍候。", timeout_ms=4000)
            return False
        globals()["_INSTANCE_MUTEX"] = handle
        return True
    except Exception as exc:
        log("mutex skipped: %r" % exc)
        return True


def service_port() -> int:
    try:
        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        return int(cfg.get("port") or 8765)
    except Exception:
        return 8765


def service_ready(port: int) -> bool:
    import socket
    with socket.socket() as s:
        s.settimeout(0.15)
        return s.connect_ex(("127.0.0.1", port)) == 0


def show_startup_progress(proc, startup: Path, port: int) -> None:
    """Visible feedback while bootstrap configures the environment (minutes on first run)."""
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception as exc:
        log("progress window unavailable: %r" % exc)
        return
    try:
        root = tk.Tk()
        root.title("YOLO 工坊 · 正在启动")
        root.geometry("600x330")
        root.resizable(False, False)
        root.configure(bg="#f7f9fc")
        tk.Label(root, text="正在配置运行环境", font=("Microsoft YaHei", 16, "bold"),
                 fg="#0f172a", bg="#f7f9fc").pack(anchor="w", padx=22, pady=(18, 2))
        tk.Label(root, text="软件可直接浏览；训练等功能所需运行环境可在软件内按需安装。",
                 font=("Microsoft YaHei", 9), fg="#64748b",
                 bg="#f7f9fc").pack(anchor="w", padx=22)
        bar = ttk.Progressbar(root, mode="indeterminate", length=556)
        bar.pack(padx=22, pady=(12, 8))
        bar.start(12)
        text = tk.Text(root, height=10, bg="#ffffff", fg="#334155", relief="flat",
                       font=("Consolas", 9), wrap="word")
        text.pack(fill="both", expand=True, padx=22, pady=(0, 18))
        text.insert("1.0", "准备中…\n")

        def poll():
            content = ""
            try:
                content = startup.read_text(encoding="utf-8", errors="replace")
                text.delete("1.0", "end")
                text.insert("1.0", "\n".join(content.splitlines()[-12:]) + "\n")
                text.see("end")
            except Exception:
                pass
            # bootstrap logs "URL: http://..." right before the app window opens
            ready = ("URL: http://" in content or "Service already running" in content
                     or service_ready(port))
            if proc.poll() is not None or ready:
                root.destroy()
                return
            root.after(600, poll)

        root.after(400, poll)
        root.mainloop()
    except Exception as exc:
        log("progress window failed: %r" % exc)


def main() -> int:
    if not single_instance():
        return 0
    bootstrap = ROOT / "bootstrap.py"
    if not bootstrap.is_file():
        native_alert("找不到 bootstrap.py：%s\n请确认 YOLO 工坊已正确安装。" % bootstrap)
        return 1
    py = find_python()
    if not py:
        native_alert("没有找到可用的 Python。\n\n请先安装 Python 3.9~3.13，"
                     "或在「设置 → 应用」中修复安装。")
        return 1

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env.pop("PYTHONPATH", None)          # avoid injected sitecustomize hooks
    (ROOT / "logs").mkdir(exist_ok=True)
    startup = ROOT / "logs" / "startup.log"
    log("launch: %s bootstrap.py (root=%s)" % (" ".join(py), ROOT))
    try:
        with startup.open("w", encoding="utf-8", errors="replace") as fh:
            proc = subprocess.Popen(
                py + [str(bootstrap)], cwd=str(ROOT), stdout=fh,
                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                env=env, creationflags=NO_WINDOW)
            show_startup_progress(proc, startup, service_port())
            code = proc.wait()
        if code:
            tail = "\n".join(startup.read_text(encoding="utf-8", errors="replace")
                             .splitlines()[-25:])
            native_alert("启动失败（退出码 %s）。\n\n%s\n\n完整日志：%s" % (code, tail, startup))
        return code
    except Exception as exc:
        log("launch failed: %r" % exc)
        native_alert("启动失败：%s" % exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
