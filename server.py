# -*- coding: utf-8 -*-
"""YOLO Studio local HTTP service. Standard library only - no Flask / FastAPI needed.

All heavy work (training, auto-label, inference, backend switching) runs in
subprocesses so the UI stays responsive and can be stopped at any time.
"""
from __future__ import annotations

import argparse
import csv
import json
import mimetypes
import os
import random
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import env_utils
import hardware

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
PRESET_MODELS = [
    {"name": "yolo26n.pt", "desc": "YOLO26 nano - 2026 最新一代，CPU/边缘最快，推荐"},
    {"name": "yolo26s.pt", "desc": "YOLO26 small - 精度与速度均衡"},
    {"name": "yolo26m.pt", "desc": "YOLO26 medium"},
    {"name": "yolo26l.pt", "desc": "YOLO26 large"},
    {"name": "yolo26x.pt", "desc": "YOLO26 xlarge"},
    {"name": "yolo11n.pt", "desc": "YOLO11 nano - 稳定老版本"},
    {"name": "yolo11s.pt", "desc": "YOLO11 small"},
    {"name": "yolov8n.pt", "desc": "YOLOv8 nano - 兼容旧项目"},
    {"name": "yolo26.yaml", "desc": "从零训练 - 无需下载任何权重（随机初始化）", "offline": True},
]

for d in (WORKSPACE, DATASETS, MODELS, EXPORTS, RUNS, LOGS, JOBS, TASK_LOGS):
    d.mkdir(parents=True, exist_ok=True)

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


def cfg_save(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


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


def refresh_task(t: dict) -> None:
    if t["status"] != "running":
        return
    if t["proc"].poll() is None:
        return
    try:
        t["fh"].flush()
        t["fh"].close()
    except Exception:
        pass
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
    src = b.get("src_dir", "").strip()
    imported = 0
    ratio = float(b.get("val_ratio", 0.2))
    if src and Path(src).is_dir():
        files = [p for p in Path(src).rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXT]
        random.Random(42).shuffle(files)
        n_val = int(len(files) * ratio)
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
    return {"name": name, "imported": imported, "info": dataset_info({"name": name, "root": root,
                                                                     "kind": "local"})}


def action_dataset_attach(b: dict) -> dict:
    root = Path(str(b.get("root", "")).strip())
    if not root.is_dir():
        raise ValueError("directory not found")
    name = str(b.get("name") or root.name).strip()
    if not (root / "images" / "train").is_dir():
        raise ValueError("expected <root>/images/train (YOLO layout)")
    if not (root / "data.yaml").is_file():
        write_dataset_meta(root, read_classes(root) or ["object"])
    cfg = cfg_load()
    entries = [x for x in cfg.get("external_datasets", []) if x["name"] != name]
    entries.append({"name": name, "root": str(root).replace("\\", "/")})
    cfg["external_datasets"] = entries
    cfg_save(cfg)
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
    write_dataset_meta(d["root"], read_classes(d["root"]))
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
    cfg = cfg_load()
    parent = ROOT.parent
    found = []
    for child in sorted(parent.iterdir()):
        if not child.is_dir() or child.name in (ROOT.name, ".venv", ".idea", "runs", "__pycache__"):
            continue
        if (child / "images" / "train").is_dir() and (child / "data.yaml").is_file():
            found.append({"name": child.name, "root": str(child).replace("\\", "/")})
    if found:
        known = {x["name"] for x in found}
        entries = [x for x in cfg.get("external_datasets", []) if x["name"] not in known]
        cfg["external_datasets"] = entries + found
    runs = [str(parent / "runs").replace("\\", "/")] if (parent / "runs").is_dir() else []
    if runs:
        cfg["external_runs"] = sorted(set(cfg.get("external_runs", []) + runs))
    cfg_save(cfg)
    return {"datasets": found, "runs": runs}


def action_task_start(b: dict) -> dict:
    kind = b.get("type", "")
    p = dict(b.get("params", {}) or {})
    label = b.get("label", "")
    if kind == "backend":
        p["python"] = venv_py()
        p["mirror"] = cfg_load().get("mirror", "tsinghua")
        return start_task("backend", p, script="worker_backend.py", label=label or f"启用 {p.get('backend')} 加速")
    if kind not in ("train", "autolabel", "predict", "validate", "export", "download_model"):
        raise ValueError("unknown task type")
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

    def _err(self, exc: Exception) -> None:
        traceback.print_exc()
        self._json({"error": str(exc)}, 400)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query, keep_blank_values=True).items()}
        path = unquote(u.path)
        try:
            if path == "/" or path == "/index.html":
                return self._static("index.html")
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path == "/api/status":
                cfg = cfg_load()
                return self._json({
                    "ok": True, "root": str(ROOT), "workspace": str(WORKSPACE),
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
                    offset = int(q.get("offset", 0))
                    text = t["log"].read_text(encoding="utf-8", errors="replace")
                    new = text[offset:]
                    events = []
                    for line in new.splitlines():
                        if line.startswith(PREFIX):
                            try:
                                events.append(json.loads(line[len(PREFIX):]))
                            except Exception:
                                pass
                    return self._json({"offset": len(text), "text": new, "events": events,
                                       "status": t["status"]})
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
        return self._send(200, target.read_bytes(), ctype)

    def do_POST(self):
        u = urlparse(self.path)
        path = unquote(u.path)
        try:
            b = self._body()
            if path == "/api/dataset/create":
                return self._json(action_dataset_create(b))
            if path == "/api/dataset/attach":
                return self._json(action_dataset_attach(b))
            if path == "/api/dataset/classes":
                return self._json(action_dataset_classes(b))
            if path == "/api/dataset/split":
                return self._json(action_dataset_split(b))
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
                cfg = cfg_load()
                for k in ("mirror", "device", "app_mode", "default_model", "auto_open_browser"):
                    if k in b:
                        cfg[k] = b[k]
                cfg_save(cfg)
                return self._json({"ok": True, "config": cfg})
            if path == "/api/open":
                target = Path(b.get("path", ""))
                allowed = [WORKSPACE.resolve(), *[d["root"].resolve() for d in dataset_roots()],
                           *[r.resolve() for r in run_roots()]]
                if not any(target.resolve().is_relative_to(a) for a in allowed):
                    raise ValueError("path not allowed")
                if os.name == "nt":
                    os.startfile(str(target))  # noqa
                else:
                    subprocess.Popen(["xdg-open", str(target)])
                return self._json({"ok": True})
            if path == "/api/shutdown":
                def _bye():
                    time.sleep(0.4)
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
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    print(f"YOLO Studio service listening on http://{args.host}:{args.port}/", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("service stopped", flush=True)


if __name__ == "__main__":
    main()
