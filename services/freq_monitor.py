from PyQt5 import Qt
from gnuradio import gr, iio, qtgui
from gnuradio.fft import window
import sip

class FreqMonitor(gr.top_block, Qt.QWidget):
    def __init__(self, center_freq=2.4e9, samp_rate=2e6, rf_bw=2e6):
        gr.top_block.__init__(self, "Freq Monitor", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("Spectrum Monitor")

        self.top_layout = Qt.QVBoxLayout(self)

        # Pluto RX
        self.src = iio.pluto_source('', [True, True], rf_bw, samp_rate, 0x8000, True, True, True, "manual", 64, '', True)
        self.src.set_bandwidth(rf_bw)
        self.src.set_center_freq(center_freq)

        # Frequency sink
        self.freq_sink = qtgui.freq_sink_c(
            1024, window.WIN_BLACKMAN_hARRIS, center_freq, samp_rate, "", 1, None
        )
        self.freq_sink.set_update_time(0.1)
        self.freq_sink.set_y_axis(-140, 10)
        self.freq_sink.enable_autoscale(False)
        self.freq_sink.enable_grid(True)

        self._freq_win = sip.wrapinstance(self.freq_sink.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._freq_win)

        self.connect(self.src, self.freq_sink)

    def widget(self):
        return self._freq_win
