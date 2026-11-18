# -*- coding: utf-8 -*-
"""Child-process environment hygiene.

Three real-world launch environments break pip / downloads, all fixed here:

1. Some IDE / agent shells inject a ``sitecustomize.py`` through ``PYTHONPATH``
   that guards ``os.unlink()`` with a bulk-delete guard. pip must unlink files
   while replacing wheels, so every install dies with ``SystemExit(1)``.
2. Agent / proxy shells export ``HTTP_PROXY`` / ``HTTPS_PROXY`` pointing at a
   per-session local port. When that tunnel is down or half-configured, pip
   dies with ``ValueError: check_hostname requires server_hostname`` (TLS to
   the proxy) - before even reaching the mirror.
3. ``SSL_CERT_FILE`` / ``REQUESTS_CA_BUNDLE`` may point at files that were
   deleted, breaking TLS verification for pip and urllib.

Downloads in this app (PyPI mirrors, download.pytorch.org, hf-mirror.com) work
without a proxy; when they do not, ``pip_install()`` retries once with the
original proxy settings restored via :func:`proxy_snapshot`.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent

_SHIM_HINTS = ("shim", "workbuddy", "app.asar", "codebuddy")

_PROXY_VARS = ("http_proxy", "https_proxy", "all_proxy", "ftp_proxy", "no_proxy",
               "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY", "NO_PROXY")
_CERT_VARS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")


def proxy_snapshot() -> dict:
    """Original proxy settings of the launching environment (for fallback retry)."""
    return {k: os.environ[k] for k in _PROXY_VARS if os.environ.get(k)}


def clean_env(extra: dict | None = None, keep_proxy: bool = False) -> dict:
    """Return an env copy safe for pip / training / probe subprocesses."""
    env = os.environ.copy()
    raw = env.get("PYTHONPATH", "") or ""
    kept = [p for p in raw.split(os.pathsep)
            if p.strip() and not any(h in p.replace("\\", "/").lower()
                                     for h in _SHIM_HINTS)]
    if kept:
        env["PYTHONPATH"] = os.pathsep.join(kept)
    else:
        env.pop("PYTHONPATH", None)
    # user-site sitecustomize could do the same thing
    env["PYTHONNOUSERSITE"] = "1"
    if keep_proxy:
        # fallback path: make sure nothing bypasses the inherited proxy
        env.pop("NO_PROXY", None)
        env.pop("no_proxy", None)
    else:
        for key in _PROXY_VARS:
            env.pop(key, None)
        # crucial: without this, pip/urllib silently fall back to the Windows
        # registry system proxy (getproxies_registry), which may point at a
        # broken local tunnel -> "check_hostname requires server_hostname".
        # "*" forces direct connections for every host.
        env["NO_PROXY"] = "*"

    # Keep pip's wheel cache and staging temp on the app's own drive. The
    # default (%LOCALAPPDATA%\pip\cache + %TEMP%) sits on C: which is often
    # nearly full - a 764 MB XPU/CUDA wheel then dies with
    # "OSError: [Errno 28] No space left on device" even when the app is
    # installed on a drive with plenty of room.
    try:
        pip_cache = ROOT / "cache" / "pip"
        tmp_dir = ROOT / "cache" / "tmp"
        pip_cache.mkdir(parents=True, exist_ok=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        env["PIP_CACHE_DIR"] = str(pip_cache)
        env["TMP"] = str(tmp_dir)
        env["TEMP"] = str(tmp_dir)
        env["TMPDIR"] = str(tmp_dir)
    except Exception:
        pass   # read-only location: keep system defaults
        # Do NOT force no_proxy here: on the keep_proxy fallback path that
        # would silently disable the inherited proxy we are trying to use.
    for key in _CERT_VARS:
        val = env.get(key)
        if val and not os.path.isfile(val):     # stale cert override breaks TLS
            env.pop(key, None)
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env


def shim_active() -> bool:
    """True when the current process was launched with an injected shim."""
    raw = os.environ.get("PYTHONPATH", "") or ""
    return any(h in p.replace("\\", "/").lower()
               for p in raw.split(os.pathsep) for h in _SHIM_HINTS)
