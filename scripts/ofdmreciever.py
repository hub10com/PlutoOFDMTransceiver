#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
# GNU Radio version: 3.10.x
# Title: Reciever

from PyQt5 import Qt
from gnuradio import qtgui
from gnuradio import analog
from gnuradio import blocks
from gnuradio import digital
from gnuradio import fft
from gnuradio.fft import window
from gnuradio import gr
from gnuradio import iio
from gnuradio import network
import sys
import signal
from argparse import ArgumentParser

class Reciever(gr.top_block, Qt.QWidget):
    def __init__(
        self,
        samp_rate=2_000_000,
        center_freq=2_400_000_000,
        rf_bw=2_000_000,           # Pluto RX bandwidth (varsa set_bandwidth ile)
        buffer_size=32768,
        gain_mode="slow_attack",   # slow_attack | fast_attack | manual
        rx_gain_db=64.0,           # manual için
        modulation="qpsk",         # ← YENİ: payload modülasyonu (bpsk/qpsk/qam16/qam64)
    ):
        gr.top_block.__init__(self, "Reciever", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("Reciever")
        qtgui.util.check_set_qss()
        try:
            self.setWindowIcon(Qt.QIcon.fromTheme('gnuradio-grc'))
        except Exception:
            pass

        # ---- UI iskeleti (GRC üretimi ile uyumlu) ----
        self.top_scroll_layout = Qt.QVBoxLayout()
        self.setLayout(self.top_scroll_layout)
        self.top_scroll = Qt.QScrollArea()
        self.top_scroll.setFrameStyle(Qt.QFrame.NoFrame)
        self.top_scroll_layout.addWidget(self.top_scroll)
        self.top_scroll.setWidgetResizable(True)
        self.top_widget = Qt.QWidget()
        self.top_scroll.setWidget(self.top_widget)
        self.top_layout = Qt.QVBoxLayout(self.top_widget)
        self.top_grid_layout = Qt.QGridLayout()
        self.top_layout.addLayout(self.top_grid_layout)

        self.settings = Qt.QSettings("GNU Radio", "Reciever")
        try:
            geometry = self.settings.value("geometry")
            if geometry:
                self.restoreGeometry(geometry)
        except Exception:
            pass

        # -----------------------------
        # Parameters (GUI kontrollü)
        # -----------------------------
        self.samp_rate   = int(samp_rate)
        self.center_freq = int(center_freq)
        self.rf_bw       = int(rf_bw)
        self.buffer_size = int(buffer_size)
        self._gain_mode  = str(gain_mode).strip().lower()
        self._rx_gain_db = float(rx_gain_db)
        self._mod_name   = str(modulation).strip().lower()   # ← YENİ

        # -----------------------------
        # PHY değişkenleri (dokunulmadı, sadece payload_mod seçimi eklendi)
        # -----------------------------

        # İsim → constellation seçimi (payload için)
        def _make_constellation(name: str):
            name = (name or "").lower()
            if name in ("bpsk", "psk2"):
                return digital.constellation_bpsk()
            if name in ("qpsk", "psk4"):
                return digital.constellation_qpsk()
            if name in ("qam16", "16qam"):
                return digital.constellation_16qam()
            if name in ("qam64", "64qam"):
                return digital.constellation_64qam()
            # default
            return digital.constellation_qpsk()

        self.pilot_symbols = pilot_symbols = ((1, 1, 1, -1,),)
        self.pilot_carriers = pilot_carriers = ((-21, -7, 7, 21,),)
        self.payload_mod = payload_mod = _make_constellation(self._mod_name)  # ← YENİ
        self.packet_length_tag_key = packet_length_tag_key = "packet_len"
        self.occupied_carriers = occupied_carriers = (
            list(range(-26, -21)) + list(range(-20, -7)) + list(range(-6, 0)) +
            list(range(1, 7)) + list(range(8, 21)) + list(range(22, 27)),
        )
        self.length_tag_key = length_tag_key = "frame_len"
        self.header_mod = header_mod = digital.constellation_bpsk()
        self.fft_len = fft_len = 64
        self.sync_word2 = sync_word2 = [0j,0j,0j,0j,0j,0j,(-1+0j),(-1+0j),(-1+0j),(-1+0j),(1+0j),(1+0j),(-1+0j),(-1+0j),(-1+0j),(1+0j),(-1+0j),(1+0j),(1+0j),(1+0j),(1+0j),(1+0j),(-1+0j),(-1+0j),(-1+0j),(-1+0j),(-1+0j),(1+0j),(-1+0j),(-1+0j),(1+0j),(-1+0j),0j,(1+0j),(-1+0j),(1+0j),(1+0j),(1+0j),(-1+0j),(1+0j),(1+0j),(1+0j),(-1+0j),(1+0j),(1+0j),(1+0j),(1+0j),(-1+0j),(1+0j),(-1+0j),(-1+0j),(-1+0j),(1+0j),(-1+0j),(1+0j),(-1+0j),(-1+0j),(-1+0j),(-1+0j),0j,0j,0j,0j,0j]
        self.sync_word1 = sync_word1 = [0.,0.,0.,0.,0.,0.,0.,1.41421356,0.,-1.41421356,0.,1.41421356,0.,-1.41421356,0.,-1.41421356,0.,-1.41421356,0.,1.41421356,0.,-1.41421356,0.,1.41421356,0.,-1.41421356,0.,-1.41421356,0.,-1.41421356,0.,-1.41421356,0.,1.41421356,0.,-1.41421356,0.,1.41421356,0.,1.41421356,0.,1.41421356,0.,-1.41421356,0.,1.41421356,0.,1.41421356,0.,1.41421356,0.,-1.41421356,0.,1.41421356,0.,1.41421356,0.,1.41421356,0.,0.,0.,0.,0.,0.]

        self.payload_equalizer = payload_equalizer = digital.ofdm_equalizer_simpledfe(
            fft_len, payload_mod.base(), occupied_carriers, pilot_carriers, pilot_symbols, 1
        )
        self.header_equalizer = header_equalizer = digital.ofdm_equalizer_simpledfe(
            fft_len, header_mod.base(), occupied_carriers, pilot_carriers, pilot_symbols
        )
        self.header_formatter = header_formatter = digital.packet_header_ofdm(
            occupied_carriers, n_syms=1,
            len_tag_key=packet_length_tag_key,
            frame_len_tag_key=length_tag_key,
            bits_per_header_sym=header_mod.bits_per_symbol(),
            bits_per_payload_sym=payload_mod.bits_per_symbol(),  # ← YENİ: seçime göre
            scramble_header=False
        )

        # -----------------------------
        # BLOKLER
        # -----------------------------
        # UDP çıkışı (değiştirme: network.udp_sink)
        self.network_udp_sink_0 = network.udp_sink(gr.sizeof_char, 1, '127.0.0.1', 2000, 0, 1472, False)

        # Pluto RX (buffer_size parametreli)
        self.iio_pluto_source_0 = iio.fmcomms2_source_fc32('' if '' else iio.get_pluto_uri(), [True, True], self.buffer_size)
        self.iio_pluto_source_0.set_len_tag_key('')
        try: self.iio_pluto_source_0.set_frequency(self.center_freq)
        except Exception: pass
        try: self.iio_pluto_source_0.set_samplerate(self.samp_rate)
        except Exception: pass
        try: self.iio_pluto_source_0.set_bandwidth(self.rf_bw)
        except Exception: pass

        # Gain mode/gain
        self._apply_gain_mode_initial()

        # Pluto ek ayarlar
        try: self.iio_pluto_source_0.set_quadrature(True)
        except Exception: pass
        try: self.iio_pluto_source_0.set_rfdc(True)
        except Exception: pass
        try: self.iio_pluto_source_0.set_bbdc(True)
        except Exception: pass
        try: self.iio_pluto_source_0.set_filter_params('Auto', '', 0, 0)
        except Exception: pass

        # PHY blokları (orijinal akış)
        self.fft_vxx_1 = fft.fft_vcc(fft_len, True, (), True, 1); self.fft_vxx_1.set_min_output_buffer(512000)
        self.fft_vxx_0 = fft.fft_vcc(fft_len, True, (), True, 1); self.fft_vxx_0.set_min_output_buffer(512000)
        self.digital_packet_headerparser_b_0 = digital.packet_headerparser_b(header_formatter.base())
        self.digital_ofdm_sync_sc_cfb_0 = digital.ofdm_sync_sc_cfb(fft_len, (fft_len//4), False, 0.9); self.digital_ofdm_sync_sc_cfb_0.set_min_output_buffer(512000)
        self.digital_ofdm_serializer_vcc_payload = digital.ofdm_serializer_vcc(fft_len, occupied_carriers, length_tag_key, packet_length_tag_key, 1, '', True); self.digital_ofdm_serializer_vcc_payload.set_min_output_buffer(512000)
        self.digital_ofdm_serializer_vcc_header = digital.ofdm_serializer_vcc(fft_len, occupied_carriers, 'header_len', '', 0, '', True); self.digital_ofdm_serializer_vcc_header.set_min_output_buffer(512000)
        self.digital_ofdm_frame_equalizer_vcvc_1 = digital.ofdm_frame_equalizer_vcvc(payload_equalizer.base(), (fft_len//4), length_tag_key, True, 0); self.digital_ofdm_frame_equalizer_vcvc_1.set_min_output_buffer(512000)
        self.digital_ofdm_frame_equalizer_vcvc_0 = digital.ofdm_frame_equalizer_vcvc(header_equalizer.base(), (fft_len//4), 'header_len', True, 1); self.digital_ofdm_frame_equalizer_vcvc_0.set_min_output_buffer(512000)
        self.digital_ofdm_chanest_vcvc_0 = digital.ofdm_chanest_vcvc(sync_word1, sync_word2, 1, 0, 3, False); self.digital_ofdm_chanest_vcvc_0.set_min_output_buffer(512000)
        self.digital_header_payload_demux_0 = digital.header_payload_demux(
            3, fft_len, (fft_len//4), length_tag_key, "", True,
            gr.sizeof_gr_complex, "rx_time", self.samp_rate, (), 0
        ); self.digital_header_payload_demux_0.set_min_output_buffer(512000)
        self.digital_crc32_bb_0 = digital.crc32_bb(True, packet_length_tag_key, False)
        self.digital_constellation_decoder_cb_1 = digital.constellation_decoder_cb(payload_mod.base())
        self.digital_constellation_decoder_cb_0 = digital.constellation_decoder_cb(header_mod.base())
        self.blocks_repack_bits_bb_0 = blocks.repack_bits_bb(payload_mod.bits_per_symbol(), 8, packet_length_tag_key, True, gr.GR_LSB_FIRST)
        self.blocks_multiply_xx_0 = blocks.multiply_vcc(1); self.blocks_multiply_xx_0.set_min_output_buffer(512000)
        self.blocks_delay_0 = blocks.delay(gr.sizeof_gr_complex*1, (fft_len//4 + fft_len)); self.blocks_delay_0.set_min_output_buffer(512000)
        self.analog_frequency_modulator_fc_0 = analog.frequency_modulator_fc((-2.0/fft_len))

        # -----------------------------
        # Bağlantılar (orijinal düzen)
        # -----------------------------
        self.msg_connect((self.digital_packet_headerparser_b_0, 'header_data'), (self.digital_header_payload_demux_0, 'header_data'))
        self.connect((self.analog_frequency_modulator_fc_0, 0), (self.blocks_multiply_xx_0, 0))
        self.connect((self.blocks_delay_0, 0), (self.blocks_multiply_xx_0, 1))
        self.connect((self.blocks_multiply_xx_0, 0), (self.digital_header_payload_demux_0, 0))
        self.connect((self.blocks_repack_bits_bb_0, 0), (self.digital_crc32_bb_0, 0))
        self.connect((self.digital_constellation_decoder_cb_0, 0), (self.digital_packet_headerparser_b_0, 0))
        self.connect((self.digital_constellation_decoder_cb_1, 0), (self.blocks_repack_bits_bb_0, 0))
        self.connect((self.digital_crc32_bb_0, 0), (self.network_udp_sink_0, 0))
        self.connect((self.digital_header_payload_demux_0, 0), (self.fft_vxx_0, 0))
        self.connect((self.digital_header_payload_demux_0, 1), (self.fft_vxx_1, 0))
        self.connect((self.digital_ofdm_chanest_vcvc_0, 0), (self.digital_ofdm_frame_equalizer_vcvc_0, 0))
        self.connect((self.digital_ofdm_frame_equalizer_vcvc_0, 0), (self.digital_ofdm_serializer_vcc_header, 0))
        self.connect((self.digital_ofdm_frame_equalizer_vcvc_1, 0), (self.digital_ofdm_serializer_vcc_payload, 0))
        self.connect((self.digital_ofdm_serializer_vcc_header, 0), (self.digital_constellation_decoder_cb_0, 0))
        self.connect((self.digital_ofdm_serializer_vcc_payload, 0), (self.digital_constellation_decoder_cb_1, 0))
        self.connect((self.digital_ofdm_sync_sc_cfb_0, 0), (self.analog_frequency_modulator_fc_0, 0))
        self.connect((self.digital_ofdm_sync_sc_cfb_0, 1), (self.digital_header_payload_demux_0, 1))
        self.connect((self.fft_vxx_0, 0), (self.digital_ofdm_chanest_vcvc_0, 0))
        self.connect((self.fft_vxx_1, 0), (self.digital_ofdm_frame_equalizer_vcvc_1, 0))
        self.connect((self.iio_pluto_source_0, 0), (self.blocks_delay_0, 0))
        self.connect((self.iio_pluto_source_0, 0), (self.digital_ofdm_sync_sc_cfb_0, 0))

    # ---------- Gain mode uygulaması ----------
    def _apply_gain_mode_initial(self):
        mode = self._gain_mode
        try:
            if mode == "manual":
                try: self.iio_pluto_source_0.set_gain_mode(0, 'manual')
                except Exception:
                    try: self.iio_pluto_source_0.set_gain_mode('manual')
                    except Exception: pass
                try:
                    self.iio_pluto_source_0.set_gain(0, float(self._rx_gain_db))
                except Exception:
                    try: self.iio_pluto_source_0.set_gain(float(self._rx_gain_db))
                    except Exception: pass
            elif mode == "fast_attack":
                try: self.iio_pluto_source_0.set_gain_mode(0, 'fast_attack')
                except Exception:
                    try: self.iio_pluto_source_0.set_gain_mode('fast_attack')
                    except Exception: pass
            else:
                try: self.iio_pluto_source_0.set_gain_mode(0, 'slow_attack')
                except Exception:
                    try: self.iio_pluto_source_0.set_gain_mode('slow_attack')
                    except Exception: pass
        except Exception:
            pass

    # ---------- Qt close ----------
    def closeEvent(self, event):
        self.settings = Qt.QSettings("GNU Radio", "Reciever")
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop(); self.wait()
        event.accept()

    # ---------- Getter / Setter (CANLI uygulanır) ----------
    def get_samp_rate(self): return self.samp_rate
    def set_samp_rate(self, v):
        self.samp_rate = int(v)
        try: self.iio_pluto_source_0.set_samplerate(self.samp_rate)
        except Exception: pass
        try: self.digital_header_payload_demux_0.set_sample_rate(self.samp_rate)
        except Exception: pass

    def get_center_freq(self): return self.center_freq
    def set_center_freq(self, v):
        self.center_freq = int(v)
        try: self.iio_pluto_source_0.set_frequency(self.center_freq)
        except Exception: pass

    def get_rf_bw(self): return self.rf_bw
    def set_rf_bw(self, v):
        self.rf_bw = int(v)
        try: self.iio_pluto_source_0.set_bandwidth(self.rf_bw)
        except Exception: pass   # bazı sürümlerde yok olabilir

    def get_buffer_size(self): return self.buffer_size
    def set_buffer_size(self, v):
        self.buffer_size = int(v)
        try: self.iio_pluto_source_0.set_buffer_size(self.buffer_size)
        except Exception: pass   # runtime her sürümde destekli olmayabilir

    def get_gain_mode(self): return self._gain_mode
    def set_gain_mode(self, mode):
        mode = str(mode).strip().lower()
        if mode not in ("slow_attack", "fast_attack", "manual"): return
        self._gain_mode = mode
        self._apply_gain_mode_initial()

    def get_rx_gain_db(self): return self._rx_gain_db
    def set_rx_gain_db(self, val):
        self._rx_gain_db = float(val)
        if self._gain_mode == "manual":
            try:
                self.iio_pluto_source_0.set_gain(0, self._rx_gain_db)
            except Exception:
                try: self.iio_pluto_source_0.set_gain(self._rx_gain_db)
                except Exception: pass

# -----------------------------
# CLI / Standalone
# -----------------------------
def argument_parser():
    ap = ArgumentParser()
    ap.add_argument("--mod",
                    dest="mod",
                    type=str,
                    choices=["bpsk", "qpsk", "qam16", "qam64"],
                    default="qpsk",
                    help="Payload modulation (header always BPSK)")
    return ap

def main(top_block_cls=Reciever, options=None):
    if options is None:
        options = argument_parser().parse_args()

    qapp = Qt.QApplication(sys.argv)
    tb = top_block_cls(
        modulation=options.mod,   # ← YENİ: CLI’dan gelen mod burada uygulanır
    )
    tb.start(); tb.show()

    def sig_handler(sig=None, frame=None):
        tb.stop(); tb.wait()
        Qt.QApplication.quit()

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)
    timer = Qt.QTimer(); timer.start(500); timer.timeout.connect(lambda: None)
    qapp.exec_()

if __name__ == '__main__':
    main()
