# -*- coding: utf-8 -*-
"""
services/bitwrap.py

Portable Bitwrap service:
- No absolute paths or MinGW hard-coding.
- DLL is loaded from the app's DLL directory (paths.dir_dll()).
- Public API intentionally unchanged:
    * wrap_file(...)
    * wrap_with_progress(...)
    * compute_bitwrap_load(theta)
"""

import os
import ctypes
from ctypes import c_char_p, c_double, c_uint32
from pathlib import Path
from typing import Optional, Callable

# ---- Resolve DLL directory via paths.py (preferred), fallback to this file dir ----
try:
    import paths  # project-level helper
    DLL_DIR = paths.dir_dll()
    # Ensure current process can load DLLs from DLL_DIR
    if hasattr(os, "add_dll_directory"):
        paths.prepare_dll_search_path()
except Exception:
    DLL_DIR = Path(__file__).resolve().parent
    if hasattr(os, "add_dll_directory"):
        try:
            os.add_dll_directory(str(DLL_DIR))
        except Exception:
            pass

DLL_PATH = DLL_DIR / "bitwrap.dll"

# Defaults
FIXED_DE_BRUIJN_SEQ = (
    "0000000100100111011100011000110101101100011110001111111001011010011011001010001011000101010010111111110101001110011101110000001"
)
DEFAULT_START_FLAG = FIXED_DE_BRUIJN_SEQ
DEFAULT_END_FLAG   = FIXED_DE_BRUIJN_SEQ[::-1]
DEFAULT_THETA      = 4.0
DEFAULT_RNG_SEED   = 0

# ---- Load DLL ----
if not DLL_PATH.exists():
    raise FileNotFoundError(f"bitwrap.dll not found at: {DLL_PATH}")

try:
    LIB = ctypes.CDLL(str(DLL_PATH))
except OSError as e:
    raise RuntimeError(
        f"Failed to load bitwrap.dll ({DLL_PATH}). "
        "Ensure required runtime DLLs (libstdc++-6.dll, libgcc_s_seh-1.dll, "
        "libwinpthread-1.dll) are present alongside bitwrap.dll."
    ) from e

# Function signature
_wrap_fn = LIB.wrap_file_bits_ratio
_wrap_fn.argtypes = [c_char_p, c_char_p, c_char_p, c_char_p, c_double, c_uint32]
_wrap_fn.restype  = ctypes.c_int


# ---- Service (API kept identical) ----
class BitwrapService:
    def __init__(self):
        pass

    def wrap_file(
        self,
        in_path: str,
        out_path: str,
        theta: float = DEFAULT_THETA,
        start_flag: str = DEFAULT_START_FLAG,
        end_flag: str = DEFAULT_END_FLAG,
        rng_seed: int = DEFAULT_RNG_SEED,
    ) -> None:
        if theta <= 0.0:
            raise ValueError("theta must be > 0")

        in_b    = str(in_path).encode("utf-8")
        out_b   = str(out_path).encode("utf-8")
        start_b = start_flag.encode("utf-8")
        end_b   = end_flag.encode("utf-8")
        theta_c = c_double(theta)
        seed_c  = c_uint32(int(rng_seed))

        rc = _wrap_fn(in_b, out_b, start_b, end_b, theta_c, seed_c)
        if rc != 0:
            raise RuntimeError(f"Bitwrap DLL process failed (code={rc})")

    def wrap_with_progress(
        self,
        in_path: str,
        out_path: str,
        theta: float,
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        total = os.path.getsize(in_path)
        self.wrap_file(in_path, out_path, theta)
        if progress_cb:
            progress_cb(total, total)

    def compute_bitwrap_load(self, theta: float) -> int:
        if theta <= 0.0:
            return 0
        # Overhead = 1/theta → percent
        return int((1.0 / float(theta)) * 100.0)
