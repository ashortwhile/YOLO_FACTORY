# -*- coding: utf-8 -*-
"""Background worker: install / complete the ML runtime from inside the app.

Run by server.py as a task:  python worker_setup.py job.json

The local service starts before torch is present, so first launch is instant.
When the user chooses "安装运行环境" on the Environment page, this worker
performs the heavy work (matching torch build, ultralytics, requirements and
the default weights) in the background while the UI streams progress.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

PREFIX = "__YSTUDIO__"
CONFIG = ROOT / "config.json"


def emit(obj: dict) -> None:
    print(PREFIX + json.dumps(obj, ensure_ascii=False), flush=True)


def log(msg: str) -> None:
    print(str(msg), flush=True)


def read_cfg() -> dict:
    if CONFIG.exists():
        try:
            return json.loads(CONFIG.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_cfg(cfg: dict) -> None:
    """Atomic replace; re-read first so server-side edits are not clobbered."""
    fresh = read_cfg()
    fresh.update(cfg)
    tmp = CONFIG.with_name(f"config.json.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(fresh, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG)


def main() -> int:
    if len(sys.argv) < 2:
        log("usage: worker_setup.py job.json")
        return 2
    job_path = Path(sys.argv[1])
    job = json.loads(job_path.read_text(encoding="utf-8"))
    params = job.get("params", {})
    result_path = Path(job["result_path"])
    force = bool(params.get("force", True))
    with_model = bool(params.get("with_model", True))

    result = {"ok": False}
    try:
        import bootstrap
        import hardware

        cfg = read_cfg()
        py = str(params.get("python") or cfg.get("venv_python") or sys.executable)
        cfg["venv_python"] = py
        if not Path(py).exists():
            raise RuntimeError(f"runtime python not found: {py}")

        # Plan drives which torch wheel to fetch. Rebuilt against the real venv
        # so a detected GPU maps to the right CUDA / XPU build.
        plan = hardware.build_plan(cfg, py)
        backend = plan.get("recommended", {}).get("backend", "cpu")
        emit({"type": "stage", "message": f"installing {backend} runtime"})
        log(f"target backend: {backend}  python: {py}")

        # Full install: this is an explicit user action, so refresh packages
        # even if a previous auto-check decided they were current.
        bootstrap.setup_dependencies(cfg, py, plan, True)
        emit({"type": "stage", "message": "runtime installed"})

        if with_model:
            emit({"type": "stage", "message": "downloading default model"})
            bootstrap.auto_fetch_model(cfg)

        # Re-probe now that torch is present; persist for the whole UI.
        full = hardware.build_plan(cfg, py)
        save_cfg({
            "python": cfg.get("python", ""),
            "venv_python": py,
            "hardware": full,
            "device": full["recommended"]["device"] if full["ready"] else "cpu",
            "setup_ok": True,
            "last_setup": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        if not full["ready"]:
            emit({"type": "warn", "message":
                  "runtime installed but accelerator is not active; "
                  "you can switch backend on the Environment page."})
        result = {"ok": True, "backend": full["active"]["backend"],
                  "ready": full["ready"]}
        emit({"type": "done", "result": result})
    except Exception as exc:
        log(traceback.format_exc())
        result = {"ok": False, "error": str(exc)}
        emit({"type": "error", "error": str(exc)})
        # Keep setup_ok False so the UI still offers the install button.
        try:
            save_cfg({"setup_ok": False, "last_error": str(exc)})
        except Exception:
            pass

    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
