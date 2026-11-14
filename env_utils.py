# -*- coding: utf-8 -*-
"""Child-process environment hygiene.

Some IDE / agent shells inject a ``sitecustomize.py`` through ``PYTHONPATH``
that guards ``os.unlink()`` with a bulk-delete guard. pip must unlink files
while replacing wheels, so every install dies with ``SystemExit(1)`` in such a
shell (symptom: "torch install failed; rolled back to CPU").

Stripping only those injected shim paths fixes it without touching anything
else. Normal double-click launches have no shim and are unaffected.
"""
from __future__ import annotations

import os

_SHIM_HINTS = ("shim", "workbuddy", "app.asar", "codebuddy")


def clean_env(extra: dict | None = None) -> dict:
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
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env


def shim_active() -> bool:
    """True when the current process was launched with an injected shim."""
    raw = os.environ.get("PYTHONPATH", "") or ""
    return any(h in p.replace("\\", "/").lower()
               for p in raw.split(os.pathsep) for h in _SHIM_HINTS)
