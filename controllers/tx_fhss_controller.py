# -*- coding: utf-8 -*-
"""
controllers/tx_fhss_controller.py

Akış:
  1) Start: dosya kontrolü (zorunlu)
  2) pluto_cmdd → hazır probe
  3) jammer_detect → tetik bekle
  4) Tetik: 4.2 s sonra FHSS görselleri BAŞLAR ve aynı anda TX zamanlanır
  5) 4.2s dolunca: JD durdur, sonra TxController ile RS→Bitwrap→TX

Stop:
  - AUTOTX_MODE OFF (best-effort)
  - Çalışanları sırayla kapat: JD, CMDD
  - TX'i de durdur: tx_controller.stop_tx()
  - Görsel / timer iptalleri, progress/time/FHSS reset'i Clear'da da mevcut.

Notlar:
  - tx_controller'a dokunmuyoruz; view'e proxy ile bağlanıyoruz.
  - Clear: progress=0, süre=00:00:00, FHSS hücreleri pasif.
"""

import re
import socket
import time
from pathlib import Path
from typing import Optional

from PyQt5.QtCore import QObject, QTimer, pyqtSlot, Qt

from scripts.pluto_cmdd_runner import PlutoCmddRunner, PlutoCmddConfig, _best_effort_autotx_off
from scripts.jammer_detection_runner import JammerDetectionRunner, JammerDetectConfig
from controllers.tx_controller import TxController


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


# -------- TxController için View Proxy (forward) --------
class _TxViewProxy:
    def __init__(self, v):
        self._v = _unwrap_view(v)

    # Log & progress
    def append_log(self, s: str):
        try: self._v.append_log(s)
        except Exception: pass

    def clear_log(self):
        try: self._v.clear_log()
        except Exception: pass

    def set_time(self, t: str):
        try: self._v.set_time(t)
        except Exception: pass

    def on_total_progress(self, pct: int):
        try: self._v.set_overall_progress(pct)
        except Exception: pass

    def set_load_text(self, text: str):
        try: self._v.set_load_text(text)
        except Exception: pass

    # Param getterlar
    def input_path(self):  return self._v.input_path()
    def theta(self):       return self._v.theta()
    def rs_r(self):        return self._v.rs_r()
    def rs_d(self):        return self._v.rs_d()
    def rs_s(self):        return self._v.rs_s()
    def center_hz(self):   return self._v.center_hz()
    def samp_rate(self):   return self._v.samp_rate()
    def rf_bw(self):       return self._v.rf_bw()
    def atten_db(self):    return self._v.atten_db()
    def buffer_size(self): return self._v.buffer_size()
    def amp(self):         return self._v.amp()
    def pkt_size(self):    return self._v.pkt_size()
    def modulation(self):  return self._v.modulation()

    # Opsiyonel embed
    def add_freq_widget(self, w):
        try:
            if hasattr(self._v, "add_freq_widget"):
                self._v.add_freq_widget(w)
        except Exception:
            pass


class TxFhssController(QObject):
    _FHSS_DELAY_S = 4.2  # görseller + TX zamanlayıcı gecikmesi

    def __init__(self, view=None, parent=None):
        super().__init__(parent)

        # View (unwrap)
        self.v = _unwrap_view(view)

        # Runners
        self._cmdd = PlutoCmddRunner()
        self._jd   = JammerDetectionRunner()

        # TxController + proxy
        self._txc = TxController(mode="inproc")
        self._txc_proxy = _TxViewProxy(self.v) if self.v is not None else None
        if self._txc_proxy is not None:
            try:
                self._txc.bind_view(self._txc_proxy)
            except Exception:
                pass

        # state
        self._pending_cmdd_cfg: Optional[PlutoCmddConfig] = None
        self._pending_jd_cfg:   Optional[JammerDetectConfig] = None

        self._probe_timer = QTimer(self)
        self._probe_timer.setSingleShot(True)
        self._probe_timer.timeout.connect(self._probe_cmdd_ready_tick)
        self._probe_attempts_left = 0
        self._session_id = 0
        self._active_session = 0

        # FHSS görsel döngüsü
        self._fhss_timer: Optional[QTimer] = None
        self._fhss_anchor_s: Optional[float] = None
        self._fhss_armed = False

        # TX gecikme zamanlayıcısı (4.4 s sonra TX başlasın)
        self._tx_delay_timer: Optional[QTimer] = None

        # JD tetik regexleri
        self._re_detect_en = re.compile(r"\bjammer\b.*\bdetected\b", re.IGNORECASE)
        self._re_detect_tr = re.compile(r"\bJammer\b.*\bbulund[uı]\b", re.IGNORECASE)

        # Runner eventleri
        self._cmdd.started.connect(lambda pid: self._log(f"[FHSS] pluto_cmdd started (pid={pid})"))
        self._cmdd.stopped.connect(lambda code, why: self._log(f"[FHSS] pluto_cmdd stopped (exit={code}, reason={why})"), Qt.QueuedConnection)

        self._jd.started.connect(lambda pid: self._log(f"[JD] jammer_detect started (pid={pid})"))
        self._jd.stopped.connect(lambda code, why: self._log(f"[JD] jammer_detect stopped (exit={code}, reason={why})"), Qt.QueuedConnection)
        self._jd.log_line.connect(self._safe_append_log, Qt.QueuedConnection)
        self._jd.log_line.connect(self._on_jd_logline, Qt.QueuedConnection)
        self._jd.warn.connect(self._safe_append_log, Qt.QueuedConnection)
        self._jd.error.connect(self._safe_append_log, Qt.QueuedConnection)
        self._jd.info_pluto_config.connect(
            lambda d: self._log(f"[JD] Pluto cfg uri={d['uri']} f={d['freq']} sr={d['samp']} bw={d['rfbw']} gain={d['gain']} frame={d['frame']}"),
            Qt.QueuedConnection
        )
        self._jd.calibration.connect(
            lambda d: self._log(f"[JD] Calibrated: thr={d['threshold_dbm']:.2f} dBm, clean={d['clean']}, rx={d['mean_rx_ms']:.2f} ms, frame={d['mean_frame_ms']:.2f} ms, n={d['frames_used']}"),
            Qt.QueuedConnection
        )
        self._jd.detected.connect(self._on_jd_detected_signal, Qt.QueuedConnection)

        # View sinyalleri
        self._connect_view_signals()

    # ---- dışarıdan view bağlamak için
    def bind_view(self, view):
        self.v = _unwrap_view(view)
        self._txc_proxy = _TxViewProxy(self.v)
        try:
            self._txc.bind_view(self._txc_proxy)
        except Exception:
            pass
        self._connect_view_signals()

    def _connect_view_signals(self):
        if not self.v:
            return
        for sig_name, slot in (("sig_start", self._on_start_clicked),
                               ("sig_stop",  self._on_stop_clicked),
                               ("sig_clear", self._on_clear_clicked)):
            try:
                sig = getattr(self.v, sig_name, None)
                if sig is None:
                    continue
                try:
                    sig.disconnect(slot)
                except Exception:
                    pass
                sig.connect(slot)
            except Exception:
                pass

    # --------------- helpers ---------------
    def _safe_append_log(self, s: str):
        try:
            if self.v is not None:
                self.v.append_log(s)
        except Exception:
            pass

    def _log(self, s: str): self._safe_append_log(s)

    def is_running(self) -> bool:
        return self._cmdd.is_running() or self._jd.is_running()

    def _arm_once(self) -> bool:
        if self._fhss_armed:
            return False
        self._fhss_armed = True
        return True

    def _cancel_tx_delay_timer(self):
        if self._tx_delay_timer is not None:
            try:
                if self._tx_delay_timer.isActive():
                    self._tx_delay_timer.stop()
            except Exception:
                pass
            self._tx_delay_timer.deleteLater()
            self._tx_delay_timer = None

    # --------------- UI slots ---------------
    @pyqtSlot()
    def _on_start_clicked(self):
        # (1) Dosya kontrolü
        in_path = ""
        try:
            in_path = (self.v.input_path() or "").strip()
        except Exception:
            in_path = ""
        if not in_path or not Path(in_path).exists():
            self._log("[ERR] No file selected (or path does not exist). Please choose a file before Start.")
            return

        if self.is_running():
            self._log("[INFO] Already running; stop first if you want to restart.")
            return

        # oturum
        self._session_id += 1
        self._active_session = self._session_id
        self._fhss_armed = False
        self._stop_fhss_cycle()
        self._cancel_tx_delay_timer()

        # config'ler
        self._pending_cmdd_cfg = PlutoCmddConfig(
            host="192.168.2.1", tcp_port=80, udp_port=6000,
            trigger="4", cmd="AUTOTX_MODE ON", off_cmd="AUTOTX_MODE OFF",
            jdx_on_value=4, jdx_autodetect=True, jdx_stop_off=True,
            udp_one_shot=True, delay_trigger_value=4, delay_ms=200
        )
        self._pending_jd_cfg = JammerDetectConfig(
            uri="ip:192.168.2.1", freq_hz=2.402e9, samp_hz=4e6, rfbw_hz=4e6,
            gain_db=-20, frame_size=4096, new_console=False, create_new_process_group=True
        )

        # başlat
        self._log("[FHSS] Starting pluto_cmdd (separate console)…")
        self._cmdd.start(self._pending_cmdd_cfg)

        # probe
        self._probe_attempts_left = 5
        self._arm_probe(250)

    @pyqtSlot()
    def _on_stop_clicked(self):
        self._log("[INFO] Stop requested…")
        self._active_session = -1
        self._fhss_armed = False
        self._stop_fhss_cycle()
        self._cancel_tx_delay_timer()

        try:
            if self._probe_timer.isActive():
                self._probe_timer.stop()
        except Exception:
            pass

        # Pluto OFF (best-effort)
        try:
            host = (self._pending_cmdd_cfg.host if self._pending_cmdd_cfg else "192.168.2.1")
            port = (self._pending_cmdd_cfg.tcp_port if self._pending_cmdd_cfg else 80)
            _best_effort_autotx_off(host, port)
            self._log("[FHSS] AUTOTX_MODE OFF sent (best-effort).")
        except Exception:
            pass

        # Runnerları durdur
        if self._jd.is_running():
            self._jd.stop()
        if self._cmdd.is_running():
            self._cmdd.stop()

        # (2) TX'i de durdur
        try:
            if hasattr(self._txc, "stop_tx"):
                self._txc.stop_tx()
        except Exception:
            pass

    @pyqtSlot()
    def _on_clear_clicked(self):
        # progress & zaman reset
        try:
            if hasattr(self.v, "set_overall_progress"):
                self.v.set_overall_progress(0)
            if hasattr(self.v, "set_time"):
                self.v.set_time("00:00:00")
        except Exception:
            pass

        try:
            if hasattr(self.v, "clear_log"):
                self.v.clear_log()
        except Exception:
            pass

        # FHSS hücrelerini pasif yap
        try:
            if hasattr(self.v, "fhssPanel"):
                self.v.fhssPanel.cell1.set_active(False)
                self.v.fhssPanel.cell2.set_active(False)
        except Exception:
            pass

    # --------------- Orkestrasyon ---------------
    def _arm_probe(self, ms: int):
        try:
            if self._probe_timer.isActive():
                self._probe_timer.stop()
        except Exception:
            pass
        self._probe_timer.start(ms)

    def _probe_cmdd_ready_tick(self):
        if self._active_session != self._session_id:
            return
        if not self._cmdd.is_running():
            self._log("[FHSS] pluto_cmdd is not running; cannot continue.")
            return

        host = self._pending_cmdd_cfg.host if self._pending_cmdd_cfg else "192.168.2.1"
        port = self._pending_cmdd_cfg.tcp_port if self._pending_cmdd_cfg else 80
        ready = self._tcp_probe(host, port, 0.2)

        if ready:
            self._log("[FHSS] pluto_cmdd ready. Starting jammer_detect immediately…")
            self._start_jammer_detect_guarded()
        else:
            self._probe_attempts_left -= 1
            if self._probe_attempts_left <= 0:
                self._log("[FHSS] TCP probe failed, but pluto_cmdd is running; starting jammer_detect immediately.")
                self._start_jammer_detect_guarded()
            else:
                self._arm_probe(250)

    def _start_jammer_detect_guarded(self):
        if self._active_session != self._session_id:
            return
        if not self._pending_jd_cfg:
            self._log("[ERR] Internal: JD config missing.")
            return
        self._log("[FHSS] Starting jammer_detect…")
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
                self._log(f"[TRG] JD trigger → FHSS visuals in {self._FHSS_DELAY_S:.1f} s; TX will start after the same delay.")
                self._start_fhss_cycle_after_delay(self._FHSS_DELAY_S)
                self._schedule_tx_after_delay(self._FHSS_DELAY_S)
            return
        if ("rx kapatildi" in low) or ("context serbest birakildi" in low):
            if self._arm_once():
                self._log(f"[TRG] JD shutdown hints → FHSS visuals in {self._FHSS_DELAY_S:.1f} s; TX will start after the same delay.")
                self._start_fhss_cycle_after_delay(self._FHSS_DELAY_S)
                self._schedule_tx_after_delay(self._FHSS_DELAY_S)

    @pyqtSlot(int)
    def _on_jd_detected_signal(self, seq: int):
        if self._active_session != self._session_id:
            return
        if self._arm_once():
            self._log(f"[TRG] JD detected signal (seq={seq}) → FHSS visuals in {self._FHSS_DELAY_S:.1f} s; TX will start after the same delay.")
            self._start_fhss_cycle_after_delay(self._FHSS_DELAY_S)
            self._schedule_tx_after_delay(self._FHSS_DELAY_S)

    # --- FHSS görselleri ---
    def _start_fhss_cycle_after_delay(self, delay_s: float):
        self._cancel_fhss_timer()
        self._fhss_anchor_s = time.monotonic() + float(delay_s)
        t = QTimer(self); t.setSingleShot(True)
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
        # Faz: 0–3 sn idx=1 (2.416), 3–5 sn idx=0 (2.404)
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

    # --- TX'i gecikmeyle başlat ---
    def _schedule_tx_after_delay(self, delay_s: float):
        self._cancel_tx_delay_timer()
        t = QTimer(self)
        t.setSingleShot(True)
        def _fire():
            # JD’yi TX’ten hemen önce durdur, sonra TX'e devir
            self._handover_to_tx()
        t.timeout.connect(_fire, Qt.QueuedConnection)
        self._tx_delay_timer = t
        self._tx_delay_timer.start(max(1, int(delay_s * 1000)))

    # --- TX devri (hemen/veya gecikmeli tetiklenen) ---
    def _handover_to_tx(self):
        self._log("[TX] Stopping JD before TX (sequential handover).")
        try:
            if self._jd.is_running():
                self._jd.stop()
        except Exception:
            pass

        in_path = ""
        try:
            in_path = (self.v.input_path() or "").strip()
        except Exception:
            in_path = ""
        if not in_path or not Path(in_path).exists():
            self._log("[ERR] No file selected for TX; skipping pipeline.")
            return

        try:
            if hasattr(self._txc, "_on_send_clicked"):
                self._txc._on_send_clicked()
            else:
                self._txc.run_pipeline_and_tx(
                    input_path=in_path,
                    r=self.v.rs_r(), interleave_depth=self.v.rs_d(),
                    slice_bytes=self.v.rs_s(), theta=self.v.theta(),
                    center=self.v.center_hz(), samp=self.v.samp_rate(),
                    rfbw=self.v.rf_bw(), atten=self.v.atten_db(),
                    buffer=self.v.buffer_size(), amp=self.v.amp(),
                    pkt=self.v.pkt_size(), mod=self.v.modulation(),
                )
        except Exception as e:
            self._log(f"[ERR] Could not handover to TxController: {e}")

    # --- utils ---
    def _tcp_probe(self, host: str, port: int, timeout: float) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except Exception:
            return False
