# -*- coding: utf-8 -*-
"""Subprocess worker: training, auto-label, inference, validation, export, model download.

Usage: python worker_task.py job.json
Every structured line is prefixed with __YSTUDIO__ and parsed by server.py.
The final result is written as JSON to job["result_path"].
"""
from __future__ import annotations

import csv
import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path

# Must be set before importing ultralytics: avoids the Arial.ttf download error on offline machines
os.environ["ULTRALYTICS_DOWNLOAD_FONT"] = "0"
os.environ["ULTRALYTICS_FONT"] = "None"
os.environ["YOLO_VERBOSE"] = "true"

ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT / "workspace"
MODELS = WORKSPACE / "models"
EXPORTS = WORKSPACE / "exports"
PREFIX = "__YSTUDIO__"


def emit(obj: dict) -> None:
    print(PREFIX + json.dumps(obj, ensure_ascii=False), flush=True)


def log(msg: str) -> None:
    print(str(msg), flush=True)


def read_job(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_result(job: dict, data: dict) -> None:
    p = Path(job["result_path"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def resolve_model(name: str) -> str:
    p = Path(name)
    if p.is_file():
        return str(p.resolve())
    cand = MODELS / name
    if cand.is_file():
        return str(cand.resolve())
    return name  # let ultralytics download from its official release


def clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None and v != ""}


def tail_metrics(project: str, stop: threading.Event, started: float) -> None:
    """Emit one structured metric event per new results.csv row."""
    seen: set[tuple[str, str]] = set()
    root = Path(project)
    while not stop.is_set():
        try:
            csvs = [p for p in (list(root.glob("*/results.csv")) +
                                list(root.glob("detect/*/results.csv")))
                    if p.stat().st_mtime >= started - 5]
            if csvs:
                f = max(csvs, key=lambda x: x.stat().st_mtime)
                text = f.read_text(encoding="utf-8", errors="replace")
                for row in csv.DictReader(text.splitlines()):
                    key = (f.parent.name, row.get("epoch", ""))
                    if key in seen or not row.get("epoch"):
                        continue
                    seen.add(key)
                    emit({"type": "metric", "run": f.parent.name, "row": row})
        except Exception:
            pass
        stop.wait(2.0)


def do_train(p: dict) -> dict:
    from ultralytics import YOLO

    MODELS.mkdir(parents=True, exist_ok=True)
    EXPORTS.mkdir(parents=True, exist_ok=True)
    project = str(Path(p.get("project") or (WORKSPACE / "runs" / "detect")).resolve())
    Path(project).mkdir(parents=True, exist_ok=True)

    model_ref = p.get("resume_path") if p.get("resume") and p.get("resume_path") else p["model"]
    model_ref = resolve_model(model_ref)
    log(f"Loading model: {model_ref}")
    model = YOLO(model_ref, task="detect")

    args = clean({
        "data": p["data_yaml"],
        "epochs": int(p.get("epochs", 50)),
        "imgsz": int(p.get("imgsz", 640)),
        "batch": int(p.get("batch", 1)),
        "device": p.get("device", "cpu"),
        "workers": int(p.get("workers", 2)),
        "project": project,
        "name": p.get("name") or "train",
        "patience": int(p.get("patience", 50)),
        "optimizer": p.get("optimizer", "auto"),
        "seed": int(p.get("seed", 0)),
        "lr0": float(p["lr0"]) if p.get("lr0") else None,
        "resume": bool(p.get("resume")),
        "amp": bool(p.get("amp", False)),
        "cache": bool(p.get("cache", False)),
        "plots": False,          # avoids the font download crash
        "exist_ok": False,
        "verbose": True,
        "val": True,
    })
    log("Training args: " + json.dumps(args, ensure_ascii=False, default=str))
    emit({"type": "start", "project": project, "args": args})

    stop = threading.Event()
    th = threading.Thread(target=tail_metrics, args=(project, stop, time.time()), daemon=True)
    th.start()
    try:
        model.train(**args)
    finally:
        stop.set()
        th.join(timeout=3)

    save_dir = getattr(getattr(model, "trainer", None), "save_dir", None)
    run_dir = str(save_dir) if save_dir else ""
    if not run_dir:
        candidates = sorted(list(Path(project).glob("*/weights/last.pt")) +
                            list(Path(project).glob("detect/*/weights/last.pt")),
                            key=lambda x: x.stat().st_mtime)
        run_dir = str(candidates[-1].parent.parent) if candidates else ""
    weights = Path(run_dir) / "weights" if run_dir else None
    out = {"run_dir": run_dir,
           "best": str(weights / "best.pt") if weights and (weights / "best.pt").exists() else "",
           "last": str(weights / "last.pt") if weights and (weights / "last.pt").exists() else ""}
    log(f"Training finished: {run_dir}")
    return out


def load_classes(data_root: str) -> list[str]:
    root = Path(data_root)
    f = root / "classes.txt"
    if f.exists():
        return [x.strip() for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]
    yaml_path = root / "data.yaml"
    if yaml_path.exists():
        import yaml
        cfg = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        names = cfg.get("names") or {}
        if isinstance(names, dict):
            return [names[k] for k in sorted(names, key=lambda x: int(x))]
        return list(names)
    return []


def write_label(path: Path, boxes: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for b in boxes:
        cls = int(b["cls"])
        vals = [cls, float(b["x"]), float(b["y"]), float(b["w"]), float(b["h"])]
        lines.append(" ".join(f"{v:.6f}" if isinstance(v, float) else str(v) for v in vals))
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def do_autolabel(p: dict) -> dict:
    """AI pre-annotation. Human review is still required - it only drafts boxes."""
    from ultralytics import YOLO

    model = YOLO(resolve_model(p["model"]), task="detect")
    classes = load_classes(p["dataset_root"])
    names = getattr(getattr(model, "model", None), "names", {}) or {}
    custom = len(names) == len(classes) and len(classes) > 0
    if not custom:
        log(f"NOTE: model has {len(names)} COCO classes but dataset has {len(classes)}. "
            "Boxes are drafts only; all classes are set to the selected default class.")
    default_cls = int(p.get("default_cls", 0))
    conf = float(p.get("conf", 0.25))
    device = p.get("device", "cpu")
    image_paths = [Path(x) for x in p["images"]]
    labels_dir = Path(p["labels_dir"])
    labels_dir.mkdir(parents=True, exist_ok=True)
    merge = bool(p.get("merge", False))

    done, boxes_total = 0, 0
    for i, img in enumerate(image_paths):
        if not img.is_file():
            continue
        res = model.predict(str(img), conf=conf, imgsz=int(p.get("imgsz", 640)),
                            device=device, verbose=False)[0]
        arr = res.boxes.xywhn.cpu().numpy() if getattr(res, "boxes", None) is not None else []
        cls_arr = res.boxes.cls.cpu().numpy() if getattr(res, "boxes", None) is not None else []
        boxes = []
        for j, xywh in enumerate(arr):
            x, y, w, h = [float(v) for v in xywh]
            cls = int(cls_arr[j]) if custom else default_cls
            if cls >= len(classes):
                cls = default_cls
            boxes.append({"cls": cls, "x": x, "y": y, "w": w, "h": h})
        if merge:
            old = labels_dir / f"{img.stem}.txt"
            if old.exists():
                for line in old.read_text(encoding="utf-8").splitlines():
                    parts = line.split()
                    if len(parts) == 5:
                        boxes.append({"cls": int(float(parts[0])), "x": float(parts[1]),
                                      "y": float(parts[2]), "w": float(parts[3]),
                                      "h": float(parts[4])})
        write_label(labels_dir / f"{img.stem}.txt", boxes)
        boxes_total += len(boxes)
        done += 1
        emit({"type": "progress", "done": done, "total": len(image_paths),
              "file": img.name, "boxes": len(boxes)})
    log(f"Auto-label finished: {done} images, {boxes_total} boxes (drafts, please review)")
    return {"labeled": done, "boxes": boxes_total, "custom_model": custom}


def do_predict(p: dict) -> dict:
    from ultralytics import YOLO
    model = YOLO(resolve_model(p["model"]), task="detect")
    names = getattr(getattr(model, "model", None), "names", {}) or {}
    res = model.predict(p["image"], conf=float(p.get("conf", 0.25)),
                        imgsz=int(p.get("imgsz", 640)), device=p.get("device", "cpu"),
                        verbose=False)[0]
    boxes = []
    if getattr(res, "boxes", None) is not None:
        xyxy = res.boxes.xyxy.cpu().numpy()
        confs = res.boxes.conf.cpu().numpy()
        clss = res.boxes.cls.cpu().numpy()
        for i, xy in enumerate(xyxy):
            cid = int(clss[i])
            boxes.append({"cls": cid, "name": str(names.get(cid, cid)),
                          "conf": round(float(confs[i]), 4),
                          "x1": float(xy[0]), "y1": float(xy[1]),
                          "x2": float(xy[2]), "y2": float(xy[3])})
    log(f"Predict: {len(boxes)} detections")
    return {"boxes": boxes, "shape": [int(res.orig_shape[0]), int(res.orig_shape[1])]}


def do_validate(p: dict) -> dict:
    from ultralytics import YOLO
    model = YOLO(resolve_model(p["model"]), task="detect")
    metrics = model.val(data=p["data_yaml"], imgsz=int(p.get("imgsz", 640)),
                        batch=int(p.get("batch", 1)), device=p.get("device", "cpu"),
                        plots=False, verbose=True)
    box = getattr(metrics, "box", None)
    return {"map50": float(box.map50) if box else 0.0,
            "map": float(box.map) if box else 0.0,
            "precision": float(box.mp) if box else 0.0,
            "recall": float(box.mr) if box else 0.0}


def do_export(p: dict) -> dict:
    from ultralytics import YOLO
    model = YOLO(resolve_model(p["model"]), task="detect")
    EXPORTS.mkdir(parents=True, exist_ok=True)
    fmt = p.get("format", "onnx")
    path = model.export(format=fmt, imgsz=int(p.get("imgsz", 640)),
                        device=p.get("device", "cpu"), dynamic=bool(p.get("dynamic", False)))
    log(f"Exported: {path}")
    return {"format": fmt, "path": str(path)}


def model_sources(name: str) -> list[str]:
    """Domestic-friendly HF mirror first, then the official GitHub release."""
    if name.startswith("yolo26"):
        repo = "YOLO26"
    elif name.startswith("yolo11"):
        repo = "YOLO11"
    elif name.startswith("yolov8"):
        repo = "YOLOv8"
    elif name.startswith("yolov5"):
        repo = "YOLOv5"
    else:
        repo = "YOLO26"
    return [
        f"https://hf-mirror.com/Ultralytics/{repo}/resolve/main/{name}",
        f"https://github.com/ultralytics/assets/releases/download/v8.4.0/{name}",
    ]


def do_download_model(p: dict) -> dict:
    """Download a pretrained weight with automatic source fallback."""
    import ssl
    import urllib.request

    MODELS.mkdir(parents=True, exist_ok=True)
    name = Path(p["model"]).name
    target = MODELS / name
    if target.is_file() and target.stat().st_size > 100000:
        return {"path": str(target), "cached": True, "size": target.stat().st_size}

    urls = ([p["url"]] if p.get("url") else []) + model_sources(name)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    errors = []
    for url in urls:
        tmp = MODELS / (name + ".part")
        try:
            log(f"Downloading {name} from {url.split('/')[2]} ...")
            emit({"type": "stage", "message": "下载源 " + url.split("/")[2]})
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            done = 0
            with urllib.request.urlopen(req, timeout=30, context=ctx) as r, tmp.open("wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                while True:
                    chunk = r.read(262144)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    emit({"type": "progress", "done": done, "total": total, "file": name})
            if done < 100000:
                raise RuntimeError(f"only {done} bytes received")
            tmp.replace(target)
            log(f"Downloaded {name}: {done / 1048576:.1f} MB")
            return {"path": str(target), "size": done, "source": url.split("/")[2]}
        except Exception as exc:
            errors.append(f"{url.split('/')[2]}: {exc}")
            log(f"  source failed: {errors[-1]}")
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
    raise RuntimeError(
        "所有下载源均失败（" + "; ".join(errors[:2]) + "）。"
        f"可手动把 {name} 放到 {MODELS}，或在「环境」页使用导入本地权重 / 自定义下载地址；"
        "也可以直接选「从零训练（yolo26.yaml）」，无需任何权重文件。")


HANDLERS = {
    "train": do_train,
    "autolabel": do_autolabel,
    "predict": do_predict,
    "validate": do_validate,
    "export": do_export,
    "download_model": do_download_model,
}


def main() -> int:
    if len(sys.argv) < 2:
        log("usage: worker_task.py job.json")
        return 2
    job = read_job(sys.argv[1])
    params = job.get("params", {})
    kind = job.get("type", "")
    try:
        handler = HANDLERS.get(kind)
        if not handler:
            raise RuntimeError(f"unknown task type: {kind}")
        result = handler(params)
        payload = {"ok": True, "type": kind, "result": result}
        emit({"type": "done", "result": result})
        write_result(job, payload)
        return 0
    except Exception as exc:
        log(traceback.format_exc())
        payload = {"ok": False, "type": kind, "error": str(exc)}
        emit({"type": "error", "error": str(exc)})
        write_result(job, payload)
        return 1


if __name__ == "__main__":
    sys.exit(main())
