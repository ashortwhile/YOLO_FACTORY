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


def _with_progress(cmd: list[str]) -> list[str]:
    """Ask pip to emit byte progress so the UI can draw a real progress bar."""
    if ("-m" in cmd and "pip" in cmd and "install" in cmd
            and "--progress-bar" not in cmd):
        # insert right after "install"
        i = cmd.index("install") + 1
        return cmd[:i] + ["--progress-bar", "on"] + cmd[i:]
    return cmd


def _backend_ready(backend: str, info: dict) -> bool:
    """Does the installed torch already satisfy the requested backend?"""
    if not info.get("version"):
        return False
    checks = {
        "xpu": bool(info.get("xpu")),
        "cuda": bool(info.get("cuda")),
        "rocm": bool(info.get("hip")),
        "mps": bool(info.get("mps")),
        "dml": bool(info.get("directml")),
    }
    if backend in checks:
        return checks[backend]
    # CPU: any working torch with no other accelerator is fine.
    return "cpu" in str(info.get("version", "")).lower() or not info.get("cuda")


def emit(obj: dict) -> None:
    print(PREFIX + json.dumps(obj, ensure_ascii=False), flush=True)


def log(msg: str) -> None:
    print(str(msg), flush=True)


def run(cmd: list[str], py: str) -> bool:
    cmd = _with_progress(cmd)
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
        variant = "xpu"
    elif backend == "cuda":
        plan = hardware.build_plan(cfg, py)
        variant = hardware.cuda_tag(plan.get("nvidia", {}).get("cuda_version", ""))
    elif backend == "mps":
        variant = "cpu"
    else:
        variant = "cpu"

    # Domestic mirror first (fast), official PyTorch index as fallback.
    indexes = hardware.torch_indexes(variant)
    index = indexes[0]
    log(f"Target backend : {backend}")
    log(f"Wheel index    : {index}  (fallback: {', '.join(indexes[1:]) or 'none'})")

    # disk preflight: never uninstall a working torch unless the new build
    # actually fits on the drive (a full C: once destroyed a working XPU env)
    import shutil
    free_gb = shutil.disk_usage(Path(py).anchor).free / 2 ** 30
    need_gb = 2.0 if backend in ("cpu", "mps") else 6.0
    log(f"Disk free      : {free_gb:.1f} GB on {Path(py).anchor} (need ~{need_gb:.0f} GB)")
    if free_gb < need_gb:
        msg = (f"磁盘剩余 {free_gb:.1f} GB，装不下 {backend} 版 torch（约需 {need_gb:.0f} GB）。"
               "已保留现有 torch 不做改动；请把软件安装到空间充足的磁盘（例如 D 盘）后重试。")
        log(msg)
        emit({"type": "warn", "message": msg})
        result["ok"] = False
        result["error"] = f"磁盘空间不足（剩余 {free_gb:.1f} GB），已保留现有环境"
        emit({"type": "done", "result": result})
        Path(job["result_path"]).write_text(
            json.dumps({"ok": False, "result": result}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        return 1

    # ---- no-op guard: already running the requested backend? ----
    # Never uninstall a working build just to reinstall the identical one —
    # that wasted minutes re-downloading 700 MB and, if the reinstall failed,
    # destroyed a perfectly good XPU env. Verify first, skip if satisfied.
    current = hardware.probe_torch(py)
    if _backend_ready(backend, current):
        log(f"Already on {backend} (torch {current.get('version')}, "
            f"torchvision {current.get('torchvision')}) -> nothing to install.")
        result["ok"] = True
        result["verified"] = True
        result["torch"] = current.get("version", "")
        result["skipped"] = True
        emit({"type": "stage", "message": f"{backend} 已就绪，无需重装"})
        emit({"type": "done", "result": result})
        Path(job["result_path"]).write_text(
            json.dumps({"ok": True, "result": result}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        return 0

    emit({"type": "stage", "message": f"installing torch for {backend}"})

    # Replace only torch / torchvision, keep the rest of the venv intact.
    # Snapshot the current build first so a failed install can restore it
    # instead of leaving the user stranded on a broken / CPU-only environment.
    prev_version = current.get("version") or ""
    prev_tv = current.get("torchvision") or ""
    run([py, "-m", "pip", "uninstall", "-y", "torch", "torchvision"], py)
    ok = False
    for idx in indexes:
        log(f"Trying wheel index: {idx}")
        ok = run([py, "-m", "pip", "install", "torch", "torchvision",
                  "--index-url", idx, "--retries", "2"], py)
        if ok:
            break
        log("  [warn] that index failed, trying the next one...")

    if not ok:
        restored = False
        if prev_version:
            # Try to put back exactly what was there before we uninstalled.
            # Infer its variant from the version tag (+xpu / +cuNNN / plain cpu).
            log(f"Install failed. Restoring previous torch {prev_version} ...")
            emit({"type": "warn", "message": "安装失败，正在恢复原有 PyTorch"})
            low = prev_version.lower()
            if "+xpu" in low:
                prev_variant = "xpu"
            elif "+cu" in low:
                prev_variant = low.split("+", 1)[1].split(".", 1)[0]
            else:
                prev_variant = "cpu"
            for idx in hardware.torch_indexes(prev_variant):
                spec = ["torch==" + prev_version]
                if prev_tv:
                    spec.append("torchvision==" + prev_tv)
                if run([py, "-m", "pip", "install", *spec,
                        "--index-url", idx, "--retries", "2"], py):
                    restored = True
                    break
        if not restored:
            log("Restore failed / no previous build. Falling back to CPU torch.")
            emit({"type": "warn", "message": "install failed, restoring CPU torch"})
            for idx in hardware.torch_indexes("cpu"):
                if run([py, "-m", "pip", "install", "torch", "torchvision",
                        "--index-url", idx, "--retries", "2"], py):
                    break
        result["error"] = ("torch install failed; restored previous build" if restored
                           else "torch install failed; rolled back to CPU")
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

    # Refresh cached hardware info. Re-read the file first: server.py and
    # bootstrap.py may have updated config.json during this pip install.
    try:
        fresh = (json.loads(CONFIG.read_text(encoding="utf-8"))
                 if CONFIG.exists() else dict(cfg))
        plan = hardware.build_plan(fresh, py)
        fresh["hardware"] = plan
        fresh["device"] = plan["recommended"]["device"] if plan["ready"] else "cpu"
        tmp = CONFIG.with_name(f"config.json.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(fresh, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, CONFIG)
    except Exception as exc:
        log(f"config refresh skipped: {exc}")

    emit({"type": "done", "result": result})
    Path(job["result_path"]).write_text(
        json.dumps({"ok": result["ok"], "result": result}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
