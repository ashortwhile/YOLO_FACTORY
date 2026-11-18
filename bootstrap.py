# -*- coding: utf-8 -*-
"""YOLO Studio bootstrap - run via the standalone launcher EXE or `python bootstrap.py`.

1. find a usable system Python
2. reuse a ready venv or create an isolated one
3. detect NVIDIA / AMD / Intel / Apple GPU and record the matching backend plan
4. start the local service in a standalone desktop app window
5. heavy runtime install (torch / ultralytics / weights) is optional and runs
   later as a background task from the Environment page, so first launch is fast

The service itself is standard-library only and runs before torch is present.
`--force` still performs the full install synchronously (CLI / advanced use).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

import env_utils
import hardware

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
PARENT = ROOT.parent
VENV = ROOT / ".venv"
WORKSPACE = ROOT / "workspace"
LOGS = ROOT / "logs"
CONFIG_PATH = ROOT / "config.json"

MIRRORS = {
    "tsinghua": "https://pypi.tuna.tsinghua.edu.cn/simple",
    "aliyun": "https://mirrors.aliyun.com/pypi/simple",
    "pypi": "https://pypi.org/simple",
}
DEFAULT_CONFIG = {
    "version": 1,
    "port": 8765,
    "host": "127.0.0.1",
    "mirror": "tsinghua",
    "python": "",
    "venv_python": "",
    "reuse_parent_venv": False,
    "device": "auto",
    "auto_open_browser": True,
    "app_mode": True,
    "default_model": "yolo26n.pt",
    # first launch pulls the newest ultralytics and the newest matching torch
    # build for whatever GPU was detected, plus the default model weights
    "auto_update": True,
    "auto_download_model": True,
    "auto_discover": False,
    "external_datasets": [],
    "external_runs": [],
    "hardware": {},
    "setup_ok": False,
    "last_setup": "",
}


def log(msg: str = "") -> None:
    print(msg, flush=True)


def section(msg: str) -> None:
    log("\n" + "=" * 64)
    log("  " + msg)
    log("=" * 64)


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception as exc:
            log(f"[warn] config.json unreadable ({exc}), using defaults")
    if cfg.get("mirror") not in MIRRORS:
        log(f"  [warn] unknown mirror {cfg.get('mirror')!r} -> using 'tsinghua'")
        cfg["mirror"] = "tsinghua"
    return cfg


def save_config(cfg: dict) -> None:
    """Atomic replace: a crash must never leave a half-written config.json."""
    tmp = CONFIG_PATH.with_name(f"config.json.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG_PATH)


def run(cmd: list[str], timeout: int = 1800, env: dict | None = None) -> bool:
    log("  $ " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
    try:
        subprocess.run(cmd, check=True, timeout=timeout, cwd=str(ROOT),
                       env=env if env is not None else env_utils.clean_env())
        return True
    except subprocess.TimeoutExpired:
        log(f"  [warn] command timed out after {timeout}s: {cmd[0]}")
    except subprocess.CalledProcessError as exc:
        log(f"  [warn] command exited with code {exc.returncode}")
    except Exception as exc:
        log(f"  [warn] command failed: {exc}")
    return False


def probe(cmd: list[str], timeout: int = 25) -> tuple[bool, str]:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                             encoding="utf-8", errors="replace",
                             env=env_utils.clean_env())
        return out.returncode == 0, (out.stdout or out.stderr or "").strip()
    except Exception as exc:
        return False, str(exc)


def py_version(exe: str) -> tuple[int, int] | None:
    ok, out = probe([exe, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"])
    m = re.search(r"(\d+)\.(\d+)", out) if ok else None
    return (int(m.group(1)), int(m.group(2))) if m else None


def find_system_python(cfg: dict) -> str:
    if cfg.get("python"):
        v = py_version(cfg["python"])
        # Keys below are major*100+minor; reject unsupported versions from a
        # stale config.json instead of proceeding with an incompatible Python.
        if v and 309 <= v[0] * 100 + v[1] <= 313:
            return cfg["python"]
    candidates: list[tuple[int, str]] = []
    ok, out = probe(["py", "-0p"])
    if ok:
        for line in out.splitlines():
            m = re.search(r"-\s*(\d+)\.(\d+)[^\s]*\s*.*?\s+(\S+\.exe)", line)
            if m and len(m.groups()) == 3:
                candidates.append((int(m.group(1)) * 100 + int(m.group(2)), m.group(3)))
            else:
                m2 = re.search(r"(\S*python\.exe)", line, re.I)
                if m2:
                    v = py_version(m2.group(1))
                    if v:
                        candidates.append((v[0] * 100 + v[1], m2.group(1)))
    for name in ("python", "python3"):
        exe = shutil.which(name)
        if exe:
            v = py_version(exe)
            if v:
                candidates.append((v[0] * 100 + v[1], exe))
    priority = {310: 0, 311: 1, 312: 2, 309: 3, 313: 4}
    usable = [(k, p) for k, p in candidates if 309 <= k <= 313]
    if not usable:
        raise RuntimeError(
            "No usable Python (3.9-3.13) found.\n"
            "  Install Python 3.11 from https://www.python.org/downloads/ and run again.\n"
            "  Remember to tick 'Add python.exe to PATH'."
        )
    usable.sort(key=lambda x: (priority.get(x[0], 9), x[1]))
    chosen = usable[0][1]
    log(f"  system Python : {chosen}  ({usable[0][0] // 100}.{usable[0][0] % 100})")
    return chosen


def venv_python(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def stack_ready(py: str) -> bool:
    code = ("import torch, ultralytics, PIL, yaml, cv2, numpy; "
            "print(torch.__version__, ultralytics.__version__)")
    ok, out = probe([py, "-c", code], timeout=90)
    if ok:
        log(f"  ready: {out}")
    return ok


def disk_free_gb(py: str) -> float:
    """Free space (GB) on the drive that holds the runtime venv."""
    try:
        import shutil
        drive = Path(py).anchor or "/"
        return shutil.disk_usage(drive).free / 2 ** 30
    except Exception:
        return 999.0


# GPU torch builds (CUDA / XPU / ROCm) unpack to several GB on top of the
# wheel itself; CPU builds are much smaller.
_TORCH_NEED_GB = {"cpu": 2.0}


def torch_need_gb(backend: str) -> float:
    return _TORCH_NEED_GB.get(backend, 6.0)


def pick_or_create_venv(cfg: dict, sys_py: str) -> str:
    if cfg.get("venv_python") and Path(cfg["venv_python"]).exists():
        if stack_ready(cfg["venv_python"]):
            log(f"  reuse configured venv: {cfg['venv_python']}")
            return cfg["venv_python"]
    if cfg.get("reuse_parent_venv"):
        parent_venv = venv_python(PARENT / ".venv")
        if parent_venv.exists():
            py = str(parent_venv)
            log(f"  checking parent venv: {py}")
            if stack_ready(py):
                log("  parent venv has the full stack - reusing it (no downloads)")
                return py
            log("  parent venv is incomplete, will install missing packages into it")
            return py
    py = venv_python(VENV)
    if not py.exists():
        section("Step 2/6  Create isolated virtual environment")
        run([sys_py, "-m", "venv", str(VENV)])
    if not py.exists():
        raise RuntimeError("venv creation failed")
    return str(py)


def _is_torch_index_arg(a: str) -> bool:
    """True if this arg points at a PyTorch wheel index (official or mirror)."""
    return ("download.pytorch.org" in a or "repo.amd.com" in a or
            "pytorch-wheels" in a or "sjtu.edu.cn" in a)


def pip_install(py: str, args: list[str], mirror: str, fallback: bool = True) -> bool:
    """Install with a long timeout. Torch wheels are large; never silently
    retry them against plain PyPI (that would pull a CUDA-heavy build)."""
    cmd = [py, "-m", "pip", "install", "--disable-pip-version-check",
           "--retries", "3", "--timeout", "120"]
    url = MIRRORS.get(mirror, MIRRORS["tsinghua"])
    if not any(_is_torch_index_arg(a) for a in args):
        cmd += ["-i", url]
    cmd += args
    if run(cmd):
        return True
    if not fallback:
        return False
    log("  [warn] install failed with that index, retrying with official PyPI...")
    retry = [c for c in cmd if c not in ("-i", url)]
    if run(retry + ["-i", MIRRORS["pypi"]]):
        return True
    proxies = env_utils.proxy_snapshot()
    if proxies:
        # direct connection failed and the launcher had proxy settings
        # (agent shells inject local per-session tunnels) -> one last try
        # through them before giving up.
        log("  [warn] direct download failed, retrying with the inherited proxy...")
        if run(cmd, env=env_utils.clean_env(keep_proxy=True)):
            return True
        return run(retry + ["-i", MIRRORS["pypi"]],
                   env=env_utils.clean_env(keep_proxy=True))
    return False


def _torch_variant(vendor: str, plan: dict) -> str:
    """The wheel tag for this machine: xpu / cuda tag / cpu."""
    if vendor == "intel":
        return "xpu"
    if vendor == "nvidia":
        return hardware.cuda_tag(plan.get("nvidia", {}).get("cuda_version", ""))
    return "cpu"


def torch_plan_for(vendor: str, plan: dict, prefer_domestic: bool = True) -> tuple[str, str]:
    """Return (index-url, human label) for a fresh install on this machine.

    Domestic mirror (SJTU) is preferred so multi-GB wheels download over a
    domestic line instead of the slow overseas download.pytorch.org.
    """
    if vendor == "nvidia":
        tag = hardware.cuda_tag(plan.get("nvidia", {}).get("cuda_version", ""))
        return hardware.torch_index_url(tag, prefer_domestic), f"CUDA ({tag})"
    if vendor == "intel":
        return hardware.torch_index_url("xpu", prefer_domestic), "Intel XPU"
    if vendor == "amd":
        # PyTorch publishes no Windows ROCm wheels; HIP SDK users can switch manually later.
        return hardware.torch_index_url("cpu", prefer_domestic), "CPU (AMD ROCm on Windows needs HIP SDK / WSL2)"
    if vendor == "apple":
        return hardware.torch_index_url("cpu", prefer_domestic), "Apple MPS (standard PyPI torch)"
    return hardware.torch_index_url("cpu", prefer_domestic), "CPU"


def backend_matches(backend: str, info: dict) -> bool:
    """Is the installed torch build already the one this machine needs?"""
    if not info.get("version"):
        return False
    checks = {
        "xpu": bool(info.get("xpu")),
        "cuda": bool(info.get("cuda")),
        "rocm": bool(info.get("hip")),
        "mps": bool(info.get("mps")),
        "dml": bool(info.get("directml")),
        "cpu": not any(info.get(k) for k in ("cuda", "xpu", "hip")),
    }
    return checks.get(backend, True)


def setup_dependencies(cfg: dict, py: str, plan: dict, force: bool) -> None:
    """First launch installs the newest torch build that suits the *detected*
    GPU, plus the newest ultralytics. Nothing is hard-wired to one vendor.

    Fast path: if the venv is already complete and the torch backend matches,
    skip ALL network work (pip self-upgrade, ultralytics sync, wheel checks).
    This is what makes the 2nd+ launch start in ~2s and work fully offline.
    `--force` always runs the full path; auto_update is throttled to once / 7d.
    """
    section("Step 3/6  Install the newest runtime matching this GPU")
    mirror = cfg.get("mirror", "tsinghua")
    mirror_url = MIRRORS.get(mirror, MIRRORS["tsinghua"])
    log(f"  pip mirror : {mirror_url}")

    vendor = plan.get("vendor", "cpu")
    recommended = plan.get("recommended") or {}
    backend = recommended.get("backend", "cpu")
    device = recommended.get("device", "cpu")
    index, label = torch_plan_for(vendor, plan)
    # Windows has no ROCm wheels and Apple uses the standard wheel: both are
    # a CPU install, so don't re-download torch on every launch.
    cpu_indexes = (hardware.torch_index_url("cpu", True), hardware.torch_index_url("cpu", False))
    if index in cpu_indexes and backend != "cpu":
        backend = "cpu"
    log(f"  detected   : {plan.get('vendor_label')}")
    log(f"  target     : {backend} / {device}  (torch from {label})")

    info = hardware.probe_torch(py)
    free_gb = disk_free_gb(py)
    log(f"  disk free  : {free_gb:.1f} GB on {Path(py).anchor}")

    # ---- fast path: a complete, matching venv needs zero network ----
    if not force and backend_matches(backend, info) and stack_ready(py):
        last = cfg.get("last_setup") or ""
        due = False
        if cfg.get("auto_update", True) and last:
            try:
                due = (time.time() - time.mktime(time.strptime(last, "%Y-%m-%d %H:%M:%S"))) >= 7 * 86400
            except Exception:
                due = True
        if not due:
            log(f"  runtime ready ({info.get('version') or 'venv'}) -> skip all network checks")
            log("  (use --force to reinstall / force-update packages)")
            return
        log("  weekly auto-update check enabled...")
    else:
        # Only upgrade pip when we're actually going to install something; a
        # self-upgrade on every launch added tens of seconds (or a 300s stall).
        up = subprocess.run([py, "-m", "pip", "install", "-q", "-U", "pip", "-i", mirror_url],
                            cwd=str(ROOT), capture_output=True, timeout=120,
                            env=env_utils.clean_env())
        if up.returncode != 0:
            detail = (up.stderr or b"").decode("utf-8", "replace").strip()[-300:]
            log(f"  [warn] pip self-upgrade skipped: {detail or 'unknown error'}")
    if force or not backend_matches(backend, info):
        need_gb = torch_need_gb(backend)
        if free_gb < need_gb:
            log(f"  [warn] 剩余空间 {free_gb:.1f} GB，装不下 {backend} 版 torch（约需 {need_gb:.0f} GB）")
            if info.get("version"):
                log("        保留现有 torch 不做任何改动，跳过本次下载（不会反复重试）")
                log("        建议把软件安装到剩余空间充足的磁盘后再切换加速。")
            else:
                if free_gb < 2.0:
                    raise RuntimeError(
                        f"磁盘剩余 {free_gb:.1f} GB，连 CPU 版 torch 都装不下。"
                        "请清理磁盘，或把软件安装到空间充足的磁盘（例如 D 盘）。")
                log("        改装体积较小的 CPU 版，软件仍可正常使用。")
                for idx in hardware.torch_indexes("cpu"):
                    if pip_install(py, ["torch", "torchvision", "--index-url", idx],
                                   mirror, fallback=False):
                        break
        else:
            log("  installing the matching PyTorch build (large download, please wait)...")
            # Try the domestic mirror first (fast), then the official index.
            # Both carry identical wheels; only the network path differs.
            ok = False
            tried = []
            for idx in dict.fromkeys([index] + hardware.torch_indexes(_torch_variant(vendor, plan))):
                where = "domestic mirror" if "sjtu.edu.cn" in idx else "official PyTorch"
                log(f"  torch index: {idx}  ({where})")
                tried.append(idx)
                if pip_install(py, ["torch", "torchvision", "--index-url", idx],
                               mirror, fallback=False):
                    ok = True
                    break
                log("  [warn] that index failed, trying the next one...")
            if not ok and backend != "cpu":
                log(f"  [warn] {backend} build unavailable on all indexes -> falling back to CPU torch")
                for idx in hardware.torch_indexes("cpu"):
                    if pip_install(py, ["torch", "torchvision", "--index-url", idx],
                                   mirror, fallback=False):
                        ok = True
                        break
    else:
        log(f"  torch already matches ({info.get('version')}) -> skip re-download")

    if cfg.get("auto_update", True) or force:
        log("  syncing ultralytics to the newest release...")
        if not pip_install(py, ["-U", "ultralytics"], mirror):
            log("  [warn] ultralytics update unavailable (offline?) - keeping installed version")

    # Offline friendly: a complete venv needs no network at all. Previously a
    # dead mirror aborted setup even when every package was already installed.
    if stack_ready(py):
        log("  dependencies already satisfied -> skip requirement install")
    elif not pip_install(py, ["-r", str(ROOT / "requirements.txt")], mirror):
        raise RuntimeError("Dependency install failed. Check network / mirror in config.json")
    if not stack_ready(py):
        raise RuntimeError("Dependencies installed but import check failed. Re-run with --force")


MODEL_REPOS = {"yolo26": "YOLO26", "yolo11": "YOLO11", "yolov8": "YOLOv8",
               "yolov5": "YOLOv5"}


def auto_fetch_model(cfg: dict) -> None:
    """First launch fetches the default weights. Failure is non-fatal."""
    if not cfg.get("auto_download_model", True):
        return
    raw = str(cfg.get("default_model") or "yolo26n.pt")
    name = Path(raw).name
    # config.json is user-editable: never let it steer the write outside models/.
    if name != raw or not re.fullmatch(r"[A-Za-z0-9._-]+\.(?:pt|onnx|yaml)", name):
        log(f"  [warn] invalid default_model {raw!r} -> skip download")
        return
    models = WORKSPACE / "models"
    models.mkdir(parents=True, exist_ok=True)
    target = models / name
    if target.is_file() and target.stat().st_size > 100_000:
        log(f"  model      : {name} already present")
        return
    expected = str(cfg.get("model_sha256") or "").strip().lower()
    if expected and not re.fullmatch(r"[0-9a-f]{64}", expected):
        log("  [warn] model_sha256 is not a sha256 hex digest -> ignored")
        expected = ""
    repo = next((v for k, v in MODEL_REPOS.items() if name.startswith(k)), "YOLO26")
    sources = [
        f"https://hf-mirror.com/Ultralytics/{repo}/resolve/main/{name}",
        f"https://huggingface.co/Ultralytics/{repo}/resolve/main/{name}",
        f"https://github.com/ultralytics/assets/releases/download/v8.4.0/{name}",
    ]
    tmp = target.with_suffix(".part")
    for url in sources:
        try:
            log(f"  downloading {name} from {url.split('/')[2]} ...")
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            # Default TLS verification is intentional: .pt files are pickles and
            # loading one executes code. Never disable certificate checks.
            with urllib.request.urlopen(req, timeout=30) as r, tmp.open("wb") as fh:
                shutil.copyfileobj(r, fh, 262144)
            size = tmp.stat().st_size
            if size < 100_000:
                raise RuntimeError(f"only {size} bytes, probably an error page")
            if expected:
                digest = hashlib.sha256()
                with tmp.open("rb") as f:
                    for chunk in iter(lambda: f.read(262144), b""):
                        digest.update(chunk)
                if digest.hexdigest() != expected:
                    raise RuntimeError("sha256 mismatch - refusing to use this file")
            tmp.replace(target)
            log(f"  model      : {name} ready ({size / 1048576:.1f} MB)")
            return
        except Exception as exc:
            log(f"  [warn] source failed: {str(exc)[:120]}")
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
    log("  [warn] model download skipped - use the 模型 page to retry later")


def discover_assets(cfg: dict) -> None:
    section("Step 4/6  Prepare workspace")
    for d in (WORKSPACE / "models", WORKSPACE / "datasets", WORKSPACE / "exports",
              WORKSPACE / "runs", LOGS):
        d.mkdir(parents=True, exist_ok=True)
    # Portable software ships clean: no datasets, weights or previous runs are
    # pulled from whatever folder it happens to be installed next to.
    # Users can opt in from the UI ("扫描本机已有数据集").
    if not cfg.get("auto_discover", False):
        log("  clean portable mode: nothing pre-mounted")
        log("  existing datasets can be imported from the 数据集 page when needed")
        return

    models = WORKSPACE / "models"
    for cand in (PARENT / "yolo26n.pt", PARENT / "yolo11n.pt", PARENT / "yolov8n.pt"):
        if cand.exists() and not (models / cand.name).exists():
            shutil.copy2(cand, models / cand.name)
            log(f"  seeded model: {cand.name}")
    for trained in (PARENT / "runs").glob("detect/*/weights/best.pt"):
        target = models / f"{trained.parent.parent.name}_best.pt"
        if not target.exists():
            shutil.copy2(trained, target)
            log(f"  seeded trained weight: {target.name}")

    found = []
    for child in sorted(PARENT.iterdir()):
        if not child.is_dir() or child.name in (ROOT.name, ".venv", ".idea", "runs", "__pycache__"):
            continue
        if (child / "images" / "train").is_dir() and (child / "data.yaml").exists():
            found.append({"name": child.name, "root": str(child).replace("\\", "/")})
    cfg["external_datasets"] = found
    for d in found:
        log(f"  dataset: {d['name']} -> {d['root']}")

    runs = [str(r).replace("\\", "/") for r in (PARENT / "runs", WORKSPACE / "runs") if r.is_dir()]
    cfg["external_runs"] = runs
    for r in runs:
        log(f"  runs dir: {r}")
    os.environ["ULTRALYTICS_DOWNLOAD_FONT"] = "0"


def find_chromium() -> str:
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        os.path.join(pf86, "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(pf, "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(pf, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(pf86, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(local, "Google", "Chrome", "Application", "chrome.exe"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    for name in ("msedge", "chrome", "chromium"):
        exe = shutil.which(name)
        if exe:
            return exe
    return ""


def open_app_window(url: str, cfg: dict) -> None:
    exe = find_chromium() if cfg.get("app_mode", True) else ""
    if exe:
        profile = WORKSPACE / ".appprofile"
        profile.mkdir(parents=True, exist_ok=True)
        args = [exe, f"--app={url}", "--window-size=1500,960", "--window-position=60,40",
                f"--user-data-dir={profile}", "--no-first-run",
                "--no-default-browser-check", "--disable-features=Translate,msEdgeSidebarV2"]
        try:
            subprocess.Popen(args, cwd=str(ROOT))
            log(f"  desktop app window: {Path(exe).name}")
            return
        except Exception as exc:
            log(f"  [warn] app window failed ({exc}), using default browser")
    try:
        if not webbrowser.open(url):
            log("  [warn] no browser registered; open " + url + " manually")
    except Exception as exc:
        log(f"  [warn] could not open browser ({exc}); open {url} manually")


def service_ready(host: str, port: int) -> bool:
    """A busy TCP port is not proof OUR service is up - ask /api/health.

    Use a raw HTTP connection instead of urllib: urllib honours the system
    HTTP_PROXY/HTTPS_PROXY, and on Windows a proxy without 127.0.0.1 in
    NO_PROXY silently swallows the loopback request. That made the app window
    never open even though the local service was healthy. http.client connects
    directly, so proxy settings can never interfere with local detection.
    """
    import http.client
    conn = None
    try:
        conn = http.client.HTTPConnection(host, port, timeout=1.5)
        conn.request("GET", "/api/health")
        resp = conn.getresponse()
        body = resp.read(256)
        return resp.status == 200 and b"yolo-studio" in body
    except Exception:
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def launch_app(url: str, cfg: dict, delay: float = 0.0) -> None:
    """Open the window only once the service actually answers."""
    if not cfg.get("auto_open_browser", True):
        return

    def _launch():
        if delay:
            time.sleep(delay)
        u = urlparse(url)
        host, port = u.hostname or "127.0.0.1", u.port or 8765
        deadline = time.time() + 90
        while time.time() < deadline and not service_ready(host, port):
            time.sleep(0.3)
        if not service_ready(host, port):
            # Never give up silently: a running service that we failed to
            # probe must still get a visible window. Fall back to a plain
            # browser tab, which is more likely to open than an Edge --app
            # window from a frozen launcher.
            log(f"  [warn] health check timed out; opening {url} in default browser anyway")
            fallback = dict(cfg)
            fallback["app_mode"] = False
            open_app_window(url, fallback)
            return
        open_app_window(url, cfg)

    threading.Thread(target=_launch, daemon=True).start()


def port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((host, port)) == 0


def main() -> int:
    ap = argparse.ArgumentParser(description="YOLO Studio bootstrap")
    ap.add_argument("--force", action="store_true", help="reinstall dependencies")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--browser", action="store_true", help="use a normal browser tab instead of app window")
    ap.add_argument("--setup-only", action="store_true", help="configure only, do not start server")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--mirror", choices=list(MIRRORS), default="")
    args = ap.parse_args()

    for d in (WORKSPACE, LOGS, WORKSPACE / "datasets", WORKSPACE / "exports",
              WORKSPACE / "models", WORKSPACE / "runs"):
        d.mkdir(parents=True, exist_ok=True)

    cfg = load_config()
    if args.port:
        cfg["port"] = args.port
    if args.mirror:
        cfg["mirror"] = args.mirror
    if args.browser:
        cfg["app_mode"] = False

    section("YOLO Studio  |  one-click environment setup")
    log(f"  install dir : {ROOT}")
    host, port = cfg.get("host", "127.0.0.1"), int(cfg.get("port", 8765))
    url = f"http://{host}:{port}/"
    if service_ready(host, port) and not args.setup_only:
        log(f"\n  Service already running at {url} - opening the app window.")
        # Open synchronously: this branch returns immediately, so a daemon
        # thread with a timer would die before opening anything.
        open_app_window(url, cfg)
        return 0
    if port_open(host, port) and not args.setup_only:
        log(f"\n  [warn] port {port} is occupied by another program, not YOLO Studio.")
        log("         Close it or change 'port' in config.json, then retry.")
        return 1

    try:
        section("Step 1/6  Detect hardware and Python")
        sys_py = find_system_python(cfg)
        cfg["python"] = sys_py
        plan = hardware.build_plan(cfg, sys.executable)
        gpus = plan.get("gpus", [])
        if gpus:
            for g in gpus:
                log(f"  GPU: {g['name']} | {g['vendor'].upper()} | "
                    f"{g['vram_mb']/1024:.1f} GB | driver {g['driver']}")
        else:
            log("  GPU: none detected -> CPU mode")
        log(f"  {plan['note']}")

        py = pick_or_create_venv(cfg, sys_py)
        cfg["venv_python"] = py
        log(f"  runtime     : {py}")

        discover_assets(cfg)

        # "Enter first, install later": the local service is pure standard
        # library and runs fine without torch. On a brand-new venv we skip the
        # multi-GB download here so the app window opens in seconds; the user
        # installs the runtime from the Environment page as a background task.
        # `--force` keeps the old blocking behaviour for CLI / advanced use.
        runtime_ready = stack_ready(py)
        if runtime_ready or args.force:
            setup_dependencies(cfg, py, plan, args.force)
            auto_fetch_model(cfg)
            cfg["setup_ok"] = True
            cfg["last_setup"] = time.strftime("%Y-%m-%d %H:%M:%S")
            log("  runtime ready - all features available")
        else:
            log("  runtime not installed yet -> opening the app first.")
            log("  Use 环境与显卡 > 安装运行环境 to install it in the background.")
            cfg["setup_ok"] = False

        # full probe against the actual runtime, saved for the UI. Safe even
        # when torch is absent: probe_torch just reports empty capabilities.
        full = hardware.build_plan(cfg, py)
        cfg["hardware"] = full
        cfg["device"] = full["recommended"]["device"] if full["ready"] else "cpu"
        log(f"  active backend: {full['active']['backend']} | "
            f"recommended: {full['recommended']['backend']} | ready: {full['ready']}")
        save_config(cfg)
    except Exception as exc:
        log(f"\n[ERROR] setup failed: {exc}")
        log("  Fix the issue above and run the launcher again.")
        # cfg still holds the previous run's setup_ok=True; never let the UI
        # believe a failed environment is ready.
        cfg["setup_ok"] = False
        cfg["last_error"] = str(exc)
        try:
            save_config(cfg)
        except Exception as write_exc:
            log(f"  [warn] could not save config: {write_exc}")
        return 1

    if args.setup_only:
        section("Setup complete (server not started, --setup-only)")
        return 0

    section("Step 5/6  Start local service / Step 6/6  Open app window")
    log(f"  URL: {url}")
    log("  Keep this window open while using the studio. Close it to stop the service.\n")
    env = env_utils.clean_env({"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
                               "ULTRALYTICS_DOWNLOAD_FONT": "0", "YSTUDIO_ROOT": str(ROOT)})
    launch_app(url, cfg)
    cmd = [py, str(ROOT / "server.py"), "--host", host, "--port", str(port)]
    try:
        return subprocess.call(cmd, cwd=str(ROOT), env=env)
    except KeyboardInterrupt:
        log("\n  stopped.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
