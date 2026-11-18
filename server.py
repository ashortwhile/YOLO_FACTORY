# -*- coding: utf-8 -*-
"""YOLO Studio local HTTP service. Standard library only - no Flask / FastAPI needed.

All heavy work (training, auto-label, inference, backend switching) runs in
subprocesses so the UI stays responsive and can be stopped at any time.
"""
from __future__ import annotations

import argparse
import codecs
import csv
import json
import mimetypes
import os
import random
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import env_utils
import hardware
from version import APP_VERSION

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
WORKSPACE = ROOT / "workspace"
DATASETS = WORKSPACE / "datasets"
MODELS = WORKSPACE / "models"
EXPORTS = WORKSPACE / "exports"
RUNS = WORKSPACE / "runs"
LOGS = ROOT / "logs"
JOBS = LOGS / "jobs"
TASK_LOGS = LOGS / "tasks"
CONFIG_PATH = ROOT / "config.json"
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff", ".jfif"}
PREFIX = "__YSTUDIO__"

# ---- local-service security -------------------------------------------------
# The service is a desktop app, but any web page in the user's browser can still
# issue "simple" cross-origin POSTs to 127.0.0.1. Require a per-process token
# (injected into index.html) plus Host/Origin checks on every mutating request.
API_TOKEN = secrets.token_urlsafe(32)
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
MAX_BODY_BYTES = 1024 * 1024
MAX_RUNNING_TASKS = 8
MODEL_NAME_RE = re.compile(r"[A-Za-z0-9._-]+\.(?:pt|onnx|yaml)\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
ALLOWED_BACKENDS = {"cpu", "cuda", "xpu", "rocm", "mps", "dml"}
ALLOWED_EXPORT_FORMATS = {"onnx", "openvino", "torchscript", "tflite"}
ALLOWED_DOWNLOAD_DOMAINS = ("hf-mirror.com", "huggingface.co", "github.com",
                            "githubusercontent.com", "ultralytics.com")
SAFE_OPEN_EXT = set(IMAGE_EXT) | {".txt", ".yaml", ".yml", ".csv", ".json",
                                  ".log", ".md", ".xml", ".pt", ".onnx"}
CONFIG_LOCK = threading.Lock()

PRESET_MODELS = [
    {"name": "yolo26n.pt"},
    {"name": "yolo26s.pt"},
    {"name": "yolo26m.pt"},
    {"name": "yolo26l.pt"},
    {"name": "yolo26x.pt"},
    {"name": "yolo11n.pt"},
    {"name": "yolo11s.pt"},
    {"name": "yolov8n.pt"},
    {"name": "yolo26.yaml", "offline": True},
]

for d in (WORKSPACE, DATASETS, MODELS, EXPORTS, RUNS, LOGS, JOBS, TASK_LOGS):
    d.mkdir(parents=True, exist_ok=True)


def prune_old_logs(max_age_days: int = 14, max_files: int = 200) -> None:
    """Bound log/result growth: drop old task files, keeping at least max_files.

    Never touches files belonging to tasks still tracked in memory (this also
    runs only once at import, when TASKS is empty).
    """
    cutoff = time.time() - max_age_days * 86400
    for folder in (TASK_LOGS, JOBS):
        try:
            files = sorted(folder.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
            for p in files[max_files:]:
                try:
                    p.unlink()
                except OSError:
                    pass
            for p in files[:max_files]:
                if p.stat().st_mtime < cutoff:
                    try:
                        p.unlink()
                    except OSError:
                        pass
        except OSError:
            pass


prune_old_logs()

TASKS: dict[str, dict] = {}
TASK_LOCK = threading.Lock()
HW_CACHE: dict = {"ts": 0.0, "data": None}


def cfg_load() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _cfg_write(cfg: dict) -> None:
    """Atomic replace: a crash mid-write must never leave a broken config.json."""
    tmp = CONFIG_PATH.with_name(f"config.json.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG_PATH)


def cfg_save(cfg: dict) -> None:
    with CONFIG_LOCK:
        _cfg_write(cfg)


def cfg_update(mutator) -> dict:
    """Locked read-modify-write. Use instead of cfg_load() + cfg_save()."""
    with CONFIG_LOCK:
        cfg = cfg_load()
        mutator(cfg)
        _cfg_write(cfg)
        return cfg


def venv_py() -> str:
    return cfg_load().get("venv_python") or sys.executable


# --------------------------------------------------------------- datasets
def dataset_roots() -> list[dict]:
    out: list[dict] = []
    for d in sorted(DATASETS.iterdir()):
        if d.is_dir() and (d / "images" / "train").is_dir():
            out.append({"name": d.name, "root": d, "kind": "local"})
    cfg = cfg_load()
    for ent in cfg.get("external_datasets", []):
        root = Path(ent["root"])
        if root.is_dir() and not any(x["name"] == ent["name"] for x in out):
            out.append({"name": ent["name"], "root": root, "kind": "external"})
    return out


def get_dataset(name: str) -> dict:
    for d in dataset_roots():
        if d["name"] == name:
            return d
    raise ValueError(f"dataset not found: {name}")


def read_classes(root: Path) -> list[str]:
    f = root / "classes.txt"
    if f.exists():
        return [x.strip() for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]
    y = root / "data.yaml"
    if y.exists():
        try:
            import yaml
            data = yaml.safe_load(y.read_text(encoding="utf-8")) or {}
            names = data.get("names") or []
            if isinstance(names, dict):
                return [str(names[k]) for k in sorted(names, key=lambda x: int(x))]
            return [str(x) for x in names]
        except Exception:
            pass
    return []


def write_dataset_meta(root: Path, classes: list[str]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "classes.txt").write_text("\n".join(classes) + "\n", encoding="utf-8")
    lines = [f"path: {root.as_posix()}", "train: images/train", "val: images/val",
             f"nc: {len(classes)}", "names:"]
    lines += [f"  {i}: {c}" for i, c in enumerate(classes)]
    (root / "data.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def split_stats(root: Path, split: str) -> dict:
    img_dir, lab_dir = root / "images" / split, root / "labels" / split
    images = [p for p in img_dir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXT] if img_dir.is_dir() else []
    labeled, boxes = 0, 0
    for p in images:
        lf = lab_dir / f"{p.stem}.txt"
        if lf.is_file():
            labeled += 1
            boxes += len([x for x in lf.read_text(encoding="utf-8", errors="replace").splitlines()
                          if x.strip() and len(x.split()) >= 5])
    return {"images": len(images), "labeled": labeled, "boxes": boxes}


def dataset_info(d: dict) -> dict:
    root = d["root"]
    return {"name": d["name"], "root": str(root), "kind": d["kind"],
            "classes": read_classes(root),
            "train": split_stats(root, "train"), "val": split_stats(root, "val"),
            "data_yaml": str(root / "data.yaml"),
            "has_yaml": (root / "data.yaml").is_file(),
            "yaml_text": (root / "data.yaml").read_text(encoding="utf-8", errors="replace")
            if (root / "data.yaml").is_file() else ""}


def image_files(d: dict, split: str) -> list[Path]:
    img_dir = d["root"] / "images" / split
    if not img_dir.is_dir():
        return []
    return sorted([p for p in img_dir.iterdir()
                   if p.is_file() and p.suffix.lower() in IMAGE_EXT],
                  key=lambda p: p.name.lower())


def label_path(d: dict, split: str, file: str) -> Path:
    return d["root"] / "labels" / split / f"{Path(file).stem}.txt"


def safe_filename(name: str) -> str:
    f = Path(name).name
    if f in ("", ".", "..") or "/" in name or "\\" in name:
        raise ValueError("invalid filename")
    return f


def validate_ds_split(name: str, split: str) -> tuple[dict, str]:
    if split not in ("train", "val"):
        raise ValueError("invalid split")
    return get_dataset(name), split


# --------------------------------------------------------------- tasks
def start_task(kind: str, params: dict, *, script: str = "worker_task.py",
               label: str = "") -> dict:
    with TASK_LOCK:
        running = sum(1 for t in TASKS.values() if t["status"] == "running")
    if running >= MAX_RUNNING_TASKS:
        raise ValueError(f"too many running tasks (limit {MAX_RUNNING_TASKS})")
    tid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:5]
    job_path = JOBS / f"{tid}.json"
    result_path = JOBS / f"{tid}.result.json"
    log_path = TASK_LOGS / f"{tid}.log"
    job = {"type": kind, "params": params, "result_path": str(result_path)}
    job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    env = env_utils.clean_env({"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
                               "ULTRALYTICS_DOWNLOAD_FONT": "0", "YSTUDIO_ROOT": str(ROOT)})
    fh = log_path.open("w", encoding="utf-8", errors="replace")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    proc = subprocess.Popen([venv_py(), str(ROOT / script), str(job_path)],
                            stdout=fh, stderr=subprocess.STDOUT, cwd=str(ROOT),
                            env=env, creationflags=flags)
    task = {"id": tid, "type": kind, "label": label or kind, "status": "running",
            "proc": proc, "log": log_path, "result_path": result_path,
            "started": time.time(), "result": None, "error": None, "fh": fh}
    with TASK_LOCK:
        TASKS[tid] = task
    return task_public(task)


def task_public(t: dict) -> dict:
    return {k: v for k, v in t.items() if k not in ("proc", "fh")} | {
        "log": str(t["log"]), "result_path": str(t["result_path"]),
        "elapsed": round(time.time() - t["started"], 1)}


def close_task_log(t: dict) -> None:
    fh = t.get("fh")
    if fh is not None and not fh.closed:
        try:
            fh.flush()
            fh.close()
        except Exception:
            pass


def refresh_task(t: dict) -> None:
    running = t["status"] == "running"
    if running and t["proc"].poll() is None:
        return
    # A stopped task never reaches the result-parsing branch, so the log file
    # handle used to stay open forever (Windows then locks the file).
    close_task_log(t)
    if not running:
        return
    try:
        payload = json.loads(t["result_path"].read_text(encoding="utf-8"))
        t["result"] = payload.get("result") or payload
        t["status"] = "done" if payload.get("ok", True) else "error"
        t["error"] = payload.get("error") or (t["result"] or {}).get("error")
    except Exception:
        t["status"] = "error" if t["proc"].returncode else "done"
        t["error"] = "worker exited without a result file (see log)"


def stop_task(tid: str) -> dict:
    with TASK_LOCK:
        t = TASKS.get(tid)
    if not t:
        raise ValueError("task not found")
    if t["status"] == "running":
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(t["proc"].pid)],
                               capture_output=True, timeout=10)
            else:
                t["proc"].terminate()
        except Exception:
            t["proc"].terminate()
        t["status"] = "stopped"
        t["error"] = "stopped by user"
        close_task_log(t)
    return {"id": tid, "status": t["status"]}


# --------------------------------------------------------------- hardware
def get_hardware(force: bool = False) -> dict:
    if not force and HW_CACHE["data"] and time.time() - HW_CACHE["ts"] < 60:
        return HW_CACHE["data"]
    cfg = cfg_load()
    data = hardware.build_plan(cfg, venv_py())
    HW_CACHE.update({"ts": time.time(), "data": data})
    return data


def resolve_device(value: str) -> str:
    hw = get_hardware()
    if value in ("auto", "", None):
        return hw["recommended"]["device"] if hw["ready"] else "cpu"
    ti = hw.get("torch", {})
    checks = {
        "xpu:0": ("xpu", bool(ti.get("xpu")), "Intel XPU 后端尚未启用"),
        "xpu": ("xpu", bool(ti.get("xpu")), "Intel XPU 后端尚未启用"),
        "0": ("cuda", bool(ti.get("cuda")), "CUDA/ROCm 后端不可用"),
        "cuda:0": ("cuda", bool(ti.get("cuda")), "CUDA/ROCm 后端不可用"),
        "mps": ("mps", bool(ti.get("mps")), "MPS 不可用"),
        "dml": ("dml", bool(ti.get("directml")), "DirectML 未安装"),
    }
    if value in checks:
        _, ok, msg = checks[value]
        if not ok:
            raise ValueError(msg + "；请到「环境」页启用显卡加速，或改用 CPU")
    return value


# --------------------------------------------------------------- runs / models
def run_roots() -> list[Path]:
    cfg = cfg_load()
    roots = [RUNS]
    for r in cfg.get("external_runs", []):
        p = Path(r)
        if p.is_dir() and p not in roots:
            roots.append(p)
    return roots


def list_runs() -> list[dict]:
    out = []
    for root in run_roots():
        # accept both runs/detect/<name> (CLI default) and runs/<name> layouts
        candidates = [d for d in root.glob("detect/*") if d.is_dir()]
        candidates += [d for d in root.glob("*") if d.is_dir()]
        for d in dict.fromkeys(candidates):
            if not ((d / "results.csv").is_file() or (d / "args.yaml").is_file()
                    or (d / "weights").is_dir()):
                continue
            csv_path = d / "results.csv"
            rows = []
            if csv_path.is_file():
                try:
                    rows = list(csv.DictReader(csv_path.read_text(encoding="utf-8",
                                                                  errors="replace").splitlines()))
                except Exception:
                    pass
            weights = sorted(str(p) for p in (d / "weights").glob("*.pt")) if (d / "weights").is_dir() else []
            out.append({"name": d.name, "dir": str(d), "root": str(root),
                        "epochs": len(rows), "weights": weights,
                        "mtime": d.stat().st_mtime,
                        "best_map": rows[-1].get("metrics/mAP50(B)", "") if rows else "",
                        "args": str(d / "args.yaml") if (d / "args.yaml").is_file() else ""})
    return sorted(out, key=lambda x: x["mtime"], reverse=True)


# Online model catalogue ---------------------------------------------------
# The model list is fetched from Ultralytics' official Hugging Face org so new
# releases appear automatically - nothing is hard-coded to "YOLO26 is newest".
# hf-mirror.com is used first (reachable in mainland China), official HF second.
ONLINE_MODEL_API = (
    "https://hf-mirror.com/api/models?author=Ultralytics&limit=100&full=false",
    "https://huggingface.co/api/models?author=Ultralytics&limit=100&full=false",
)
ONLINE_CACHE_SECONDS = 6 * 3600
_online_models_cache: dict = {"at": 0.0, "data": []}
# A proxy-free opener so local/proxy quirks on the host cannot block the fetch.
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _fetch_json(url: str):
    """Fetch JSON trying a direct connection first, then the system proxy.

    Some machines only work one way; neither should break the catalogue.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "YOLOStudio/1.0"})
    last = None
    for opener in (_DIRECT_OPENER, urllib.request):
        try:
            with opener.open(req, timeout=12) as resp:
                return json.loads(resp.read(2_000_000).decode("utf-8", "replace"))
        except Exception as exc:
            last = exc
    raise last if last else RuntimeError("fetch failed")


def _model_scale(name: str) -> tuple:
    """Sort key: detect first, then n/s/m/l/x; pose/seg/cls/obb after."""
    stem = name.rsplit(".", 1)[0].lower()
    suffix_rank = 0
    for tag, rank in (("-pose", 2), ("-seg", 3), ("-cls", 4), ("-obb", 5)):
        if tag in stem:
            suffix_rank = rank
            stem = stem.replace(tag, "")
            break
    scale = {"n": 0, "s": 1, "m": 2, "l": 3, "x": 4}.get(stem[-1:], 5)
    return suffix_rank, scale, name


def fetch_online_models(force: bool = False) -> dict:
    """Return official weights with live download URLs, or fall back to presets.

    Result is cached for ONLINE_CACHE_SECONDS. Network failures never break the
    UI: the built-in preset list is returned with ``online=False``.
    """
    now = time.time()
    if not force and (now - _online_models_cache["at"]) < ONLINE_CACHE_SECONDS \
            and _online_models_cache["data"]:
        return {"online": True, "models": _online_models_cache["data"],
                "cached": True}

    repos = None
    for api_url in ONLINE_MODEL_API:
        try:
            data = _fetch_json(api_url)
            if isinstance(data, list) and data:
                repos = data
                break
        except Exception:
            continue
    if not repos:
        return {"online": False, "models": [], "cached": False}

    items = []
    for repo in repos:
        repo_id = str(repo.get("id") or repo.get("modelId") or "")
        if "/" not in repo_id:
            continue
        _, short = repo_id.split("/", 1)
        if not re.fullmatch(r"YOLO[\w-]*", short, re.I):
            continue
        siblings = repo.get("siblings")
        if siblings is None:  # list endpoint may omit file list
            try:
                detail_url = "https://hf-mirror.com/api/models/" + repo_id
                detail = _fetch_json(detail_url)
                siblings = detail.get("siblings") or []
            except Exception:
                siblings = []
        for sib in siblings:
            fname = str(sib.get("rfilename", "")) if isinstance(sib, dict) else str(sib)
            if "/" in fname or not MODEL_NAME_RE.fullmatch(fname) or not fname.endswith(".pt"):
                continue
            # keep only detection / pose / seg / cls / obb weights, skip e.g. .yaml
            items.append({
                "name": fname,
                "repo": short,
                "source": repo_id,
                "modified": str(repo.get("lastModified") or ""),
                "url": f"https://hf-mirror.com/{repo_id}/resolve/main/{fname}",
            })
    if not items:
        return {"online": False, "models": [], "cached": False}

    # newest repository first, then by model scale
    items.sort(key=lambda m: (m["modified"], m["repo"]), reverse=True)
    unique, seen = [], set()
    for m in items:
        if m["name"] in seen:
            continue
        seen.add(m["name"])
        unique.append(m)
    # cap to a sane number: newest repo's weights plus a few older families
    unique = unique[:120]
    _online_models_cache.update({"at": now, "data": unique})
    return {"online": True, "models": unique, "cached": False}


def list_models() -> dict:
    local = [{"name": p.name, "size": p.stat().st_size, "path": str(p)}
             for p in sorted(MODELS.glob("*.pt"))]
    for r in run_roots():
        for p in sorted(r.glob("detect/*/weights/*.pt")):
            local.append({"name": f"{p.parent.parent.name}/{p.name}", "size": p.stat().st_size,
                          "path": str(p)})
    return {"local": local, "presets": PRESET_MODELS,
            "models_dir": str(MODELS)}


# --------------------------------------------------------------- API actions
def _clean_path(v) -> str:
    """Strip whitespace and matching quotes - Windows 'copy as path' pastes
    paths as "C:\\folder", which silently broke image import."""
    s = str(v or "").strip()
    while len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1].strip()
    return s


def _import_images_from(src_raw: str, root: Path, ratio: float, rng_seed: int = 42) -> int:
    """Copy a folder of images into a dataset, auto-splitting train/val.
    Matching .txt label files are carried along. Returns the import count."""
    src = _clean_path(src_raw)
    if not src:
        raise ValueError("请填写图片源目录")
    if not Path(src).is_dir():
        raise ValueError(f"图片源目录不存在或无法访问：{src}")
    files = [p for p in Path(src).rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXT]
    if not files:
        raise ValueError(f"目录里没有图片（支持 {'/'.join(sorted(set(IMAGE_EXT)))}）：{src}")
    random.Random(rng_seed).shuffle(files)
    ratio = min(max(float(ratio), 0.0), 0.9)
    n_val = int(len(files) * ratio)
    imported = 0
    for i, p in enumerate(files):
        split = "val" if i < n_val else "train"
        target = root / "images" / split / p.name
        k = 1
        while target.exists():
            target = root / "images" / split / f"{p.stem}_{k}{p.suffix}"
            k += 1
        shutil.copy2(p, target)
        lab = p.with_suffix(".txt")
        if lab.is_file():
            shutil.copy2(lab, root / "labels" / split / f"{target.stem}.txt")
        imported += 1
    return imported


def action_dataset_create(b: dict) -> dict:
    name = str(b.get("name", "")).strip()
    if not name or any(c in name for c in '/\\:*?"<>|'):
        raise ValueError("invalid dataset name")
    root = DATASETS / name
    if root.exists() and any(root.iterdir()):
        raise ValueError("dataset already exists")
    classes = [str(c).strip() for c in b.get("classes", []) if str(c).strip()]
    if not classes:
        raise ValueError("at least one class is required")
    for split in ("train", "val"):
        (root / "images" / split).mkdir(parents=True, exist_ok=True)
        (root / "labels" / split).mkdir(parents=True, exist_ok=True)
    write_dataset_meta(root, classes)
    src = _clean_path(b.get("src_dir", ""))
    imported = 0
    if src:
        imported = _import_images_from(src, root, b.get("val_ratio", 0.2))
    return {"name": name, "imported": imported, "info": dataset_info({"name": name, "root": root,
                                                                     "kind": "local"})}


def action_dataset_import(b: dict) -> dict:
    """Import another folder of images into an EXISTING dataset (auto split)."""
    d = get_dataset(str(b.get("name", "")).strip())
    root = Path(d["root"])
    imported = _import_images_from(b.get("src_dir", ""), root, b.get("val_ratio", 0.2))
    return {"name": d["name"], "imported": imported, "info": dataset_info(d)}


def action_pick_folder(b: dict) -> dict:
    """Open a NATIVE Explorer folder-picker dialog. The server runs on the
    same machine as the browser, so it can show the real Windows dialog and
    return the chosen path - no typing required."""
    result: dict = {}

    def ask() -> None:
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            path = filedialog.askdirectory(title="选择图片文件夹")
            root.destroy()
            result["path"] = path or ""
        except Exception as exc:
            result["error"] = f"无法打开文件夹选择窗口：{exc}"

    t = threading.Thread(target=ask, daemon=True)
    t.start()
    t.join(timeout=600)
    if t.is_alive():
        raise ValueError("文件夹选择超时（10 分钟未选择）")
    if result.get("error"):
        raise ValueError(result["error"])
    if not result.get("path"):
        return {"path": "", "canceled": True}
    return {"path": result["path"]}


def action_dataset_attach(b: dict) -> dict:
    root = Path(str(b.get("root", "")).strip())
    if not root.is_dir():
        raise ValueError("directory not found")
    name = str(b.get("name") or root.name).strip()
    if not (root / "images" / "train").is_dir():
        raise ValueError("expected <root>/images/train (YOLO layout)")
    if not (root / "data.yaml").is_file():
        write_dataset_meta(root, read_classes(root) or ["object"])
    def _attach(cfg: dict) -> None:
        entries = [x for x in cfg.get("external_datasets", []) if x.get("name") != name]
        entries.append({"name": name, "root": str(root).replace("\\", "/")})
        cfg["external_datasets"] = entries
    cfg_update(_attach)
    return {"name": name, "root": str(root)}


def action_dataset_classes(b: dict) -> dict:
    d = get_dataset(b["name"])
    classes = [str(c).strip() for c in b.get("classes", []) if str(c).strip()]
    write_dataset_meta(d["root"], classes)
    return {"classes": classes}


def action_dataset_split(b: dict) -> dict:
    d = get_dataset(b["name"])
    ratio = float(b.get("ratio", 0.2))
    seed = int(b.get("seed", 42))
    all_items: list[tuple[Path, Path | None, str]] = []
    for split in ("train", "val"):
        for img in image_files(d, split):
            lab = label_path(d, split, img.name)
            all_items.append((img, lab if lab.is_file() else None, split))
    random.Random(seed).shuffle(all_items)
    n_val = round(len(all_items) * ratio)
    moved = 0
    for i, (img, lab, old_split) in enumerate(all_items):
        new_split = "val" if i < n_val else "train"
        if new_split == old_split:
            continue
        for src, sub in ((img, "images"), (lab, "labels")):
            if src is None:
                continue
            dst = d["root"] / sub / new_split / src.name
            k = 1
            while dst.exists():
                dst = d["root"] / sub / new_split / f"{src.stem}_{k}{src.suffix}"
                k += 1
            shutil.move(str(src), str(dst))
        moved += 1
    # Re-splitting changes no classes; rewriting data.yaml here would silently
    # discard custom keys the user added to that file.
    return {"moved": moved, "total": len(all_items)}


def action_label_save(b: dict) -> dict:
    d, split = validate_ds_split(b["name"], b.get("split", "train"))
    file = safe_filename(b["file"])
    boxes = b.get("boxes", [])
    lines = []
    for box in boxes:
        vals = [int(box["cls"]), float(box["x"]), float(box["y"]),
                float(box["w"]), float(box["h"])]
        lines.append(" ".join(str(vals[0]) if i == 0 else f"{vals[i]:.6f}"
                              for i in range(5)))
    p = label_path(d, split, file)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return {"file": file, "boxes": len(lines), "path": str(p)}


def action_model_import(b: dict) -> dict:
    """Copy a weight file chosen by the user into the studio's model folder."""
    src = Path(str(b.get("path", "")).strip())
    if not src.is_file() or src.suffix.lower() not in (".pt", ".onnx"):
        raise ValueError("choose an existing .pt / .onnx file")
    dst = MODELS / src.name
    MODELS.mkdir(parents=True, exist_ok=True)
    if src.resolve() != dst.resolve():
        shutil.copy2(src, dst)
    return {"name": dst.name, "size": dst.stat().st_size, "path": str(dst)}


def action_discover(b: dict) -> dict:
    """Opt-in scan: attach YOLO datasets / run folders found next to the studio.

    Nothing is copied or modified; only paths are registered in config.json.
    """
    parent = ROOT.parent
    found = []
    for child in sorted(parent.iterdir()):
        if not child.is_dir() or child.name in (ROOT.name, ".venv", ".idea", "runs", "__pycache__"):
            continue
        if (child / "images" / "train").is_dir() and (child / "data.yaml").is_file():
            found.append({"name": child.name, "root": str(child).replace("\\", "/")})
    runs = [str(parent / "runs").replace("\\", "/")] if (parent / "runs").is_dir() else []

    def _record(cfg: dict) -> None:
        if found:
            known = {x["name"] for x in found}
            entries = [x for x in cfg.get("external_datasets", [])
                       if x.get("name") not in known]
            cfg["external_datasets"] = entries + found
        if runs:
            cfg["external_runs"] = sorted(set(cfg.get("external_runs", []) + runs))
    cfg_update(_record)
    return {"datasets": found, "runs": runs}


def _allowed_download_url(url: str) -> bool:
    try:
        u = urlparse(url)
        host = (u.hostname or "").lower()
        return (u.scheme == "https" and bool(host)
                and not u.username and not u.password and u.port is None
                and any(host == d or host.endswith("." + d)
                        for d in ALLOWED_DOWNLOAD_DOMAINS))
    except Exception:
        return False


def validate_download_params(p: dict) -> dict:
    """Only the fields a download job needs may reach the worker process."""
    name = str(p.get("model", ""))
    if not MODEL_NAME_RE.fullmatch(name):
        raise ValueError("invalid model filename (expected name.pt / .onnx / .yaml)")
    out = {"model": name}
    url = str(p.get("url") or "").strip()
    if url:
        if not _allowed_download_url(url):
            raise ValueError("download URL must be https from a known model host")
        out["url"] = url
    sha = str(p.get("sha256") or "").strip().lower()
    if sha:
        if not SHA256_RE.fullmatch(sha):
            raise ValueError("invalid sha256")
        out["sha256"] = sha
    return out


def action_task_start(b: dict) -> dict:
    kind = b.get("type", "")
    p = dict(b.get("params", {}) or {})
    label = b.get("label", "")
    if kind == "backend":
        backend = str(p.get("backend", "cpu"))
        if backend not in ALLOWED_BACKENDS:
            raise ValueError("unknown accelerator backend")
        # Never forward client-supplied index / python / mirror: the backend
        # worker would otherwise pip-install from an arbitrary wheel index.
        safe = {"backend": backend, "python": venv_py(),
                "mirror": cfg_load().get("mirror", "tsinghua")}
        return start_task("backend", safe, script="worker_backend.py",
                          label=label or f"启用 {backend} 加速")
    if kind == "download_model":
        return start_task(kind, validate_download_params(p), label=label or kind)
    if kind == "setup_env":
        # The runtime installer is an explicit user action. Only allow a fixed
        # set of flags and use OUR configured venv - never a client-supplied
        # executable or index. backend/force/with_model are harmless switches.
        safe = {
            "python": venv_py(),
            "force": bool(p.get("force", True)),
            "with_model": bool(p.get("with_model", True)),
        }
        return start_task("setup_env", safe, script="worker_setup.py",
                          label=label or "安装运行环境")
    if kind not in ("train", "autolabel", "predict", "validate", "export"):
        raise ValueError("unknown task type")
    if kind == "export":
        fmt = str(p.get("format", "onnx"))
        if fmt not in ALLOWED_EXPORT_FORMATS:
            raise ValueError("unsupported export format")
        p["format"] = fmt
    p["device"] = resolve_device(p.get("device", "auto"))
    if kind in ("train", "validate"):
        d = get_dataset(p["dataset"])
        p["data_yaml"] = str(d["root"] / "data.yaml")
        del p["dataset"]
    if kind == "train":
        p["project"] = p.get("project") or str(RUNS / "detect")
        if p.get("resume"):
            cand = sorted(list(Path(p["project"]).glob("*/weights/last.pt")) +
                          list(Path(p["project"]).glob("detect/*/weights/last.pt")),
                          key=lambda x: x.stat().st_mtime)
            if not cand:
                raise ValueError("no last.pt found to resume from")
            p["resume_path"] = str(cand[-1])
        p.setdefault("amp", p["device"] not in ("cpu", "xpu:0", "xpu"))
    if kind == "predict":
        d, split = validate_ds_split(p["dataset"], p.get("split", "train"))
        file = safe_filename(p["file"])
        img = d["root"] / "images" / split / file
        if not img.is_file():
            raise ValueError("image not found")
        p["image"] = str(img)
        del p["dataset"]
    if kind == "autolabel":
        d, split = validate_ds_split(p["dataset"], p.get("split", "train"))
        scope = p.get("scope", "unlabeled")
        files = image_files(d, split)
        if scope == "current":
            files = [f for f in files if f.name == p.get("file", "")]
        elif scope == "unlabeled":
            files = [f for f in files if not label_path(d, split, f.name).is_file()]
        p["images"] = [str(f) for f in files]
        p["labels_dir"] = str(d["root"] / "labels" / split)
        p["dataset_root"] = str(d["root"])
        del p["dataset"]
        if not p["images"]:
            raise ValueError("no images match this scope")
    return start_task(kind, p, label=label or kind)


# --------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "YOLOStudio/1.0"

    def log_message(self, fmt, *args):
        if "--quiet" not in sys.argv:
            sys.stderr.write("  [%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def _send(self, code: int, body: bytes, ctype: str, cache: str = "no-store") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _guard(self, require_token: bool) -> None:
        """Block cross-site / DNS-rebinding requests to this local service."""
        raw_host = (self.headers.get("Host") or "").strip().lower()
        host = raw_host.rsplit(":", 1)[0].strip("[]")
        if host not in LOOPBACK_HOSTS:
            raise PermissionError("blocked: non-local Host header")
        origin = self.headers.get("Origin")
        if origin:
            u = urlparse(origin)
            if (u.scheme not in ("http", "https")
                    or (u.hostname or "").lower() not in LOOPBACK_HOSTS):
                raise PermissionError("blocked: cross-origin request")
        if require_token and not secrets.compare_digest(
                self.headers.get("X-YStudio-Token", ""), API_TOKEN):
            raise PermissionError("blocked: missing or invalid API token")

    def _err(self, exc: Exception) -> None:
        # Expected validation / auth rejections should not dump a stack trace
        # into the console on every blocked cross-site request.
        if not isinstance(exc, (PermissionError, ValueError)):
            traceback.print_exc()
        self._json({"error": str(exc)}, 403 if isinstance(exc, PermissionError) else 400)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        if n <= 0:
            return {}
        if n > MAX_BODY_BYTES:
            raise ValueError("request body too large")
        obj = json.loads(self.rfile.read(n).decode("utf-8"))
        if not isinstance(obj, dict):
            raise ValueError("a JSON object is required")
        return obj

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query, keep_blank_values=True).items()}
        path = unquote(u.path)
        try:
            self._guard(False)
            if path == "/api/health":
                return self._json({"ok": True, "app": "yolo-studio"})
            if path == "/" or path == "/index.html":
                return self._static("index.html")
            if path == "/favicon.ico":
                return self._static("favicon.ico")
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path == "/api/status":
                cfg = cfg_load()
                return self._json({
                    "ok": True, "version": APP_VERSION,
                    "runtime_ready": bool(cfg.get("setup_ok", False)),
                    "root": str(ROOT), "workspace": str(WORKSPACE),
                    "config": {k: cfg.get(k) for k in
                               ("port", "mirror", "device", "app_mode", "default_model",
                                "last_setup", "setup_ok", "venv_python")},
                    "hardware": cfg.get("hardware", {}),
                    "datasets": len(dataset_roots()),
                    "models": len(list_models()["local"]),
                    "runs": len(list_runs()),
                    "tasks_running": sum(1 for t in TASKS.values() if t["status"] == "running"),
                })
            if path == "/api/hardware":
                return self._json(get_hardware(force=q.get("refresh") == "1"))
            if path == "/api/datasets":
                return self._json([dataset_info(d) for d in dataset_roots()])
            if path == "/api/dataset/info":
                return self._json(dataset_info(get_dataset(q["name"])))
            if path == "/api/dataset/images":
                d, split = validate_ds_split(q["name"], q.get("split", "train"))
                flt = q.get("filter", "all")
                out = []
                for f in image_files(d, split):
                    lp = label_path(d, split, f.name)
                    exists = lp.is_file()
                    if flt == "unlabeled" and exists:
                        continue
                    if flt == "labeled" and not exists:
                        continue
                    n = len([x for x in lp.read_text(encoding="utf-8", errors="replace").splitlines()
                             if x.strip() and len(x.split()) >= 5]) if exists else 0
                    out.append({"name": f.name, "labeled": exists, "boxes": n,
                                "size": f.stat().st_size})
                return self._json({"images": out, "classes": read_classes(d["root"]),
                                   "split": split, "dataset": d["name"]})
            if path == "/api/image":
                d, split = validate_ds_split(q["name"], q.get("split", "train"))
                file = safe_filename(q["file"])
                f = d["root"] / "images" / split / file
                if not f.is_file():
                    raise ValueError("image not found")
                ctype = mimetypes.guess_type(f.name)[0] or "image/jpeg"
                return self._send(200, f.read_bytes(), ctype, "max-age=3600")
            if path == "/api/label":
                d, split = validate_ds_split(q["name"], q.get("split", "train"))
                file = safe_filename(q["file"])
                lp = label_path(d, split, file)
                boxes = []
                raw = lp.read_text(encoding="utf-8", errors="replace") if lp.is_file() else ""
                for line in raw.splitlines():
                    parts = line.split()
                    if len(parts) >= 5:
                        boxes.append({"cls": int(float(parts[0])), "x": float(parts[1]),
                                      "y": float(parts[2]), "w": float(parts[3]),
                                      "h": float(parts[4])})
                return self._json({"boxes": boxes, "raw": raw, "exists": lp.is_file()})
            if path == "/api/models":
                return self._json(list_models())
            if path == "/api/models/online":
                return self._json(fetch_online_models(force=q.get("refresh") == "1"))
            if path == "/api/runs":
                return self._json(list_runs())
            if path == "/api/run/detail":
                target = Path(q["path"])
                if not any(target.is_relative_to(r) for r in run_roots()):
                    raise ValueError("path not allowed")
                csv_p = target / "results.csv"
                rows = list(csv.DictReader(csv_p.read_text(encoding="utf-8",
                                                           errors="replace").splitlines())) if csv_p.is_file() else []
                args = (target / "args.yaml").read_text(encoding="utf-8", errors="replace") if (target / "args.yaml").is_file() else ""
                return self._json({"rows": rows, "args": args, "dir": str(target),
                                   "weights": [str(p) for p in (target / "weights").glob("*.pt")]})
            if path == "/api/tasks":
                with TASK_LOCK:
                    for t in TASKS.values():
                        refresh_task(t)
                    return self._json([task_public(t) for t in TASKS.values()])
            if path == "/api/task/status":
                with TASK_LOCK:
                    t = TASKS.get(q.get("id", ""))
                    if not t:
                        raise ValueError("task not found")
                    refresh_task(t)
                    return self._json(task_public(t))
            if path == "/api/task/log":
                with TASK_LOCK:
                    t = TASKS.get(q.get("id", ""))
                    if not t:
                        raise ValueError("task not found")
                    # Byte offset + incremental decoder: never re-read the whole
                    # (possibly multi-MB) log on every 1s poll, and never split
                    # a multi-byte UTF-8 character across two polls.
                    offset = max(0, int(q.get("offset", 0)))
                    with t["log"].open("rb") as f:
                        f.seek(0, 2)
                        offset = min(offset, f.tell())
                        f.seek(offset)
                        data = f.read()
                    dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
                    new = dec.decode(data, final=False)
                    next_offset = offset + len(data) - len(dec.getstate()[0])
                    events = []
                    for line in new.splitlines():
                        if line.startswith(PREFIX):
                            try:
                                events.append(json.loads(line[len(PREFIX):]))
                            except Exception:
                                pass
                    return self._json({"offset": next_offset, "text": new,
                                       "events": events, "status": t["status"]})
            if path == "/api/config":
                cfg = cfg_load()
                return self._json({k: cfg.get(k) for k in
                                   ("port", "mirror", "device", "app_mode", "default_model",
                                    "auto_open_browser", "reuse_parent_venv")})
            self._json({"error": "not found"}, 404)
        except Exception as exc:
            self._err(exc)

    def _static(self, rel: str):
        target = (WEB / rel).resolve()
        if not target.is_relative_to(WEB.resolve()) or not target.is_file():
            return self._json({"error": "not found"}, 404)
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or target.suffix in (".js", ".css", ".html"):
            ctype += "; charset=utf-8"
        if target.name == "index.html":
            # Hand the per-process token to the UI. A cross-origin page cannot
            # read this response (no CORS headers), so it cannot forge POSTs.
            tag = f'<script>window.YSTUDIO_TOKEN="{API_TOKEN}";</script>\n'
            body = target.read_text(encoding="utf-8").replace(
                "</head>", tag + "</head>", 1).encode("utf-8")
        else:
            body = target.read_bytes()
        return self._send(200, body, ctype)

    def do_POST(self):
        u = urlparse(self.path)
        path = unquote(u.path)
        try:
            self._guard(True)
            b = self._body()
            if path == "/api/dataset/create":
                return self._json(action_dataset_create(b))
            if path == "/api/dataset/attach":
                return self._json(action_dataset_attach(b))
            if path == "/api/dataset/classes":
                return self._json(action_dataset_classes(b))
            if path == "/api/dataset/split":
                return self._json(action_dataset_split(b))
            if path == "/api/dataset/import":
                return self._json(action_dataset_import(b))
            if path == "/api/pick_folder":
                return self._json(action_pick_folder(b))
            if path == "/api/dataset/discover":
                return self._json(action_discover(b))
            if path == "/api/model/import":
                return self._json(action_model_import(b))
            if path == "/api/label/save":
                return self._json(action_label_save(b))
            if path == "/api/task/start":
                return self._json(action_task_start(b))
            if path == "/api/task/stop":
                return self._json(stop_task(b["id"]))
            if path == "/api/config/save":
                if "mirror" in b and b["mirror"] not in ("tsinghua", "aliyun", "pypi"):
                    raise ValueError("unknown mirror")
                if "default_model" in b and not MODEL_NAME_RE.fullmatch(str(b["default_model"])):
                    raise ValueError("invalid default model name")
                def _apply(cfg: dict) -> None:
                    for k in ("mirror", "device", "app_mode", "default_model", "auto_open_browser"):
                        if k in b:
                            cfg[k] = b[k]
                cfg = cfg_update(_apply)
                return self._json({"ok": True, "config": cfg})
            if path == "/api/open":
                target = Path(b.get("path", ""))
                allowed = [WORKSPACE.resolve(), *[d["root"].resolve() for d in dataset_roots()],
                           *[r.resolve() for r in run_roots()]]
                if not any(target.resolve().is_relative_to(a) for a in allowed):
                    raise ValueError("path not allowed")
                # Directories are always fine (dataset names may contain dots).
                # For files, check the suffix itself instead of is_file(): a
                # not-yet-created .exe path must not reach os.startfile().
                if (not target.is_dir() and target.suffix
                        and target.suffix.lower() not in SAFE_OPEN_EXT):
                    raise ValueError("this file type cannot be opened")
                if os.name == "nt":
                    os.startfile(str(target))  # noqa
                else:
                    subprocess.Popen(["xdg-open", str(target)])
                return self._json({"ok": True})
            if path == "/api/shutdown":
                def _bye():
                    time.sleep(0.4)
                    # os._exit() skips cleanup: terminate worker processes first,
                    # otherwise training keeps running as an orphan with no UI.
                    with TASK_LOCK:
                        tasks = list(TASKS.values())
                    for t in tasks:
                        try:
                            if t["proc"].poll() is None:
                                if os.name == "nt":
                                    subprocess.run(["taskkill", "/F", "/T", "/PID",
                                                    str(t["proc"].pid)],
                                                   capture_output=True, timeout=10)
                                else:
                                    t["proc"].terminate()
                        except Exception:
                            pass
                        close_task_log(t)
                    os._exit(0)
                threading.Thread(target=_bye, daemon=True).start()
                return self._json({"ok": True, "message": "service stopping"})
            self._json({"error": "not found"}, 404)
        except Exception as exc:
            self._err(exc)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--quiet", action="store_true")
    args, _ = ap.parse_known_args()
    host = args.host
    if host not in LOOPBACK_HOSTS:
        print(f"[warn] binding to {host} is not supported; using 127.0.0.1", flush=True)
        host = "127.0.0.1"
    srv = ThreadingHTTPServer((host, args.port), Handler)
    srv.daemon_threads = True
    print(f"YOLO Studio service listening on http://{host}:{args.port}/", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("service stopped", flush=True)


if __name__ == "__main__":
    main()
