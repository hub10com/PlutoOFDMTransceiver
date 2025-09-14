# scripts/tx_runner.py
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import signal
import argparse
import traceback
from pathlib import Path

# ofdmtransmitter.py must be in the SAME folder
THIS_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(THIS_DIR))

try:
    from ofdmtransmitter import ofdmtransmit
except Exception as e:
    print(f"[TX] ERROR: ofdmtransmitter import error: {e}", flush=True)
    sys.exit(2)

from PyQt5 import Qt


def parse_args():
    ap = argparse.ArgumentParser(description="OFDM TX Runner (GNURadio + Qt)")
    ap.add_argument("--bitwrap", required=True, help=".bitwrap file to send")
    ap.add_argument("--center", type=float, default=2.4e9)
    ap.add_argument("--samp",   type=float, default=2e6)
    ap.add_argument("--rfbw",   type=float, default=None)
    ap.add_argument("--atten",  type=float, default=10.0)
    ap.add_argument("--buffer", type=int,   default=32768)
    ap.add_argument("--amp",    type=float, default=0.03)
    ap.add_argument("--pkt",    type=int,   default=512)
    ap.add_argument("--roll",   type=int,   default=0)
    ap.add_argument("--mod",    type=str,   default="qpsk", choices=["bpsk", "qpsk", "16qam"])
    return ap.parse_args()


def main():
    args = parse_args()

    bitwrap_path = Path(args.bitwrap)
    if not bitwrap_path.exists():
        print(f"[TX] ERROR: .bitwrap file not found: {bitwrap_path}", flush=True)
        sys.exit(2)

    try:
        app = Qt.QApplication(sys.argv)

        tb = ofdmtransmit(
            rolloff=args.roll,
            samp_rate=args.samp,
            center_freq=args.center,
            rf_bw=args.rfbw or args.samp,
            tx_atten_db=args.atten,
            buffer_size=args.buffer,
            amp=args.amp,
            packet_len=args.pkt,
            input_path=str(bitwrap_path),
            modulation=args.mod,
            embed_widget=True,  # show GNURadio freq sink widget
        )

        # Start
        tb.start()
        tb.show()
        print("[TX] GNURadio STARTED", flush=True)

        # Graceful shutdown
        def _stop(*_):
            print("[TX] ending...", flush=True)
            try:
                tb.stop()
                tb.wait()
            finally:
                Qt.QApplication.quit()

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)

        # Event loop keep-alive (needed on some Qt setups)
        timer = Qt.QTimer()
        timer.start(500)
        timer.timeout.connect(lambda: None)

        rc = app.exec_()
        print(f"[TX] Finished. rc={rc}", flush=True)
        sys.exit(rc)

    except Exception:
        print("[TX] FATAL ERROR:\n" + traceback.format_exc(), flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
