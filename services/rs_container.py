# -*- coding: utf-8 -*-
"""
services/rs_container.py
- Wraps rs_container.dll for RS encode/decode
- Provides progress callbacks, cancel support, and stats
"""

import ctypes
from pathlib import Path

# ---- DLL via paths.py (portable) ----
DLL_NAME = "rs_container.dll"
try:
    import paths
    DLL_DIR = Path(paths.dir_dll())
except Exception:
    DLL_DIR = Path(__file__).resolve().parent

dll_path = DLL_DIR / DLL_NAME
if not dll_path.exists():
    raise FileNotFoundError(f"'{DLL_NAME}' not found: {dll_path}")

# pad_mode: 0 RAW, 1 ZERO, 2 TEMPORAL
PAD_RAW, PAD_ZERO, PAD_TEMPORAL = 0, 1, 2


class RSStatsV1(ctypes.Structure):
    _fields_ = [
        ("frames_total",        ctypes.c_uint64),
        ("slices_total_est",    ctypes.c_uint64),
        ("slices_ok",           ctypes.c_uint64),
        ("slices_bad",          ctypes.c_uint64),
        ("codewords_total",     ctypes.c_uint64),
        ("symbols_total",       ctypes.c_uint64),
        ("data_symbols_total",  ctypes.c_uint64),
        ("corrected_symbols",   ctypes.c_uint64),
        ("used_erasures_cols",  ctypes.c_uint64),
        ("rs_fail_columns",     ctypes.c_uint64),
        ("pad_mode_used",       ctypes.c_int),
        ("ser_rs",              ctypes.c_double),  # kept at 0.0
        ("ber_est",             ctypes.c_double),
    ]


class RSContainer:
    def __init__(self):
        self._lib = ctypes.CDLL(str(dll_path))

        # ------- ENCODE (pack) -------
        self._rs_pack_ex = getattr(self._lib, "rs_pack_container_ex", None)
        if self._rs_pack_ex is None:
            raise RuntimeError("rs_pack_container_ex not found in DLL.")
        self._rs_pack_ex.argtypes = [
            ctypes.c_char_p,  # input
            ctypes.c_char_p,  # output
            ctypes.c_int,     # r
            ctypes.c_int,     # il_depth
            ctypes.c_int,     # slice_bytes
        ]
        self._rs_pack_ex.restype = ctypes.c_int

        # ------- DECODE (unpack) -------
        self._rs_unpack_ex = getattr(self._lib, "rs_unpack_container_ex", None)
        if self._rs_unpack_ex:
            self._rs_unpack_ex.argtypes = [
                ctypes.c_char_p,  # container_path (.rse)
                ctypes.c_char_p,  # output_path   (decoded/original)
                ctypes.c_int,     # pad_mode
            ]
            self._rs_unpack_ex.restype = ctypes.c_int

        self._rs_unpack = getattr(self._lib, "rs_unpack_container", None)
        if self._rs_unpack:
            self._rs_unpack.argtypes = [
                ctypes.c_char_p,  # container_path
                ctypes.c_char_p,  # output_path
            ]
            self._rs_unpack.restype = ctypes.c_int

        if not (self._rs_unpack_ex or self._rs_unpack):
            raise RuntimeError("rs_unpack_container(_ex) not found in DLL.")

        # ------- Progress / Cancel -------
        self._cb_type = ctypes.CFUNCTYPE(None, ctypes.c_uint64, ctypes.c_uint64)
        self._set_cb = getattr(self._lib, "rs_set_progress_cb", None)
        self._cancel = getattr(self._lib, "rs_request_cancel", None)
        if self._set_cb:
            self._cb_ref = self._cb_type(lambda a, b: None)
            self._set_cb(self._cb_ref)

        # ------- Stats / Residual coeff -------
        self._get_stats = getattr(self._lib, "rs_get_stats_v1", None)
        if self._get_stats:
            self._get_stats.argtypes = [ctypes.POINTER(RSStatsV1)]
            self._get_stats.restype = None

        self._set_res_coeff = getattr(self._lib, "rs_set_residual_coeff", None)
        if self._set_res_coeff:
            self._set_res_coeff.argtypes = [ctypes.c_double]
            self._set_res_coeff.restype = None
            # --- Silent default: residual coeff = 0.65 (not exposed in GUI)
            try:
                self.set_residual_coeff(0.65)
            except Exception:
                pass

    # ---------------- ENCODE ----------------
    def encode_file(self, input_path: str, output_path: str,
                    r: int, il_depth: int, slice_bytes: int,
                    progress_cb=None):
        if not Path(input_path).is_file():
            raise FileNotFoundError(f"Input file not found: {input_path}")

        if progress_cb and self._set_cb:
            self._cb_ref = self._cb_type(progress_cb)
            self._set_cb(self._cb_ref)

        if self._cancel:
            self._cancel(0)

        rc = self._rs_pack_ex(
            input_path.encode("utf-8"),
            output_path.encode("utf-8"),
            ctypes.c_int(int(r)),
            ctypes.c_int(int(il_depth)),
            ctypes.c_int(int(slice_bytes)),
        )
        if rc != 0:
            raise RuntimeError(f"Encode failed (rc={rc}).")

    # ---------------- DECODE ----------------
    def decode_file(self, container_path: str, output_path: str,
                    pad_mode: int = PAD_RAW, progress_cb=None):
        """
        .rse → original file
        pad_mode: 0 RAW, 1 ZERO, 2 TEMPORAL
        """
        if not Path(container_path).is_file():
            raise FileNotFoundError(f"Input (RSE) file not found: {container_path}")

        if progress_cb and self._set_cb:
            self._cb_ref = self._cb_type(progress_cb)
            self._set_cb(self._cb_ref)

        if self._cancel:
            self._cancel(0)

        if self._rs_unpack_ex:
            rc = self._rs_unpack_ex(
                container_path.encode("utf-8"),
                output_path.encode("utf-8"),
                ctypes.c_int(int(pad_mode)),
            )
        else:
            rc = self._rs_unpack(
                container_path.encode("utf-8"),
                output_path.encode("utf-8"),
            )

        if rc != 0:
            raise RuntimeError(f"Decode failed (rc={rc}).")

    # ---------------- Stats / Residual coeff ----------------
    def get_stats_v1(self):
        if not self._get_stats:
            return None
        st = RSStatsV1()
        self._get_stats(ctypes.byref(st))
        return {
            "frames_total":       int(st.frames_total),
            "slices_total_est":   int(st.slices_total_est),
            "slices_ok":          int(st.slices_ok),
            "slices_bad":         int(st.slices_bad),
            "codewords_total":    int(st.codewords_total),
            "symbols_total":      int(st.symbols_total),
            "data_symbols_total": int(st.data_symbols_total),
            "corrected_symbols":  int(st.corrected_symbols),
            "used_erasures_cols": int(st.used_erasures_cols),
            "rs_fail_columns":    int(st.rs_fail_columns),
            "pad_mode_used":      int(st.pad_mode_used),
            "ser_rs":             float(st.ser_rs),
            "ber_est":            float(st.ber_est),
        }

    def set_residual_coeff(self, v: float):
        if self._set_res_coeff:
            self._set_res_coeff(float(v))

    def request_cancel(self):
        if self._cancel:
            self._cancel(1)
