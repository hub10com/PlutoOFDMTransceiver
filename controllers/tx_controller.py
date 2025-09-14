# -*- coding: utf-8 -*-
"""
controllers/tx_controller.py

Flow: input → RS (encode_file) → Bitwrap (wrap_file) → OFDM TX (subproc | inproc)
- RS/Bitwrap progress: NO DLL callbacks; time/formula‑based
- Total progress: weighted 15/15/70 with smooth animation
- TX: keep the project's “intentionally reversed” direction (mode=="subproc" → start in‑proc)
"""

from pathlib import Path
from typing import Optional
import sys
import math
import time
import threading

from PyQt5.QtCore import QObject, QTimer, pyqtSignal, QCoreApplication

from services.rs_container import RSContainer
from services.bitwrap import (
    BitwrapService,
    DEFAULT_THETA, DEFAULT_START_FLAG, DEFAULT_END_FLAG, DEFAULT_RNG_SEED
)
from services.ofdm_tx_subproc import OfdmTxServiceSubproc, TxConfig as SubprocTxConfig

# Optional portable paths (for python exe / scripts dir defaults)
try:
    import paths  # should provide exe_python(), dir_scripts()
except Exception:
    paths = None  # fallback handled later

# In‑process GNURadio (QThread) – for embedding
_INPROC_IMPORT_ERR = None
TxWorker = None
InprocTxConfig = None
try:
    from services.ofdm_tx_inproc import TxWorker as _TxWorker, TxConfig as _InprocTxConfig
    TxWorker = _TxWorker
    InprocTxConfig = _InprocTxConfig
except Exception as _e:
    _INPROC_IMPORT_ERR = _e


class TxController(QObject):
    # UI signals
    sig_log = pyqtSignal(str)
    sig_rs_progress = pyqtSignal(int)   # 0..100 (we emit internally)
    sig_bw_progress = pyqtSignal(int)   # 0..100
    sig_elapsed = pyqtSignal(str)       # "HH:MM:SS"
    sig_tx_started = pyqtSignal()
    sig_tx_stopped = pyqtSignal()

    # Total progress weights
    _W_RS = 15.0
    _W_BW = 15.0
    _W_TX = 70.0

    # TX estimate slack and start delay
    _TX_EPS = 0.03
    _TX_START_DELAY = 4.0

    # --------- RS/Bitwrap estimator defaults (can be calibrated) ---------
    _FRAME_BYTES = 12288  # RS frame size (B)

    # RS pack throughput / speeds (bytes/s) — tune per machine if needed
    _B_READ   = 800e6
    _B_WRITE  = 700e6
    _B_MEMCPY = 8e9
    _B_CRC16  = 3e9
    _B_CRC32  = 6e9
    _ALPHA_RS = 1.8e-10   # s per (byte * parity_count)
    _C_SLICE  = 6e-6      # s per slice overhead

    # Bitwrap throughput
    _B_COPY_ALIGNED  = 300e6   # aligned file copy speed (B/s)
    _B_COPY_BITWISE  = 40e6    # unaligned (bitwise) effective speed (B/s)
    _B_DUMMY         = 120e6   # RNG + write effective speed (B/s)

    # Smooth animation params
    _ANIM_INTERVAL_MS = 100     # 10 Hz
    _ANIM_ALPHA = 0.25          # approach factor to target (0..1)
    _ANIM_MAX_STEP = 5.0        # at most +/‑5% per tick

    def __init__(self, view=None, mode: str = "subproc"):
        super().__init__(view)
        self.view = None
        # keep the intentional reversal
        self.mode = (mode or "inproc").lower().strip()

        self._rs = RSContainer()
        self._bw = BitwrapService()

        # TX services
        self._tx_subproc = OfdmTxServiceSubproc(on_log=self._on_tx_log)
        self._tx_worker = None  # in‑proc QThread

        # single timer (100 ms — both animation and elapsed counter)
        self._timer = QTimer(self)
        self._timer.setInterval(self._ANIM_INTERVAL_MS)
        self._timer.timeout.connect(self._on_tick)

        # elapsed time counter
        self._elapsed_secs = 0.0

        # raw percentages (model targets)
        self._rs_raw = 0
        self._bw_raw = 0
        self._tx_raw = 0

        # displayed total (smooth)
        self._anim_total = 0.0
        self._target_total = 0.0

        # phase timers
        self._rs_started_mono: Optional[float] = None
        self._rs_est_ms: Optional[int] = None

        self._bw_started_mono: Optional[float] = None
        self._bw_est_ms: Optional[int] = None

        self._tx_started_mono: Optional[float] = None
        self._tx_est_ms: Optional[int] = None

        # output paths
        self._last_in: Optional[Path] = None
        self._last_rs: Optional[Path] = None
        self._last_bw: Optional[Path] = None

        # for the “load” label
        self._last_params = None
        self._load_timer = QTimer(self)
        self._load_timer.setInterval(250)
        self._load_timer.timeout.connect(self._update_load_label_if_changed)

        # Start re‑entrancy / “single run” guard
        self._running = False  # while True, new starts are rejected

        if view is not None:
            self.bind_view(view)

        self._log(f"[CTRL] TxController ready. Mode: {self.mode}")

    # -------- Bind view --------
    def bind_view(self, v):
        self.view = v

        # Controller → View
        self.sig_log.connect(v.append_log)
        self.sig_elapsed.connect(v.set_time)
        self.sig_tx_started.connect(lambda: v.append_log("[CTRL] TX started."))
        self.sig_tx_stopped.connect(lambda: v.append_log("[CTRL] TX stopped."))

        # View → Controller
        v.sig_start.connect(self._on_send_clicked)
        v.sig_send.connect(self._on_send_clicked)  # backward compatibility
        v.sig_stop.connect(self.stop_tx)
        v.sig_clear.connect(self._on_clear_clicked)
        v.sig_back.connect(self._on_back_clicked)
        v.sig_file_selected.connect(self._on_file_selected)

        for sig_name in ["sig_params_changed","sig_theta_changed","sig_rs_changed","sig_rs_r_changed","sig_rs_s_changed"]:
            try:
                getattr(v, sig_name).connect(self._update_load_label)
            except Exception:
                pass

        self._update_load_label()
        self._load_timer.start()

    # -------- helpers --------
    def _log(self, s: str):
        self.sig_log.emit(s)

    def _on_tick(self):
        # elapsed (write to UI roughly once per second)
        self._elapsed_secs += self._ANIM_INTERVAL_MS / 1000.0
        if int(self._elapsed_secs * 10) % 10 == 0:  # ~1s
            hh = int(self._elapsed_secs) // 3600
            mm = (int(self._elapsed_secs) % 3600) // 60
            ss = int(self._elapsed_secs) % 60
            self.sig_elapsed.emit(f"{hh:02d}:{mm:02d}:{ss:02d}")

        # RS time‑based percent
        if self._rs_started_mono is not None and self._rs_est_ms:
            elapsed_ms = (time.monotonic() - self._rs_started_mono) * 1000.0
            self._rs_raw = max(0, min(100, int((elapsed_ms / self._rs_est_ms) * 100)))

        # Bitwrap time‑based percent
        if self._bw_started_mono is not None and self._bw_est_ms:
            elapsed_ms = (time.monotonic() - self._bw_started_mono) * 1000.0
            self._bw_raw = max(0, min(100, int((elapsed_ms / self._bw_est_ms) * 100)))

        # TX time‑based percent
        if self._tx_started_mono is not None and self._tx_est_ms:
            elapsed_ms = (time.monotonic() - self._tx_started_mono) * 1000.0
            self._tx_raw = max(0, min(100, int((elapsed_ms / self._tx_est_ms) * 100)))

        # update target total
        self._target_total = self._compute_weighted_total(self._rs_raw, self._bw_raw, self._tx_raw)

        # smooth animation
        diff = self._target_total - self._anim_total
        step = diff * self._ANIM_ALPHA
        if step > self._ANIM_MAX_STEP:
            step = self._ANIM_MAX_STEP
        elif step < -self._ANIM_MAX_STEP:
            step = -self._ANIM_MAX_STEP
        self._anim_total += step

        total_int = max(0, min(100, int(round(self._anim_total))))
        if hasattr(self.view, "on_total_progress"):
            self.view.on_total_progress(total_int)

        # when reaching 100%, stop the timer
        if total_int >= 100 and self._target_total >= 100:
            self._timer.stop()
            self._log("[CTRL] Progress 100% → timer stopped.")

    def _compute_weighted_total(self, rs_raw: int, bw_raw: int, tx_raw: int) -> float:
        rs_term = (float(rs_raw) / 100.0) * self._W_RS
        bw_term = (float(bw_raw) / 100.0) * self._W_BW
        tx_term = (float(tx_raw) / 100.0) * self._W_TX
        return rs_term + bw_term + tx_term

    def _reset_timer(self):
        self._elapsed_secs = 0.0
        self.sig_elapsed.emit("00:00:00")

    def _on_tx_log(self, line: str):
        self.sig_log.emit(line)

    def _on_clear_clicked(self):
        self._timer.stop()
        self._elapsed_secs = 0.0
        self.sig_elapsed.emit("00:00:00")

        self._rs_raw = self._bw_raw = self._tx_raw = 0
        self._anim_total = 0.0
        self._target_total = 0.0

        self._rs_started_mono = self._bw_started_mono = self._tx_started_mono = None
        self._rs_est_ms = self._bw_est_ms = self._tx_est_ms = None

        if hasattr(self.view, "on_total_progress"):
            self.view.on_total_progress(0)

        try:
            self.view.clear_log()
        except Exception:
            pass

        self._log("[CTRL] Clear: log + timer + progress reset.")

    # -------- Load label --------
    def _compute_load_factor_and_pct(self, r: int, s: int, theta: float):
        try:
            r = int(r)
            s = max(1, int(s))
            theta = float(theta)
        except Exception:
            return (1.0, 0)

        if theta <= 0:
            return (1.0, 0)

        FRAME_BYTES = self._FRAME_BYTES
        # RS payload increase (v4 container): extra_fixed + slice header overhead
        extra_fixed = 64 * r + 2 * (192 + r)                # parity and CRC tables
        total_payload = FRAME_BYTES + extra_fixed
        slices = math.ceil(total_payload / s)
        slice_overhead = 24 * slices                        # per‑slice header
        total_extra = extra_fixed + slice_overhead
        rs_encoded = FRAME_BYTES + total_extra

        # Bitwrap ratio
        bitwrap_ratio = 1.0 + (1.0 / theta)
        bitwrapped_total = rs_encoded * bitwrap_ratio

        total_increase = (bitwrapped_total - FRAME_BYTES) / FRAME_BYTES
        load_percent = int(total_increase * 100)
        factor = 1.0 + total_increase
        return (round(factor, 3), load_percent)

    def _update_load_label(self):
        if not self.view:
            return
        try:
            r = self.view.rs_r()
            s = self.view.rs_s()
            theta = self.view.theta()
        except Exception:
            return
        factor, pct = self._compute_load_factor_and_pct(r, s, theta)
        self.view.set_load_text(f"{factor:.3f}×  (+%{pct})")

    def _update_load_label_if_changed(self):
        if not self.view:
            return
        try:
            r_now = int(self.view.rs_r()); s_now = int(self.view.rs_s()); t_now = float(self.view.theta())
        except Exception:
            return
        key = (r_now, s_now, t_now)
        if key != self._last_params:
            self._last_params = key
            self._update_load_label()

    # -------- View events --------
    def _on_back_clicked(self):
        pass

    def _on_file_selected(self, path: str):
        self._update_load_label()

    def _amp_from_view(self) -> float:
        """Clamp amplitude value from UI into a safe range."""
        try:
            k = float(self.view.amp())
        except Exception:
            k = 0.03
        if k != k:  # NaN
            k = 0.03
        return max(0.0, min(1.0, k))

    def _on_send_clicked(self):
        """Start send pipeline (RS→Bitwrap→TX)."""
        # “single run” guard: while TX is active/Until Stop, ignore new starts
        if self._running:
            self._log("[CTRL] Start ignored: pipeline already running.")
            return
        self._running = True
        try:
            self._reset_timer()
            self._timer.start()

            # clear state
            self._rs_raw = self._bw_raw = self._tx_raw = 0
            self._anim_total = 0.0
            self._target_total = 0.0

            self._rs_started_mono = self._bw_started_mono = self._tx_started_mono = None
            self._rs_est_ms = self._bw_est_ms = self._tx_est_ms = None

            in_path = self.view.input_path()
            if not in_path:
                self._log("[ERR] No file selected.")
                return

            # RS + Bitwrap (progress is time‑based)
            self.run_pipeline(
                input_path=in_path,
                out_dir=str(Path(in_path).parent),
                r=self.view.rs_r(),
                interleave_depth=self.view.rs_d(),
                slice_bytes=self.view.rs_s(),
                theta=self.view.theta(),
            )

            # Load stats label
            self._emit_load_stats(in_path, self.view.theta())
            self._on_file_selected(in_path)

            # amplitude from UI
            amp_ui = self._amp_from_view()

            # TX — YOUR WORKING DIRECTION (mode=="subproc" → start in‑proc)
            if self.mode == "subproc":
                self._start_inproc_tx(
                    input_path=self.last_bitwrap_output(),
                    center=self.view.center_hz(),
                    samp=self.view.samp_rate(),
                    rfbw=self.view.rf_bw(),
                    atten=self.view.atten_db(),
                    buffer=self.view.buffer_size(),
                    amp=amp_ui,
                    pkt=self.view.pkt_size(),
                    roll=0,
                    mod=self.view.modulation(),
                )
            else:
                self._start_subproc_tx(
                    center=self.view.center_hz(),
                    samp=self.view.samp_rate(),
                    rfbw=self.view.rf_bw(),
                    atten=self.view.atten_db(),
                    buffer=self.view.buffer_size(),
                    amp=amp_ui,
                    pkt=self.view.pkt_size(),
                    roll=0,
                    mod=self.view.modulation(),
                )

        except Exception as e:
            self._log(f"[ERR] Error while starting TX: {e}")
        # NOTE: we **do not** release _running here; it will be released when TX stops

    # -------- pipeline (RS→BW) --------
    def run_pipeline(
        self,
        *,
        input_path: str,
        out_dir: Optional[str] = None,
        r: int = 16,
        interleave_depth: int = 32,
        slice_bytes: int = 1024,
        theta: float = DEFAULT_THETA,
        start_flag: str = DEFAULT_START_FLAG,
        end_flag: str = DEFAULT_END_FLAG,
        rng_seed: int = DEFAULT_RNG_SEED,
    ):
        in_p = Path(input_path).resolve()
        if not in_p.exists():
            raise FileNotFoundError(f"Input file not found: {in_p}")

        out_base = Path(out_dir).resolve() if out_dir else in_p.parent
        out_base.mkdir(parents=True, exist_ok=True)
        rs_out = out_base / (in_p.stem + ".rse")
        bw_out = out_base / (in_p.stem + ".bitwrap")

        self._last_in = in_p
        self._last_rs = rs_out
        self._last_bw = bw_out

        self._log(f"[INFO] Pipeline starting (r={r}, D={interleave_depth}, S={slice_bytes}, theta={theta}).")

        # ---------------- RS: formula‑based progress + background encode ----------------
        self._rs_raw = 0
        self._log(f"[RS] Starting → {rs_out}")

        rs_est_s = self._estimate_rs_pack_seconds(in_p.stat().st_size, int(r), int(slice_bytes))
        self._rs_est_ms = int(max(0.001, rs_est_s) * 1000.0)
        self._rs_started_mono = time.monotonic()

        rs_done = {"ok": False, "err": None}
        def _rs_job():
            try:
                def _noop_cb(_a, _b):
                    pass
                self._rs.encode_file(str(in_p), str(rs_out), int(r), int(interleave_depth), int(slice_bytes), _noop_cb)
                rs_done["ok"] = True
            except Exception as e:
                rs_done["err"] = e

        t_rs = threading.Thread(target=_rs_job, daemon=True)
        t_rs.start()
        while t_rs.is_alive():
            QCoreApplication.processEvents()
            time.sleep(0.01)

        self._rs_raw = 100
        self._rs_started_mono = None
        self._log(f"[OK] RS completed → {rs_out}")
        if rs_done["err"]:
            raise rs_done["err"]

        # ---------------- Bitwrap: formula‑based progress + background wrap -------------
        self._bw_raw = 0
        self._log(f"[BITWRAP] Starting → {bw_out}")

        rs_size = rs_out.stat().st_size if rs_out.exists() else in_p.stat().st_size
        start_bits_len = len(start_flag) if start_flag else 0
        end_bits_len   = len(end_flag) if end_flag else 0

        bw_est_s = self._estimate_bitwrap_seconds(
            file_size_bytes=rs_size,
            theta=float(theta),
            start_bits_len=start_bits_len,
            end_bits_len=end_bits_len
        )
        self._bw_est_ms = int(max(0.001, bw_est_s) * 1000.0)
        self._bw_started_mono = time.monotonic()

        bw_done = {"ok": False, "err": None}
        def _bw_job():
            try:
                self._bw.wrap_file(str(rs_out), str(bw_out), float(theta), start_flag, end_flag, int(rng_seed))
                bw_done["ok"] = True
            except Exception as e:
                bw_done["err"] = e

        t_bw = threading.Thread(target=_bw_job, daemon=True)
        t_bw.start()
        while t_bw.is_alive():
            QCoreApplication.processEvents()
            time.sleep(0.01)

        self._bw_raw = 100
        self._bw_started_mono = None
        self._log(f"[OK] Bitwrap completed → {bw_out}")
        if bw_done["err"]:
            raise bw_done["err"]

    # -------- TX (subproc) --------
    def _start_subproc_tx(self, **tx_kwargs):
        bw = self.last_bitwrap_output()
        if not bw:
            raise RuntimeError("No Bitwrap output.")
        cfg = SubprocTxConfig(
            bitwrap_path=bw,
            center=float(tx_kwargs.get("center")), samp=float(tx_kwargs.get("samp")),
            rfbw=float(tx_kwargs.get("rfbw")), atten=float(tx_kwargs.get("atten")),
            buffer=int(tx_kwargs.get("buffer")), amp=float(tx_kwargs.get("amp")),
            pkt=int(tx_kwargs.get("pkt")), roll=int(tx_kwargs.get("roll")),
            mod=str(tx_kwargs.get("mod") or "qpsk"),
        )
        self._log("[TX] Subprocess starting…")
        self._tx_subproc.start(cfg)
        self.sig_tx_started.emit()

        # TX duration estimate and stopwatch
        self._tx_est_ms = self._estimate_tx_duration_ms()
        self._tx_started_mono = time.monotonic()
        self._tx_raw = 0

    # -------- TX (in‑proc + embed) --------
    def _start_inproc_tx(self, *, input_path, center, samp, rfbw, atten, buffer, amp, pkt, roll, mod):
        if TxWorker is None or InprocTxConfig is None:
            self._log("[WARN] In‑process GNURadio could not be imported; falling back to subproc mode.")
            self._log(f"[WARN] Python: {sys.executable}\n[WARN] Import detail: {repr(_INPROC_IMPORT_ERR)}")
            self.set_mode("subproc")
            self._start_subproc_tx(
                center=center, samp=samp, rfbw=rfbw, atten=atten,
                buffer=buffer, amp=amp, pkt=pkt, roll=roll, mod=mod
            )
            return

        if not input_path:
            raise RuntimeError("Missing .bitwrap path for in‑proc TX.")

        if self._tx_worker:
            try:
                self._tx_worker.stop()
                self._tx_worker.wait()
            except Exception:
                pass
            self._tx_worker = None

        rf_bw_val = float(rfbw) if (rfbw is not None) else float(samp)
        cfg = InprocTxConfig(
            bitwrap_path=str(input_path),
            center=float(center),
            samp=float(samp),
            rfbw=float(rf_bw_val),
            atten=float(atten),
            buffer=int(buffer),
            amp=float(amp),
            pkt=int(pkt),
            roll=int(roll),
            mod=str(mod or "qpsk"),
        )

        self._log("[TX] In‑process starting… (embed active)")
        self._tx_worker = TxWorker(cfg)
        self._tx_worker.sig_freq_widget.connect(self._on_freq_widget_ready)
        self._tx_worker.sig_started.connect(lambda: self._log("[TX] In‑proc GNURadio started."))
        self._tx_worker.sig_stopped.connect(lambda: self._log("[TX] In‑proc GNURadio stopped."))
        self._tx_worker.sig_log.connect(self._log)
        self._tx_worker.start()
        self.sig_tx_started.emit()

        self._tx_est_ms = self._estimate_tx_duration_ms()
        self._tx_started_mono = time.monotonic()
        self._tx_raw = 0

    def _on_freq_widget_ready(self, w):
        try:
            self.view.add_freq_widget(w)
        except Exception as e:
            self._log(f"[WARN] Could not add to Frequency Pool: {e}")

    # -------- Stop --------
    def stop_tx(self):
        try:
            if self._tx_subproc and self._tx_subproc.running:
                self._tx_subproc.stop()
        except Exception:
            pass
        try:
            if self._tx_worker:
                self._tx_worker.stop()
                self._tx_worker.wait()
                self._tx_worker = None
        except Exception:
            pass

        self._timer.stop()
        self._rs_started_mono = self._bw_started_mono = self._tx_started_mono = None
        self._running = False  # release start guard

        self.sig_tx_stopped.emit()
        self._log("[CTRL] TX stopped: timer/progress left at current values.")

    # -------- utils --------
    def last_rs_output(self) -> Optional[str]:
        return None if self._last_rs is None else str(self._last_rs)

    def last_bitwrap_output(self) -> Optional[str]:
        return None if self._last_bw is None else str(self._last_bw)

    def set_mode(self, mode: str):
        self.mode = (mode or "").lower().strip() or "inproc"
        self._log(f"[CTRL] Mode changed: {self.mode}")

    def _emit_load_stats(self, input_path: str, theta: float):
        try:
            r = int(self.view.rs_r()); s = int(self.view.rs_s()); theta_val = float(self.view.theta())
        except Exception:
            return
        factor, pct = self._compute_load_factor_and_pct(r, s, theta_val)
        self._log(f"[LOAD] ~{factor:.3f}×  (+%{pct})")

    @staticmethod
    def _human(n: int) -> str:
        for unit in ["B", "KB", "MB", "GB"]:
            if n < 1024:
                return f"{n:.0f} {unit}"
            n /= 1024.0
        return f"{n:.1f} TB"

    # ---- Backward compat: old MainWindow call (subproc) ----
    def run_pipeline_and_tx(
        self,
        *,
        input_path: str,
        out_dir: Optional[str] = None,
        r: int = 16,
        interleave_depth: int = 32,
        slice_bytes: int = 1024,
        theta: float = DEFAULT_THETA,
        start_flag: str = DEFAULT_START_FLAG,
        end_flag: str = DEFAULT_END_FLAG,
        rng_seed: int = DEFAULT_RNG_SEED,
        center: float = 2.4e9,
        samp: float = 2e6,
        rfbw: Optional[float] = None,
        atten: float = 10.0,
        buffer: int = 32768,
        amp: float = 0.03,
        pkt: int = 512,
        roll: int = 0,
        mod: str = "qpsk",
        python_exe: Optional[str] = None,
        runner_path: Optional[str] = None,
        show_console: bool = False,
    ):
        # single‑run guard applies here as well
        if self._running:
            self._log("[CTRL] Start (legacy) ignored: pipeline already running.")
            return
        self._running = True
        try:
            in_p = Path(input_path).resolve()
            if not in_p.exists():
                raise FileNotFoundError(f"Input file not found: {in_p}")

            self.run_pipeline(
                input_path=str(in_p),
                out_dir=out_dir,
                r=int(r),
                interleave_depth=int(interleave_depth),
                slice_bytes=int(slice_bytes),
                theta=float(theta),
                start_flag=start_flag,
                end_flag=end_flag,
                rng_seed=int(rng_seed),
            )

            self._emit_load_stats(str(in_p), float(theta))

            bw_out = self.last_bitwrap_output()
            if not bw_out:
                raise RuntimeError("No Bitwrap output; cannot start TX.")

            # ---- paths.py defaults for portable execution ----
            if python_exe is None and paths and hasattr(paths, "exe_python"):
                try:
                    pxe = Path(paths.exe_python())
                    if pxe.exists():
                        python_exe = str(pxe)
                except Exception:
                    python_exe = None

            if runner_path is None and paths and hasattr(paths, "dir_scripts"):
                try:
                    runner_cand = Path(paths.dir_scripts()) / "tx_runner.py"
                    if runner_cand.exists():
                        runner_path = str(runner_cand)
                except Exception:
                    runner_path = None

            cfg = SubprocTxConfig(
                bitwrap_path=bw_out,
                center=float(center),
                samp=float(samp),
                rfbw=(float(rfbw) if rfbw is not None else None),
                atten=float(atten),
                buffer=int(buffer),
                amp=float(amp),
                pkt=int(pkt),
                roll=int(roll),
                mod=str(mod or "qpsk"),
                python_exe=python_exe,
                runner_path=runner_path,
                show_console=bool(show_console),
            )

            self._log(f"[TX] Starting… (runner={runner_path or 'scripts/tx_runner.py'})")
            if python_exe:
                self._log(f"[TX] Python exe: {python_exe}")
            self._tx_subproc.start(cfg)
            self.sig_tx_started.emit()

            self._tx_est_ms = self._estimate_tx_duration_ms()
            self._tx_started_mono = time.monotonic()
            self._tx_raw = 0
        except Exception as e:
            self._log(f"[ERR] Legacy start error: {e}")
        # _running is not released here; it will be released in stop_tx

    # -------- [PROGRESS] TX duration estimate --------
    def _estimate_tx_duration_ms(self) -> Optional[int]:
        """
        Flowgraph TX duration (ms): packet structure + modulation + fs + slack.
        """
        try:
            fs = float(self.view.samp_rate())
            pkt_bytes = int(self.view.pkt_size())
            mod = str(self.view.modulation()).lower()
            roll = 0
            try:
                roll = int(self.view.roll())
            except Exception:
                pass
            cp = roll if roll > 0 else 16

            if ("bpsk" in mod) or (mod.strip() == "b"):
                bps = 1
            elif ("qpsk" in mod) or (mod.strip() == "q"):
                bps = 2
            else:
                bps = 4  # 16QAM

            Ts = (64 + cp) / fs
            bits_per_sym = 48 * bps
            payload_bits = (pkt_bytes + 4) * 8
            n_pay = math.ceil(payload_bits / bits_per_sym)
            n_sym_pkt = 2 + 1 + n_pay
            T_pkt = n_sym_pkt * Ts

            total_bytes = None
            for name in ("tx_total_bytes", "total_tx_bytes", "bitwrap_size", "payload_bytes"):
                if hasattr(self.view, name):
                    try:
                        total_bytes = int(getattr(self.view, name)())
                        break
                    except Exception:
                        pass
            if total_bytes is None:
                bw = self.last_bitwrap_output()
                if bw:
                    total_bytes = Path(bw).stat().st_size

            if not total_bytes or total_bytes <= 0:
                return None

            n_pkts = math.ceil(total_bytes / pkt_bytes)
            t_est_s = n_pkts * T_pkt
            t_est_s = t_est_s * (1.0 + self._TX_EPS) + self._TX_START_DELAY
            return int(t_est_s * 1000.0)
        except Exception:
            return None

    # ==================== DURATION ESTIMATORS ====================

    @staticmethod
    def _pay_bytes(r: int) -> int:
        # PAY(r) = 12,672 + 66r (rs_container.c)
        return 12672 + 66 * int(r)

    def _estimate_rs_pack_seconds(self, file_size_bytes: int, r: int, slice_bytes: int) -> float:
        """
        Deterministic time estimate for rs_container.c pack_impl.
        """
        if file_size_bytes <= 0 or r <= 0 or slice_bytes <= 0:
            return 0.0

        F = (file_size_bytes + self._FRAME_BYTES - 1) // self._FRAME_BYTES
        P = self._pay_bytes(r)

        t_read   = self._FRAME_BYTES / self._B_READ
        t_rs     = self._ALPHA_RS * r * self._FRAME_BYTES
        t_crc16  = (self._FRAME_BYTES + 64 * r) / self._B_CRC16
        t_crc32f = (self._FRAME_BYTES + 64 * r) / self._B_CRC32
        t_copy   = P / self._B_MEMCPY
        t_write  = P / self._B_WRITE
        t_crc32s = P / self._B_CRC32
        n_slices = (P + slice_bytes - 1) // slice_bytes
        t_sliceo = n_slices * self._C_SLICE

        t_frame = t_read + t_rs + t_crc16 + t_crc32f + t_copy + t_write + t_crc32s + t_sliceo
        return F * t_frame

    def _estimate_bitwrap_seconds(
        self,
        *,
        file_size_bytes: int,
        theta: float,
        start_bits_len: int,
        end_bits_len: int
    ) -> float:
        """
        Deterministic time estimate for bitwrap.cpp (including alignment effects).
        """
        if file_size_bytes <= 0 or theta <= 0:
            return 0.0

        n_bits = 8 * file_size_bytes
        dummy_each = int((n_bits / (2.0 * theta)))        # floor
        dummy_total_bits = 2 * dummy_each
        D_bytes = dummy_total_bits // 8

        # alignment: (dummy_left_bits + |S|) % 8
        a = (dummy_each + start_bits_len) % 8
        B_copy = self._B_COPY_ALIGNED if a == 0 else self._B_COPY_BITWISE

        b_bit = 8.0 * self._B_COPY_BITWISE
        tail_bits = (dummy_each % 8) + (dummy_each % 8)
        pad_bits = (8 - ((dummy_total_bits + start_bits_len + end_bits_len + n_bits) % 8)) % 8
        flags_bits_total = start_bits_len + end_bits_len + tail_bits + pad_bits

        T_dummy = (D_bytes / self._B_DUMMY) if self._B_DUMMY > 0 else 0.0
        T_copy  = (file_size_bytes / B_copy) if B_copy > 0 else 0.0
        T_flags = (flags_bits_total / b_bit) if b_bit > 0 else 0.0
        return T_dummy + T_copy + T_flags
