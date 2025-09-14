# -*- coding: utf-8 -*-
"""
services/ofdm_tx_inproc.py

Purpose:
- Run GNURadio ofdmtransmit top_block inside the GUI process (in-proc) in a QThread
- Embed qtgui.freq_sink_c widget into the GUI (emitted via sig_freq_widget)
- Parameter schema is 100% identical to subproc: center, samp, rfbw, atten, buffer, amp, pkt, roll, mod
"""

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PyQt5.QtCore import QThread, pyqtSignal, QMetaObject, Qt as QtConst

# --- Prefer portable scripts dir via paths.py; fallback to repo layout ---
try:
    import paths
    SCRIPTS_DIR = Path(paths.dir_scripts())
except Exception:
    PROJ_ROOT = Path(__file__).resolve().parents[1]  # repo: services/ → ../
    SCRIPTS_DIR = PROJ_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

try:
    # Class name in scripts/ofdmtransmitter.py: ofdmtransmit
    from ofdmtransmitter import ofdmtransmit
except Exception as e:
    raise RuntimeError(f"[ofdm_tx_inproc] ofdmtransmitter import error: {e}")

# --- Canonical TxConfig schema (same as subproc) ---
try:
    from services.ofdm_tx_subproc import TxConfig  # type: ignore
except Exception:
    @dataclass
    class TxConfig:
        bitwrap_path: str
        center: float = 2.4e9
        samp: float = 2e6
        rfbw: Optional[float] = None
        atten: float = 10.0
        buffer: int = 32768
        amp: float = 0.03
        pkt: int = 512
        roll: int = 0
        mod: str = "qpsk"  # 'bpsk' | 'qpsk' | '16qam'


class TxWorker(QThread):
    """Runs GNURadio ofdmtransmit top_block inside a QThread (in-proc)."""
    sig_started = pyqtSignal()
    sig_stopped = pyqtSignal()
    sig_freq_widget = pyqtSignal(object)   # PyQt5 QWidget (qtgui.freq_sink_c)
    sig_log = pyqtSignal(str)

    def __init__(self, cfg: TxConfig):
        super().__init__()
        self.cfg = cfg
        self.tb: Optional[ofdmtransmit] = None
        self._freq_widget = None
        self._pending = None  # queued call carrier

    # ---------- QThread API ----------
    def run(self):
        try:
            # Normalize params
            rf_bw = self.cfg.rfbw if (self.cfg.rfbw is not None) else self.cfg.samp
            amp = float(self.cfg.amp)
            if amp != amp:  # NaN guard
                amp = 0.03
            amp = max(0.0, min(1.0, amp))  # clamp to 0..1

            bw_path = Path(str(self.cfg.bitwrap_path)).resolve()
            if not bw_path.exists():
                self.sig_log.emit(f"[TX-INPROC] ERROR: .bitwrap not found: {bw_path}")
                return

            # Log params
            self.sig_log.emit(
                "[TX-INPROC] Params: "
                f"center={self.cfg.center} samp={self.cfg.samp} rfbw={rf_bw} "
                f"atten={self.cfg.atten} buffer={self.cfg.buffer} pkt={self.cfg.pkt} "
                f"mod={self.cfg.mod} amp={amp} path='{bw_path}'"
            )

            # Create GNURadio top_block
            self.tb = ofdmtransmit(
                rolloff=int(self.cfg.roll),                # CP (samples)
                samp_rate=int(self.cfg.samp),
                center_freq=int(self.cfg.center),
                rf_bw=int(rf_bw),
                tx_atten_db=float(self.cfg.atten),
                buffer_size=int(self.cfg.buffer),
                amp=float(amp),
                packet_len=int(self.cfg.pkt),
                input_path=str(bw_path),
                modulation=str(self.cfg.mod).lower(),
                embed_widget=False,  # in-proc: don't open its own window
            )

            # Send freq sink widget back to GUI
            try:
                self._freq_widget = self.tb.qtgui_widget()
                if self._freq_widget is not None:
                    self.sig_freq_widget.emit(self._freq_widget)
            except Exception as e:
                self.sig_log.emit(f"[TX-INPROC] Could not get qtgui widget: {e}")

            # Start
            self.tb.start()
            self.sig_started.emit()
            self.sig_log.emit("[TX-INPROC] GNURadio started.")

            # Thread event loop (until stop() is called)
            self.exec_()

        except Exception as e:
            self.sig_log.emit(f"[TX-INPROC] FATAL: {e}")
        finally:
            # Ensure clean shutdown
            try:
                if self.tb:
                    self.tb.stop()
                    self.tb.wait()
            except Exception:
                pass
            self.sig_stopped.emit()
            self.sig_log.emit("[TX-INPROC] Stopped.")

    def stop(self):
        """Graceful stop from outside."""
        try:
            if self.tb:
                self.tb.stop()
                self.tb.wait()
        except Exception:
            pass
        self.quit()

    # ---------- Runtime parameter changes (queued) ----------
    def set_center(self, hz: float):
        self._invoke(lambda: self.tb and self.tb.set_center_freq(hz))

    def set_samp(self, fs: float):
        self._invoke(lambda: self.tb and self.tb.set_samp_rate(fs))

    def set_rfbw(self, hz: float):
        self._invoke(lambda: self.tb and self.tb.set_rf_bw(hz))

    def set_atten(self, db: float):
        self._invoke(lambda: self.tb and self.tb.set_tx_atten_db(db))

    def set_buffer(self, n: int):
        self._invoke(lambda: self.tb and self.tb.set_buffer_size(n))

    def set_amp(self, k: float):
        k = 0.0 if k != k else max(0.0, min(1.0, float(k)))  # clamp
        self._invoke(lambda: self.tb and self.tb.set_amp(k))

    def set_pkt(self, n: int):
        self._invoke(lambda: self.tb and self.tb.set_packet_len(n))

    def set_roll(self, n: int):
        self._invoke(lambda: self.tb and self.tb.set_rolloff(n))

    def set_bitwrap_path(self, path: str):
        self._invoke(lambda: self.tb and self.tb.set_input_path(path))

    # ---------- Helpers ----------
    def _invoke(self, fn):
        """Queue a call into the running QThread."""
        self._pending = fn
        QMetaObject.invokeMethod(self, "_call", QtConst.QueuedConnection)  # type: ignore

    def _call(self):
        try:
            fn = self._pending
            if fn:
                fn()
        except Exception:
            pass
        finally:
            self._pending = None
