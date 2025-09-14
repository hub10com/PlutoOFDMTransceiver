# -*- coding: utf-8 -*-
"""
ui/views/tx_fhss_view.py

TxFhssView:
- Sol kolon: File / PlutoSDR / RS / OFDM PHY / Progress + Buttons
- Sağ kolon: Log (üstte) + FHSS paneli (altta)
- API: TxController ile uyumlu (append_log, clear_log, set_time, set_overall_progress,
       set_load_text, input getters, add_freq_widget, ...)

Not:
- freq sink yerleşimi için TxView’deki ile bire bir aynı imzayı sağlıyoruz:
  add_freq_widget(w) → self.freqSinkLayout varsa tek çocuk olacak şekilde w’yi ekler.
  (Ek UI/sekme EKLEMİYORUZ.)
"""

from PyQt5.QtCore import Qt, pyqtSignal, QEasingCurve, QPropertyAnimation
from PyQt5.QtWidgets import (
    QWidget, QLabel, QPushButton, QLineEdit, QTextEdit,
    QVBoxLayout, QHBoxLayout, QGroupBox, QFormLayout, QRadioButton,
    QFrame, QFileDialog, QBoxLayout, QGraphicsDropShadowEffect, QSizePolicy
)

from ui.style import apply as apply_theme
from ui.views.tx_view import (
    ProgressPanel, int_line_edit, float_line_edit, float_spinbox, add_form_row
)


def _repolish(w: QWidget):
    st = w.style()
    st.unpolish(w); st.polish(w)
    w.update()


class _FhssCell(QWidget):
    def __init__(self, label_text: str, parent=None):
        super().__init__(parent)
        self._active = False
        self.setFixedWidth(110)

        self.box = QFrame()
        self.box.setObjectName("fhssBox")
        self.box.setFixedSize(56, 56)

        self.shadow = QGraphicsDropShadowEffect(self.box)
        self.shadow.setBlurRadius(8)
        self.shadow.setOffset(0, 0)
        self.shadow.setColor(Qt.transparent)
        self.box.setGraphicsEffect(self.shadow)

        self.lbl = QLabel(label_text)
        f = self.lbl.font(); f.setPointSize(f.pointSize() + 1)
        self.lbl.setFont(f)
        self.lbl.setAlignment(Qt.AlignHCenter | Qt.AlignTop)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)
        root.addWidget(self.box, 0, Qt.AlignHCenter)
        root.addWidget(self.lbl, 0, Qt.AlignHCenter)

        self._pulse = QPropertyAnimation(self.shadow, b"blurRadius", self)
        self._pulse.setDuration(220)
        self._pulse.setStartValue(8.0)
        self._pulse.setEndValue(18.0)
        self._pulse.setEasingCurve(QEasingCurve.InOutCubic)

        self._apply_state(first=True)

    def set_text(self, text: str):
        self.lbl.setText(text)

    def set_active(self, on: bool):
        on = bool(on)
        if on == self._active:
            return
        self._active = on
        self._apply_state()

    def _apply_state(self, first: bool = False):
        self.box.setProperty("active", "true" if self._active else "false")
        _repolish(self.box)

        if self._active:
            self.shadow.setColor(Qt.white)
            self._pulse.setDirection(QPropertyAnimation.Forward)
        else:
            self.shadow.setColor(Qt.transparent)
            self._pulse.setDirection(QPropertyAnimation.Backward)

        if not first:
            self._pulse.start()


class _FhssPanel(QGroupBox):
    """FHSS paneli: başlık + iki göstergeli kutu."""
    def __init__(self, f1_label="2.404 GHz", f2_label="2.416 GHz", parent=None):
        super().__init__("FHSS", parent)

        self.cell1 = _FhssCell(f1_label)
        self.cell2 = _FhssCell(f2_label)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(48)
        row.addWidget(self.cell1)
        row.addWidget(self.cell2)
        row.setAlignment(Qt.AlignHCenter | Qt.AlignVCenter)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.addStretch(1)
        root.addLayout(row, 0)
        root.addStretch(1)

    def set_labels(self, f1_text: str, f2_text: str):
        self.cell1.set_text(f1_text)
        self.cell2.set_text(f2_text)

    def set_active_index(self, idx: int):
        self.cell1.set_active(idx == 0)
        self.cell2.set_active(idx == 1)


class TxFhssView(QWidget):
    sig_send = pyqtSignal()
    sig_start = pyqtSignal()
    sig_stop = pyqtSignal()
    sig_clear = pyqtSignal()
    sig_back = pyqtSignal()
    sig_file_selected = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)

        root = QHBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(12)

        left = QVBoxLayout();  left.setSpacing(12)
        right = QVBoxLayout(); right.setSpacing(12)
        root.addLayout(left, 3)
        root.addLayout(right, 2)

        # --- File ---
        grp_file = QGroupBox("File")
        file_row = QHBoxLayout(grp_file)
        file_row.setSpacing(8)
        self.filePathEdit = QLineEdit(); self.filePathEdit.setReadOnly(True)
        self.filePathEdit.setPlaceholderText("Selected file path/name…")
        self.fileBrowseButton = QPushButton("Browse")
        self.fileBrowseButton.clicked.connect(self._browse_file)
        file_row.addWidget(self.filePathEdit, 1)
        file_row.addWidget(self.fileBrowseButton, 0)
        left.addWidget(grp_file)

        # --- PlutoSDR ---
        grp_pluto = QGroupBox("PlutoSDR")
        pluto_form = QFormLayout(grp_pluto)
        self.plutoFreqEdit   = add_form_row(pluto_form, "Frequency (Hz)",   int_line_edit("2400000000", "Hz"))
        self.plutoBwEdit     = add_form_row(pluto_form, "Sample Rate",      int_line_edit("2000000",    "Hz"))
        self.plutoPowerEdit  = add_form_row(pluto_form, "Gain (dB)",        int_line_edit("10", "dB"))
        self.plutoBufferEdit = add_form_row(pluto_form, "Buffer Size",      int_line_edit("32768", "e.g. 32768"))
        self.plutoAmpEdit    = add_form_row(pluto_form, "Amplitude",        float_line_edit("0.03", "0.00 – 1.00"))
        left.addWidget(grp_pluto)

        # --- RS ---
        grp_rs = QGroupBox("Reed-Solomon")
        rs_form = QFormLayout(grp_rs)
        self.rsREdit   = add_form_row(rs_form, "r (parity)",     int_line_edit("16", "r"))
        self.rsDEdit   = add_form_row(rs_form, "d (interleave)", int_line_edit("32", "d"))
        self.rsSEdit   = add_form_row(rs_form, "s (slice)",      int_line_edit("1024", "s"))
        self.thetaEdit = add_form_row(rs_form, "θ (theta)",      float_spinbox(4.0, 0.5, 64.0, 0.5))
        left.addWidget(grp_rs)

        # --- PHY ---
        grp_phy = QGroupBox("OFDM PHY")
        phy_form = QFormLayout(grp_phy)
        self.modBpskRadio  = QRadioButton("BPSK")
        self.modQpskRadio  = QRadioButton("QPSK"); self.modQpskRadio.setChecked(True)
        self.mod16qamRadio = QRadioButton("16-QAM")
        mod_row = QHBoxLayout()
        mod_row.addWidget(self.modBpskRadio)
        mod_row.addWidget(self.modQpskRadio)
        mod_row.addWidget(self.mod16qamRadio)
        mod_row.addStretch(1)
        mod_wrap = QWidget(); mod_wrap.setLayout(mod_row)
        add_form_row(phy_form, "Modulation", mod_wrap)
        self.pktSizeEdit = add_form_row(phy_form, "Packet Size", int_line_edit("512", "byte"))
        left.addWidget(grp_phy)

        # --- Footer (Progress + Buttons) ---
        footer = QFrame(); footer.setObjectName("footerCard")
        footer_v = QVBoxLayout(footer)
        self.progressPanel = ProgressPanel()
        footer_v.addWidget(self.progressPanel)

        btn_row = QHBoxLayout()
        self.txBackButton  = QPushButton("← Back")
        self.txClearButton = QPushButton("Clear")
        self.txStopButton  = QPushButton("Stop")
        self.txStartButton = QPushButton("Start")
        self.txSendButton  = self.txStartButton
        self.txStartButton.setObjectName("primary")
        self.txStopButton.setObjectName("danger")
        for b in (self.txBackButton, self.txClearButton, self.txStopButton, self.txStartButton):
            b.setMinimumHeight(34)
        btn_row.addWidget(self.txBackButton, 0)
        btn_row.addStretch(1)
        btn_row.addWidget(self.txClearButton, 0)
        btn_row.addWidget(self.txStopButton, 0)
        btn_row.addWidget(self.txStartButton, 0)
        footer_v.addLayout(btn_row)
        left.addWidget(footer)

        # --- Buttons → Signals ---
        self.txStartButton.clicked.connect(self.sig_start.emit)
        self.txStartButton.clicked.connect(self.sig_send.emit)
        self.txStopButton.clicked.connect(self.sig_stop.emit)
        self.txClearButton.clicked.connect(self.sig_clear.emit)
        self.txBackButton.clicked.connect(self.sig_back.emit)

        # --- Right: Log + FHSS ---
        grp_log = QGroupBox("Log")
        right_v = QVBoxLayout(grp_log)
        self.txLogText = QTextEdit()
        self.txLogText.setReadOnly(True)
        self.txLogText.setLineWrapMode(QTextEdit.NoWrap)
        self.fhssPanel = _FhssPanel("2.404 GHz", "2.416 GHz")
        right_v.addWidget(self.txLogText, 3)
        right_v.addWidget(self.fhssPanel, 2)
        grp_log.setMinimumWidth(320)
        grp_log.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        right.addWidget(grp_log, 1)

        apply_theme(self)

    # ---------- API (TxController uyumu) ----------
    def input_path(self): return self.filePathEdit.text().strip()
    def theta(self): return float(self.thetaEdit.value())
    def rs_r(self): return int(self.rsREdit.text() or "16")
    def rs_d(self): return int(self.rsDEdit.text() or "32")
    def rs_s(self): return int(self.rsSEdit.text() or "1024")
    def center_hz(self): return float(self.plutoFreqEdit.text() or "2400000000")
    def samp_rate(self): return float(self.plutoBwEdit.text() or "2000000")
    def rf_bw(self): return float(self.plutoBwEdit.text() or "2000000")
    def atten_db(self): return float(self.plutoPowerEdit.text() or "10")
    def buffer_size(self): return int(self.plutoBufferEdit.text() or "32768")
    def amp(self):
        try: return float(self.plutoAmpEdit.text() or "0.03")
        except Exception: return 0.03
    def pkt_size(self): return int(self.pktSizeEdit.text() or "512")
    def modulation(self):
        if self.modBpskRadio.isChecked(): return "bpsk"
        if self.mod16qamRadio.isChecked(): return "16qam"
        return "qpsk"

    def append_log(self, s: str): self.txLogText.append(s)
    def clear_log(self): self.txLogText.clear()
    def set_time(self, hhmmss: str): self.progressPanel.set_time(hhmmss)
    def set_overall_progress(self, pct: int): self.progressPanel.set_bar(pct)
    def set_load_text(self, text: str): self.progressPanel.set_load(text)

    def set_fhss_labels(self, f1_text, f2_text): self.fhssPanel.set_labels(f1_text, f2_text)
    def set_fhss_active_index(self, idx: int): self.fhssPanel.set_active_index(idx)

    def add_freq_widget(self, w: QWidget):
        """
        TxView ile aynı davranış: freqSinkLayout varsa eskiyi temizleyip w'yi ekler.
        Ek bir UI/sekme oluşturmaz.
        """
        if hasattr(self, "freqSinkLayout") and isinstance(self.freqSinkLayout, QBoxLayout):
            while self.freqSinkLayout.count():
                item = self.freqSinkLayout.takeAt(0)
                if item and item.widget():
                    item.widget().setParent(None)
            self.freqSinkLayout.addWidget(w)

    # ---------- Helpers ----------
    def _browse_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select File", "", "All Files (*)")
        if path:
            self.filePathEdit.setText(path)
            self.sig_file_selected.emit(path)
