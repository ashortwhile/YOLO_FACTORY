# -*- coding: utf-8 -*-
"""YOLO 工坊 —— 单文件 EXE（安装程序 / 软件主程序 / 卸载程序 三合一）。

同一个 EXE 有三种身份：
  1. 放在任意位置、旁边没有 bootstrap.py      -> 图形安装向导
  2. 位于安装目录、旁边有 bootstrap.py         -> 软件主程序（调用 bootstrap.py 自动配环境）
  3. 带 --uninstall 参数                       -> 卸载（控制面板「添加或删除程序」入口）

静默部署参数：
  --silent --dir="D:\\Somewhere" [--no-shortcuts] [--launch]
  --uninstall [--silent] [--keep-data]

设计要点：EXE 里只内嵌纯代码压缩包（不含 venv / 不含本机 config / 不含数据集），
装到任何一台 Windows 机器后首次运行才由 bootstrap.py 自动配置 Python 环境。
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

APP_NAME = "YOLO 工坊"
APP_ID = "YOLOStudio"
VERSION = "1.0.0"
PUBLISHER = "YOLO Studio"
EXE_NAME = "YOLO工坊.exe"
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


def alert(msg: str, title: str = APP_NAME, kind: str = "info") -> bool:
    """Show a native dialog. Returns True for yes / ok."""
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
        return False


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
    exe = inst / EXE_NAME
    k = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY, 0, winreg.KEY_WRITE)
    values = {
        "DisplayName": APP_NAME,
        "DisplayVersion": VERSION,
        "Publisher": PUBLISHER,
        "DisplayIcon": str(exe) + ",0",
        "InstallLocation": str(inst),
        "UninstallString": '"%s" --uninstall' % exe,
        "QuietUninstallString": '"%s" --uninstall --silent --keep-data' % exe,
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
def extract(inst: Path, progress=None) -> int:
    payload = find_payload()
    if not payload:
        raise RuntimeError("安装包内缺少程序文件（payload.zip）。请重新下载完整的安装程序。")
    inst.mkdir(parents=True, exist_ok=True)
    written = 0
    with zipfile.ZipFile(payload) as zf:
        members = zf.infolist()
        total = max(1, len(members))
        for i, m in enumerate(members):
            parts = [p for p in m.filename.replace("\\", "/").split("/") if p not in ("", ".")]
            if parts and parts[0] == "YOLOStudio":
                parts = parts[1:]
            if not parts or any(p == ".." for p in parts):
                continue                      # zip-slip guard
            rel = Path(*parts)
            dest = inst / rel
            if m.is_dir() or m.filename.endswith("/"):
                dest.mkdir(parents=True, exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(m) as src, dest.open("wb") as out:
                    shutil.copyfileobj(src, out, 262144)
                written += 1
            if progress:
                progress(i + 1, total, rel.name or rel.parent.name)
    return written


def install(inst: Path, shortcuts: bool = True, progress=None) -> dict:
    log("install start -> %s" % inst)
    if not inst.is_absolute():
        raise RuntimeError("安装目录必须是绝对路径。")
    inst.mkdir(parents=True, exist_ok=True)
    count = extract(inst, progress)

    dst_exe = inst / EXE_NAME
    if SELF.resolve() != dst_exe.resolve():
        shutil.copy2(SELF, dst_exe)
    log("copied launcher -> %s" % dst_exe)

    info = {"app": APP_NAME, "version": VERSION, "install_dir": str(inst),
            "installed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "files": count, "shortcuts": []}
    if shortcuts and IS_WIN:
        desktop, programs = shell_folders()
        folder = programs / APP_NAME
        targets = [
            (desktop / SHORTCUT_NAME, "", "YOLO 工坊 · 标注 / 训练 / 推理"),
            (folder / SHORTCUT_NAME, "", "YOLO 工坊 · 标注 / 训练 / 推理"),
            (folder / UNINSTALL_NAME, "--uninstall", "卸载 YOLO 工坊"),
        ]
        for lnk, args, desc in targets:
            if make_shortcut(lnk, dst_exe, inst, args, desc):
                info["shortcuts"].append(str(lnk))
    (inst / INFO_NAME).write_text(json.dumps(info, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
    reg_write(inst)
    log("install done: %d files, %d shortcuts" % (count, len(info["shortcuts"])))
    return info


# --------------------------------------------------------------------------- launch
def find_python(root: Path) -> list[str] | None:
    cfg = root / "config.json"
    if cfg.is_file():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            for key in ("venv_python", "python"):
                val = data.get(key)
                if val and Path(val).is_file():
                    return [val]
        except Exception:
            pass
    for name in ("python", "python3"):
        found = shutil.which(name)
        if found:
            return [found]
    if shutil.which("py"):
        return ["py", "-3"]
    return None


def launch() -> int:
    """Software mode: delegate to bootstrap.py which sets up the environment."""
    py = find_python(HERE)
    if not py:
        alert("没有找到 Python。请先安装 Python 3.9~3.12 再启动本软件，"
              "或从开始菜单重新安装。", kind="error")
        return 1
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    logs = HERE / "logs"
    logs.mkdir(exist_ok=True)
    startup = logs / "startup.log"
    log("launch: %s bootstrap.py" % " ".join(py))
    try:
        with startup.open("w", encoding="utf-8", errors="replace") as fh:
            proc = subprocess.Popen(
                py + [str(HERE / "bootstrap.py")], cwd=str(HERE),
                stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                env=env, creationflags=NO_WINDOW)
            code = proc.wait()
        if code:
            tail = "\n".join(startup.read_text(encoding="utf-8", errors="replace")
                             .splitlines()[-25:])
            alert("启动失败（退出码 %s）。\n\n%s\n\n完整日志：%s" % (code, tail, startup),
                  kind="error")
        return code
    except Exception as exc:
        log("launch failed: %r" % exc)
        alert("启动失败：%s" % exc, kind="error")
        return 1


# --------------------------------------------------------------------------- uninstall
def finish_uninstall(inst: Path, keep_data: bool, silent: bool) -> int:
    """Runs from a temp copy so the installed EXE is no longer locked."""
    info_file = inst / INFO_NAME if inst else None
    if info_file and info_file.is_file():
        try:
            info = json.loads(info_file.read_text(encoding="utf-8"))
            for lnk in info.get("shortcuts", []):
                try:
                    Path(lnk).unlink(missing_ok=True)
                    log("removed shortcut %s" % lnk)
                except Exception as exc:
                    log("shortcut remove failed %r" % exc)
            folder = Path(info.get("shortcuts", [None])[-1] or "").parent
            if folder and folder.is_dir():
                try:
                    folder.rmdir()
                except Exception:
                    pass
        except Exception as exc:
            log("info read failed %r" % exc)
    reg_delete()

    if inst and inst.is_dir():
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
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    BG = "#f7f9fc"
    BLUE = "#2563eb"
    DARK = "#0f172a"
    MUTED = "#64748b"
    F = ("Microsoft YaHei", 10)
    FB = ("Microsoft YaHei", 10, "bold")
    FT = ("Microsoft YaHei", 20, "bold")

    root = tk.Tk()
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
                  "首次启动时会自动检测显卡并配置 Python 环境（需联网，约几分钟）。"
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

    tk.Label(body, text="程序文件约 1 MB；环境依赖与模型权重在首次启动时按需下载。",
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
        if not desk_var.get() and not menu_var.get():
            opts["no_shortcuts"] = True
        opts["target_dir"] = target
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
        tk.Checkbutton(done_frame, text="立即启动 YOLO 工坊（首次启动会自动配置环境）",
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
    opts: dict = {"no_shortcuts": False, "target_dir": default_dir()}

    def run_worker():
        target = opts["target_dir"]
        shortcuts = not opts["no_shortcuts"]
        # both checkboxes off -> no shortcuts at all; one on -> create all (harmless extras)

        def work():
            try:
                info = install(target, shortcuts,
                               progress=lambda cur, tot, name: q_.put(("p", cur, tot, name)))
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

    if opts["uninstall"]:
        return uninstall(opts, argv)

    installed_here = ((HERE / "bootstrap.py").is_file()
                      and (HERE / "web" / "index.html").is_file())
    if installed_here and not opts["install"]:
        return launch()                       # software mode

    if opts["silent"] or opts["install"]:
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

    return gui_install()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:                # windowed build has no console
        log("FATAL %r" % exc)
        alert("发生错误：%s\n\n日志：%s" %
              (exc, Path(tempfile.gettempdir()) / "YOLOStudio_setup.log"), kind="error")
        sys.exit(1)
