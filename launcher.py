# -*- coding: utf-8 -*-
"""Small windowed EXE entry point. It only runs bootstrap.py with a usable Python.

The EXE is a launcher, not a frozen copy of PyTorch: torch/ultralytics stay in the
venv, which keeps the executable tiny and lets bootstrap manage the environment.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(sys.executable).resolve().parent


def find_python() -> list[str]:
    cfg_path = ROOT / "config.json"
    cfg = {}
    if cfg_path.is_file():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    for key in ("python", "venv_python"):
        p = cfg.get(key)
        if p and Path(p).is_file():
            return [p]
    py = shutil.which("python")
    if py:
        return [py]
    if shutil.which("py"):
        return ["py", "-3"]
    return [sys.executable]


def alert(msg: str) -> None:
    try:
        import tkinter.messagebox as mb
        import tkinter as tk
        r = tk.Tk(); r.withdraw(); mb.showerror("YOLO 工坊", msg); r.destroy()
    except Exception:
        print(msg)


def main() -> int:
    (ROOT / "logs").mkdir(exist_ok=True)
    log_path = ROOT / "logs" / "startup.log"
    bootstrap = ROOT / "bootstrap.py"
    if not bootstrap.is_file():
        alert(f"bootstrap.py not found next to the EXE.\nExpected: {bootstrap}")
        return 1
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    with log_path.open("w", encoding="utf-8", errors="replace") as fh:
        proc = subprocess.Popen(find_python() + [str(bootstrap)], cwd=str(ROOT),
                                stdout=fh, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=env, creationflags=flags)
        code = proc.wait()
    if code:
        tail = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-25:])
        alert(f"Startup failed (code {code}).\n\n{tail}\n\nLog: {log_path}")
    return code


if __name__ == "__main__":
    sys.exit(main())
