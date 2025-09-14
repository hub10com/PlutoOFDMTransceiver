#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/rx_runner.py — Robust cleanup version
• New console; prints stdout (flush=True)
• Strongly guaranteed teardown for libiio/GNURadio
"""

import sys
import os
import time
import atexit
import signal
import argparse
import traceback
from pathlib import Path

THIS_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(THIS_DIR))

try:
    from ofdmreciever import Reciever
except Exception as e:
    print(f"[RX] ERROR: ofdmreciever import error: {e}", flush=True)
    sys.exit(2)


def parse_args():
    ap = argparse.ArgumentParser(description="OFDM RX Runner (GNURadio)")
    ap.add_argument("--center", type=float, default=2.4e9, help="Center frequency [Hz]")
    ap.add_argument("--samp",   type=float, default=2e6,   help="Sample rate [S/s]")
    ap.add_argument("--rfbw",   type=float, default=2e6,   help="RF bandwidth [Hz]")
    ap.add_argument("--buffer", type=int,   default=32768, help="Pluto buffer size")
    ap.add_argument("--gain_mode", type=str, default="slow_attack",
                    choices=["slow_attack", "fast_attack", "manual"], help="Gain mode")
    ap.add_argument("--gain_db", type=float, default=64.0,
                    help="RX gain (only in manual mode)")
    ap.add_argument("--mod", type=str, default="qpsk",
                    choices=["bpsk", "qpsk", "qam16", "qam64"],
                    help="Payload modulation (header=BPSK)")
    return ap.parse_args()


def main():
    args = parse_args()

    # ---- Optional QApplication (qtgui sinks) ----
    app = None
    try:
        from PyQt5 import QtWidgets
        app = QtWidgets.QApplication.instance()
        if app is None:
            app = QtWidgets.QApplication(sys.argv)
    except Exception:
        app = None  # Qt yoksa sorun değil

    print("[RX] Subprocess started.", flush=True)
    print(f"[RX] Params: center={args.center} samp={args.samp} rfbw={args.rfbw} "
          f"buf={args.buffer} gain_mode={args.gain_mode} gain_db={args.gain_db} "
          f"mod={args.mod}", flush=True)

    tb = None
    _stopped = {"done": False}

    def safe_stop(reason="signal"):
        if _stopped["done"]:
            return
        _stopped["done"] = True
        print(f"[RX] ending... (reason={reason})", flush=True)
        # GNURadio top block stop/wait
        try:
            if tb is not None:
                try:
                    tb.stop()
                except Exception as e:
                    print(f"[RX][DBG] tb.stop() err: {e}", flush=True)
                try:
                    tb.wait()
                except Exception as e:
                    print(f"[RX][DBG] tb.wait() err: {e}", flush=True)
        finally:
            # Qt kapat (varsa)
            try:
                if app is not None:
                    app.quit()
            except Exception:
                pass

            # Nesneleri bırak ve libiio handle’larına zaman tanı
            try:
                import gc
                del_vars = []
                if tb is not None:
                    del_vars.append("tb")
                for _ in range(2):
                    gc.collect()
                    time.sleep(0.25)
                # Windows’ta driver handle’ları için biraz daha bekle
                if os.name == "nt":
                    time.sleep(0.5)
            except Exception:
                pass

    # Sinyal yakalayıcılar
    def _on_sigint(sig, frm):  safe_stop("SIGINT");  sys.exit(0)
    def _on_sigterm(sig, frm): safe_stop("SIGTERM"); sys.exit(0)
    signal.signal(signal.SIGINT, _on_sigint)
    signal.signal(signal.SIGTERM, _on_sigterm)
    # Windows yeni konsolda Ctrl+Break → SIGBREAK
    if hasattr(signal, "SIGBREAK"):
        def _on_sigbreak(sig, frm): safe_stop("SIGBREAK"); sys.exit(0)
        signal.signal(signal.SIGBREAK, _on_sigbreak)

    # Çıkışta her halükârda temizlik
    atexit.register(lambda: safe_stop("atexit"))

    try:
        tb = Reciever(
            samp_rate=args.samp,
            center_freq=args.center,
            rf_bw=args.rfbw,
            buffer_size=args.buffer,
            gain_mode=args.gain_mode,
            rx_gain_db=args.gain_db,
            modulation=args.mod,
        )
        tb.start()
        print("[RX] GNURadio started", flush=True)

        # Blokla
        tb.wait()
        print("[RX] Finished. (FINISHED)", flush=True)
        safe_stop("finished")
        sys.exit(0)

    except SystemExit:
        # üstteki sys.exit akışı
        raise
    except KeyboardInterrupt:
        safe_stop("KeyboardInterrupt")
        sys.exit(0)
    except Exception:
        print("[RX] FATAL ERROR:\n" + traceback.format_exc(), flush=True)
        safe_stop("exception")
        sys.exit(1)
    finally:
        # Ek güvence: burada da temizlik yap
        safe_stop("finally")


if __name__ == "__main__":
    main()
