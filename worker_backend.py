# -*- coding: utf-8 -*-
"""Install / switch the PyTorch accelerator build (NVIDIA CUDA, Intel XPU, AMD ROCm, CPU).

Run as a subprocess by server.py:  python worker_backend.py job.json
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import env_utils

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"
MIRRORS = {
    "tsinghua": "https://pypi.tuna.tsinghua.edu.cn/simple",
    "aliyun": "https://mirrors.aliyun.com/pypi/simple",
    "pypi": "https://pypi.org/simple",
}
PREFIX = "__YSTUDIO__"


def emit(obj: dict) -> None:
    print(PREFIX + json.dumps(obj, ensure_ascii=False), flush=True)


def log(msg: str) -> None:
    print(str(msg), flush=True)


def run(cmd: list[str], py: str) -> bool:
    log("\n$ " + " ".join(cmd))
    try:
        # Strip injected sitecustomize shims: pip must be able to unlink files
        # while replacing wheels, otherwise every backend switch fails.
        p = subprocess.run(cmd, cwd=str(ROOT), text=True, encoding="utf-8",
                           errors="replace", timeout=3600,
                           env=env_utils.clean_env())
        ok = p.returncode == 0
    except Exception as exc:
        log(f"failed: {exc}")
        ok = False
    if not ok:
        emit({"type": "warn", "message": "command failed: " + " ".join(cmd[:6])})
    return ok


def main() -> int:
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    params = job.get("params", {})
    backend = params.get("backend", "cpu")
    index = params.get("index", "")
    mirror = params.get("mirror", "tsinghua")
    py = params.get("python") or sys.executable
    cfg = json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}
    mirror_url = MIRRORS.get(mirror, MIRRORS["tsinghua"])
    result = {"backend": backend, "ok": False}

    sys.path.insert(0, str(ROOT))
    import hardware

    if backend == "rocm":
        emit({"type": "warn", "message":
              "ROCm Windows wheels are not published by PyTorch. Install AMD HIP SDK + ROCm torch "
              "manually (or use WSL2/Linux), then re-detect; the studio will use device=0 automatically."})
        result["error"] = "Windows ROCm requires AMD HIP SDK; no automatic install"
        emit({"type": "done", "result": result})
        Path(job["result_path"]).write_text(json.dumps({"ok": False, "result": result},
                                                       ensure_ascii=False), encoding="utf-8")
        return 1

    if backend == "dml":
        log("Installing torch-directml (experimental, inference oriented)...")
        ok = run([py, "-m", "pip", "install", "-U", "torch-directml", "-i", mirror_url], py)
        result["ok"] = ok
        emit({"type": "done", "result": result})
        Path(job["result_path"]).write_text(json.dumps({"ok": ok, "result": result},
                                                       ensure_ascii=False), encoding="utf-8")
        return 0 if ok else 1

    if backend == "xpu":
        index = hardware.TORCH_ROOT + "xpu"
    elif backend == "cuda":
        plan = hardware.build_plan(cfg, py)
        tag = hardware.cuda_tag(plan.get("nvidia", {}).get("cuda_version", ""))
        index = hardware.TORCH_ROOT + tag
    elif backend == "cpu":
        index = hardware.TORCH_ROOT + "cpu"
    elif backend == "mps":
        index = hardware.TORCH_ROOT + "cpu"

    log(f"Target backend : {backend}")
    log(f"Wheel index    : {index}")
    emit({"type": "stage", "message": f"installing torch for {backend}"})

    # Replace only torch / torchvision, keep the rest of the venv intact
    run([py, "-m", "pip", "uninstall", "-y", "torch", "torchvision"], py)
    ok = run([py, "-m", "pip", "install", "torch", "torchvision",
              "--index-url", index, "--retries", "2"], py)

    if not ok:
        log("Install failed. Restoring a working CPU torch so the environment stays usable...")
        emit({"type": "warn", "message": "install failed, restoring CPU torch"})
        run([py, "-m", "pip", "install", "torch", "torchvision",
             "--index-url", hardware.TORCH_ROOT + "cpu", "--retries", "2"], py)
        result["error"] = "torch install failed; rolled back to CPU"
        result["ok"] = False
    else:
        if backend in ("xpu", "rocm", "cuda", "mps"):
            emit({"type": "stage", "message": "upgrading ultralytics for multi-accelerator support"})
            run([py, "-m", "pip", "install", "-U", "ultralytics", "-i", mirror_url], py)
        emit({"type": "stage", "message": "verifying accelerator"})
        info = hardware.probe_torch(py)
        log("torch: " + json.dumps(info, ensure_ascii=False))
        key = {"xpu": "xpu", "cuda": "cuda", "rocm": "hip", "cpu": "version",
               "mps": "mps"}.get(backend, "version")
        verified = bool(info.get(key)) if key != "hip" else bool(info.get("hip"))
        if backend == "cpu":
            verified = "cpu" in info.get("version", "") or not info.get("cuda")
        result["ok"] = verified
        result["torch"] = info.get("version", "")
        result["verified"] = verified
        if not verified:
            result["error"] = (f"torch installed but {backend} is not available. "
                               "Check the GPU driver / runtime, or stay on CPU.")
            emit({"type": "warn", "message": result["error"]})

    # refresh cached hardware info in config.json
    try:
        cfg["hardware"] = hardware.build_plan(cfg, py)
        cfg["device"] = cfg["hardware"]["recommended"]["device"] if cfg["hardware"]["ready"] else "cpu"
        CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        log(f"config refresh skipped: {exc}")

    emit({"type": "done", "result": result})
    Path(job["result_path"]).write_text(
        json.dumps({"ok": result["ok"], "result": result}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
