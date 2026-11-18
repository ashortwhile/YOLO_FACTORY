# -*- coding: utf-8 -*-
"""YOLO 工坊 —— 安装程序（安装 / 卸载两用）。

安装器与启动器是**两个独立 EXE**：
  * YOLOStudioSetup.exe（本程序）—— 安装向导 + 卸载程序；
  * YOLO工坊.exe（launcher.py 单独打包，随 payload 一起分发）—— 软件启动器。

安装器不再充当启动器。它把 payload 解压到目标目录（其中已含独立启动器），
再创建指向该启动器的桌面 / 开始菜单快捷方式，并写入卸载注册表项。
控制面板「添加或删除程序」用 --uninstall 调用本 EXE。

静默部署参数：
  --silent --dir="D:\\Somewhere" [--no-shortcuts] [--launch]
  --uninstall [--silent] [--keep-data]

设计要点：EXE 里只内嵌纯代码压缩包（不含 venv / 不含本机 config / 不含数据集），
装到任何一台 Windows 机器后首次运行才由独立启动器 + bootstrap.py 配置环境。
"""
from __future__ import annotations

import base64
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

from version import APP_VERSION

APP_NAME = "YOLO 工坊"
APP_ID = "YOLOStudio"
VERSION = APP_VERSION
PUBLISHER = "YOLO Studio"
EXE_NAME = "YOLO工坊.exe"          # 独立启动器（随 payload 分发，安装器不再复制自身充当）
LAUNCH_EXE = EXE_NAME
UNINSTALL_EXE = "卸载YOLO工坊.exe"   # 安装目录里保留的安装器副本，专供卸载
SHORTCUT_NAME = "YOLO 工坊.lnk"
UNINSTALL_NAME = "卸载 YOLO 工坊.lnk"
INFO_NAME = "安装信息.json"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\YOLOStudio"

SELF = Path(sys.executable).resolve()
HERE = SELF.parent
BUNDLE = Path(getattr(sys, "_MEIPASS", str(HERE)))
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DETACHED = 0x00000008 if os.name == "nt" else 0
IS_WIN = os.name == "nt"


# --------------------------------------------------------------------------- utils
def log(msg: str) -> None:
    try:
        p = Path(tempfile.gettempdir()) / "YOLOStudio_setup.log"
        with p.open("a", encoding="utf-8", errors="replace") as fh:
            fh.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "gbk", "cp936"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", "replace")


def alert(msg: str, title: str = APP_NAME, kind: str = "info",
          default: bool = True) -> bool:
    """Show a native dialog. Returns True for yes / ok.

    If Tk is broken the dialog cannot be shown - default to the SAFE answer so
    a failed "keep user data?" prompt never silently deletes the workspace.
    """
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        fn = {"info": messagebox.showinfo, "error": messagebox.showerror,
              "yesno": messagebox.askyesno}.get(kind, messagebox.showinfo)
        res = fn(title, msg)
        root.destroy()
        return bool(res)
    except Exception:
        log("dialog failed: " + msg)
        return default


def native_alert(msg: str, title: str = APP_NAME, timeout_ms: int = 0) -> None:
    """Win32 MessageBox - works before Tk is loaded and even if Tk is broken.
    timeout_ms > 0 auto-dismisses, so a stray double-click never leaves a modal stuck.
    """
    if not IS_WIN:
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        flags = 0x40 | 0x1000                      # MB_ICONINFORMATION | MB_TOPMOST
        if timeout_ms:
            try:
                user32.MessageBoxTimeoutW(0, str(msg), str(title), flags, 0, timeout_ms)
                return
            except Exception:
                pass
        user32.MessageBoxW(0, str(msg), str(title), flags)
    except Exception as exc:
        log("native alert failed: %r" % exc)


def close_splash() -> None:
    """Close PyInstaller's boot splash, if this build actually has one.

    Builds made without a splash (e.g. a Python without tkinter) still include
    the pyi_splash stub, whose import raises KeyError and prints a traceback to
    stderr. Detect the IPC handle first so those builds stay silent.
    """
    import os
    if not os.environ.get("_PYI_SPLASH_IPC"):
        return
    try:
        import pyi_splash  # type: ignore
        pyi_splash.close()
        log("splash closed")
    except Exception:
        pass


def single_instance() -> bool:
    """Prevent five silent processes piling up when the user double-clicks again."""
    if not IS_WIN:
        return True
    try:
        import ctypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        k.CreateMutexW.restype = ctypes.c_void_p
        handle = k.CreateMutexW(None, False, "Local\\YOLOStudioSingleInstance")
        already = ctypes.get_last_error() == 183       # ERROR_ALREADY_EXISTS
        if not handle or already:
            native_alert("YOLO 工坊正在启动中。\n\n"
                         "单文件程序首次解压需要十几秒，请稍等窗口出现，不要重复双击。",
                         timeout_ms=4000)
            log("another instance already running -> exit")
            return False
        globals()["_INSTANCE_MUTEX"] = handle          # keep alive for process lifetime
        return True
    except Exception as exc:
        log("mutex skipped: %r" % exc)
        return True


def ps(script: str, timeout: int = 45) -> tuple[int, str]:
    """Run PowerShell without flashing a console; -EncodedCommand avoids CJK quoting bugs."""
    cmd = shutil.which("powershell") or "powershell.exe"
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        r = subprocess.run(
            [cmd, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-EncodedCommand", enc],
            capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
        return r.returncode, _decode((r.stdout or b"") + (r.stderr or b""))
    except Exception as exc:
        log("powershell error: %r" % exc)
        return -1, str(exc)


def q(value: str) -> str:
    """Single-quoted PowerShell literal."""
    return "'" + str(value).replace("'", "''") + "'"


def default_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / APP_ID


def find_payload() -> Path | None:
    for cand in (BUNDLE / "payload.zip", HERE / "payload.zip",
                 Path(__file__).resolve().parent / "payload.zip"):
        try:
            if cand.is_file():
                return cand
        except Exception:
            pass
    return None


def parse_args(argv: list[str]) -> dict:
    out = {"silent": "--silent" in argv, "uninstall": "--uninstall" in argv,
           "from_temp": "--from-temp" in argv, "keep_data": "--keep-data" in argv,
           "no_shortcuts": "--no-shortcuts" in argv, "launch": "--launch" in argv,
           "install": "--install" in argv, "dir": None, "target": None}
    for a in argv:
        if a.startswith("--dir="):
            out["dir"] = a[6:].strip('"')
        elif a.startswith("--target="):
            out["target"] = a[9:].strip('"')
    return out


# --------------------------------------------------------------------------- registry
def reg_write(inst: Path) -> None:
    if not IS_WIN:
        return
    import winreg
    launcher = inst / EXE_NAME
    uninstaller = inst / UNINSTALL_EXE
    k = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY, 0, winreg.KEY_WRITE)
    values = {
        "DisplayName": APP_NAME,
        "DisplayVersion": VERSION,
        "Publisher": PUBLISHER,
        "DisplayIcon": str(launcher) + ",0",
        "InstallLocation": str(inst),
        "UninstallString": '"%s" --uninstall' % uninstaller,
        "QuietUninstallString": '"%s" --uninstall --silent --keep-data' % uninstaller,
        "URLInfoAbout": "https://docs.ultralytics.com/",
        "Comments": "YOLO 数据集标注 / 训练 / 推理一体化工作台",
    }
    for name, val in values.items():
        winreg.SetValueEx(k, name, 0, winreg.REG_SZ, val)
    winreg.SetValueEx(k, "NoModify", 0, winreg.REG_DWORD, 1)
    winreg.SetValueEx(k, "NoRepair", 0, winreg.REG_DWORD, 1)
    k.Close()
    log("uninstall registry written")


def reg_read() -> Path | None:
    if not IS_WIN:
        return None
    import winreg
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
        val = winreg.QueryValueEx(k, "InstallLocation")[0]
        k.Close()
        return Path(val)
    except Exception:
        return None


def reg_delete() -> None:
    if not IS_WIN:
        return
    import winreg
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
        log("uninstall registry removed")
    except Exception as exc:
        log("registry delete skipped: %r" % exc)


# --------------------------------------------------------------------------- shortcuts
def shell_folders() -> tuple[Path, Path]:
    """Resolve Desktop / Start Menu paths from the registry (no shell output
    parsing, which breaks on CJK consoles and produces invalid paths)."""
    fallback_desktop = str(Path.home() / "Desktop")
    fallback_programs = str(Path(os.environ.get("APPDATA") or Path.home()) /
                            "Microsoft/Windows/Start Menu/Programs")

    def known(name: str, fallback: str) -> Path:
        if IS_WIN:
            try:
                import winreg
                k = winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders")
                val, _ = winreg.QueryValueEx(k, name)
                k.Close()
                expanded = os.path.expandvars(str(val).strip().strip('"'))
                if expanded and Path(expanded).is_dir():
                    return Path(expanded)
            except Exception as exc:
                log("shell folder %s lookup skipped: %r" % (name, exc))
        return Path(fallback)

    return known("Desktop", fallback_desktop), known("Programs", fallback_programs)


def make_shortcut(lnk: Path, target: Path, workdir: Path, args: str, desc: str) -> bool:
    try:
        lnk.parent.mkdir(parents=True, exist_ok=True)
        script = f"""
$w = New-Object -ComObject WScript.Shell
$s = $w.CreateShortcut({q(str(lnk))})
$s.TargetPath = {q(str(target))}
$s.Arguments = {q(args)}
$s.WorkingDirectory = {q(str(workdir))}
$s.IconLocation = {q(str(target) + ',0')}
$s.Description = {q(desc)}
$s.Save()
"""
        rc, out = ps(script)
        ok = lnk.is_file()
        log("shortcut %s rc=%s ok=%s %s" % (lnk, rc, ok, out.strip()[:120]))
        return ok
    except Exception as exc:
        log("shortcut failed: %r" % exc)
        return False


# --------------------------------------------------------------------------- install
MAX_UNPACK_BYTES = 300 * 1024 * 1024


def _copy_limited(src, out, limit: int) -> int:
    done = 0
    while True:
        chunk = src.read(262144)
        if not chunk:
            break
        done += len(chunk)
        if done > limit:
            raise RuntimeError("payload exceeds the size limit (possible zip bomb)")
        out.write(chunk)
    return done


def extract(inst: Path, progress=None) -> int:
    payload = find_payload()
    if not payload:
        raise RuntimeError("安装包内缺少程序文件（payload.zip）。请重新下载完整的安装程序。")
    inst.mkdir(parents=True, exist_ok=True)
    root = inst.resolve()
    written, unpacked = 0, 0
    with zipfile.ZipFile(payload) as zf:
        members = zf.infolist()
        total = max(1, len(members))
        for i, m in enumerate(members):
            parts = [p for p in m.filename.replace("\\", "/").split("/") if p not in ("", ".")]
            if parts and parts[0] == "YOLOStudio":
                parts = parts[1:]
            if not parts or any(p == ".." for p in parts):
                continue                      # zip-slip guard (first line)
            rel = Path(*parts)
            dest = (root / rel).resolve()
            # Filtering ".." is not enough on Windows: drive-relative or
            # absolute entries ("C:\\...", "\\\\server\\...") override the
            # left operand and would write outside the install directory.
            if not dest.is_relative_to(root):
                raise RuntimeError("unsafe path in payload: " + m.filename)
            if m.is_dir() or m.filename.endswith("/"):
                dest.mkdir(parents=True, exist_ok=True)
            else:
                limit = MAX_UNPACK_BYTES - unpacked
                if limit <= 0 or m.file_size > limit:
                    raise RuntimeError("payload exceeds the size limit (possible zip bomb)")
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(m) as src, dest.open("wb") as out:
                    unpacked += _copy_limited(src, out, limit)
                written += 1
            if progress:
                progress(i + 1, total, rel.name or rel.parent.name)
    return written


def install(inst: Path, shortcuts: bool = True, progress=None,
            desk: bool = True, menu: bool = True) -> dict:
    log("install start -> %s" % inst)
    if not inst.is_absolute():
        raise RuntimeError("安装目录必须是绝对路径。")
    inst.mkdir(parents=True, exist_ok=True)
    # Stage first, commit second: a failed install must never leave a
    # half-overwritten program directory (the old code wrote in place).
    staging = inst.parent / ("." + APP_ID + "-staging-" + str(os.getpid()))
    try:
        if staging.exists():
            shutil.rmtree(staging)
        count = extract(staging, progress)
        # The launcher is an independent EXE shipped inside payload.zip. The
        # installer no longer clones itself to impersonate the app launcher.
        staged_launcher = staging / EXE_NAME
        if not staged_launcher.is_file():
            raise RuntimeError("安装包缺少启动器（YOLO工坊.exe），请重新下载完整的安装程序。")
        # Keep a private copy of this EXE in the install dir as the uninstaller,
        # so Control Panel / Settings can remove the app even if the setup file
        # the user downloaded is later deleted.
        staged_uninstaller = staging / UNINSTALL_EXE
        if SELF.resolve() != staged_uninstaller.resolve():
            shutil.copy2(SELF, staged_uninstaller)
        for src in staging.iterdir():
            dst = inst / src.name
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    launcher = inst / EXE_NAME
    uninstaller = inst / UNINSTALL_EXE
    log("installed launcher -> %s ; uninstaller -> %s" % (launcher, uninstaller))

    info = {"app": APP_NAME, "version": VERSION, "install_dir": str(inst),
            "installed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "files": count, "shortcuts": []}
    if shortcuts and IS_WIN:
        desktop, programs = shell_folders()
        folder = programs / APP_NAME
        desc = "YOLO 工坊 · 标注 / 训练 / 推理"
        targets = []
        if desk:
            targets.append((desktop / SHORTCUT_NAME, launcher, "", desc))
        if menu:
            targets.append((folder / SHORTCUT_NAME, launcher, "", desc))
            targets.append((folder / UNINSTALL_NAME, uninstaller, "--uninstall", "卸载 YOLO 工坊"))
        for lnk, exe, args, desc in targets:
            if make_shortcut(lnk, exe, inst, args, desc):
                info["shortcuts"].append(str(lnk))
    (inst / INFO_NAME).write_text(json.dumps(info, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
    reg_write(inst)
    log("install done: %d files, %d shortcuts" % (count, len(info["shortcuts"])))
    return info


# --------------------------------------------------------------------------- uninstall
def finish_uninstall(inst: Path, keep_data: bool, silent: bool) -> int:
    """Runs from a temp copy so the installed EXE is no longer locked."""
    info_file = inst / INFO_NAME if inst else None
    if info_file and info_file.is_file():
        try:
            info = json.loads(info_file.read_text(encoding="utf-8"))
            shortcuts = info.get("shortcuts") or []
            allowed_roots = []
            try:
                allowed_roots = [p.resolve() for p in shell_folders()]
            except Exception:
                pass
            for lnk in shortcuts:
                p = Path(lnk)
                # The info file is user-writable; never let it point unlink()
                # at an arbitrary file on the machine.
                if p.suffix.lower() != ".lnk":
                    log("skip non-shortcut path %s" % lnk)
                    continue
                if allowed_roots and not any(p.resolve().is_relative_to(r)
                                             for r in allowed_roots):
                    log("skip shortcut outside shell folders %s" % lnk)
                    continue
                try:
                    p.unlink(missing_ok=True)
                    log("removed shortcut %s" % lnk)
                except Exception as exc:
                    log("shortcut remove failed %r" % exc)
            if shortcuts:
                folder = Path(shortcuts[-1]).parent
                if folder.is_dir():
                    try:
                        folder.rmdir()
                    except Exception:
                        pass
        except Exception as exc:
            log("info read failed %r" % exc)
    reg_delete()

    if inst and inst.is_dir():
        # Guard against a corrupted/edited install path: rmtree() is not undoable.
        if not ((inst / "bootstrap.py").is_file() or (inst / INFO_NAME).is_file()):
            log("refusing to delete %s: not a YOLO Studio install" % inst)
            return 1
        if keep_data:
            data = inst / "workspace"
            if data.is_dir():
                backup = inst.parent / ("%s-用户数据" % APP_ID)
                n = 1
                while backup.exists():
                    backup = inst.parent / ("%s-用户数据-%d" % (APP_ID, n))
                    n += 1
                try:
                    shutil.move(str(data), str(backup))
                    log("user data kept at %s" % backup)
                except Exception as exc:
                    log("data move failed %r" % exc)
        for attempt in range(4):
            try:
                shutil.rmtree(inst)
                log("removed install dir %s" % inst)
                break
            except Exception as exc:
                log("rmtree attempt %d: %r" % (attempt, exc))
                time.sleep(0.8)
    if not silent:
        alert("YOLO 工坊已卸载。\n" + ("数据集、模型与训练结果已保留在用户数据目录。"
                                    if keep_data else "程序文件已全部删除。"))
    return 0


def uninstall(opts: dict, argv: list[str]) -> int:
    inst = Path(opts["target"]) if opts["target"] else None
    if inst is None:
        inst = HERE if (HERE / "bootstrap.py").is_file() else reg_read()
    if not inst:
        alert("没有找到已安装的 YOLO 工坊。", kind="error")
        return 1
    log("uninstall target=%s from_temp=%s" % (inst, opts["from_temp"]))

    keep = opts["keep_data"]
    if not opts["silent"] and not opts["from_temp"]:
        keep = alert("是否保留数据集、模型与训练结果？\n\n"
                     "选「是」会保留 workspace 数据目录，选「否」会一并删除。",
                     title="卸载 YOLO 工坊", kind="yesno")

    if opts["from_temp"] or SELF.resolve().parent != inst.resolve():
        return finish_uninstall(inst, keep, opts["silent"])

    # Running inside the install dir: relaunch from a temp copy so the EXE is unlocked.
    try:
        tmp = Path(tempfile.gettempdir()) / "YOLOStudioUninstall.exe"
        shutil.copy2(SELF, tmp)
        child = [str(tmp), "--uninstall", "--from-temp", "--target=%s" % inst]
        if keep:
            child.append("--keep-data")
        if opts["silent"]:
            child.append("--silent")
        subprocess.Popen(child, cwd=str(tmp.parent),
                         creationflags=NO_WINDOW | DETACHED)
        log("relaunched uninstaller from %s" % tmp)
        return 0
    except Exception as exc:
        log("temp relaunch failed %r" % exc)
        return finish_uninstall(inst, keep, opts["silent"])


# --------------------------------------------------------------------------- GUI wizard
def gui_install() -> int:
    log("gui: importing tkinter")
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    log("gui: tkinter imported, creating Tk root")

    BG = "#f7f9fc"
    BLUE = "#2563eb"
    DARK = "#0f172a"
    MUTED = "#64748b"
    F = ("Microsoft YaHei", 10)
    FB = ("Microsoft YaHei", 10, "bold")
    FT = ("Microsoft YaHei", 20, "bold")

    close_splash()                 # Tcl/Tk conflict: splash must die before Tk root
    root = tk.Tk()
    log("gui: Tk root created")
    try:
        import faulthandler
        faulthandler.cancel_dump_traceback_later()
    except Exception:
        pass
    root.title("%s 安装程序" % APP_NAME)
    root.geometry("640x450")
    root.resizable(False, False)
    root.configure(bg=BG)
    try:
        root.iconbitmap(str(BUNDLE / "app.ico"))
    except Exception:
        pass

    page1 = tk.Frame(root, bg=BG)
    page2 = tk.Frame(root, bg=BG)

    # ---- page 1 : welcome + options
    head = tk.Frame(page1, bg=BLUE, height=104)
    head.pack(fill="x")
    head.pack_propagate(False)
    tk.Label(head, text="YOLO 工坊", font=FT, fg="white", bg=BLUE).pack(anchor="w", padx=26, pady=(20, 0))
    tk.Label(head, text="数据集标注 · 模型训练 · 推理验证  一体化工作台",
             font=F, fg="#dbeafe", bg=BLUE).pack(anchor="w", padx=26, pady=(2, 0))

    body = tk.Frame(page1, bg=BG)
    body.pack(fill="both", expand=True, padx=26, pady=18)
    tk.Label(body, text="欢迎安装", font=FB, fg=DARK, bg=BG).pack(anchor="w")
    tk.Label(body, justify="left", anchor="w", wraplength=580, fg=MUTED, bg=BG, font=F,
             text="安装程序会把软件放到你选择的目录，并创建桌面与开始菜单快捷方式。\n"
                  "安装完成后即可打开软件；训练等功能所需的运行环境可在软件内按需安装。"
             ).pack(anchor="w", pady=(6, 14))

    tk.Label(body, text="安装位置", font=FB, fg=DARK, bg=BG).pack(anchor="w")
    row = tk.Frame(body, bg=BG)
    row.pack(fill="x", pady=(5, 12))
    dir_var = tk.StringVar(value=str(default_dir()))
    ent = ttk.Entry(row, textvariable=dir_var, font=F)
    ent.pack(side="left", fill="x", expand=True, ipady=4)

    def browse():
        picked = filedialog.askdirectory(title="选择安装目录", initialdir=str(default_dir().parent))
        if picked:
            dir_var.set(str(Path(picked) / APP_ID))

    ttk.Button(row, text="浏览…", command=browse, width=9).pack(side="left", padx=(8, 0))

    desk_var = tk.BooleanVar(value=True)
    menu_var = tk.BooleanVar(value=True)
    tk.Checkbutton(body, text="创建桌面快捷方式", variable=desk_var, font=F,
                   fg=DARK, bg=BG, activebackground=BG, selectcolor="white",
                   bd=0, highlightthickness=0).pack(anchor="w")
    tk.Checkbutton(body, text="创建开始菜单快捷方式（含卸载入口）", variable=menu_var, font=F,
                   fg=DARK, bg=BG, activebackground=BG, selectcolor="white",
                   bd=0, highlightthickness=0).pack(anchor="w", pady=(4, 0))

    tk.Label(body, text="程序文件约 1 MB；运行环境（约 6-8 GB）可在软件内按需下载安装。",
             fg=MUTED, bg=BG, font=("Microsoft YaHei", 9)).pack(anchor="w", pady=(14, 0))

    foot = tk.Frame(page1, bg=BG)
    foot.pack(fill="x", padx=26, pady=(0, 18))
    ttk.Button(foot, text="取消", command=root.destroy, width=10).pack(side="right")

    def start_install():
        raw = dir_var.get().strip()
        if not raw:
            messagebox.showwarning(APP_NAME, "请选择安装位置。", parent=root)
            return
        target = Path(raw)
        opts["desk"] = bool(desk_var.get())
        opts["menu"] = bool(menu_var.get())
        opts["no_shortcuts"] = not (opts["desk"] or opts["menu"])
        opts["target_dir"] = target
        try:
            free_gb = shutil.disk_usage(target.anchor).free / 2 ** 30
            if free_gb < 8 and not messagebox.askyesno(
                    APP_NAME,
                    "目标磁盘剩余 %.1f GB，后续按需安装运行环境通常需要 6-8 GB。仍要继续安装吗？" % free_gb,
                    parent=root):
                return
        except Exception:
            pass
        page1.pack_forget()
        page2.pack(fill="both", expand=True)
        run_worker()

    ttk.Button(foot, text="立即安装", command=start_install, width=14).pack(side="right", padx=(0, 10))

    # ---- page 2 : progress + done
    tk.Label(page2, text="正在安装", font=FT, fg=DARK, bg=BG).pack(anchor="w", padx=26, pady=(30, 4))
    status = tk.Label(page2, text="准备中…", font=F, fg=MUTED, bg=BG, anchor="w")
    status.pack(anchor="w", padx=26)
    bar = ttk.Progressbar(page2, orient="horizontal", mode="determinate", length=588)
    bar.pack(padx=26, pady=(14, 8))
    detail = tk.Label(page2, text="", font=("Microsoft YaHei", 9), fg=MUTED, bg=BG, anchor="w")
    detail.pack(anchor="w", padx=26)
    done_frame = tk.Frame(page2, bg=BG)

    launch_var = tk.BooleanVar(value=True)

    def show_done(info: dict):
        status.configure(text="安装完成", fg=BLUE, font=FB)
        bar["value"] = 100
        detail.configure(text="已安装 %d 个文件 · %d 个快捷方式" %
                              (info.get("files", 0), len(info.get("shortcuts", []))))
        tk.Label(done_frame, justify="left", anchor="w", wraplength=580,
                 fg=DARK, bg=BG, font=F,
                 text="安装目录：%s\n\n以后双击桌面「YOLO 工坊」即可启动；"
                      "也可在「设置 → 应用 → 已安装的应用」中卸载。" % info["install_dir"]
                 ).pack(anchor="w", pady=(16, 10))
        tk.Checkbutton(done_frame, text="立即启动 YOLO 工坊",
                       variable=launch_var, font=F, fg=DARK, bg=BG, activebackground=BG,
                       selectcolor="white", bd=0, highlightthickness=0).pack(anchor="w")

        def finish():
            if launch_var.get():
                try:
                    subprocess.Popen([str(Path(info["install_dir"]) / EXE_NAME)],
                                     cwd=info["install_dir"], creationflags=NO_WINDOW | DETACHED)
                except Exception as exc:
                    log("post-install launch failed %r" % exc)
            root.destroy()

        ttk.Button(done_frame, text="完成", command=finish, width=14).pack(anchor="e", pady=(16, 0))
        done_frame.pack(fill="both", expand=True, padx=26)

    q_: queue.Queue = queue.Queue()
    opts: dict = {"no_shortcuts": False, "target_dir": default_dir(),
                  "desk": True, "menu": True}

    def run_worker():
        target = opts["target_dir"]
        shortcuts = not opts["no_shortcuts"]
        desk = bool(opts.get("desk", True))
        menu = bool(opts.get("menu", True))

        def work():
            try:
                info = install(target, shortcuts,
                               progress=lambda cur, tot, name: q_.put(("p", cur, tot, name)),
                               desk=desk, menu=menu)
                q_.put(("ok", info))
            except Exception as exc:
                log("install error %r" % exc)
                q_.put(("err", str(exc)))

        threading.Thread(target=work, daemon=True).start()

        def poll():
            try:
                while True:
                    item = q_.get_nowait()
                    if item[0] == "p":
                        cur, tot, name = item[1], item[2], item[3]
                        bar["value"] = min(96, int(cur * 96 / max(1, tot)))
                        status.configure(text="正在释放程序文件…")
                        detail.configure(text=name)
                    elif item[0] == "ok":
                        show_done(item[1])
                        return
                    elif item[0] == "err":
                        status.configure(text="安装失败", fg="#dc2626")
                        detail.configure(text=item[1])
                        ttk.Button(page2, text="关闭", command=root.destroy, width=10).pack(pady=20)
                        return
            except queue.Empty:
                pass
            root.after(60, poll)

        poll()

    page1.pack(fill="both", expand=True)
    root.mainloop()
    return 0


# --------------------------------------------------------------------------- entry
def main() -> int:
    argv = sys.argv[1:]
    opts = parse_args(argv)
    log("argv=%r self=%s bundle=%s" % (argv, SELF, BUNDLE))

    # The uninstaller is a self-copy of this EXE named 卸载YOLO工坊.exe.
    # Double-clicking that file passes NO arguments, so without this it would
    # open the *install* wizard instead of uninstalling. Recognise it by name
    # and treat it as an uninstall request. Explicit --install still wins, and
    # --uninstall from a shortcut / Control Panel is unchanged.
    is_uninstaller_copy = SELF.name == UNINSTALL_EXE or (
        SELF.stem.startswith("卸载") and not opts["install"])
    if is_uninstaller_copy and not opts["uninstall"] and not opts["install"]:
        log("uninstaller copy launched without args -> treating as uninstall")
        opts["uninstall"] = True

    if opts["uninstall"]:
        close_splash()
        return uninstall(opts, argv)

    if not single_instance():
        close_splash()
        return 0

    # Installer is install/uninstall only. Launching the app is the job of the
    # independent YOLO工坊.exe inside the install dir - never this EXE.
    if opts["silent"] or opts["install"]:
        close_splash()
        target = Path(opts["dir"]) if opts["dir"] else default_dir()
        try:
            info = install(target, not opts["no_shortcuts"])
            log("silent install ok -> %s" % info["install_dir"])
            if opts["launch"]:
                subprocess.Popen([str(target / EXE_NAME)], cwd=str(target),
                                 creationflags=NO_WINDOW | DETACHED)
            return 0
        except Exception as exc:
            log("silent install failed: %r" % exc)
            return 1

    # A windowed EXE has no console: if the GUI ever hangs, dump the Python
    # stack so the failure is diagnosable instead of looking like "nothing happened".
    start_watchdog()
    log("main: entering GUI wizard")
    return gui_install()


_WATCHDOG_FILE = None


def start_watchdog(seconds: int = 8) -> None:
    global _WATCHDOG_FILE
    try:
        import faulthandler
        _WATCHDOG_FILE = (Path(tempfile.gettempdir()) / "YOLOStudio_stack.log").open(
            "w", encoding="utf-8", errors="replace")
        faulthandler.dump_traceback_later(seconds, file=_WATCHDOG_FILE, exit=False)
    except Exception as exc:
        log("watchdog unavailable: %r" % exc)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:                # windowed build has no console
        log("FATAL %r" % exc)
        native_alert("发生错误：%s\n\n日志：%s" %
                     (exc, Path(tempfile.gettempdir()) / "YOLOStudio_setup.log"),
                     timeout_ms=0)
        sys.exit(1)
