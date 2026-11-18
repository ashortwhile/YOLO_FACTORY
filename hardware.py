# -*- coding: utf-8 -*-
"""Hardware / accelerator detection shared by bootstrap.py and server.py.

Vendors covered: NVIDIA (CUDA), AMD (ROCm / HIP), Intel (XPU), Apple (MPS),
plus DirectML as a universal Windows fallback. Detection never installs
anything - it only reports what is present and what would be needed.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import env_utils

# Official PyTorch wheel index. Kept as the fallback of last resort.
TORCH_ROOT = "https://download.pytorch.org/whl/"
# Domestic mirror of the SAME index (full mirror: cpu / xpu / cuNNN tags).
# download.pytorch.org is hosted overseas and is very slow from mainland China;
# the SJTU mirror carries identical wheels over a domestic line. If a tag is
# ever missing there, callers fall back to TORCH_ROOT.
TORCH_MIRROR_ROOT = "https://mirror.sjtu.edu.cn/pytorch-wheels/"
CUDA_TAGS = ["cu132", "cu130", "cu128", "cu126", "cu124", "cu121"]
MIN_ULTRALYTICS = "8.4.111"  # first release with unified XPU / ROCm / NPU device handling

# Variants that the domestic mirror is known to carry. Keep this explicit so a
# future tag is never silently assumed to exist (would break offline installs).
_MIRRORED_TAGS = {"cpu", "xpu", "cu132", "cu130", "cu128", "cu126", "cu124", "cu121"}


def torch_index_url(variant: str, prefer_domestic: bool = True) -> str:
    """Return the --index-url for a torch variant (cpu/xpu/cuNNN).

    Domestic mirror is preferred for speed; official index stays available as
    a fallback via the caller. Pass prefer_domestic=False to force official.
    """
    tag = variant.strip().rstrip("/").split("/")[-1] or "cpu"
    if prefer_domestic and tag.lower() in _MIRRORED_TAGS:
        return TORCH_MIRROR_ROOT + tag
    return TORCH_ROOT + tag


def torch_indexes(variant: str) -> list[str]:
    """Ordered list of indexes to try: domestic first, official fallback."""
    seen, out = set(), []
    for url in (torch_index_url(variant, True), torch_index_url(variant, False)):
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out

VIRTUAL_HINTS = ("virtual display", "microsoft basic", "vmware", "virtualbox",
                 "parsec", "gameviewer", "remote display", "dummy")

PROBE_CODE = r'''
import json, importlib.util, importlib.metadata as md
import torch
def v(p):
    try: return md.version(p)
    except Exception: return ""
d = {"version": torch.__version__,
     "hip": getattr(torch.version, "hip", None),
     "cuda": bool(torch.cuda.is_available()),
     "cuda_name": "", "xpu": False, "xpu_name": "",
     "mps": False, "directml": bool(importlib.util.find_spec("torch_directml")),
     "ultralytics": v("ultralytics"), "torchvision": v("torchvision")}
if d["cuda"]:
    try: d["cuda_name"] = torch.cuda.get_device_name(0)
    except Exception: pass
try:
    if hasattr(torch, "xpu") and torch.xpu.is_available():
        d["xpu"] = True
        try: d["xpu_name"] = torch.xpu.get_device_name(0)
        except Exception: pass
except Exception: pass
try:
    d["mps"] = bool(getattr(torch.backends, "mps", None) and torch.backends.mps.is_available())
except Exception: pass
print("__YHW__" + json.dumps(d, ensure_ascii=False))
'''


def _run(cmd: list[str], timeout: int = 20) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                             encoding="utf-8", errors="replace",
                             env=env_utils.clean_env())
        return out.stdout or ""
    except Exception:
        return ""


def vendor_of(name: str) -> str:
    n = name.lower()
    if any(k in n for k in ("nvidia", "geforce", "rtx", "quadro", "tesla", "a100", "h100")):
        return "nvidia"
    if any(k in n for k in ("amd", "radeon", "instinct", "ati ", "rx 6", "rx 7", "rx 9", "gfx")):
        return "amd"
    if any(k in n for k in ("intel", "arc", "iris", "uhd graphics", "hd graphics")):
        return "intel"
    if "apple" in n:
        return "apple"
    return "unknown"


def list_gpus() -> list[dict]:
    """Return physical display adapters with vendor / driver / VRAM."""
    gpus: list[dict] = []
    if sys.platform == "win32":
        # Win32_VideoController.AdapterRAM is a signed 32-bit value and wraps above 4 GB,
        # so read the 64-bit HardwareInformation.qwMemorySize from the display class registry key.
        ps = (
            "$key='HKLM:\\SYSTEM\\ControlSet001\\Control\\Class\\"
            "{4d36e968-e325-11ce-bfc1-08002be10318}'; "
            "$reg=@{}; Get-ChildItem $key -ErrorAction SilentlyContinue | ForEach-Object { "
            "$p=Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue; "
            "if($p.DriverDesc -and $p.'HardwareInformation.qwMemorySize'){ "
            "$reg[[string]$p.DriverDesc]=[int64]$p.'HardwareInformation.qwMemorySize' } }; "
            "Get-CimInstance Win32_VideoController | ForEach-Object { "
            "[pscustomobject]@{ Name=$_.Name; DriverVersion=$_.DriverVersion; "
            "AdapterRAM=$_.AdapterRAM; QW=[int64]($reg[[string]$_.Name]) } } | "
            "ConvertTo-Json -Compress"
        )
        raw = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], 30)
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                data = [data]
            for item in data or []:
                name = str(item.get("Name") or "").strip()
                if name and not any(h in name.lower() for h in VIRTUAL_HINTS):
                    qw = int(item.get("QW") or 0)
                    fallback = int(item.get("AdapterRAM") or 0)
                    vram = qw if qw > 0 and qw < (1 << 48) else fallback
                    gpus.append({
                        "name": name,
                        "vendor": vendor_of(name),
                        "driver": str(item.get("DriverVersion") or ""),
                        "vram_mb": round(vram / 1048576),
                        "vram_source": "registry(qwMemorySize)" if qw > 0 else "WMI(32-bit, may wrap)",
                    })
        except Exception:
            # older Windows: wmic fallback
            raw = _run(["wmic", "path", "win32_VideoController", "get", "name,driverversion", "/format:csv"], 15)
            for line in raw.splitlines():
                parts = line.split(",")
                if len(parts) >= 3 and parts[1].strip() and "Name" not in parts[1]:
                    name = parts[1].strip()
                    if not any(h in name.lower() for h in VIRTUAL_HINTS):
                        gpus.append({"name": name, "vendor": vendor_of(name),
                                     "driver": parts[2].strip(), "vram_mb": 0})
    elif sys.platform == "linux":
        raw = _run(["bash", "-lc", "lspci 2>/dev/null | grep -Ei 'vga|3d|display'"], 12)
        for line in raw.splitlines():
            name = line.split(":")[-1].strip()
            gpus.append({"name": name, "vendor": vendor_of(name), "driver": "", "vram_mb": 0})
    elif sys.platform == "darwin":
        raw = _run(["system_profiler", "SPDisplaysDataType"], 12)
        for line in raw.splitlines():
            if "Chipset Model:" in line:
                gpus.append({"name": line.split(":", 1)[1].strip(),
                             "vendor": "apple", "driver": "", "vram_mb": 0})
    return gpus


def nvidia_info() -> dict:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return {}
    out = _run([exe])
    m = re.search(r"CUDA Version:\s*(\d+\.\d+)", out)
    name = _run([exe, "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader"]).strip()
    return {"cuda_version": m.group(1) if m else "", "smi": name}


def cuda_tag(cuda_version: str) -> str:
    """Map a driver CUDA version to the newest *published* wheel tag.

    Only tags listed in CUDA_TAGS exist on download.pytorch.org; returning an
    invented one (e.g. cu134) makes the whole install fail with a 404.
    """
    try:
        v = float(cuda_version)
    except (TypeError, ValueError):
        return CUDA_TAGS[2]
    thresholds = ((13.4, CUDA_TAGS[0]), (13.2, CUDA_TAGS[0]),
                  (13.0, CUDA_TAGS[1]), (12.8, CUDA_TAGS[2]),
                  (12.6, CUDA_TAGS[3]), (12.4, CUDA_TAGS[4]))
    for minimum, tag in thresholds:
        if v >= minimum:
            return tag
    return CUDA_TAGS[5]


def probe_torch(python_exe: str) -> dict:
    out = _run([python_exe, "-c", PROBE_CODE], 90)
    m = re.search(r"__YHW__(\{.*\})", out)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    return {"version": "", "hip": None, "cuda": False, "cuda_name": "",
            "xpu": False, "xpu_name": "", "mps": False, "directml": False,
            "ultralytics": "", "torchvision": "", "error": out[-400:] or "probe failed"}


def version_tuple(v: str) -> tuple:
    nums = re.findall(r"\d+", v or "0")
    return tuple(int(x) for x in (nums + ["0", "0", "0"])[:3])


def build_plan(cfg: dict, python_exe: str = "", *, quick: bool = False) -> dict:
    """Combine physical GPUs + installed torch capabilities into an actionable plan."""
    gpus = list_gpus()
    nv = nvidia_info()
    order = {"nvidia": 0, "amd": 1, "intel": 2, "apple": 3, "unknown": 9}
    primary = sorted(gpus, key=lambda g: order.get(g["vendor"], 9))[0] if gpus else None
    vendor = primary["vendor"] if primary else ("cpu" if not nv else "nvidia")

    py = python_exe or cfg.get("venv_python") or sys.executable
    torch_info = probe_torch(py) if Path(py).exists() else {}

    # what is actually usable right now
    if torch_info.get("hip"):
        active, active_device = "rocm", "0"
    elif torch_info.get("cuda"):
        active, active_device = "cuda", "0"
    elif torch_info.get("xpu"):
        active, active_device = "xpu", "xpu:0"
    elif torch_info.get("mps"):
        active, active_device = "mps", "mps"
    else:
        active, active_device = "cpu", "cpu"

    labels = {"nvidia": "NVIDIA 显卡 (CUDA)", "amd": "AMD 显卡 (ROCm/HIP)",
              "intel": "Intel 显卡 (XPU)", "apple": "Apple Silicon (MPS)",
              "cpu": "仅 CPU / 未检测到显卡", "unknown": "未识别显卡"}

    recommended = "cpu"
    device = "cpu"
    torch_index = torch_index_url("cpu")
    min_ultra = ""
    note = "未检测到可用显卡，使用 CPU 训练。"
    experimental = False
    steps: list[str] = []

    if vendor == "nvidia":
        recommended, device = "cuda", "0"
        tag = cuda_tag(nv.get("cuda_version", ""))
        torch_index = torch_index_url(tag)
        # primary can be None when nvidia-smi answers but WDDM enumeration
        # found no adapter; indexing into None used to crash the whole setup.
        gpu = primary or {}
        note = (f"检测到 {gpu.get('name') or 'NVIDIA GPU'}，推荐 CUDA 后端（{tag}）。"
                f"驱动 {gpu.get('driver') or '未知'}，运行时 CUDA {nv.get('cuda_version') or '未知'}。")
        steps = [f"安装 {tag} 版 torch / torchvision", "验证 torch.cuda.is_available()"]
    elif vendor == "intel":
        recommended, device = "xpu", "xpu:0"
        torch_index = torch_index_url("xpu")
        min_ultra = MIN_ULTRALYTICS
        note = (f"检测到 {primary['name']}（驱动 {primary['driver']}）。"
                "官方 PyTorch XPU 构建可直接用 device=xpu:0 训练；"
                f"需要 ultralytics >= {MIN_ULTRALYTICS} 才支持 XPU 设备选择。")
        steps = ["安装 XPU 版 torch / torchvision", f"升级 ultralytics 到 >= {MIN_ULTRALYTICS}",
                 "验证 torch.xpu.is_available()"]
    elif vendor == "amd":
        recommended, device = "rocm", "0"
        torch_index = "https://repo.amd.com/rocm/whl/gfx110X-all/  (AMD HIP SDK)"
        min_ultra = MIN_ULTRALYTICS
        experimental = True
        note = (f"检测到 {primary['name']}。ROCm 官方 wheel 目前不提供 Windows 原生版本："
                "若已装好 AMD HIP SDK + ROCm 版 torch，本软件会自动用 device=0；"
                "否则建议 WSL2/Linux 走 ROCm，Windows 下先回退 CPU（也可用 DirectML 做推理）。")
        steps = ["确认已安装 AMD HIP SDK / ROCm 运行时", "安装 ROCm 版 torch",
                 f"升级 ultralytics 到 >= {MIN_ULTRALYTICS}", "验证 torch.cuda.is_available()"]
    elif vendor == "apple":
        recommended, device = "mps", "mps"
        torch_index = torch_index_url("cpu")
        note = "检测到 Apple Silicon，使用 MPS 后端。"

    ultra = torch_info.get("ultralytics", "")
    need_upgrade = bool(min_ultra and version_tuple(ultra) < version_tuple(min_ultra))
    ready = (active == recommended) and not need_upgrade

    return {
        "gpus": gpus,
        "primary": primary,
        "vendor": vendor,
        "vendor_label": labels.get(vendor, vendor),
        "nvidia": nv,
        "torch": torch_info,
        "active": {"backend": active, "device": active_device, "ready": True},
        "recommended": {"backend": recommended, "device": device,
                        "torch_index": torch_index, "min_ultralytics": min_ultra,
                        "experimental": experimental, "steps": steps},
        "ready": ready,
        "need_upgrade": need_upgrade,
        "note": note,
        "training_advice": training_advice(active, int(primary["vram_mb"]) if primary else 0),
        "device_options": [
            {**o, "ready": (active == recommended)} if o["value"] == device and recommended != "cpu" else o
            for o in build_device_options(gpus, torch_info, recommended, device)
        ],
    }


def training_advice(backend: str, vram_mb: int) -> dict:
    """Sensible defaults derived from real (64-bit) VRAM, not the wrapping WMI value."""
    if backend == "cpu":
        return {"batch": 1, "model": "yolo26n.pt", "imgsz": 640,
                "note": "CPU 训练：batch 保持 1，模型选 n，避免内存与耗时暴涨。"}
    if vram_mb >= 12000:
        return {"batch": 8, "model": "yolo26s.pt", "imgsz": 640,
                "note": f"显存 {vram_mb/1024:.0f} GB：可用 s 规模模型、batch 8，仍有余量可上调。"}
    if vram_mb >= 8000:
        return {"batch": 4, "model": "yolo26s.pt", "imgsz": 640,
                "note": f"显存 {vram_mb/1024:.0f} GB：建议 s 规模模型、batch 4。"}
    if vram_mb >= 6000:
        return {"batch": 4, "model": "yolo26n.pt", "imgsz": 640,
                "note": f"显存 {vram_mb/1024:.0f} GB：建议 n 规模模型、batch 4。"}
    if vram_mb >= 4000:
        return {"batch": 2, "model": "yolo26n.pt", "imgsz": 640,
                "note": f"显存 {vram_mb/1024:.0f} GB：建议 n 规模模型、batch 2。"}
    return {"batch": 1, "model": "yolo26n.pt", "imgsz": 640,
            "note": "显存较小：n 模型 + batch 1，必要时把 imgsz 降到 512。"}


def build_device_options(gpus: list[dict], torch_info: dict, recommended: str, device: str) -> list[dict]:
    """Device dropdown entries; disabled ones are shown but explained."""
    opts = [{"value": "auto", "label": f"自动（当前推荐：{recommended} / {device}）", "ready": True}]
    if recommended != "cpu":
        opts.append({"value": device, "label": f"{recommended.upper()} 加速（{device}）",
                     "ready": True})
    opts.append({"value": "cpu", "label": "CPU（必定可用，速度较慢）", "ready": True})
    # explicit alternates
    for val, label, ok in [
        ("0", "NVIDIA CUDA / AMD ROCm (cuda:0)", bool(torch_info.get("cuda"))),
        ("xpu:0", "Intel XPU (xpu:0)", bool(torch_info.get("xpu"))),
        ("mps", "Apple MPS", bool(torch_info.get("mps"))),
        ("dml", "Windows DirectML（实验性，建议仅推理）", bool(torch_info.get("directml"))),
    ]:
        if not any(o["value"] == val for o in opts):
            opts.append({"value": val, "label": label, "ready": ok})
    return opts


if __name__ == "__main__":
    cfg = {"venv_python": sys.argv[1] if len(sys.argv) > 1 else sys.executable}
    print(json.dumps(build_plan(cfg), ensure_ascii=False, indent=2))
