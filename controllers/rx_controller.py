# -*- coding: utf-8 -*-
"""
controllers/rx_controller.py
"""

import os
import sys
import time
import math
import platform
import signal
import threading
import subprocess
from pathlib import Path

from PyQt5.QtCore import QObject, QTimer, QTime, pyqtSlot, pyqtSignal
from PyQt5.QtWidgets import QFileDialog

from ui.views.rx_view import RxView
from scripts.udp_runner import UdpRunner, UdpRunnerConfig

# --- Bitunwrap service ---
from services.bitunwrap import BitUnwrapService, BitUnwrapError
# --- RS decode (DLL) ---
from services.rs_container import RSContainer

# --- Centralized paths (portable) ---
try:
    import paths  # must provide dir_scripts(), maybe exe_python()
except Exception:
    paths = None


class RxController(QObject):
    # External signal to return to MainWindow
    sig_back = pyqtSignal()

    RX_DELAY_MS = 1000        # delay to start RX after UDP listener starts
    QUIET_TIMEOUT_MS = 5000   # inactivity timeout after first data observed
    TICK_MS = 200

    # --- Smooth progress constants (global) ---
    _ALPHA = 0.12
    _VMAX = 0.35
    _PCAP = 0.97
    _EASE_T = 0.6
    _ETA_ALPHA = 0.20
    _DECODE_R_EXP = 0.5

    # Initial speed estimates (will be calibrated)
    _BETA_UNWRAP_MBPS = 48.0
    _BETA_DECODE_BASE = 26.0
    _BETA_EMA = 0.30

    def __init__(self, view: RxView, parent=None):
        super().__init__(parent)
        self.view = view

        # UDP runner
        self._udp = UdpRunner()
        self._bind_signals()

        # RX subprocess handle
        self._rx_proc = None
        self._rx_started = False

        # Start/stop gating
        self._rx_enable = False          # <- RX başlatılabilir mi?
        self._last_shutdown_reason = ""  # user-stop / no-progress-5s / etc.

        # Timer and counters
        self._tick = QTimer(self)
        self._tick.setInterval(self.TICK_MS)
        self._tick.timeout.connect(self._on_tick)

        self._t0 = QTime()
        self._udp_start_ms = None
        self._rx_deadline_ms = None

        # Watchdog state
        self._armed = False
        self._bytes_prev = 0
        self._last_change_ms = None
        self._wd_fired = False  # ← tek seferlik log için

        # Post-process guard
        self._post_done = False

        # Output paths
        self._dir_path = None
        self._out_name = "out.bitwrap"
        self._bitwrap_path = None
        self._unwrapped_path = None
        self._decoded_path = None

        # RS decode service
        self._rs = RSContainer()

        # --- Smooth progress state ---
        self._phase = "idle"    # idle | unwrap | decode | done | error
        self._phase_t0 = None
        self._ease_t0 = None
        self._unwrap_est_s = None
        self._decode_est_s = None
        self._unwrap_s = 0.0
        self._decode_s = 0.0
        self._eta_vis_s = None
        self._worker = None
        self._worker_stop_flag = False

        # Speed calibration state
        self._beta_unwrap = self._BETA_UNWRAP_MBPS
        self._beta_decode_base = self._BETA_DECODE_BASE

        # UI init
        self._overall_pct = 0
        self._set_kpis_zero()
        self.view.set_time("00:00:00")
        self._set_total_progress(0)

        # Start disabled: until a folder is selected
        if hasattr(self.view, "set_start_enabled"):
            self.view.set_start_enabled(False)

        # View signals
        self.view.sig_start.connect(self.on_start_clicked)
        self.view.sig_stop.connect(self.on_stop_clicked)
        self.view.sig_clear.connect(self.on_clear_clicked)

        # Browse (folder only)
        if hasattr(self.view, "fileBrowseButton"):
            self.view.fileBrowseButton.clicked.connect(self._on_browse_dir)

        # --- Post-process log path ---
        self._pp_log_path = None  # post_process_log.txt tam yolu

    # ---------- UDP Runner signal bindings ----------
    def _bind_signals(self):
        self._udp.stats.connect(self._on_udp_stats)
        self._udp.listening.connect(self._on_udp_listening)
        self._udp.started.connect(self._on_udp_started)
        self._udp.stopped.connect(self._on_udp_stopped)

    # ---------- Browse: folder only ----------
    @pyqtSlot()
    def _on_browse_dir(self):
        start_dir = self.view.input_path().strip() or str(Path.home())
        dirname = QFileDialog.getExistingDirectory(None, "Select folder to save", start_dir)
        if not dirname:
            return

        if hasattr(self.view, "set_input_path"):
            self.view.set_input_path(dirname)
        if hasattr(self.view, "set_start_enabled"):
            self.view.set_start_enabled(True)

        self.view.append_log("[RX] Folder selected: %s" % dirname)

    # ---------- Start ----------
    @pyqtSlot()
    def on_start_clicked(self):
        if self._udp.is_running():
            self.view.append_log("[RX] Already listening.")
            return

        # Validate folder
        dir_str = self.view.input_path().strip()
        if (not dir_str) or (not os.path.isdir(dir_str)):
            self.view.append_log("[RX][ERROR] Please select a valid save directory first.")
            if hasattr(self.view, "set_start_enabled"):
                self.view.set_start_enabled(False)
            return

        # Prepare paths
        self._dir_path = Path(dir_str).resolve()
        self._bitwrap_path   = self._dir_path / self._out_name
        self._unwrapped_path = self._dir_path / "out.unwrapped"

        # final decode output → extension chosen by combobox
        try:
            ext = (getattr(self.view, "file_type", lambda: "bin")() or "bin").strip().lower()
        except Exception:
            ext = "bin"
        allowed = {"mp4", "mp3", "jpg", "png", "txt"}
        if ext not in allowed:
            ext = "bin"
        self._decoded_path = self._dir_path / ("out.%s" % ext)

        # Reset runtime
        self._armed = False
        self._bytes_prev = 0
        self._last_change_ms = None
        self._rx_started = False
        self._rx_deadline_ms = None
        self._post_done = False
        self._last_shutdown_reason = ""
        self._wd_fired = False   # ← WD log bayrağı reset

        # Reset smooth progress
        self._phase = "idle"
        self._unwrap_s = 0.0
        self._decode_s = 0.0
        self._unwrap_est_s = None
        self._decode_est_s = None
        self._ease_t0 = None
        self._eta_vis_s = None

        self._set_kpis_zero()
        self.view.set_ber_text("BER: —")
        self._set_total_progress(0)
        self.view.append_log("[RX] Starting UDP listening → 0.0.0.0:2000 → %s" % self._bitwrap_path)

        # Enable RX starts
        self._rx_enable = True

        # ---- Portable exe path via paths.py ----
        if paths and hasattr(paths, "dir_scripts"):
            scripts_dir = Path(paths.dir_scripts())
        else:
            scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
        udp_exe = scripts_dir / "udp_dump+.exe"

        cfg = UdpRunnerConfig(
            exe_path=str(udp_exe),
            bind_ip="0.0.0.0",
            port=2000,
            out_file=self._out_name,
            cwd=str(self._dir_path),
            create_new_process_group=True,
            detached_process=True
        )
        self._udp.start(cfg)

        # Start elapsed timer
        self._t0 = QTime.currentTime()
        self._tick.start()

    def _stop_timer(self):
        """Elapsed timer'ı güvenli biçimde durdur ve yeni tick'leri engelle."""
        if self._tick.isActive():
            self._tick.stop()
        # _t0'ı geçersiz yap → _on_tick artık süreyi artırmaz
        self._t0 = QTime()
        try:
            self.view.set_eta_text("")
        except Exception:
            pass
        self.view.append_log("[DBG] timer stopped.")

    # ---------- Stop ----------
    @pyqtSlot()
    def on_stop_clicked(self):
        self.view.append_log("[RX] Stopping…")
        self._stop_timer()
        self._auto_shutdown(reason="user-stop")

    # ---------- Clear ----------
    @pyqtSlot()
    def on_clear_clicked(self):
        self._tick.stop()
        self.view.clear_log()
        self._set_kpis_zero()
        self.view.set_time("00:00:00")
        self._set_total_progress(0)
        self.view.set_ber_text("BER: —")
        self.view.append_log("[RX] Cleared.")

    # ---------- UDP Runner events ----------
    @pyqtSlot(int)
    def _on_udp_started(self, pid):
        self.view.append_log("[RX] udp_dump+ started (pid=%s)." % pid)

        now_ms = self._now_ms()
        self._udp_start_ms = now_ms
        self._rx_deadline_ms = now_ms + self.RX_DELAY_MS

        # RX tekrarlarına izin ver (start'ta set edildi)
        self._rx_enable = True

        # Schedule RX start; don't rely on tick timing
        self.view.append_log(f"[DBG] scheduling _start_rx in {self.RX_DELAY_MS} ms")
        QTimer.singleShot(self.RX_DELAY_MS, self._start_rx_wrapper)

    @pyqtSlot(int, str)
    def _on_udp_stopped(self, exit_code, reason):
        self.view.append_log("[RX] udp_dump+ stopped. exit=%s, reason=%s" % (exit_code, reason))
        # RX yeniden başlatmayı kapat
        self._rx_enable = False
        self._rx_deadline_ms = None

        # **POST-PROC'TA WATCHDOG YOK**
        self._armed = False
        self._wd_fired = False

        # if RX is alive, stop it
        if self._rx_started:
            self._stop_rx(grace_ms=800)
        # trigger post-process after UDP stop
        self._postprocess_after_udp()

    @pyqtSlot(str, int)
    def _on_udp_listening(self, ip, port):
        self.view.append_log("[RX] Listening UDP %s:%s" % (ip, port))

    @pyqtSlot(dict)
    def _on_udp_stats(self, d):
        pkts = "%s" % d.get('pkts', 0)
        total_bytes = int(d.get('bytes', 0))
        mb = float(d.get('mb', 0.0))
        bytes_str = "%d (%.2f MB)" % (total_bytes, mb)

        rate_MBps = float(d.get('mbps', 0.0)) / 8.0
        rate_str = "%.2f MB/s" % rate_MBps

        q_used = float(d.get('queue_mb', 0.0))
        q_str = "%.2f / 8 MB" % q_used
        drops = "%d" % int(d.get('drops', 0))

        self.view.udpStatsPanel.set_kpis(
            pkts=pkts, bytes=bytes_str, rate=rate_str, queue=q_str, drops=drops, flush="200 ms"
        )

        if total_bytes > self._bytes_prev:
            self._bytes_prev = total_bytes
            self._last_change_ms = self._now_ms()
            self._wd_fired = False  # Aktivitede WD log bayrağını sıfırla
            if not self._armed:
                self._armed = True
                self.view.append_log("[RX] First packet received → watchdog ARMED.")

    # ---------- Timer tick ----------
    def _on_tick(self):
        # Elapsed time UI
        if self._overall_pct < 100 and self._t0.isValid():
            ms = self._t0.msecsTo(QTime.currentTime())
            if ms < 0:
                ms = 0
            hh = ms // 3600000
            mm = (ms % 3600000) // 60000
            ss = (ms % 60000) // 1000
            self.view.set_time("%02d:%02d:%02d" % (hh, mm, ss))

        now_ms = self._now_ms()

        # Fallback: SingleShot kaçarsa (yalnızca enable iken)
        if (self._rx_enable and (not self._rx_started) and
            (self._rx_deadline_ms is not None) and (now_ms >= self._rx_deadline_ms)):
            self.view.append_log("[DBG] tick fallback → calling _start_rx()")
            self._start_rx()

        # Watchdog — SADECE UDP AKTİFKEN ve ARMED İKEN
        if self._udp.is_running() and self._armed and (self._last_change_ms is not None):
            if (now_ms - self._last_change_ms) >= self.QUIET_TIMEOUT_MS:
                if not self._wd_fired:
                    self._wd_fired = True  # tek seferlik log
                    self.view.append_log("[RX] No progress for 5s → shutting down automatically…")
                self._auto_shutdown(reason="no-progress-5s")

        # Smooth progress update
        self._update_progress_smooth()

    # ---------- RX start/stop ----------
    def _start_rx(self):
        """Start rx_runner.py in a NEW console and show logs in that console."""
        if not self._rx_enable:
            self.view.append_log("[DBG] _start_rx: disabled; skip.")
            return
        if self._rx_started:
            self.view.append_log("[DBG] _start_rx: already started; bail.")
            return

        # Scripts dir
        if paths and hasattr(paths, "dir_scripts"):
            scripts_dir = Path(paths.dir_scripts())
        else:
            scripts_dir = Path(__file__).resolve().parent.parent / "scripts"

        self.view.append_log(f"[DBG] scripts_dir={scripts_dir}")
        rx_runner = scripts_dir / "rx_runner.py"
        self.view.append_log(f"[DBG] rx_runner exists={rx_runner.exists()} path={rx_runner}")

        if not rx_runner.exists():
            self.view.append_log("[RX][ERROR] rx_runner.py not found: %s" % rx_runner)
            return

        # View params
        center = getattr(self.view, "center_hz", lambda: 2.4e9)()
        samp   = getattr(self.view, "samp_rate", lambda: 2e6)()
        rfbw   = getattr(self.view, "rf_bw", lambda: 2e6)()
        buf    = getattr(self.view, "buffer_size", lambda: 32768)()

        gm = getattr(self.view, "gain_mode", lambda: "slow")()
        gm_map = {"slow": "slow_attack", "fast": "fast_attack", "manual": "manual"}
        gain_mode = gm_map.get(gm, "slow_attack")

        gv = getattr(self.view, "gain_value", lambda: None)()
        gain_db = 64.0 if gv is None else float(gv)

        try:
            mod_view = getattr(self.view, "modulation", lambda: "qpsk")().strip().lower()
        except Exception:
            mod_view = "qpsk"
        mod_map = {"bpsk": "bpsk", "qpsk": "qpsk", "16qam": "qam16"}
        mod_arg = mod_map.get(mod_view, "qpsk")

        # Python exe (prefer portable)
        py = None
        if paths and hasattr(paths, "exe_python"):
            try:
                p = Path(paths.exe_python())
                if p.exists():
                    py = str(p)
            except Exception:
                py = None
        if not py:
            py = sys.executable or "python"
            if platform.system().lower().startswith("win"):
                exe_path = Path(py)
                if exe_path.name.lower() == "pythonw.exe":
                    cand = exe_path.with_name("python.exe")
                    py = str(cand) if cand.exists() else "python.exe"

        self.view.append_log(f"[DBG] python={py}")

        # Build args (NO cmd.exe)
        args = [
            py, "-u", str(rx_runner),
            "--center", str(center),
            "--samp",   str(samp),
            "--rfbw",   str(rfbw),
            "--buffer", str(buf),
            "--gain_mode", gain_mode,
            "--gain_db",   str(gain_db),
            "--mod", mod_arg,
        ]
        self.view.append_log("[RX] Spawn: %s" % " ".join(args))

        # Env (portable PATH augmentation)
        env = os.environ.copy()
        env.setdefault("PYTHONUNBUFFERED", "1")
        try:
            py_dir = str(Path(py).parent)
            cand = [
                py_dir,
                str(Path(py_dir) / "Library" / "bin"),
                str(Path(py_dir) / "Scripts"),
            ]
            existing = env.get("PATH", "")
            prepend = ";".join([c for c in cand if os.path.isdir(c)])
            if prepend:
                env["PATH"] = prepend + (";" + existing if existing else "")
            self.view.append_log(f"[DBG] PATH prepend={prepend}")
        except Exception as e:
            self.view.append_log(f"[DBG] PATH prep error: {e}")

        try:
            creationflags = 0
            if platform.system().lower().startswith("win"):
                creationflags = (
                    subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
                )

            # DİKKAT: stdin/stdout/stderr yönlendirme YOK → yeni konsola akar
            self._rx_proc = subprocess.Popen(
                args,
                cwd=str(scripts_dir),
                creationflags=creationflags,
                env=env,
                shell=False,
            )
            self._rx_started = True
            self.view.append_log("[RX] rx_runner started (pid=%s) — new console. mod=%s" %
                                 (self._rx_proc.pid, mod_arg))
        except Exception as e:
            self.view.append_log("[RX][ERROR] rx_runner could not be started: %s" % e)

    def _start_rx_wrapper(self):
        # Guard against multiple triggers + enable kontrolü
        if not self._rx_enable:
            self.view.append_log("[DBG] _start_rx_wrapper: disabled; skip.")
            return
        if self._rx_started:
            self.view.append_log("[DBG] _start_rx_wrapper: already started; skipping.")
            return
        self.view.append_log("[DBG] _start_rx_wrapper: calling _start_rx()")
        self._start_rx()

    def _stop_rx(self, grace_ms=1200):
        """Try graceful stop; if not, kill process tree on Windows."""
        if (not self._rx_started) or (not self._rx_proc):
            return
        try:
            if platform.system().lower().startswith("win"):
                # 1) Kibar dene (çoğu durumda etkisiz olabilir)
                try:
                    self._rx_proc.send_signal(signal.CTRL_BREAK_EVENT)
                except Exception:
                    pass

                # 2) Kısa bekle
                t0 = time.time()
                while (self._rx_proc.poll() is None) and ((time.time() - t0) < (grace_ms / 1000.0)):
                    time.sleep(0.05)

                # 3) Hâlâ yaşıyorsa — ağaçla birlikte öldür
                if self._rx_proc.poll() is None:
                    try:
                        subprocess.run(["taskkill", "/PID", str(self._rx_proc.pid), "/T", "/F"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                    except Exception:
                        pass
                    # ek kısa bekleme
                    t1 = time.time()
                    while (self._rx_proc.poll() is None) and ((time.time() - t1) < 1.0):
                        time.sleep(0.05)
            else:
                try:
                    os.killpg(os.getpgid(self._rx_proc.pid), signal.SIGINT)
                except Exception:
                    pass

                t0 = time.time()
                while (self._rx_proc.poll() is None) and ((time.time() - t0) < (grace_ms / 1000.0)):
                    time.sleep(0.05)

                if self._rx_proc.poll() is None:
                    try:
                        self._rx_proc.kill()
                    except Exception:
                        pass
        except Exception:
            pass

        code = self._rx_proc.poll()
        self.view.append_log("[RX] rx_runner stopped (exit=%s)." % code)
        self._rx_proc = None
        self._rx_started = False

    # ---------- General shutdown & post-process ----------
    def _auto_shutdown(self, reason):
        """Stop RX and UDP in order (then run bitunwrap+decode)."""
        # RX yeniden başlatmayı KAPAT
        self._rx_enable = False
        self._rx_deadline_ms = None
        self._last_shutdown_reason = str(reason or "")

        # **POST-PROC'TA WATCHDOG YOK**
        self._armed = False
        self._wd_fired = False

        # If a worker is running, set stop flag
        self._worker_stop_flag = True

        # Stop RX → then UDP
        if self._rx_started:
            self._stop_rx(grace_ms=1200)

        if self._udp.is_running():
            self._udp.stop(grace_ms=600)

        self.view.append_log("[RX] Shutdown complete. (reason=%s)" % reason)

        # For safety (if UDP already stopped)
        self._postprocess_after_udp()

    def _postprocess_after_udp(self):
        if self._post_done:
            return
        if self._udp.is_running():
            return
        self._post_done = True

        # **POST-PROC BAŞLARKEN WATCHDOG TAMAMEN KAPALI**
        self._armed = False
        self._wd_fired = False

        # --- Post-process log dosyasını baştan oluştur (overwrite) ---
        self._pp_log_reset()
        self._pp_log("POST: entering post-process")
        try:
            self._pp_log(f"POST: output_dir={self._dir_path}")
        except Exception:
            pass

        if (not self._dir_path) or (not self._bitwrap_path):
            self._pp_log("POST[INFO] Output path unknown; skipping bitunwrap.")
            self.view.append_log("[RX][POST] Output path unknown; skipping bitunwrap.")
            return
        if not self._bitwrap_path.exists():
            self._pp_log(f"POST[INFO] Expected file not found: {self._bitwrap_path}")
            self.view.append_log("[RX][POST][INFO] Expected file not found (probably stopped early): %s" % self._bitwrap_path)
            return
        size = self._bitwrap_path.stat().st_size
        self._pp_log(f"POST: bitwrap file={self._bitwrap_path.name} size={size} bytes")
        if size <= 0:
            if "user" in (self._last_shutdown_reason or ""):
                self._pp_log("POST[INFO] Output is empty (stopped by user).")
                self.view.append_log("[RX][POST][INFO] Output is empty (stopped by user): %s" % self._bitwrap_path)
            else:
                self._pp_log("POST[ERROR] File seems empty.")
                self.view.append_log("[RX][POST][ERROR] File seems empty: %s" % self._bitwrap_path)
            return

        # Smooth: start unwrap phase (thread)
        self._start_unwrap_worker()

    # ---------- Smooth progress & workers ----------
    def _start_unwrap_worker(self):
        if self._worker and self._worker.is_alive():
            return

        # **Ek güvenlik: post-proc sırasında WD kapalı**
        self._armed = False
        self._wd_fired = False

        try:
            B_in = float(self._bitwrap_path.stat().st_size)
        except Exception:
            B_in = 0.0
        beta = max(1e-6, self._beta_unwrap)
        t_over = 0.20
        T_est = (B_in / (1024.0 * 1024.0)) / beta + t_over

        self._phase = "unwrap"
        self._phase_t0 = time.perf_counter()
        self._ease_t0 = None
        self._unwrap_est_s = max(0.05, T_est)
        self._unwrap_s = 0.0
        self._decode_s = 0.0
        self._eta_vis_s = self._unwrap_est_s

        self._pp_log("UNWRAP: starting")

        def _worker():
            t0 = time.perf_counter()
            try:
                self.view.append_log("[BITUNWRAP] Starting → %s" % self._bitwrap_path.name)
                svc = BitUnwrapService()
                res = svc.unwrap_with_progress(self._bitwrap_path, self._unwrapped_path, progress_cb=None)
                if (res.start_flag_pos is not None) or (res.end_flag_pos is not None):
                    self.view.append_log("[BITUNWRAP] Flags: start=%s  end=%s" %
                                         (res.start_flag_pos, res.end_flag_pos))
                self._pp_log(f"UNWRAP: flags start={res.start_flag_pos} end={res.end_flag_pos}")
                dt = time.perf_counter() - t0

                self._pp_log(f"UNWRAP: done in {dt:.2f}s → out={self._unwrapped_path.name}")
                self._start_ease_out(phase="unwrap")
                self._calibrate_beta_unwrap(bytes_count=B_in, elapsed_s=dt - t_over)

                self._start_decode_worker()

            except BitUnwrapError as e:
                self.view.append_log("[BITUNWRAP][ERROR] %s" % e)
                self._pp_log(f"UNWRAP[ERROR] {e}")
                self._phase = "error"
            except Exception as e:
                self.view.append_log("[BITUNWRAP][ERROR] %s" % e)
                self._pp_log(f"UNWRAP[ERROR] {e}")
                self._phase = "error"

        self._worker_stop_flag = False
        self._worker = threading.Thread(target=_worker, daemon=True)
        self._worker.start()

    def _start_decode_worker(self):
        if not self._unwrapped_path or (not self._unwrapped_path.exists()):
            self.view.append_log("[DECODE][ERROR] Unwrapped file not found: %s" % self._unwrapped_path)
            self._pp_log("DECODE[ERROR] Unwrapped file not found.")
            self._phase = "error"
            return

        try:
            pad = int(getattr(self.view, "pad_mode", lambda: 0)())
        except Exception:
            pad = 0

        B_ct = float(self._unwrapped_path.stat().st_size) if self._unwrapped_path.exists() else 0.0
        try:
            r = int(getattr(self.view, "rs_r_value", lambda: 16)())
        except Exception:
            r = 16
        r = max(1, min(63, r))
        beta_r = self._beta_decode_base * math.pow(16.0 / float(r), self._DECODE_R_EXP)
        beta_r = max(1e-6, beta_r)
        t_over = 0.30
        T_est = (B_ct / (1024.0 * 1024.0)) / beta_r + t_over

        self.view.append_log("[DECODE] Starting → pad=%s  in=%s  out=%s" %
                             (pad, self._unwrapped_path.name, self._decoded_path.name))
        self._pp_log(f"DECODE: starting pad={pad} r={r} in={self._unwrapped_path.name} out={self._decoded_path.name}")

        self._phase = "decode"
        self._phase_t0 = time.perf_counter()
        self._ease_t0 = None
        self._decode_est_s = max(0.05, T_est)
        self._decode_s = 0.0
        self._eta_vis_s = self._decode_est_s

        def _worker():
            t0 = time.perf_counter()
            try:
                self._rs.decode_file(str(self._unwrapped_path), str(self._decoded_path),
                                     pad_mode=pad, progress_cb=None)
                dt = time.perf_counter() - t0

                try:
                    st = self._rs.get_stats_v1()
                    if st:
                        ber = float(st.get("ber_est", 0.0))
                        ok  = st.get("slices_ok", 0)
                        bad = st.get("slices_bad", 0)
                        fail_cols = st.get("rs_fail_columns", 0)
                        self.view.set_ber_value(ber)
                        self.view.append_log("[STATS] slices ok/bad=%s/%s  rs_fail_cols=%s  BER≈%.3e"
                                             % (ok, bad, fail_cols, ber))
                        # post-process log (BER dahil)
                        self._pp_log(f"STATS: slices ok/bad={ok}/{bad} rs_fail_cols={fail_cols} BER≈{ber:.3e}")
                    else:
                        self.view.set_ber_text("BER: —")
                        self._pp_log("STATS: BER unavailable (—)")
                except Exception as e:
                    self.view.set_ber_text("BER: —")
                    self._pp_log(f"STATS[WARN] Could not fetch stats: {e}")

                self._start_ease_out(phase="decode")
                self._calibrate_beta_decode(bytes_count=B_ct, elapsed_s=dt - t_over, r=r)

                self._phase = "done"
                self.view.append_log("[DECODE] Completed → %s" % self._decoded_path.name)
                self._pp_log(f"DECODE: done in {dt:.2f}s → out={self._decoded_path.name}")
                self._pp_log("POST: completed")

            except Exception as e:
                self.view.append_log("[DECODE][ERROR] %s" % e)
                self._pp_log(f"DECODE[ERROR] {e}")
                self._phase = "error"

        self._worker_stop_flag = False
        self._worker = threading.Thread(target=_worker, daemon=True)
        self._worker.start()

    def _start_ease_out(self, phase):
        self._ease_t0 = time.perf_counter()

    def _update_progress_smooth(self):
        now = time.perf_counter()

        def _ema(prev, new, a):
            return (1.0 - a) * prev + a * new if prev is not None else new

        if self._phase == "unwrap" and self._phase_t0 and self._unwrap_est_s:
            elapsed = now - self._phase_t0
            r = max(0.0, min(1.0, elapsed / self._unwrap_est_s))
            s = self._unwrap_s + self._ALPHA * (r - self._unwrap_s)
            s = min(s, self._unwrap_s + self._VMAX * (self.TICK_MS / 1000.0))
            if self._ease_t0 is None:
                s = min(s, self._PCAP)
            else:
                x = max(0.0, min(1.0, (now - self._ease_t0) / self._EASE_T))
                p0 = self._unwrap_s
                s = p0 + (1.0 - p0) * (1.0 - math.pow(1.0 - x, 3.0))
            self._unwrap_s = max(0.0, min(1.0, s))
            eta_raw = max(0.0, self._unwrap_est_s - elapsed)
            self._eta_vis_s = _ema(self._eta_vis_s, eta_raw, self._ETA_ALPHA)

        elif self._phase == "decode" and self._phase_t0 and self._decode_est_s:
            elapsed = now - self._phase_t0
            r = max(0.0, min(1.0, elapsed / self._decode_est_s))
            s = self._decode_s + self._ALPHA * (r - self._decode_s)
            s = min(s, self._decode_s + self._VMAX * (self.TICK_MS / 1000.0))
            if self._ease_t0 is None:
                s = min(s, self._PCAP)
            else:
                x = max(0.0, min(1.0, (now - self._ease_t0) / self._EASE_T))
                p0 = self._decode_s
                s = p0 + (1.0 - p0) * (1.0 - math.pow(1.0 - x, 3.0))
            self._decode_s = max(0.0, min(1.0, s))
            eta_raw = max(0.0, self._decode_est_s - elapsed)
            self._eta_vis_s = _ema(self._eta_vis_s, eta_raw, self._ETA_ALPHA)

        elif self._phase in ("done", "error"):
            pass

        total_pct = int(round(100.0 * (0.5 * self._unwrap_s + 0.5 * self._decode_s)))
        self._set_total_progress(total_pct)

        try:
            if self._phase == "unwrap":
                rest = (self._decode_est_s if self._decode_est_s else 0.0)
                eta = (self._eta_vis_s if self._eta_vis_s is not None else 0.0) + rest
            elif self._phase == "decode":
                eta = (self._eta_vis_s if self._eta_vis_s is not None else 0.0)
            else:
                eta = None
            if eta is not None:
                if eta < 1.0:
                    self.view.set_eta_text("ETA: <1 s")
                else:
                    mm = int(eta // 60)
                    ss = int(round(eta % 60))
                    self.view.set_eta_text("ETA: %02d:%02d" % (mm, ss))
            else:
                self.view.set_eta_text("")
        except Exception:
            pass

        if self._phase == "done":
            self._set_total_progress(100)
            if self._tick.isActive():
                self._tick.stop()
            self.view.append_log("[RX] Progress 100% — timer stopped.")
            self._phase = "idle"

    def _calibrate_beta_unwrap(self, bytes_count, elapsed_s):
        try:
            if elapsed_s <= 0:
                return
            meas = (bytes_count / (1024.0 * 1024.0)) / max(1e-6, elapsed_s)
            self._beta_unwrap = (1.0 - self._BETA_EMA) * self._beta_unwrap + self._BETA_EMA * meas
        except Exception:
            pass

    def _calibrate_beta_decode(self, bytes_count, elapsed_s, r):
        try:
            if elapsed_s <= 0:
                return
            meas_abs = (bytes_count / (1024.0 * 1024.0)) / max(1e-6, elapsed_s)
            norm = meas_abs / math.pow(16.0 / float(r), self._DECODE_R_EXP)
            self._beta_decode_base = (1.0 - self._BETA_EMA) * self._beta_decode_base + self._BETA_EMA * norm
            self.view.append_log("[CAL] β_decode_base → %.2f MB/s (meas@r=%d=%.2f)" %
                                 (self._beta_decode_base, r, meas_abs))
        except Exception:
            pass

    # ---------- helpers ----------
    def _decode_unwrapped(self):
        self._start_decode_worker()

    def _set_kpis_zero(self):
        self.view.udpStatsPanel.set_kpis(
            pkts="0", bytes="0 (0.00 MB)", rate="0.00 MB/s",
            queue="0.00 / 8 MB", drops="0", flush="200 ms"
        )

    def _set_total_progress(self, pct):
        try:
            pct = int(max(0, min(100, pct)))
        except Exception:
            pct = 0
        self._overall_pct = pct
        self.view.set_overall_progress(pct)

    def _now_ms(self):
        return int(time.monotonic() * 1000)

    # ---------- post_process_log helpers ----------
    def _pp_log_reset(self):
        """post_process_log.txt dosyasını overwrite ederek başlat."""
        try:
            if not self._dir_path:
                return
            self._pp_log_path = self._dir_path / "post_process_log.txt"
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            with open(self._pp_log_path, "w", encoding="utf-8") as f:
                f.write(f"=== POST-PROCESS START {ts} ===\n")
        except Exception as e:
            self.view.append_log(f"[POSTLOG][WARN] reset failed: {e}")

    def _pp_log(self, msg: str):
        """post_process_log.txt içine zaman damgalı satır ekle (append)."""
        try:
            if not self._pp_log_path:
                # güvenli tarafta kal: reset et
                self._pp_log_reset()
            if not self._pp_log_path:
                return
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            with open(self._pp_log_path, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] {msg}\n")
        except Exception as e:
            self.view.append_log(f"[POSTLOG][WARN] write failed: {e}")
