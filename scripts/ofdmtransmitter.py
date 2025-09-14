#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
scripts/ofdmtransmitter.py

- GNU Radio 3.10.x + PyQt5
- PlutoSDR (iio.fmcomms2_sink_fc32) OFDM vericisi
- qtgui.freq_sink_c widget'ını GUI'ye embed etmek için qtgui_widget() sağlar
- tx_runner.py ile birebir parametre uyumlu

Kullanım (subprocess, tx_runner.py zaten böyle yapıyor):
    from ofdmtransmitter import ofdmtransmit
    tb = ofdmtransmit(..., embed_widget=True)
    tb.start(); tb.show()

GUI’ye embed (in-proc):
    tb = ofdmtransmit(..., embed_widget=False)
    widget = tb.qtgui_widget()
    layout.addWidget(widget)
    tb.start()
"""

import sys
import signal
from pathlib import Path

from PyQt5 import Qt
import sip

from gnuradio import gr, blocks, digital, fft, qtgui, iio
from gnuradio.fft import window
import pmt


class ofdmtransmit(gr.top_block, Qt.QWidget):
    """
    Dışarıdan güvenli setter'larla kontrol edilebilir:
      - set_center_freq(float Hz)
      - set_samp_rate(float S/s)
      - set_rf_bw(float Hz)
      - set_tx_atten_db(float dB)   # küçüldükçe güç artar
      - set_buffer_size(int)
      - set_amp(float)
      - set_packet_len(int bytes)
      - set_rolloff(int samples)    # CP örnek sayısı
      - set_input_path(str path)
    Not: modulation ("bpsk"|"qpsk"|"16qam") __init__’te seçilir.
    """

    def __init__(
        self,
        rolloff=0,
        samp_rate=2_000_000,
        center_freq=2_400_000_000,
        rf_bw=2_000_000,
        tx_atten_db=-5,
        buffer_size=32768,
        amp=0.03,
        packet_len=512,
        input_path=r"C:\tmp\file.bitwrap",
        modulation="qpsk",
        embed_widget=False,   # True: standalone pencere içinde widget'ı da göster
    ):
        gr.top_block.__init__(self, "OFDM Transmitter", catch_exceptions=True)
        Qt.QWidget.__init__(self)

        # ---- Paramlar ----
        self.rolloff     = int(rolloff)           # CP örnek sayısı (0 => fft_len//4)
        self.samp_rate   = int(samp_rate)
        self.center_freq = int(center_freq)
        self.rf_bw       = int(rf_bw)
        self.tx_atten_db = float(tx_atten_db)
        self.buffer_size = int(buffer_size)
        self.amp         = float(amp)
        self.packet_len  = int(packet_len)
        self.input_path  = str(input_path)
        self.modulation  = str(modulation).strip().lower()
        self._embed_widget = bool(embed_widget)

        # ---- Modülasyon ----
        if self.modulation in ("bpsk", "b"):
            self.payload_mod = digital.constellation_bpsk()
        elif self.modulation in ("qpsk", "q"):
            self.payload_mod = digital.constellation_qpsk()
        else:
            self.payload_mod = digital.constellation_16qam()
        self.header_mod = digital.constellation_bpsk()

        # ---- OFDM yerleşimi ----
        self.length_tag_key = "packet_len"
        self.fft_len = 64
        # 802.11'e benzer taşıyıcı haritası (DC boş, guard'lar mevcut)
        self.occupied_carriers = (
            list(range(-26, -21)) + list(range(-20, -7)) + list(range(-6, 0)) +
            list(range(1, 7)) + list(range(8, 21)) + list(range(22, 27)),
        )
        self.pilot_carriers = ((-21, -7, 7, 21),)
        self.pilot_symbols  = ((1, 1, 1, -1),)
        # Senk kelimeleri (standart test vektörleri)
        self.sync_word1 = [0.,0.,0.,0.,0.,0.,0.,1.41421356,0.,-1.41421356,0.,1.41421356,0.,-1.41421356,0.,-1.41421356,0.,-1.41421356,0.,1.41421356,0.,-1.41421356,0.,1.41421356,0.,-1.41421356,0.,-1.41421356,0.,-1.41421356,0.,-1.41421356,0.,1.41421356,0.,-1.41421356,0.,1.41421356,0.,1.41421356,0.,1.41421356,0.,-1.41421356,0.,1.41421356,0.,1.41421356,0.,1.41421356,0.,-1.41421356,0.,1.41421356,0.,1.41421356,0.,1.41421356,0.,0.,0.,0.,0.,0.]
        self.sync_word2 = [0,0,0,0,0,0,-1,-1,-1,-1,1,1,-1,-1,-1,1,-1,1,1,1,1,1,-1,-1,-1,-1,-1,1,-1,-1,1,-1,0,1,-1,1,1,1,-1,1,1,1,-1,1,1,1,1,-1,1,-1,-1,-1,1,-1,1,-1,-1,-1,-1,0,0,0,0,0]

        self.header_formatter = digital.packet_header_ofdm(
            self.occupied_carriers, n_syms=1,
            len_tag_key=self.length_tag_key,
            frame_len_tag_key=self.length_tag_key,
            bits_per_header_sym=self.header_mod.bits_per_symbol(),
            bits_per_payload_sym=self.payload_mod.bits_per_symbol(),
            scramble_header=False
        )

        # ---- QtGUI Freq Sink (embed için) ----
        self.freq_sink = qtgui.freq_sink_c(
            1024, window.WIN_BLACKMAN_hARRIS, 0, self.samp_rate, "", 1, None
        )
        self.freq_sink.enable_control_panel(False)
        self.freq_sink.set_fft_average(1.0)

        # PyQt5 widget'ını al
        self._freq_widget = None
        for getter in ("qwidget", "pyqwidget"):
            try:
                self._freq_widget = sip.wrapinstance(getattr(self.freq_sink, getter)(), Qt.QWidget)
                break
            except Exception:
                pass
        if self._embed_widget:
            self.setWindowTitle("OFDM Transmitter")
            lay = Qt.QVBoxLayout(self)
            lay.addWidget(self._freq_widget)

        # ---- Pluto TX ----
        self.tx = iio.fmcomms2_sink_fc32('' if '' else iio.get_pluto_uri(), [True, True], self.buffer_size, False)
        self.tx.set_len_tag_key('')
        self.tx.set_bandwidth(self.rf_bw)
        self.tx.set_frequency(self.center_freq)
        self.tx.set_samplerate(self.samp_rate)
        try:
            self.tx.set_attenuation(0, self.tx_atten_db)
            self.tx.set_attenuation(1, self.tx_atten_db)
        except Exception:
            pass
        self.tx.set_filter_params('Auto', '', 0, 0)

        # ---- Kaynak / paketleme ----
        src_path = str(Path(self.input_path).expanduser())
        self.src    = blocks.file_source(gr.sizeof_char, src_path, False, 0, 0)
        self.src.set_begin_tag(pmt.PMT_NIL)

        self.t2t    = blocks.stream_to_tagged_stream(gr.sizeof_char, 1, self.packet_len, self.length_tag_key)
        self.crc    = digital.crc32_bb(False, self.length_tag_key, False)
        self.repack = blocks.repack_bits_bb(8, self.payload_mod.bits_per_symbol(), self.length_tag_key, False, gr.GR_LSB_FIRST)
        self.hdrgen = digital.packet_headergenerator_bb(self.header_formatter.base(), self.length_tag_key)
        self.map_hdr = digital.chunks_to_symbols_bc(self.header_mod.points(), 1)
        self.map_pay = digital.chunks_to_symbols_bc(self.payload_mod.points(), 1)

        # ---- OFDM zinciri ----
        self.alloc = digital.ofdm_carrier_allocator_cvc(
            self.fft_len, self.occupied_carriers, self.pilot_carriers, self.pilot_symbols,
            (self.sync_word1, self.sync_word2), self.length_tag_key, True
        )
        self.fft = fft.fft_vcc(self.fft_len, False, (), True, 1)

        # CP örnek sayısı: 0 verilirse fft_len//4 kullan (ör. 64 → 16)
        cp_samp = self.rolloff if self.rolloff > 0 else (self.fft_len // 4)
        # ofdm_cyclic_prefixer(total_len = FFT + CP) ve window_rolloff=0
        self.cp = digital.ofdm_cyclic_prefixer(
            self.fft_len, self.fft_len + max(1, cp_samp), 0, self.length_tag_key
        )

        # Güç ve çıkış
        self.mux  = blocks.tagged_stream_mux(gr.sizeof_gr_complex, self.length_tag_key, 0)
        self.mul  = blocks.multiply_const_cc(self.amp)
        self.gate = blocks.tag_gate(gr.sizeof_gr_complex, False); self.gate.set_single_key("")

        # ---- Bağlantılar ----
        self.connect(self.src, self.t2t)
        self.connect(self.t2t, self.crc)
        self.connect(self.crc, self.repack)
        self.connect(self.crc, self.hdrgen)
        self.connect(self.hdrgen, self.map_hdr)
        self.connect(self.repack, self.map_pay)
        self.connect(self.map_hdr, (self.mux, 0))
        self.connect(self.map_pay, (self.mux, 1))
        self.connect(self.mux, self.alloc)
        self.connect(self.alloc, self.fft)
        self.connect(self.fft, self.cp)
        self.connect(self.cp, self.mul)
        self.connect(self.mul, self.gate)
        self.connect(self.gate, self.tx)
        self.connect(self.gate, self.freq_sink)

    # ---------- Embed için ----------
    def qtgui_widget(self) -> Qt.QWidget:
        """Frekans spektrumu için PyQt5 QWidget (GUI'ye addWidget ile ekleyin)."""
        return self._freq_widget

    # ---------- Setter'lar ----------
    def set_center_freq(self, v):
        self.center_freq = int(v)
        try: self.tx.set_frequency(self.center_freq)
        except Exception: pass

    def set_samp_rate(self, v):
        self.samp_rate = int(v)
        try: self.tx.set_samplerate(self.samp_rate)
        except Exception: pass
        try: self.freq_sink.set_frequency_range(0, self.samp_rate)
        except Exception: pass

    def set_rf_bw(self, v):
        self.rf_bw = int(v)
        try: self.tx.set_bandwidth(self.rf_bw)
        except Exception: pass

    def set_tx_atten_db(self, v):
        self.tx_atten_db = float(v)
        try:
            self.tx.set_attenuation(0, self.tx_atten_db)
            self.tx.set_attenuation(1, self.tx_atten_db)
        except Exception: pass

    def set_buffer_size(self, v):
        self.buffer_size = int(v)
        try: self.tx.set_buffer_size(self.buffer_size)
        except Exception: pass  # bazı sürümlerde runtime destekli değil

    def set_amp(self, v):
        self.amp = float(v)
        try: self.mul.set_k(self.amp)
        except Exception: pass

    def set_packet_len(self, v):
        self.packet_len = int(v)
        try:
            self.t2t.set_packet_len(self.packet_len)
            self.t2t.set_packet_len_pmt(self.packet_len)
        except Exception: pass

    def set_rolloff(self, v):
        # rolloff = CP örnek sayısı
        self.rolloff = int(v)
        try:
            cp_samp = self.rolloff if self.rolloff > 0 else (self.fft_len // 4)
            # bazı sürümlerde runtime değişim API'si kısıtlı olabilir; dene
            self.cp.set_cp_len(self.fft_len + max(1, cp_samp))
            self.cp.set_rolloff_len(0)
        except Exception:
            pass

    def set_input_path(self, path):
        self.input_path = str(path)
        try:
            self.src.open(str(Path(self.input_path).expanduser()), False)
        except Exception as e:
            print(f"[WARN] file open failed: {e}", file=sys.stderr)

    def set_modulation(self, name):
        # Çalışan akışta modülasyon değiştirmek güvenli değildir; bir sonraki start’ta kullanın.
        self.modulation = str(name).lower()


# ---- Standalone (opsiyonel) ----
def _main():
    app = Qt.QApplication(sys.argv)
    tb = ofdmtransmit(embed_widget=True)
    tb.start()
    tb.show()

    def _sig(*_):
        try:
            tb.stop(); tb.wait()
        finally:
            Qt.QApplication.quit()

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    timer = Qt.QTimer(); timer.start(500); timer.timeout.connect(lambda: None)
    rc = app.exec_()
    sys.exit(rc)

if __name__ == "__main__":
    _main()
