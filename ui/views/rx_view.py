# -*- coding: utf-8 -*-
"""
ui/views/rx_view.py

RxView (UI only):
- PlutoSDR: Gain mode (Slow/Manual/Fast). The 'Gain' QLineEdit is visible only in Manual mode.
  * Gain mode default: Slow Attack
  * Gain default: "64"
- RS/OFDM: No Packet Size, **θ (theta) REMOVED**
- Under OFDM PHY: UDP Stats (separate panel, raw row REMOVED)
- Footer: compact progress bar + timer + buttons
- Progress: Bitunwrap 50% + Decode 50%  (UDP receive is NOT included in the progress)
- Signals: sig_start, sig_stop, sig_clear, sig_back, sig_file_selected
"""

from PyQt5.QtCore import Qt, QRegExp, pyqtSignal
from PyQt5.QtGui import QRegExpValidator, QFont
from PyQt5.QtWidgets import (
    QWidget, QLabel, QPushButton, QLineEdit, QTextEdit, QProgressBar,
    QVBoxLayout, QHBoxLayout, QGroupBox, QFormLayout, QRadioButton,
    QSizePolicy, QFrame, QBoxLayout, QComboBox, QGridLayout
)
from ui.style import apply as apply_theme

LABEL_W = 140

# ---- compactness knobs ----
CTRL_H        = 36     # min height for all input controls
FONT_BUMP_PT  = 1.0    # global font size bump
RB_IND_SZ     = 18     # radio indicator px


# ---------- helpers ----------

def _expand_h(widget: QWidget) -> QWidget:
    sp = widget.sizePolicy()
    sp.setHorizontalPolicy(QSizePolicy.Expanding)
    widget.setSizePolicy(sp)
    return widget

def int_line_edit(default_text: str = "", placeholder: str = "") -> QLineEdit:
    e = QLineEdit()
    e.setValidator(QRegExpValidator(QRegExp(r"\d+")))
    if placeholder: e.setPlaceholderText(placeholder)
    if default_text: e.setText(default_text)
    e.setMinimumHeight(CTRL_H)
    return _expand_h(e)

def add_form_row(form: QFormLayout, label_text: str, field: QWidget) -> QWidget:
    lbl = QLabel(label_text)
    lbl.setMinimumWidth(LABEL_W)
    lbl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    _expand_h(field)
    form.addRow(lbl, field)
    return field


# ---------- Progress (bitunwrap+decode) ----------

class ProgressPanel(QWidget):
    """Only shows overall % (Bitunwrap 50 + Decode 50) + BER (left) + timer (right)."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setFormat("%p%")
        self.bar.setTextVisible(True)
        _expand_h(self.bar)

        # BER (left)
        self.berLabel = QLabel("BER: —")
        self.berLabel.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        # Timer (right)
        self.timerLabel = QLabel("00:00:00")
        self.timerLabel.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        meta = QHBoxLayout()
        meta.setContentsMargins(0, 0, 0, 0)
        meta.setSpacing(6)
        meta.addWidget(self.berLabel)   # LEFT
        meta.addStretch(1)
        meta.addWidget(self.timerLabel) # RIGHT

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)
        root.addWidget(self.bar)
        root.addLayout(meta)

    def set_bar(self, v: int):
        self.bar.setValue(max(0, min(100, int(v))))

    def set_time(self, hhmmss: str):
        self.timerLabel.setText(hhmmss)

    def set_ber_text(self, text: str):
        self.berLabel.setText(text)

    def set_ber_value(self, ber: float):
        try:
            self.berLabel.setText(f"BER: {ber:.3e}")
        except Exception:
            self.berLabel.setText("BER: —")


# ---------- UDP Stats (KPI grid, RAW REMOVED) ----------

class UdpStatsPanel(QWidget):
    """
    set_kpis(pkts='0', bytes='0 B', rate='0 MB/s', queue='0/128', drops='0', flush='200 ms')
    set_raw(...) is a NO-OP (raw row removed)
    """
    def __init__(self, parent=None, show_title: bool = True):
        super().__init__(parent)

        title = QLabel("UDP Stats")
        title.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        divider = QFrame()
        divider.setFrameShape(QFrame.HLine)
        divider.setFrameShadow(QFrame.Sunken)
        divider.setObjectName("thinDivider")

        def make_kpi(caption: str):
            card = QFrame()
            card.setObjectName("kpiCard")
            card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

            v = QVBoxLayout(card)
            v.setContentsMargins(10, 8, 10, 8)
            v.setSpacing(2)

            cap = QLabel(caption)
            cap.setObjectName("kpiCaption")

            val = QLabel("—")
            val.setObjectName("kpiValue")

            mono = QFont("Consolas")
            mono.setStyleHint(QFont.Monospace)
            try:
                mono.setPointSizeF(self.font().pointSizeF() + 3)
            except Exception:
                mono.setPointSize(self.font().pointSize() + 3)
            val.setFont(mono)

            cap.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            val.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)

            v.addWidget(cap)
            v.addWidget(val)
            return card, val

        self._cardPkts,  self._pkts  = make_kpi("Packets")
        self._cardBytes, self._bytes = make_kpi("Bytes")
        self._cardRate,  self._rate  = make_kpi("Rate")
        self._cardQueue, self._queue = make_kpi("Queue")
        self._cardDrops, self._drops = make_kpi("Drops")
        self._cardFlush, self._flush = make_kpi("File Flush")

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        grid.addWidget(self._cardPkts,  0, 0)
        grid.addWidget(self._cardBytes, 0, 1)
        grid.addWidget(self._cardRate,  0, 2)
        grid.addWidget(self._cardQueue, 1, 0)
        grid.addWidget(self._cardDrops, 1, 1)
        grid.addWidget(self._cardFlush, 1, 2)
        for c in (0, 1, 2):
            grid.setColumnStretch(c, 1)

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        if show_title:
            root.addWidget(title)
            root.addWidget(divider)

        root.addLayout(grid)

        # compact yet “full” — keeps overall height even if an RS row is removed elsewhere
        self.setMinimumHeight(170)

    def set_kpis(self, pkts=None, bytes=None, rate=None, queue=None, drops=None, flush=None):
        if pkts  is not None: self._pkts.setText(pkts)
        if bytes is not None: self._bytes.setText(bytes)
        if rate  is not None: self._rate.setText(rate)
        if queue is not None: self._queue.setText(queue)
        if drops is not None: self._drops.setText(drops)
        if flush is not None: self._flush.setText(flush)

    def set_raw(self, text: str):
        # raw line removed; keep as no-op for API compatibility
        pass


# ---------- Main RX View ----------

class RxView(QWidget):
    sig_start = pyqtSignal()
    sig_stop = pyqtSignal()
    sig_clear = pyqtSignal()
    sig_back = pyqtSignal()
    sig_file_selected = pyqtSignal(str)  # controller will use this

    def __init__(self, parent=None):
        super().__init__(parent)

        # --- root layouts ---
        root = QHBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(10)

        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(0)  # no gaps between stacked panels
        right = QVBoxLayout()
        right.setSpacing(8)
        root.addLayout(left, 3)   # form + udp + footer
        root.addLayout(right, 2)  # log

        # --- File ---
        grp_file = QGroupBox("File")
        grp_file.setMinimumHeight(96)
        file_row = QHBoxLayout(grp_file)
        file_row.setContentsMargins(6, 6, 6, 6)
        file_row.setSpacing(8)

        self.filePathEdit = _expand_h(QLineEdit())
        self.filePathEdit.setReadOnly(True)
        self.filePathEdit.setPlaceholderText("Selected folder path…")
        self.filePathEdit.setMinimumHeight(CTRL_H)

        self.fileBrowseButton = QPushButton("Browse")
        self.fileBrowseButton.setMinimumHeight(CTRL_H)
        self.fileBrowseButton.setMinimumWidth(74)

        # File Type combobox (default mp4) — compact
        self.fileTypeCombo = QComboBox()
        self.fileTypeCombo.setObjectName("fileTypeCombo")
        self.fileTypeCombo.addItems(["mp4", "mp3", "jpg", "png", "txt"])
        self.fileTypeCombo.setCurrentIndex(0)
        self.fileTypeCombo.setMinimumHeight(CTRL_H)
        self.fileTypeCombo.setFixedWidth(84)

        file_row.addWidget(self.filePathEdit, 1)
        file_row.addWidget(self.fileBrowseButton, 0)
        file_row.addSpacing(8)  # breathing space between Browse and combo
        file_row.addWidget(self.fileTypeCombo, 0)
        left.addWidget(grp_file, 0)

        # --- PlutoSDR ---
        grp_pluto = QGroupBox("PlutoSDR")
        grp_pluto.setMinimumHeight(150)
        pluto_form = QFormLayout(grp_pluto)
        pluto_form.setHorizontalSpacing(6)
        pluto_form.setVerticalSpacing(6)
        pluto_form.setContentsMargins(6, 6, 6, 6)
        pluto_form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        pluto_form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        pluto_form.setFormAlignment(Qt.AlignTop)

        self.plutoFreqEdit   = add_form_row(pluto_form, "Frequency (Hz)", int_line_edit("2400000000", "Hz"))
        self.plutoBwEdit     = add_form_row(pluto_form, "Sample Rate",    int_line_edit("2000000",    "Hz"))
        self.plutoBufferEdit = add_form_row(pluto_form, "Buffer Size",    int_line_edit("32768",      "e.g. 32768"))

        # Gain mode row — in Manual mode the 'Gain' edit is shown
        self.gainModeRow = QWidget()
        gm = QHBoxLayout(self.gainModeRow)
        gm.setContentsMargins(0, 0, 0, 0)
        gm.setSpacing(8)

        self.gainModeCombo = QComboBox()
        self.gainModeCombo.addItems(["Slow Attack", "Manual", "Fast Attack"])
        self.gainModeCombo.setCurrentIndex(0)
        self.gainModeCombo.setMinimumHeight(CTRL_H)
        self.gainModeCombo.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.gainModeCombo.setMaximumWidth(220)

        self.gainLabel = QLabel("Gain")
        self.gainLabel.setVisible(False)

        self.gainEdit = int_line_edit("64")
        self.gainEdit.setVisible(False)
        self.gainEdit.setMaximumWidth(180)

        gm.addWidget(self.gainModeCombo, 0)
        gm.addWidget(self.gainLabel, 0)
        gm.addWidget(self.gainEdit, 0)
        gm.addStretch(1)

        add_form_row(pluto_form, "Gain Mode", self.gainModeRow)
        left.addWidget(grp_pluto, 0)

        self.gainModeCombo.currentIndexChanged.connect(self._on_gain_mode_changed)

        # --- Reed-Solomon (θ REMOVED, keep min height for visual balance) ---
        grp_rs = QGroupBox("Reed-Solomon")
        grp_rs.setMinimumHeight(160)
        rs_form = QFormLayout(grp_rs)
        rs_form.setHorizontalSpacing(6)
        rs_form.setVerticalSpacing(6)
        rs_form.setContentsMargins(6, 6, 6, 6)
        rs_form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        rs_form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        rs_form.setFormAlignment(Qt.AlignTop)

        self.rsREdit = add_form_row(rs_form, "r (parity)",     int_line_edit("16",   "r"))
        self.rsDEdit = add_form_row(rs_form, "d (interleave)", int_line_edit("32",   "d"))
        self.rsSEdit = add_form_row(rs_form, "s (slice)",      int_line_edit("1024", "s"))

        # Pad Mode combobox (RAW / ZERO / TEMPORAL)
        self.padModeCombo = QComboBox()
        self.padModeCombo.addItems(["RAW", "ZERO", "TEMPORAL"])
        self.padModeCombo.setCurrentIndex(1)  # default ZERO (kept from original behavior)
        self.padModeCombo.setMinimumHeight(CTRL_H)
        self.padModeCombo.setFixedWidth(120)
        add_form_row(rs_form, "Pad Mode", self.padModeCombo)

        left.addWidget(grp_rs, 0)

        # --- OFDM PHY (no Packet Size) ---
        grp_phy = QGroupBox("OFDM PHY")
        grp_phy.setMinimumHeight(0)  # size to contents
        grp_phy.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        phy_form = QFormLayout(grp_phy)
        phy_form.setHorizontalSpacing(6)
        phy_form.setVerticalSpacing(6)
        phy_form.setContentsMargins(6, 6, 6, 6)
        phy_form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        phy_form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        phy_form.setFormAlignment(Qt.AlignTop)

        mod_row = QHBoxLayout()
        mod_row.setSpacing(12)
        self.modBpskRadio  = QRadioButton("BPSK")
        self.modQpskRadio  = QRadioButton("QPSK"); self.modQpskRadio.setChecked(True)
        self.mod16qamRadio = QRadioButton("16-QAM")
        for rb in (self.modBpskRadio, self.modQpskRadio, self.mod16qamRadio):
            rb.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            rb.setMinimumHeight(CTRL_H)

        mod_wrap = QWidget(); mod_wrap.setLayout(mod_row)
        mod_row.addWidget(self.modBpskRadio)
        mod_row.addWidget(self.modQpskRadio)
        mod_row.addWidget(self.mod16qamRadio)
        mod_row.addStretch(1)

        # slightly enlarge radio indicators
        self.setStyleSheet(self.styleSheet() + f"""
        QRadioButton::indicator {{ width: {RB_IND_SZ}px; height: {RB_IND_SZ}px; }}
        """)

        add_form_row(phy_form, "Modulation", mod_wrap)
        left.addWidget(grp_phy, 0)

        # --- UDP Stats (SEPARATE PANEL) ---
        grp_udp = QGroupBox("UDP Stats")
        grp_udp.setMinimumHeight(180)
        udp_v = QVBoxLayout(grp_udp)
        udp_v.setContentsMargins(6, 6, 6, 6)
        udp_v.setSpacing(6)
        self.udpStatsPanel = UdpStatsPanel(show_title=False)
        udp_v.addWidget(self.udpStatsPanel)
        left.addWidget(grp_udp, 1)  # take remaining flexible space

        # --- Footer: progress + actions (COMPACT) ---
        footer = QFrame()
        footer.setObjectName("footerCard")
        footer.setMinimumHeight(120)
        footer.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

        footer_v = QVBoxLayout(footer)
        footer_v.setContentsMargins(14, 10, 14, 10)
        footer_v.setSpacing(8)

        self.progressPanel = ProgressPanel()
        footer_v.addWidget(self.progressPanel)
        footer_v.addSpacing(6)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        btn_row.setContentsMargins(0, 0, 0, 0)

        self.rxBackButton  = QPushButton("← Back")
        btn_row.addWidget(self.rxBackButton, 0)
        btn_row.addStretch(1)

        self.rxClearButton = QPushButton("Clear")
        self.rxStopButton  = QPushButton("Stop")
        self.rxStartButton = QPushButton("Start")
        self.rxStartButton.setObjectName("primary")
        self.rxStopButton.setObjectName("danger")
        for b in (self.rxClearButton, self.rxStopButton, self.rxStartButton):
            b.setMinimumHeight(32)

        btn_row.addWidget(self.rxClearButton, 0)
        btn_row.addWidget(self.rxStopButton, 0)
        btn_row.addWidget(self.rxStartButton, 0)
        footer_v.addLayout(btn_row)

        left.addWidget(footer, 0)

        # --- signals ---
        self.rxStartButton.clicked.connect(self.sig_start.emit)
        self.rxStopButton.clicked.connect(self.sig_stop.emit)
        self.rxClearButton.clicked.connect(self.sig_clear.emit)
        self.rxBackButton.clicked.connect(self.sig_back.emit)
        # NOTE: fileBrowseButton slot will be connected in the controller

        # --- right: log ---
        grp_log = QGroupBox("Log")
        log_v = QVBoxLayout(grp_log)
        log_v.setContentsMargins(6, 6, 6, 6)
        self.rxLogText = QTextEdit()
        self.rxLogText.setReadOnly(True)
        self.rxLogText.setLineWrapMode(QTextEdit.NoWrap)
        log_v.addWidget(self.rxLogText, 1)
        grp_log.setMinimumWidth(360)
        grp_log.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        right.addWidget(grp_log, 1)

        # Consistent margins/spacings for all group boxes
        for grp in self.findChildren(QGroupBox):
            if grp.layout():
                grp.layout().setContentsMargins(6, 6, 6, 6)
                if isinstance(grp.layout(), QFormLayout):
                    grp.layout().setHorizontalSpacing(6)
                    grp.layout().setVerticalSpacing(6)

        self._apply_scaling()
        apply_theme(self)

        # --- internal progress state (only BW/RS) ---
        self._bw_raw = 0   # Bitunwrap
        self._rs_raw = 0   # Decode

    # ----- getters -----
    def input_path(self) -> str: return self.filePathEdit.text().strip()
    def file_type(self) -> str:  return self.fileTypeCombo.currentText()
    def rs_r(self) -> int:       return int(self.rsREdit.text() or "16")
    def rs_d(self) -> int:       return int(self.rsDEdit.text() or "32")
    def rs_s(self) -> int:       return int(self.rsSEdit.text() or "1024")
    def pad_mode(self) -> int:   return self.padModeCombo.currentIndex()  # 0 RAW, 1 ZERO, 2 TEMPORAL
    def center_hz(self) -> float:return float(self.plutoFreqEdit.text() or "2400000000")
    def samp_rate(self) -> float:return float(self.plutoBwEdit.text() or "2000000")
    def rf_bw(self) -> float:    return float(self.plutoBwEdit.text() or "2000000")
    def buffer_size(self) -> int:return int(self.plutoBufferEdit.text() or "32768")

    def gain_mode(self) -> str:
        return ('slow', 'manual', 'fast')[self.gainModeCombo.currentIndex()]

    def gain_value(self):
        if self.gain_mode() != 'manual': return None
        t = self.gainEdit.text().strip()
        return float(t) if t else None

    def modulation(self) -> str:
        if self.modBpskRadio.isChecked(): return "bpsk"
        if self.mod16qamRadio.isChecked():return "16qam"
        return "qpsk"

    # ----- setters / helpers for controller -----
    def set_input_path(self, s: str):
        self.filePathEdit.setText(s)

    def set_start_enabled(self, enabled: bool):
        self.rxStartButton.setEnabled(bool(enabled))

    def set_ber_text(self, text: str):
        self.progressPanel.set_ber_text(text)

    def set_ber_value(self, ber: float):
        self.progressPanel.set_ber_value(ber)

    # ----- log & progress -----
    def append_log(self, s: str): self.rxLogText.append(s)
    def clear_log(self): self.rxLogText.clear()
    def set_time(self, hhmmss: str): self.progressPanel.set_time(hhmmss)
    def set_overall_progress(self, pct: int): self.progressPanel.set_bar(pct)

    # partial progress signals (Bitunwrap 50% + Decode 50%)
    def on_bw_progress(self, pct: int):
        self._bw_raw = max(0, min(100, int(pct)))
        total = (50*self._bw_raw + 50*self._rs_raw) // 100
        self.set_overall_progress(total)

    def on_rs_progress(self, pct: int):
        self._rs_raw = max(0, min(100, int(pct)))
        total = (50*self._bw_raw + 50*self._rs_raw) // 100
        self.set_overall_progress(total)

    def on_total_progress(self, pct: int):
        self.set_overall_progress(pct)

    # ----- embed helper -----
    def add_freq_widget(self, w: QWidget):
        if hasattr(self, "freqSinkLayout") and isinstance(self.freqSinkLayout, QBoxLayout):
            while self.freqSinkLayout.count():
                it = self.freqSinkLayout.takeAt(0)
                if it and it.widget(): it.widget().setParent(None)
            self.freqSinkLayout.addWidget(w)

    # ----- gain mode behaviour -----
    def _on_gain_mode_changed(self, idx: int):
        is_manual = (idx == 1)
        self.gainLabel.setVisible(is_manual)
        self.gainEdit.setVisible(is_manual)

    # ----- scale -----
    def _apply_scaling(self):
        f = self.font()
        try:
            f.setPointSizeF(f.pointSizeF() + FONT_BUMP_PT)
        except Exception:
            f.setPointSize(f.pointSize() + 1)
        self.setFont(f)
        for grp in self.findChildren(QGroupBox):
            lay = grp.layout()
            if isinstance(lay, QFormLayout):
                lay.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                lay.setFormAlignment(Qt.AlignTop)
                lay.setHorizontalSpacing(6)
                lay.setVerticalSpacing(6)
