# -*- coding: utf-8 -*-
"""
paths.py
- Centralized, installer-friendly path helpers.
- No hard-coded absolute paths.
- Works both from source and after packaging/installation.
"""

import os
import sys
from pathlib import Path
from typing import Iterable, Optional, Dict


# --- App root ---------------------------------------------------------------

def _installed_app_root() -> Path:
    """When launched from a packaged EXE/launcher, infer app root from argv[0]."""
    return Path(sys.argv[0]).resolve().parent

def _source_app_root() -> Path:
    """When running from source, infer app root from this file location."""
    return Path(__file__).resolve().parent

def app_root() -> Path:
    """Application root directory (works in both installed and source modes)."""
    root = _installed_app_root()
    return root if root.exists() else _source_app_root()


# --- Project directories -----------------------------------------------------

def dir_scripts() -> Path:
    return app_root() / "scripts"

def dir_services() -> Path:
    return app_root() / "services"

def dir_assets() -> Path:
    # If you later rename 'doc' to 'assets', change here in one place.
    return app_root() / "doc"

def dir_dll() -> Path:
    # Place app-bundled DLLs here (bitwrap/bitunwrap and their runtimes).
    return app_root() / "services"

def _user_base() -> Path:
    """Writable base for logs/outputs (no admin rights required)."""
    return Path(os.getenv("APPDATA", str(app_root()))) / "Hub10"

def dir_logs() -> Path:
    p = _user_base() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p

def dir_out() -> Path:
    p = _user_base() / "out"
    p.mkdir(parents=True, exist_ok=True)
    return p


# --- Portable Python (portable Radioconda) ----------------------------------

def portable_python_root() -> Path:
    """Root of the portable environment placed under {app}\\python."""
    return app_root() / "python"

def portable_python_exe() -> Path:
    """Return {app}\\python\\python.exe if it exists, else empty Path()."""
    cand = portable_python_root() / "python.exe"
    return cand if cand.exists() else Path()

def system_python_candidates() -> Iterable[Path]:
    """Yield likely system Python locations (including Radioconda), if any."""
    # Explicit hint (env)
    hint = os.getenv("HUB10_PYTHON")
    if hint:
        p = Path(hint)
        if p.exists():
            yield p

    # Conda/Radioconda defaults
    conda_prefix = os.getenv("CONDA_PREFIX", "")
    if conda_prefix:
        p = Path(conda_prefix) / "python.exe"
        if p.exists():
            yield p

    user_rc = Path(os.getenv("USERPROFILE", "")) / "radioconda" / "python.exe"
    if user_rc.exists():
        yield user_rc

    # PATH lookup
    for d in os.getenv("PATH", "").split(os.pathsep):
        p = Path(d) / "python.exe"
        if p.exists():
            yield p

def resolve_python_exe() -> Path:
    """
    Preferred Python interpreter:
    1) Portable env under {app}\\python\\python.exe
    2) First system candidate (Radioconda, PATH, etc.)
    3) Fallback 'python' (let the OS PATH resolve it)
    """
    p = portable_python_exe()
    if p:
        return p
    for cand in system_python_candidates():
        return cand
    return Path("python")


# --- DLL search configuration ----------------------------------------------

def prepare_dll_search_path() -> None:
    """
    Add the app-bundled DLL directory to the current process DLL search path.
    Use this early in the main process (e.g., at app startup).
    """
    if hasattr(os, "add_dll_directory"):
        try:
            os.add_dll_directory(str(dir_dll()))
        except Exception:
            # Older Python/Non-Windows: ignore gracefully.
            pass

def portable_library_bin() -> Path:
    """
    Return {app}\\python\\Library\\bin (holds libiio/libusb/etc. in portable env).
    Useful to extend PATH for subprocesses (TX/RX runners).
    """
    return portable_python_root() / "Library" / "bin"

def subprocess_env_with_portable_paths(base_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """
    Return an environment dict that includes portable Library\\bin in PATH.
    Use this dict in subprocess.Popen(..., env=...) so child processes
    can locate libiio/libusb/libad9361, etc.
    """
    env = dict(base_env) if base_env is not None else os.environ.copy()
    libbin = portable_library_bin()
    if libbin.exists():
        env["PATH"] = str(libbin) + os.pathsep + env.get("PATH", "")
    return env
