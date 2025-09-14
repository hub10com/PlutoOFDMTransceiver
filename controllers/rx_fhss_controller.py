# -*- coding: utf-8 -*-
"""
controllers/rx_fhss_controller.py

Sekans (değişmedi):
  1) Start → klasör doğrula → pluto_cmdd başlat → (beklemeden) JD başlat
  2) JD tetik → 4.2 s sonra FHSS görseller + aynı gecikmeyle RX start
  3) RX devri → RX başlamadan JD durur

Stop (reboot ARTIK otomatik değil):
  RX → JD → AUTOTX/AUTORX OFF → cmdd.stop(reboot_first=False) → bitti
  (Start butonu 6 sn kilitlenir; tekrar tıklamalar engellenir)

Notlar:
- Pluto HTTP hazır/probe/log akışı KALDIRILDI.
- Reboot artık manuel bir “reboot” tuşuna bağlandı (sig_reboot veya rebootButton).
"""

import re
import os
import socket
import time
import threading
import subprocess
import shutil
from pathlib import Path
from typing import Optional

from PyQt5.QtCore import QObject, QTimer, pyqtSlot, Qt

from controllers.rx_controller import RxController
from scripts.pluto_cmdd_runner import PlutoCmddRunner, PlutoCmddConfig, _best_effort_autorx_off, _default_exe
from scripts.jammer_detection_runner import JammerDetectionRunner, JammerDetectConfig


# -------- yardımcı: proxy ise iç view'u sök --------
def _unwrap_view(maybe_view):
    if maybe_view is None:
        return None
    for attr in ("_v", "v", "view", "_view", "delegate", "inner", "wrapped", "target"):
        try:
            inner = getattr(maybe_view, attr)
            if inner is not None:
                return inner
        except Exception:
            pass
    return maybe_view


# -------- best-effort HTTP komutları (komut gönderimleri korunuyor) --------
def _best_effort_send_cmd(host: str = "192.168.2.1", port: int = 80, line: str = "") -> None:
    try:
        with socket.create_connection((host, port), timeout=0.8) as s:
            if not line.endswith("\r\n"):
                line = line + "\r\n"
            s.sendall(line.encode("ascii", "ignore"))
            try:
                s.shutdown(socket.SHUT_WR)
            except Exception:
                pass
    except Exception:
        pass


def _best_effort_autotx_off(host: str = "192.168.2.1", port: int = 80) -> None:
    _best_effort_send_cmd(host, port, "AUTOTX_MODE OFF")


# ---------- Sinyal stub (RxController'ı otomatik tetiklememek için) ----------
class _SigStub:
    def __init__(self):
        self._slots = []

    def connect(self, slot):
        if callable(slot):
            self._slots.append(slot)

    def disconnect(self, slot=None):
        if slot is None:
            self._slots.clear()
        else:
            try:
                self._slots.remove(slot)
            except ValueError:
                pass

    def emit(self, *args, **kwargs):
        # Bilerek boş: RxController bu sinyallere bağlansa da
        # emit edilmeyecek; RX sadece FHSS akışından başlatılacak.
        pass


# -------- RxController için View Proxy (forward adapter) --------
class _RxViewProxy:
    """
    RxController'ın beklediği UI API'lerini RxFhssView'e map eder.
    - Tüm UI yazmaları GUI thread'ine QTimer.singleShot(0, ...) ile post edilir.
    - self.view.udpStatsPanel.set_kpis(...) çağrılarını karşılamak için shim içerir.
    - Start/Stop/Clear sinyalleri STUB: RxController otomatik tetiklenmesin.
    """

    # --- GUI thread'e post yardımcı
    def _gui(self, fn, *args, **kwargs):
        try:
            QTimer.singleShot(0, lambda: fn(*args, **kwargs))
        except Exception:
            pass

    def __init__(self, v):
        self._v = _unwrap_view(v)

        # --- Sinyaller: STUB (forward ETME)
        self.sig_start = _SigStub()
        self.sig_stop = _SigStub()
        self.sig_clear = _SigStub()
        self.sig_back = _SigStub()
        self.sig_file_selected = _SigStub()
        self.fileBrowseButton = getattr(self._v, "fileBrowseButton", None)

        # --- UDP panel shim'i
        class _UdpPanelShim:
            def __init__(self, real_panel, post_fn):
                self._real = real_panel
                self._post = post_fn

            def set_kpis(self, *args, **kwargs):
                try:
                    if self._real and hasattr(self._real, "set_kpis"):
                        self._post(self._real.set_kpis, *args, **kwargs)
                except Exception:
                    pass

        real_udp = getattr(self._v, "udpStatsPanel", None)
        self.udpStatsPanel = _UdpPanelShim(real_udp, self._gui)

    # ---------- Log & zaman ----------
    def append_log(self, s: str):
        try:
            self._gui(self._v.append_log, s)
        except Exception:
            pass

    def clear_log(self):
        try:
            self._gui(self._v.clear_log)
        except Exception:
            pass

    def set_time(self, hhmmss: str):
        try:
            self._gui(self._v.set_time, hhmmss)
        except Exception:
            pass

    # Eski controller adları ile uyumluluk
    def log(self, s: str): self.append_log(s)
    def add_log(self, s: str): self.append_log(s)
    def add_log_line(self, s: str): self.append_log(s)
    def write_log(self, s: str): self.append_log(s)
    def println(self, s: str = ""): self.append_log(s)

    # ---------- Progress ----------
    def set_overall_progress(self, pct: int):
        try:
            self._gui(self._v.set_overall_progress, int(pct))
        except Exception:
            pass

    def on_total_progress(self, pct: int): self.set_overall_progress(pct)

    def on_bw_progress(self, pct: int):
        try:
            self._gui(self._v.on_bw_progress, int(pct))
        except Exception:
            self.set_overall_progress(pct)

    def on_rs_progress(self, pct: int):
        try:
            self._gui(self._v.on_rs_progress, int(pct))
        except Exception:
            self.set_overall_progress(pct)

    # alias'lar
    def on_bitunwrap_progress(self, pct: int): self.on_bw_progress(pct)
    def on_decode_progress(self, pct: int): self.on_rs_progress(pct)
    def on_progress_total(self, pct: int): self.on_total_progress(pct)

    # ---------- BER ----------
    def set_ber_value(self, ber: float):
        """RxController doğrudan çağırıyor; gerçek view desteklemiyorsa graceful degrade."""
        try:
            if hasattr(self._v, "set_ber_value"):
                self._gui(self._v.set_ber_value, float(ber))
                return
        except Exception:
            pass
        # Fallback
        self.set_ber(ber)

    def set_ber(self, ber: float):
        try:
            if hasattr(self._v, "set_ber_value"):
                self._gui(self._v.set_ber_value, float(ber))
            elif hasattr(self._v, "set_ber_text"):
                self._gui(self._v.set_ber_text, f"BER: {float(ber):.3e}")
        except Exception:
            pass

    def set_ber_text(self, text: str):
        try:
            self._gui(self._v.set_ber_text, text)
        except Exception:
            pass

    def set_final_ber(self, ber: float): self.set_ber(ber)
    def show_ber(self, text: str): self.set_ber_text(text)
    def show_final_ber(self, text: str): self.set_ber_text(text)

    # ---------- ETA (view'de yoksa sessiz) ----------
    def set_eta_text(self, text: str):
        try:
            if hasattr(self._v, "set_eta_text"):
                self._gui(self._v.set_eta_text, text)
        except Exception:
            pass

    def set_eta(self, text: str): self.set_eta_text(text)

    # ---------- UDP Stats (alternatif yol) ----------
    def set_udp_stats(self, **k):
        try:
            p = getattr(self._v, "udpStatsPanel", None)
            if p and hasattr(p, "set_kpis"):
                self._gui(
                    p.set_kpis,
                    pkts=k.get("pkts") or k.get("packets"),
                    bytes=k.get("bytes") or k.get("size"),
                    rate=k.get("rate") or k.get("throughput"),
                    queue=k.get("queue") or k.get("q"),
                    drops=k.get("drops") or k.get("loss"),
                    flush=k.get("flush") or k.get("file_flush"),
                )
        except Exception:
            pass

    def update_udp_stats(self, **k): self.set_udp_stats(**k)

    # ---------- FHSS görselleri ----------
    def set_fhss_active_index(self, idx: int):
        try:
            self._gui(self._v.set_fhss_active_index, int(idx))
        except Exception:
            pass

    # ---------- Buton/etkileşim ----------
    def set_start_enabled(self, enabled: bool):
        try:
            self._gui(self._v.set_start_enabled, bool(enabled))
        except Exception:
            pass

    def set_input_path(self, s: str):
        try:
            self._gui(self._v.set_input_path, s)
        except Exception:
            pass

    # ---------- Getter'lar ----------
    def input_path(self):
        try:
            return self._v.input_path()
        except Exception:
            return ""

    def file_type(self):
        try:
            return self._v.file_type()
        except Exception:
            return "bin"

    def rs_r(self):
        try:
            return self._v.rs_r()
        except Exception:
            return 16

    def rs_r_value(self):
        return self.rs_r()

    def rs_d(self):
        try:
            return self._v.rs_d()
        except Exception:
            return 32

    def rs_s(self):
        try:
            return self._v.rs_s()
        except Exception:
            return 1024

    def pad_mode(self):
        try:
            return self._v.pad_mode()
        except Exception:
            return 0  # ZERO (RxController varsayılanıyla uyumlu)

    def center_hz(self):
        try:
            return self._v.center_hz()
        except Exception:
            return 2.4e9

    def samp_rate(self):
        try:
            return self._v.samp_rate()
        except Exception:
            return 2e6

    def rf_bw(self):
        try:
            return self._v.rf_bw()
        except Exception:
            return 2e6

    def buffer_size(self):
        try:
            return self._v.buffer_size()
        except Exception:
            return 32768

    def gain_mode(self):
        try:
            return self._v.gain_mode()
        except Exception:
            return "slow"

    def gain_value(self):
        try:
            return self._v.gain_value()
        except Exception:
            return None

    def modulation(self):
        try:
            return self._v.modulation()
        except Exception:
            return "qpsk"


# ---- REBOOT yardımcıları ----
def _send_cmd_drain_then_close(host: str, port: int, line: str,
                               drain_ms: int = 800, grace_ms: int = 800) -> bool:
    """
    REPL güvenilir tek-atış: kısa banner/prompt drain → komutu gönder → SHUT_WR → kısa bekleme.
    """
    try:
        with socket.create_connection((host, port), timeout=1.6) as s:
            try:
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except Exception:
                pass

            # 1) Drain (banner/prompt vb.)
            end = time.monotonic() + (drain_ms / 1000.0)
            try:
                s.settimeout(0.12)
            except Exception:
                pass
            while time.monotonic() < end:
                try:
                    data = s.recv(4096)
                    if not data:
                        break
                except socket.timeout:
                    time.sleep(0.02)
                except Exception:
                    break

            # 2) Komutu gönder
            if not line.endswith("\n"):
                line = line + "\n"
            s.sendall(line.encode("ascii", "ignore"))

            # 3) Half-close & kısa bekleme
            try:
                s.shutdown(socket.SHUT_WR)
            except Exception:
                pass

            end2 = time.monotonic() + (grace_ms / 1000.0)
            while time.monotonic() < end2:
                try:
                    data = s.recv(4096)
                    if not data:
                        break
                except socket.timeout:
                    time.sleep(0.02)
                except Exception:
                    break
        return True
    except Exception:
        return False


def _reboot_one_shot_via_exe(exe_path: Optional[str] = None,
                             host: str = "192.168.2.1",
                             port: int = 80) -> bool:
    """
    C++ köprüyü --reboot-once ile çalıştırır; diğer akışları etkilemez.
    Bağımsız process olarak (job/grup dışı) ve pencere açmadan çalıştırır.
    """
    try:
        if exe_path is None:
            exe_path = _default_exe()
        if not exe_path or not os.path.isfile(exe_path):
            exe_path = shutil.which("pluto_udp_cmdd_bridge.exe") or exe_path
        if not exe_path or not os.path.isfile(exe_path):
            return False

        creationflags = 0
        if os.name == "nt":
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            CREATE_NO_WINDOW = 0x08000000
            DETACHED_PROCESS = 0x00000008
            creationflags = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW | DETACHED_PROCESS

        args = [exe_path, "--host", host, "--port", str(port), "--reboot-once"]

        p = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags
        )
        try:
            p.wait(timeout=4.0)
        except Exception:
            # one-shot zaten bitecektir; bekleyemesek de sorun değil
            pass
        return True
    except Exception:
        return False


class RxFhssController(QObject):
    _FHSS_DELAY_S = 4.2
    _PLUTO_HOST = "192.168.2.1"
    _PLUTO_PORT = 80

    def __init__(self, view=None, parent=None):
        super().__init__(parent)

        self.v = _unwrap_view(view)

        # Runners
        self._cmdd = PlutoCmddRunner()
        self._jd = JammerDetectionRunner()

        # Debounce for manual reboot
        self._reboot_busy = False
        self._reboot_last_ts = 0.0
        self._reboot_throttle_ms = 1200  # 1.2s

        # RxController + Proxy (baştan PROXY ile)
        self._rxc: Optional[RxController] = None
        self._rx_proxy: Optional[_RxViewProxy] = None
        if self.v is not None:
            try:
                self._rx_proxy = _RxViewProxy(self.v)
                self._rxc = RxController(self._rx_proxy)
            except Exception as e:
                print(f"[ERR] RxFhssController: RxController init failed: {e}")
                self._rxc = None

        # Pending cfg/state
        self._pending_cmdd_cfg: Optional[PlutoCmddConfig] = None
        self._pending_jd_cfg: Optional[JammerDetectConfig] = None

        self._session_id = 0
        self._active_session = 0

        # FHSS visuals
        self._fhss_timer: Optional[QTimer] = None
        self._fhss_anchor_s: Optional[float] = None
        self._fhss_armed = False

        # RX gecikme zamanlayıcısı
        self._rx_delay_timer: Optional[QTimer] = None

        # --- Post-process log watcher (post_process_log.txt) ---
        self._pp_timer: Optional[QTimer] = None
        self._pp_path: Optional[Path] = None
        self._pp_last_pos: int = 0
        self._pp_last_ber: Optional[float] = None
        self._pp_re_ber = re.compile(r'BER[≈~=]?\s*([0-9]*\.?[0-9]+(?:e[-+]\d+)?)', re.IGNORECASE)

        # JD tetik regexleri
        self._re_detect_en = re.compile(r"\bjammer\b.*\bdetected\b", re.IGNORECASE)
        self._re_detect_tr = re.compile(r"\bJammer\b.*\bbulund[uı]\b", re.IGNORECASE)

        # Runner eventleri
        self._cmdd.started.connect(lambda pid: self._log(f"[FHSS] pluto_cmdd started (pid={pid})"))
        self._cmdd.stopped.connect(
            lambda code, why: self._log(f"[FHSS] pluto_cmdd stopped (exit={code}, reason={why})"), Qt.QueuedConnection
        )

        self._jd.started.connect(lambda pid: self._log(f"[JD] jammer_detect started (pid={pid})"))
        self._jd.stopped.connect(
            lambda code, why: self._log(f"[JD] jammer_detect stopped (exit={code}, reason={why})"), Qt.QueuedConnection
        )
        self._jd.log_line.connect(self._safe_append_log, Qt.QueuedConnection)
        self._jd.log_line.connect(self._on_jd_logline, Qt.QueuedConnection)
        self._jd.warn.connect(self._safe_append_log, Qt.QueuedConnection)
        self._jd.error.connect(self._safe_append_log, Qt.QueuedConnection)
        self._jd.info_pluto_config.connect(
            lambda d: self._log(
                f"[JD] Pluto cfg uri={d['uri']} f={d['freq']} sr={d['samp']} bw={d['rfbw']} gain={d['gain']} frame={d['frame']}"
            ),
            Qt.QueuedConnection,
        )
        self._jd.calibration.connect(
            lambda d: self._log(
                f"[JD] Calibrated: thr={d['threshold_dbm']:.2f} dBm, clean={d['clean']}, "
                f"rx={d['mean_rx_ms']:.2f} ms, frame={d['mean_frame_ms']:.2f} ms, n={d['frames_used']}"
            ),
            Qt.QueuedConnection,
        )
        self._jd.detected.connect(self._on_jd_detected_signal, Qt.QueuedConnection)

        # View sinyalleri (gerekirse Back vb.)
        self._connect_view_signals()

    # ---- sinyal bağlama helper'ı (idempotent) ----
    def _safe_connect(self, sender_signal, slot):
        try:
            sender_signal.disconnect()  # tüm eski bağları kes
        except Exception:
            pass
        try:
            sender_signal.connect(slot)
        except Exception:
            pass

    # ---- dışarıdan view bağlamak için
    def bind_view(self, view):
        self.v = _unwrap_view(view)
        try:
            self._rx_proxy = _RxViewProxy(self.v)
            self._rxc = RxController(self._rx_proxy)
        except Exception as e:
            print(f"[ERR] RxFhssController.bind_view: RxController init failed: {e}")
            self._rxc = None
        self._connect_view_signals()

    def _connect_view_signals(self):
        if not self.v:
            return
        for sig_name, slot in (
            ("sig_start", self._on_start_clicked),
            ("sig_stop", self._on_stop_clicked),
            ("sig_clear", self._on_clear_clicked),
            ("sig_reboot", self._on_reboot_clicked),  # manuel reboot
        ):
            try:
                sig = getattr(self.v, sig_name, None)
                if sig is None:
                    continue
                self._safe_connect(sig, slot)
            except Exception:
                pass
        # olası düğme (opsiyonel)
        try:
            if hasattr(self.v, "rebootButton") and self.v.rebootButton:
                try:
                    self.v.rebootButton.clicked.disconnect()
                except Exception:
                    pass
                self.v.rebootButton.clicked.connect(self._on_reboot_clicked)
        except Exception:
            pass

    # --------------- helpers ---------------
    def _safe_append_log(self, s: str):
        try:
            if self.v is not None:
                self.v.append_log(s)
        except Exception:
            pass

    def _log(self, s: str):
        self._safe_append_log(s)

    def is_running(self) -> bool:
        return self._cmdd.is_running() or self._jd.is_running()

    def _arm_once(self) -> bool:
        if self._fhss_armed:
            return False
        self._fhss_armed = True
        return True

    def _cancel_rx_delay_timer(self):
        if self._rx_delay_timer is not None:
            try:
                if self._rx_delay_timer.isActive():
                    self._rx_delay_timer.stop()
            except Exception:
                pass
            self._rx_delay_timer.deleteLater()
            self._rx_delay_timer = None

    # --- Post-process watcher helpers ---
    def _start_postprocess_watch(self):
        try:
            dir_path = (self.v.input_path() or "").strip()
        except Exception:
            dir_path = ""
        if not dir_path:
            return
        self._pp_path = Path(dir_path) / "post_process_log.txt"
        self._pp_last_pos = 0
        self._pp_last_ber = None

        if self._pp_timer is None:
            self._pp_timer = QTimer(self)
            self._pp_timer.setInterval(400)   # 0.4s
            self._pp_timer.setSingleShot(False)
            self._pp_timer.timeout.connect(self._pp_tick, Qt.QueuedConnection)
        if not self._pp_timer.isActive():
            self._pp_timer.start()

    def _stop_postprocess_watch(self):
        if self._pp_timer is not None:
            try:
                if self._pp_timer.isActive():
                    self._pp_timer.stop()
            except Exception:
                pass
        self._pp_last_pos = 0
        self._pp_last_ber = None

    def _pp_tick(self):
        p = self._pp_path
        if not p:
            return
        try:
            if not p.exists():
                return

            size = p.stat().st_size
            if self._pp_last_pos > size:
                self._pp_last_pos = 0

            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(self._pp_last_pos, 0)
                chunk = f.read()
                self._pp_last_pos = f.tell()

            if not chunk:
                return

            for raw in chunk.splitlines():
                line = (raw or "").strip()
                if not line:
                    continue

                # Post-process log satırlarını GUI loguna geçir
                try:
                    self._log(f"[PP] {line}")
                except Exception:
                    pass

                # BER varsa GUI'de güncelle
                try:
                    m = self._pp_re_ber.search(line)
                    if m:
                        val = float(m.group(1))
                        self._pp_last_ber = val
                        if hasattr(self.v, "set_ber_value"):
                            self.v.set_ber_value(val)
                        elif hasattr(self.v, "set_ber_text"):
                            self.v.set_ber_text(f"BER: {val:.3e}")
                except Exception:
                    pass

        except Exception:
            pass

    # --------------- UI slots ---------------
    @pyqtSlot()
    def _on_start_clicked(self):
        # klasör kontrolü
        try:
            dir_path = (self.v.input_path() or "").strip()
        except Exception:
            dir_path = ""
        if not dir_path or (not Path(dir_path).exists()):
            self._log("[ERR] No save folder selected (or path does not exist).")
            return

        # --- Eski post_process_log.txt temizle ---
        try:
            pp_path = Path(dir_path) / "post_process_log.txt"
            if pp_path.exists():
                pp_path.unlink()
                self._log("[FHSS] Old post_process_log.txt removed.")
        except Exception as e:
            self._log(f"[WARN] Could not remove old post_process_log.txt: {e}")

        if self.is_running():
            self._log("[INFO] Already running; stop first if you want to restart.")
            return

        self._session_id += 1
        self._active_session = self._session_id
        self._fhss_armed = False
        self._stop_fhss_cycle()
        self._cancel_rx_delay_timer()

        # --- Post-process watcher başlasın ---
        self._start_postprocess_watch()

        # Akışı sadeleştirdik: HTTP probe yok, direkt başlat
        self._pending_cmdd_cfg = PlutoCmddConfig(
            host=self._PLUTO_HOST,
            tcp_port=self._PLUTO_PORT,
            udp_port=6000,
            trigger="4",
            cmd="AUTORX_MODE ON",
            off_cmd="AUTORX_MODE OFF",
            jdx_on_value=4,
            jdx_autodetect=True,
            jdx_stop_off=True,
            udp_one_shot=True,
            delay_trigger_value=4,
            delay_ms=200,
        )
        self._pending_jd_cfg = JammerDetectConfig(
            uri=f"ip:{self._PLUTO_HOST}",
            freq_hz=2.402e9,
            samp_hz=4e6,
            rfbw_hz=4e6,
            gain_db=-20,
            frame_size=4096,
            new_console=False,
            create_new_process_group=True,
        )

        self._log("[FHSS] Starting pluto_cmdd (separate console)…")
        self._cmdd.start(self._pending_cmdd_cfg)

        # Pluto HTTP hazır/probe beklemek yerine doğrudan JD'yi başlat
        self._log("[FHSS] Starting jammer_detect…")
        self._start_jammer_detect_guarded()

    @pyqtSlot()
    def _on_stop_clicked(self):
        self._log("[INFO] Stop requested…")
        self._active_session = -1
        self._fhss_armed = False
        self._stop_fhss_cycle()
        self._cancel_rx_delay_timer()
        self._stop_postprocess_watch()

        # Start yeniden tıklanmasın
        try:
            if hasattr(self.v, "set_start_enabled"):
                self.v.set_start_enabled(False)
        except Exception:
            pass

        # 1) RX'i kapat
        try:
            if self._rxc and hasattr(self._rxc, "on_stop_clicked"):
                self._rxc.on_stop_clicked()
        except Exception:
            pass

        # 2) JD'yi kapat
        try:
            if self._jd.is_running():
                self._jd.stop()
        except Exception:
            pass

        # 3) AUTOTX/AUTORX OFF (best-effort)
        try:
            _best_effort_autotx_off(self._PLUTO_HOST, self._PLUTO_PORT)
            _best_effort_autorx_off(self._PLUTO_HOST, self._PLUTO_PORT)
            self._log("[FHSS] AUTOTX/AUTORX OFF sent (best-effort).")
        except Exception:
            pass

        # 4) CMDD'yi reboot ETMEDEN kapat
        try:
            if self._cmdd.is_running():
                self._log("[FHSS] Stopping pluto_cmdd (no reboot)…")
                self._cmdd.stop(reboot_first=False)
            else:
                self._log("[FHSS] pluto_cmdd already stopped.")
        except Exception:
            pass

        self._log("[FHSS] Stop sequence complete.")

        # Reboot debounce reset (edge-case temizlik)
        self._reboot_busy = False
        self._reboot_last_ts = 0.0

        # 5) 6 saniye sonra start butonunu serbest bırak
        t = QTimer(self)
        t.setSingleShot(True)

        def _reenable():
            try:
                if hasattr(self.v, "set_start_enabled"):
                    self.v.set_start_enabled(True)
            except Exception:
                pass
            t.deleteLater()

        t.timeout.connect(_reenable)
        t.start(6000)

    @pyqtSlot()
    def _on_clear_clicked(self):
        # Progress/time/BER temizle
        try:
            if hasattr(self.v, "set_overall_progress"):
                self.v.set_overall_progress(0)
            if hasattr(self.v, "set_time"):
                self.v.set_time("00:00:00")
            if hasattr(self.v, "set_ber_text"):
                self.v.set_ber_text("BER: —")
        except Exception:
            pass
        # UDP KPI reset (RxController._set_kpis_zero ile aynı format)
        try:
            p = getattr(self.v, "udpStatsPanel", None)
            if p and hasattr(p, "set_kpis"):
                p.set_kpis(
                    pkts="0",
                    bytes="0 (0.00 MB)",
                    rate="0.00 MB/s",
                    queue="0.00 / 8 MB",
                    drops="0",
                    flush="200 ms",
                )
        except Exception:
            pass
        # Log ve FHSS görsellerini temizle
        try:
            if hasattr(self.v, "clear_log"):
                self.v.clear_log()
        except Exception:
            pass
        try:
            if hasattr(self.v, "fhssPanel"):
                self.v.fhssPanel.cell1.set_active(False)
                self.v.fhssPanel.cell2.set_active(False)
        except Exception:
            pass
        # Post-process watcher'ı da kapat
        self._stop_postprocess_watch()

    # --------------- Orkestrasyon ---------------
    def _start_jammer_detect_guarded(self):
        if self._active_session != self._session_id:
            return
        if not self._pending_jd_cfg:
            self._log("[ERR] Internal: JD config missing.")
            return
        self._jd.start(self._pending_jd_cfg)

    # --- JD tetik ---
    @pyqtSlot(str)
    def _on_jd_logline(self, line: str):
        if self._active_session != self._session_id or self._fhss_armed:
            return
        txt = (line or "").strip()
        low = txt.lower()
        if self._re_detect_en.search(txt) or self._re_detect_tr.search(txt):
            if self._arm_once():
                self._log(f"[TRG] JD trigger → FHSS visuals in {self._FHSS_DELAY_S:.1f} s; RX will start after the same delay.")
                self._start_fhss_cycle_after_delay(self._FHSS_DELAY_S)
                self._schedule_rx_after_delay(self._FHSS_DELAY_S)
            return
        if ("rx kapatildi" in low) or ("context serbest birakildi" in low):
            if self._arm_once():
                self._log(f"[TRG] JD shutdown hints → FHSS visuals in {self._FHSS_DELAY_S:.1f} s; RX will start after the same delay.")
                self._start_fhss_cycle_after_delay(self._FHSS_DELAY_S)
                self._schedule_rx_after_delay(self._FHSS_DELAY_S)

    @pyqtSlot(int)
    def _on_jd_detected_signal(self, seq: int):
        if self._active_session != self._session_id:
            return
        if self._arm_once():
            self._log(f"[TRG] JD detected (seq={seq}) → FHSS visuals in {self._FHSS_DELAY_S:.1f} s; RX will start after the same delay.")
            self._start_fhss_cycle_after_delay(self._FHSS_DELAY_S)
            self._schedule_rx_after_delay(self._FHSS_DELAY_S)

    # --- FHSS görselleri ---
    def _start_fhss_cycle_after_delay(self, delay_s: float):
        self._cancel_fhss_timer()
        self._fhss_anchor_s = time.monotonic() + float(delay_s)
        t = QTimer(self)
        t.setSingleShot(True)
        t.timeout.connect(self._fhss_tick, Qt.QueuedConnection)
        self._fhss_timer = t
        self._schedule_next_fhss_timeout()

    def _fhss_tick(self):
        if self._fhss_anchor_s is None or self._active_session != self._session_id:
            return
        now = time.monotonic()
        elapsed = now - self._fhss_anchor_s
        if elapsed < 0:
            self._schedule_next_fhss_timeout()
            return
        phase = elapsed % 5.0
        idx = 1 if phase < 3.0 else 0
        try:
            self.v.set_fhss_active_index(idx)
        except Exception:
            pass
        next_boundary = 3.0 if phase < 3.0 else 5.0
        delay_s = max(0.001, next_boundary - phase)
        self._schedule_next_fhss_timeout(delay_override_ms=int(delay_s * 1000))

    def _schedule_next_fhss_timeout(self, delay_override_ms: Optional[int] = None):
        if not self._fhss_timer or self._fhss_anchor_s is None:
            return
        if delay_override_ms is None:
            now = time.monotonic()
            ms = max(1, int((self._fhss_anchor_s - now) * 1000))
        else:
            ms = max(1, delay_override_ms)
        try:
            if self._fhss_timer.isActive():
                self._fhss_timer.stop()
        except Exception:
            pass
        self._fhss_timer.start(ms)

    def _cancel_fhss_timer(self):
        if self._fhss_timer is not None:
            try:
                if self._fhss_timer.isActive():
                    self._fhss_timer.stop()
            except Exception:
                pass
            self._fhss_timer.deleteLater()
            self._fhss_timer = None

    def _stop_fhss_cycle(self):
        self._fhss_anchor_s = None
        self._cancel_fhss_timer()

    # --- RX’i gecikmeyle başlat ---
    def _schedule_rx_after_delay(self, delay_s: float):
        self._cancel_rx_delay_timer()
        t = QTimer(self)
        t.setSingleShot(True)
        t.timeout.connect(self._handover_to_rx, Qt.QueuedConnection)
        self._rx_delay_timer = t
        self._rx_delay_timer.start(max(1, int(delay_s * 1000)))

    # --- RX devri ---
    def _handover_to_rx(self):
        self._log("[RX] Stopping JD before RX (sequential handover).")
        try:
            if self._jd.is_running():
                self._jd.stop()
        except Exception:
            pass

        try:
            dir_path = (self.v.input_path() or "").strip()
        except Exception:
            dir_path = ""
        if not dir_path or (not Path(dir_path).exists()):
            self._log("[ERR] No save folder for RX; skipping.")
            return

        if not self._rxc:
            self._log("[ERR] RxController not ready; cannot start RX.")
            return

        try:
            # RX'i sadece burada başlatıyoruz
            self._rxc.on_start_clicked()
        except Exception as e:
            self._log(f"[ERR] Could not handover to RxController: {e}")

    # --- MANUEL REBOOT: C++ one-shot çağrısı + güçlü fallback + debounce ---
    @pyqtSlot()
    def _on_reboot_clicked(self):
        # Debounce (1.2s)
        if self._reboot_busy:
            return
        now = time.monotonic()
        if (now - self._reboot_last_ts) * 1000.0 < self._reboot_throttle_ms:
            return
        self._reboot_busy = True
        self._reboot_last_ts = now

        self._log("[FHSS] Manual REBOOT: using bridge --reboot-once…")
        try:
            ok = _reboot_one_shot_via_exe(None, self._PLUTO_HOST, self._PLUTO_PORT)
            if not ok:
                # Daha sağlam fallback: drenajlı tek-atış
                if not _send_cmd_drain_then_close(self._PLUTO_HOST, self._PLUTO_PORT, "REBOOT"):
                    # En son çare: sessiz kısa yol
                    _best_effort_send_cmd(self._PLUTO_HOST, self._PLUTO_PORT, "REBOOT")
        except Exception:
            pass
        finally:
            t = QTimer(self)
            t.setSingleShot(True)

            def _clr():
                self._reboot_busy = False
                t.deleteLater()

            t.timeout.connect(_clr)
            t.start(self._reboot_throttle_ms)

    # (isteğe bağlı dış API) — view doğrudan çağırmak isterse
    def reboot(self):
        self._on_reboot_clicked()
