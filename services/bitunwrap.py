# services/bitunwrap.py
# -*- coding: utf-8 -*-
"""
Portable BitUnwrap service:
- No absolute paths or MinGW hard-coding.
- DLL is loaded from the app's DLL directory (paths.dir_dll()).
- Public API intentionally unchanged:
    * BitUnwrapService
    * bitunwrap_file(...)
"""

from __future__ import annotations
import os
import ctypes
from ctypes import c_char_p, c_uint64, c_int
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# ---- Resolve DLL directory via paths.py (preferred), fallback to this file dir ----
try:
    import paths  # project-level helper
    DLL_DIR = paths.dir_dll()
    # Ensure current process can load DLLs from DLL_DIR (and any extra runtime dirs)
    if hasattr(os, "add_dll_directory"):
        paths.prepare_dll_search_path()
except Exception:
    DLL_DIR = Path(__file__).resolve().parent
    if hasattr(os, "add_dll_directory"):
        try:
            os.add_dll_directory(str(DLL_DIR))
        except Exception:
            pass

DLL_PATH = DLL_DIR / "bitunwrap.dll"

# --- Default flags (kept consistent with bitwrap.py) ---
FIXED_DE_BRUIJN_SEQ = (
    "0000000100100111011100011000110101101100011110001111111001011010011011001010001011000101010010111111110101001110011101110000001"
)
DEFAULT_START_FLAG = FIXED_DE_BRUIJN_SEQ
DEFAULT_END_FLAG   = FIXED_DE_BRUIJN_SEQ[::-1]

# --- Load DLL and define function signatures ---
if not DLL_PATH.exists():
    raise FileNotFoundError(f"bitunwrap.dll not found at: {DLL_PATH}")

try:
    _LIB = ctypes.CDLL(str(DLL_PATH))
except OSError as e:
    raise RuntimeError(
        f"Failed to load bitunwrap.dll ({DLL_PATH}). "
        "Ensure required runtime DLLs (e.g., libstdc++-6.dll, libgcc_s_seh-1.dll, "
        "libwinpthread-1.dll) are present alongside bitunwrap.dll."
    ) from e

_unwrap = _LIB.unwrap_file_bits
_unwrap.argtypes = [c_char_p, c_char_p, c_char_p, c_char_p]
_unwrap.restype  = c_int

_get_start = _LIB.get_last_start_flag_pos
_get_start.argtypes = []
_get_start.restype  = c_uint64

_get_end = _LIB.get_last_end_flag_pos
_get_end.argtypes = []
_get_end.restype  = c_uint64


# --- Data classes / Errors ---
class BitUnwrapError(RuntimeError):
    def __init__(self, code: int, message: str):
        super().__init__(f"[{code}] {message}")
        self.code = code

@dataclass
class BitUnwrapResult:
    ok: bool
    code: int
    in_path: Path
    out_path: Path
    start_flag_pos: Optional[int] = None  # bit index (C++ side counts +1)
    end_flag_pos: Optional[int]   = None
    msg: str = ""


# --- Helpers ---
def _to_cpath(p: Path) -> bytes:
    return os.fsencode(str(p))


def _err_message(code: int) -> str:
    return {
        0:   "OK",
        -1:  "Input file could not be opened",
        -2:  "Output file could not be opened",
        -3:  "Invalid/empty start/end flag bit strings",
        -4:  "Start or end flag not found",
        -99: "Unexpected exception",
    }.get(code, "Unknown error")


# --- Service class ---
class BitUnwrapService:
    def __init__(self, start_flag_bits: str = DEFAULT_START_FLAG, end_flag_bits: str = DEFAULT_END_FLAG):
        if not start_flag_bits or not end_flag_bits:
            raise ValueError("start_flag_bits and end_flag_bits cannot be empty.")
        # Simple validation: only '0'/'1'
        for name, s in (("start_flag_bits", start_flag_bits), ("end_flag_bits", end_flag_bits)):
            if any(c not in ("0", "1") for c in s):
                raise ValueError(f"{name} contains invalid characters (only '0'/'1' allowed).")
        self.start_flag_bits = start_flag_bits
        self.end_flag_bits   = end_flag_bits

    def unwrap_file(self, in_path: str | Path, out_path: str | Path) -> BitUnwrapResult:
        in_p  = Path(in_path).resolve()
        out_p = Path(out_path).resolve()
        out_p.parent.mkdir(parents=True, exist_ok=True)

        ret = _unwrap(
            _to_cpath(in_p),
            _to_cpath(out_p),
            self.start_flag_bits.encode("utf-8"),
            self.end_flag_bits.encode("utf-8"),
        )

        # C++ side sets global counters; read after call
        start_pos = int(_get_start())
        end_pos   = int(_get_end())

        ok  = (ret == 0)
        msg = _err_message(ret)

        result = BitUnwrapResult(
            ok=ok,
            code=ret,
            in_path=in_p,
            out_path=out_p,
            start_flag_pos=start_pos if start_pos > 0 else None,
            end_flag_pos=end_pos if end_pos > 0 else None,
            msg=msg,
        )

        if not ok:
            raise BitUnwrapError(ret, msg)

        return result

    # RX progress rule is project-specific: Bitunwrap 50% + Decode 50%.
    # This service intentionally does not report byte-level progress (no native progress in DLL).
    # Controller side should interpret completion of this call as "Bitunwrap 50% → 100%".
    def unwrap_with_progress(self, in_path: str | Path, out_path: str | Path, progress_cb=None) -> BitUnwrapResult:
        if progress_cb:
            try:
                progress_cb(0.0)
            except Exception:
                pass

        res = self.unwrap_file(in_path, out_path)

        if progress_cb:
            try:
                progress_cb(1.0)  # done
            except Exception:
                pass

        return res


# --- Functional shortcut ---
def bitunwrap_file(in_path: str | Path,
                   out_path: str | Path,
                   start_flag_bits: str = DEFAULT_START_FLAG,
                   end_flag_bits: str   = DEFAULT_END_FLAG) -> BitUnwrapResult:
    svc = BitUnwrapService(start_flag_bits=start_flag_bits, end_flag_bits=end_flag_bits)
    return svc.unwrap_file(in_path, out_path)


# --- CLI usage (optional) ---
if __name__ == "__main__":
    import argparse, sys
    ap = argparse.ArgumentParser(description="BitUnwrap CLI")
    ap.add_argument("--in",  dest="in_path",  required=True, help="Input file (e.g. out.bitwrap)")
    ap.add_argument("--out", dest="out_path", required=True, help="Output file (e.g. out.unwrapped)")
    ap.add_argument("--start", dest="start_bits", default=DEFAULT_START_FLAG, help="Start flag bit string (0/1)")
    ap.add_argument("--end",   dest="end_bits",   default=DEFAULT_END_FLAG,   help="End flag bit string (0/1)")
    args = ap.parse_args()

    try:
        r = bitunwrap_file(args.in_path, args.out_path, args.start_bits, args.end_bits)
        print(f"[BITUNWRAP] OK -> {r.out_path}")
        if r.start_flag_pos is not None or r.end_flag_pos is not None:
            print(f"[BITUNWRAP] start_pos={r.start_flag_pos}  end_pos={r.end_flag_pos}")
        sys.exit(0)
    except BitUnwrapError as e:
        print(f"[BITUNWRAP][ERROR] {e}")
        sys.exit(2)
    except Exception as e:
        print(f"[BITUNWRAP][ERROR] {e}")
        sys.exit(3)
