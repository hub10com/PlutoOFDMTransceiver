# -*- coding: utf-8 -*-
"""
ui/views/tx_view.py

TxView: Pluto OFDM Transmitter interface.
- File selection
- PlutoSDR / RS / OFDM PHY parameters
- Progress bar + load text + timer
- Bottom buttons: [Clear] [Stop] [Start] + (← Back)
- Backward compatibility: sig_send (emitted together with Start)

Note: Business logic is in the controller. Listen to these signals:
    sig_start, sig_stop, sig_clear, sig_back, sig_file_selected
"""

from pathlib import Path
from PyQt5.QtCore import Qt, QRegExp, pyqtSignal
from PyQt5.QtGui import QRegExpValidator
from PyQt5.QtWidgets import (
    QWidget, QLabel, QPushButton, QLineEdit, QTextEdit, QProgressBar,
    QVBoxLayout, QHBoxLayout, QGroupBox, QFormLayout, QRadioButton,
    QSizePolicy, QFrame, QFileDialog, QDoubleSpinBox, QBoxLayout
)

from ui.style import apply as apply_theme

LABEL_W = 140

# ---- Progress weights (aligned with controller) ----
_W_RS = 15
_W_BW = 15
_W_TX = 70

# ----------- Input Helpers -----------

def int_line_edit(default_text: str = "", placeholder: str = "") -> QLineEdit:
    e = QLineEdit()
    e.setValidator(QRegExpValidator(QRegExp(r"\d+")))
    if placeholder:
        e.setPlaceholderText(placeholder)
    if default_text:
        e.setText(default_text)
    return e

def float_line_edit(default_text: str = "", placeholder: str = "") -> QLineEdit:
    e = QLineEdit()
    e.setValidator(QRegExpValidator(QRegExp(r"^\d*\.?\d+$")))
    if placeholder:
        e.setPlaceholderText(placeholder)
    if default_text:
        e.setText(default_text)
    return e

def float_spinbox(v=4.0, mn=0.5, mx=64.0, step=0.5, decimals: int = 2) -> QDoubleSpinBox:
    sb = QDoubleSpinBox()
    sb.setRange(mn, mx)
    sb.setSingleStep(step)
    sb.setDecimals(decimals)
    sb.setValue(v)
    sb.setKeyboardTracking(False)
    sb.setButtonSymbols(QDoubleSpinBox.UpDownArrows)
    sb.lineEdit().setReadOnly(True)
    return sb

def add_form_row(form: QFormLayout, label_text: str, field: QWidget) -> QWidget:
    lbl = QLabel(label_text)
    lbl.setMinimumWidth(LABEL_W)
    lbl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    form.addRow(lbl, field)
    return field

# ----------- Progress Panel -----------

class ProgressPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setFormat("%p%")
        self.bar.setTextVisible(True)

        self.loadLabel = QLabel("Load: %0")
        self.timerLabel = QLabel("00:00:00")
        self.loadLabel.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.timerLabel.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        meta = QHBoxLayout()
        meta.setContentsMargins(0, 0, 0, 0)
        meta.setSpacing(6)
        meta.addWidget(self.loadLabel)
        meta.addStretch(1)
        meta.addWidget(self.timerLabel)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)
        root.addWidget(self.bar)
        root.addLayout(meta)

    def set_bar(self, v: int):
        self.bar.setValue(max(0, min(100, int(v))))

    def set_load(self, text: str):
        self.loadLabel.setText(f"Load: {text}")

    def set_time(self, hhmmss: str):
        self.timerLabel.setText(hhmmss)

# ----------- Main TX View -----------

class TxView(QWidget):
    sig_send = pyqtSignal()
    sig_start = pyqtSignal()
    sig_stop = pyqtSignal()
    sig_clear = pyqtSignal()
    sig_back = pyqtSignal()
    sig_file_selected = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)

        self._rs_raw = 0
        self._bw_raw = 0

        root = QHBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(12)

        left = QVBoxLayout();  left.setSpacing(12)
        right = QVBoxLayout(); right.setSpacing(12)

        root.addLayout(left, 3)
        root.addLayout(right, 2)

        # --- File Panel ---
        grp_file = QGroupBox("File")
        file_row = QHBoxLayout(grp_file)
        file_row.setSpacing(8)
        self.filePathEdit = QLineEdit()
        self.filePathEdit.setReadOnly(True)
        self.filePathEdit.setPlaceholderText("Selected file path/name…")
        self.fileBrowseButton = QPushButton("Browse")
        self.fileBrowseButton.clicked.connect(self._browse_file)
        file_row.addWidget(self.filePathEdit, 1)
        file_row.addWidget(self.fileBrowseButton, 0)
        left.addWidget(grp_file)

        # --- PlutoSDR Panel ---
        grp_pluto = QGroupBox("PlutoSDR")
        pluto_form = QFormLayout(grp_pluto)
        pluto_form.setHorizontalSpacing(10)
        pluto_form.setVerticalSpacing(8)
        self.plutoFreqEdit   = add_form_row(pluto_form, "Frequency (Hz)",   int_line_edit("2400000000", "Hz"))
        self.plutoBwEdit     = add_form_row(pluto_form, "Sample Rate",      int_line_edit("2000000",    "Hz"))
        self.plutoPowerEdit  = add_form_row(pluto_form, "Gain (dB)", int_line_edit("10",         "dB"))
        self.plutoBufferEdit = add_form_row(pluto_form, "Buffer Size",      int_line_edit("32768",      "e.g. 32768"))
        self.plutoAmpEdit    = add_form_row(pluto_form, "Amplitude",        float_line_edit("0.03", "0.00 – 1.00"))
        left.addWidget(grp_pluto)

        # --- RS Panel ---
        grp_rs = QGroupBox("Reed-Solomon")
        rs_form = QFormLayout(grp_rs)
        rs_form.setHorizontalSpacing(10)
        rs_form.setVerticalSpacing(8)
        self.rsREdit   = add_form_row(rs_form, "r (parity)",     int_line_edit("16",   "r"))
        self.rsDEdit   = add_form_row(rs_form, "d (interleave)", int_line_edit("32",   "d"))
        self.rsSEdit   = add_form_row(rs_form, "s (slice)",      int_line_edit("1024", "s"))
        self.thetaEdit = add_form_row(rs_form, "θ (theta)",      float_spinbox(4.0, 0.5, 64.0, 0.5))
        left.addWidget(grp_rs)

        # --- PHY Panel ---
        grp_phy = QGroupBox("OFDM PHY")
        phy_form = QFormLayout(grp_phy)
        phy_form.setHorizontalSpacing(10)
        phy_form.setVerticalSpacing(8)
        mod_row = QHBoxLayout()
        self.modBpskRadio  = QRadioButton("BPSK")
        self.modQpskRadio  = QRadioButton("QPSK")
        self.mod16qamRadio = QRadioButton("16-QAM")
        self.modQpskRadio.setChecked(True)
        mod_row.addWidget(self.modBpskRadio)
        mod_row.addWidget(self.modQpskRadio)
        mod_row.addWidget(self.mod16qamRadio)
        mod_row.addStretch(1)
        mod_wrap = QWidget(); mod_wrap.setLayout(mod_row)
        add_form_row(phy_form, "Modulation", mod_wrap)
        self.pktSizeEdit = add_form_row(phy_form, "Packet Size", int_line_edit("512", "byte"))
        left.addWidget(grp_phy)

        # --- Footer ---
        footer = QFrame()
        footer.setObjectName("footerCard")
        footer_v = QVBoxLayout(footer)
        footer_v.setContentsMargins(8, 8, 8, 8)
        footer_v.setSpacing(10)

        self.progressPanel = ProgressPanel()
        footer_v.addWidget(self.progressPanel)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
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

        self.txStartButton.clicked.connect(self.sig_start.emit)
        self.txStartButton.clicked.connect(self.sig_send.emit)
        self.txStopButton.clicked.connect(self.sig_stop.emit)
        self.txClearButton.clicked.connect(self.sig_clear.emit)
        self.txBackButton.clicked.connect(self.sig_back.emit)

        # Right: Log
        grp_log = QGroupBox("Log")
        log_v = QVBoxLayout(grp_log)
        self.txLogText = QTextEdit()
        self.txLogText.setReadOnly(True)
        self.txLogText.setLineWrapMode(QTextEdit.NoWrap)
        log_v.addWidget(self.txLogText, 1)
        grp_log.setMinimumWidth(320)
        grp_log.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        right.addWidget(grp_log, 1)

        self._apply_scaling()
        apply_theme(self)

    # --------- Getters ---------
    def input_path(self) -> str: return self.filePathEdit.text().strip()
    def theta(self) -> float: return float(self.thetaEdit.value())
    def rs_r(self) -> int: return int(self.rsREdit.text() or "16")
    def rs_d(self) -> int: return int(self.rsDEdit.text() or "32")
    def rs_s(self) -> int: return int(self.rsSEdit.text() or "1024")
    def center_hz(self) -> float: return float(self.plutoFreqEdit.text() or "2400000000")
    def samp_rate(self) -> float: return float(self.plutoBwEdit.text() or "2000000")
    def rf_bw(self) -> float: return float(self.plutoBwEdit.text() or "2000000")
    def atten_db(self) -> float: return float(self.plutoPowerEdit.text() or "10")
    def buffer_size(self) -> int: return int(self.plutoBufferEdit.text() or "32768")
    def amp(self) -> float:
        try: return float(self.plutoAmpEdit.text() or "0.03")
        except Exception: return 0.03
    def pkt_size(self) -> int: return int(self.pktSizeEdit.text() or "512")
    def modulation(self) -> str:
        if self.modBpskRadio.isChecked(): return "bpsk"
        if self.mod16qamRadio.isChecked(): return "16qam"
        return "qpsk"

    # --------- Log & Progress ----------
    def append_log(self, s: str): self.txLogText.append(s)
    def clear_log(self): self.txLogText.clear()
    def set_time(self, hhmmss: str): self.progressPanel.set_time(hhmmss)
    def set_overall_progress(self, pct: int): self.progressPanel.set_bar(pct)

    def on_rs_progress(self, pct: int):
        self._rs_raw = max(0, min(100, int(pct)))
        total = ((_W_RS * self._rs_raw) + (_W_BW * self._bw_raw)) // 100
        self.set_overall_progress(total)
    def on_bw_progress(self, pct: int):
        self._bw_raw = max(0, min(100, int(pct)))
        total = ((_W_RS * self._rs_raw) + (_W_BW * self._bw_raw)) // 100
        self.set_overall_progress(total)
    def on_total_progress(self, pct: int): self.set_overall_progress(pct)

    def _browse_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select File to Transmit", "", "All Files (*)")
        if path:
            self.filePathEdit.setText(path)
            self.sig_file_selected.emit(path)
    def set_load_text(self, text: str): self.progressPanel.set_load(text)

    def add_freq_widget(self, w: QWidget):
        if hasattr(self, "freqSinkLayout") and isinstance(self.freqSinkLayout, QBoxLayout):
            while self.freqSinkLayout.count():
                item = self.freqSinkLayout.takeAt(0)
                if item and item.widget(): item.widget().setParent(None)
            self.freqSinkLayout.addWidget(w)

    def _apply_scaling(self):
        f = self.font()
        try: f.setPointSizeF(f.pointSizeF() + 2.0)
        except Exception: f.setPointSize(f.pointSize() + 2)
        self.setFont(f)
        for grp in self.findChildren(QGroupBox):
            lay = grp.layout()
            if isinstance(lay, QFormLayout):
                lay.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                lay.setFormAlignment(Qt.AlignTop)
                lay.setHorizontalSpacing(10)
                lay.setVerticalSpacing(8)
