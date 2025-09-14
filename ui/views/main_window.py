# -*- coding: utf-8 -*-
from pathlib import Path

from PyQt5.QtCore import Qt, QRect, QTimer, QObject, pyqtSignal, QSize
from PyQt5.QtGui import QKeySequence, QPixmap, QFont, QFontMetrics
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QStackedWidget, QPushButton, QLabel,
    QShortcut, QSizePolicy
)
from PyQt5.QtNetwork import QTcpSocket, QAbstractSocket, QNetworkProxy

from ui.style import apply as apply_theme
from ui.views.tx_view import TxView
from ui.views.rx_view import RxView
# !!! DİKKAT: FHSS view'lar burada import EDİLMEZ (lazy import ile aşağıda)

# ---------------------------
# Controller imports (güvenli)
# ---------------------------
try:
    from controllers.tx_controller import TxController
    print("[DEBUG] TxController successfully imported.")
except Exception as e:
    print(f"[ERROR] TxController import failed: {e}")
    TxController = None

try:
    from controllers.rx_controller import RxController
    print("[DEBUG] RxController successfully imported.")
except Exception as e:
    print(f"[ERROR] RxController import failed: {e}")
    RxController = None


def _resolve_logo_path() -> Path:
    """
    Portable logo resolver:
    1) paths.dir_doc()/paths.dir_docs() → logo.png
    2) paths.dir_project()/paths.dir_root() → doc/logo.png
    3) repo fallback: <this>/../../doc/logo.png
    """
    try:
        import paths
        if hasattr(paths, "dir_doc"):
            p = Path(paths.dir_doc()) / "logo.png"
            if p.exists():
                return p
        if hasattr(paths, "dir_docs"):
            p = Path(paths.dir_docs()) / "logo.png"
            if p.exists():
                return p
        if hasattr(paths, "dir_project"):
            p = Path(paths.dir_project()) / "doc" / "logo.png"
            if p.exists():
                return p
        if hasattr(paths, "dir_root"):
            p = Path(paths.dir_root()) / "doc" / "logo.png"
            if p.exists():
                return p
    except Exception:
        pass

    here = Path(__file__).resolve()
    fallback = here.parents[2] / "doc" / "logo.png"
    return fallback


# ============================================================
#   Minimal LED + Yazı: PlutoStatusPanel (sol üst köşe)
# ============================================================
class PlutoStatusPanel(QWidget):
    """
    Sol üst köşede küçük bir durum paneli:
      • Solda LED (kırmızı/yeşil)
      • Sağda kısa metin ('Pluto: Online / Offline')
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self._online = False

        self.setObjectName("plutoStatusPanel")
        self.setProperty("class", "status panel")

        self._led = QLabel("●", self)
        lf = QFont()
        lf.setPointSize(12)
        lf.setBold(True)
        self._led.setFont(lf)
        self._led.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        self._text = QLabel("Pluto: Not Found", self)
        tf = QFont()
        tf.setPointSize(10)
        self._text.setFont(tf)
        self._text.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        self._apply_led_color(False)

    def sizeHint(self) -> QSize:
        w = self._led.sizeHint().width() + 8 + self._text.sizeHint().width() + 12
        h = max(self._led.sizeHint().height(), self._text.sizeHint().height()) + 6
        return QSize(w, h)

    def resizeEvent(self, e):
        W, H = self.width(), self.height()
        led_w = self._led.sizeHint().width()
        led_h = self._led.sizeHint().height()
        text_w = max(10, W - led_w - 12)
        text_h = self._text.sizeHint().height()
        self._led.setGeometry(QRect(6, (H - led_h) // 2, led_w, led_h))
        self._text.setGeometry(QRect(6 + led_w + 6, (H - text_h) // 2, text_w, text_h))

    def set_online(self, online: bool):
        if self._online == online:
            return
        self._online = online
        self._text.setText("Pluto: Attached" if online else "Pluto: Not Found")
        self._apply_led_color(online)

    def _apply_led_color(self, online: bool):
        color = "#3CCB5A" if online else "#E05A5A"  # yeşil / kırmızı
        self._led.setStyleSheet(f"color: {color}; margin: 0; padding: 0;")


# ============================================================
#   SSH 22/tcp Pasif Yoklama + Debounce (dayanıklı sürüm)
# ============================================================
class SshPortWatcher(QObject):
    """
    192.168.2.1:22'yi periyodik yoklar.
    Sadece TCP connect; bağlanır bağlanmaz kapatır (auth yok).
    Debounce: 2 ardışık başarı = online, 2 ardışık hata = offline.
    """
    onlineChanged = pyqtSignal(bool)

    def __init__(self, host="192.168.2.1", port=22, interval_ms=1000, timeout_ms=300, parent=None):
        super().__init__(parent)
        self._host = host
        self._port = port
        self._interval = interval_ms
        self._timeout = timeout_ms

        self._timer = QTimer(self)
        self._timer.setInterval(self._interval)
        self._timer.timeout.connect(self._tick)

        self._sock = None
        self._timeout_timer = None
        self._inflight = False

        self._online = False
        self._succ_streak = 0
        self._fail_streak = 0
        self._debounce_n = 2

    def start(self):
        if not self._timer.isActive():
            self._timer.start()
            QTimer.singleShot(0, self._tick)

    def stop(self):
        self._timer.stop()
        self._finalize_attempt(clean_only=True)

    # ----- İç işler -----
    def _tick(self):
        if self._inflight:
            return
        self._inflight = True

        self._sock = QTcpSocket(self)
        self._sock.setProxy(QNetworkProxy(QNetworkProxy.NoProxy))
        self._sock.connected.connect(self._on_connected)
        self._sock.errorOccurred.connect(self._on_error)
        self._sock.disconnected.connect(self._on_disconnected)
        self._sock.stateChanged.connect(self._on_state_changed)

        self._timeout_timer = QTimer(self)
        self._timeout_timer.setSingleShot(True)
        self._timeout_timer.setInterval(self._timeout)
        self._timeout_timer.timeout.connect(self._on_timeout)
        self._timeout_timer.start()

        self._sock.connectToHost(self._host, self._port, QTcpSocket.ReadOnly)

    def _on_connected(self):
        self._mark_success()
        self._finalize_attempt()

    def _on_error(self, _err):
        self._mark_failure()
        self._finalize_attempt()

    def _on_timeout(self):
        # Timeout'u bizzat failure say ve kapat
        self._mark_failure()
        if self._sock and self._sock.state() != QAbstractSocket.UnconnectedState:
            self._sock.abort()
        self._finalize_attempt()

    def _on_disconnected(self):
        # Ek güvence; burada ekstra iş yapmaya gerek yok
        pass

    def _on_state_changed(self, _state):
        # Debug gerekiyorsa print edebilirsin
        pass

    def _finalize_attempt(self, clean_only=False):
        if self._timeout_timer:
            self._timeout_timer.stop()
            self._timeout_timer.deleteLater()
            self._timeout_timer = None

        if self._sock:
            try:
                if self._sock.state() != QAbstractSocket.UnconnectedState:
                    self._sock.abort()
            except Exception:
                pass
            self._sock.deleteLater()
            self._sock = None

        self._inflight = False
        if not clean_only:
            # onlineChanged sinyali _emit_online içinde atılıyor; burada ekstra yok
            pass

    def _emit_online(self, new_state: bool):
        if self._online != new_state:
            self._online = new_state
            self.onlineChanged.emit(self._online)

    def _mark_success(self):
        self._succ_streak += 1
        self._fail_streak = 0
        if self._succ_streak >= self._debounce_n:
            self._emit_online(True)

    def _mark_failure(self):
        self._fail_streak += 1
        self._succ_streak = 0
        if self._fail_streak >= self._debounce_n:
            self._emit_online(False)


# ============================================================
#   UI Bileşenleri
# ============================================================
class NavButton(QPushButton):
    def __init__(self, text, kind="primary", parent=None):
        super().__init__(text, parent)
        self.setObjectName(text.replace(" ", "") + "Btn")
        self.setProperty("class", f"nav {kind}")
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)


class LandingCanvas(QWidget):
    """
    Pixel-based layout.
    - Title: centered; fixed Y
    - Buttons: fixed size & spacing
      İSTENEN SIRA: Tx → Rx → Tx(FHSS) → Rx(FHSS)
    - Exit: bottom-left; Logo: bottom-right
    - Sol üst: PlutoStatusPanel
    """
    def __init__(self, parent=None):
        super().__init__(parent)

        # --- Layout parameters
        self.MARGIN_X     = 28
        self.MARGIN_Y     = 28
        self.TITLE_Y      = 100
        self.TITLE_SIZE   = 26
        self.BUTTON_W     = 500
        self.BUTTON_H     = 150
        self.BTN_GAP      = 36
        self.BTN_TOP_GAP  = 100
        self.EXIT_W       = 220
        self.EXIT_H       = 56
        self.LOGO_W       = 100  # varsayılan minimum referans

        # --- Widgets
        self.title = QLabel("Pluto OFDM Transceiver", self)
        tf = QFont(); tf.setPointSize(self.TITLE_SIZE); tf.setBold(True)
        self.title.setFont(tf)
        self.title.setAlignment(Qt.AlignHCenter | Qt.AlignVCenter)

        # İSTENEN BUTON SIRASI: Tx → Rx → Tx(FHSS) → Rx(FHSS)
        self.btnTx = NavButton("Transmitter", "primary", self)
        self.btnTx.setFixedSize(self.BUTTON_W, self.BUTTON_H)

        self.btnRx = NavButton("Receiver", "secondary", self)
        self.btnRx.setFixedSize(self.BUTTON_W, self.BUTTON_H)

        self.btnTxFhss = NavButton("FHSS Transmitter", "primary", self)
        self.btnTxFhss.setFixedSize(self.BUTTON_W, self.BUTTON_H)

        self.btnRxFhss = NavButton("FHSS Receiver", "secondary", self)
        self.btnRxFhss.setFixedSize(self.BUTTON_W, self.BUTTON_H)

        self.btnExit = NavButton("Exit", "danger", self)
        self.btnExit.setFixedSize(self.EXIT_W, self.EXIT_H)

        self.logo = QLabel(self)
        logo_path = _resolve_logo_path()
        pm = QPixmap(str(logo_path))
        if not pm.isNull():
            self.logo.setPixmap(pm)

        # Sol üst: Pluto durum paneli
        self.plutoPanel = PlutoStatusPanel(self)

    def resizeEvent(self, e):
        W, H = self.width(), self.height()

        # Pluto panel — sol üst
        pp_sz = self.plutoPanel.sizeHint()
        self.plutoPanel.setGeometry(QRect(self.MARGIN_X, self.MARGIN_Y, pp_sz.width(), pp_sz.height()))

        # Başlık
        fm = QFontMetrics(self.title.font())
        title_h = fm.height() + 8
        title_w = min(int(W * 0.9), 1100)
        title_x = (W - title_w) // 2
        title_y = self.TITLE_Y
        self.title.setGeometry(QRect(title_x, title_y, title_w, title_h))

        # Butonlar (Tx → Rx → Tx(FHSS) → Rx(FHSS))
        btn_x = (W - self.BUTTON_W) // 2
        first_btn_y = title_y + title_h + self.BTN_TOP_GAP

        self.btnTx.setGeometry(QRect(btn_x, first_btn_y, self.BUTTON_W, self.BUTTON_H))

        self.btnRx.setGeometry(QRect(
            btn_x,
            first_btn_y + self.BUTTON_H + self.BTN_GAP,
            self.BUTTON_W, self.BUTTON_H
        ))

        self.btnTxFhss.setGeometry(QRect(
            btn_x,
            self.btnRx.geometry().bottom() + self.BTN_GAP,
            self.BUTTON_W, self.BUTTON_H
        ))

        self.btnRxFhss.setGeometry(QRect(
            btn_x,
            self.btnTxFhss.geometry().bottom() + self.BTN_GAP,
            self.BUTTON_W, self.BUTTON_H
        ))

        # Exit (sol-alt)
        exit_x = self.MARGIN_X
        exit_y = H - self.MARGIN_Y - self.EXIT_H
        self.btnExit.setGeometry(QRect(exit_x, exit_y, self.EXIT_W, self.EXIT_H))

        # Logo (sağ-alt)
        if self.logo.pixmap() and not self.logo.pixmap().isNull():
            max_w = max(self.LOGO_W, int(W * 0.14))
            max_h = max(self.LOGO_W, int(H * 0.12))
            pm_scaled = self.logo.pixmap().scaled(
                max_w, max_h, Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
            self.logo.setPixmap(pm_scaled)
            logo_w = pm_scaled.width()
            logo_h = pm_scaled.height()
        else:
            logo_w = self.LOGO_W
            logo_h = self.LOGO_W

        logo_x = W - self.MARGIN_X - logo_w
        logo_y = H - self.MARGIN_Y - logo_h
        self.logo.setGeometry(QRect(logo_x, logo_y, logo_w, logo_h))


# ============================================================
#   Main Window
# ============================================================
class MainWindow(QMainWindow):
    def __init__(self, services, parent=None):
        super().__init__(parent)
        self.services = services

        self.setWindowTitle("OFDM Control")
        self.setMinimumSize(960, 620)
        self.resize(1280, 768)
        apply_theme(self)

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)

        # Pages (FHSS sayfaları lazy)
        self.landing    = LandingCanvas()
        self.txPage     = TxView()
        self.txFhssPage = None   # ← lazy (tx fhss)
        self.rxPage     = RxView()
        self.rxFhssPage = None   # ← lazy (rx fhss)

        # Stack indices
        self.HOME     = self.stack.addWidget(self.landing)
        self.TX       = self.stack.addWidget(self.txPage)
        self.TX_FHSS  = -1  # henüz eklenmedi
        self.RX       = self.stack.addWidget(self.rxPage)
        self.RX_FHSS  = -1  # henüz eklenmedi

        # Landing → page navigation
        self.landing.btnTx.clicked.connect(self.show_tx)
        self.landing.btnTxFhss.clicked.connect(self.show_tx_fhss)
        self.landing.btnRx.clicked.connect(self.show_rx)
        self.landing.btnRxFhss.clicked.connect(self.show_rx_fhss)
        self.landing.btnExit.clicked.connect(self.close)

        # Back buttons
        self.txPage.sig_back.connect(self.show_home)
        self.rxPage.sig_back.connect(self.show_home)

        # ---------------------------
        # Controller binding
        # ---------------------------
        # Tx (klasik)
        self.txController = None
        if TxController is not None:
            try:
                self.txController = TxController(mode="inproc")
                if hasattr(self.txController, "bind_view"):
                    self.txController.bind_view(self.txPage)
                print("[DEBUG] TxController instance created for TxView.")
            except Exception as e:
                print(f"[ERROR] TxController init error (TxView): {e}")

        # Rx (klasik)
        self.rxController = None
        if RxController is not None:
            try:
                try:
                    self.rxController = RxController(self.rxPage)
                except TypeError:
                    self.rxController = RxController()
                    if hasattr(self.rxController, "bind_view"):
                        self.rxController.bind_view(self.rxPage)
                print("[DEBUG] RxController instance created.")
            except Exception as e:
                print(f"[ERROR] RxController init error: {e}")

        # FHSS controller'lar lazy
        self.txFhssController = None
        self.rxFhssController = None

        # Shortcuts (sıraya uygun)
        QShortcut(QKeySequence("Esc"),    self, activated=self.show_home)
        QShortcut(QKeySequence("Ctrl+1"), self, activated=self.show_tx)
        QShortcut(QKeySequence("Ctrl+2"), self, activated=self.show_rx)
        QShortcut(QKeySequence("Ctrl+3"), self, activated=self.show_tx_fhss)
        QShortcut(QKeySequence("Ctrl+4"), self, activated=self.show_rx_fhss)
        QShortcut(QKeySequence("Ctrl+Q"), self, activated=self.close)

        # ---------------------------
        # Pluto canlı durum izlemesi
        # ---------------------------
        self._ssh_watch = SshPortWatcher(
            host="192.168.2.1", port=22,
            interval_ms=1000,  # 1 sn
            timeout_ms=300,    # 300 ms
            parent=self
        )
        self._ssh_watch.onlineChanged.connect(self._on_pluto_online_changed)
        self._ssh_watch.start()

        self.show_home()
        self.showFullScreen()

    # Güvenli temizlik
    def closeEvent(self, event):
        try:
            if hasattr(self, "_ssh_watch") and self._ssh_watch:
                self._ssh_watch.stop()
        except Exception:
            pass
        super().closeEvent(event)

    # ----- Pluto status callback -----
    def _on_pluto_online_changed(self, online: bool):
        if self.landing and hasattr(self.landing, "plutoPanel"):
            self.landing.plutoPanel.set_online(online)

    # ---------- Lazy FHSS kurulum: TX ----------
    def _ensure_tx_fhss_page(self):
        if self.txFhssPage is not None:
            return

        try:
            from ui.views.tx_fhss_view import TxFhssView
        except Exception as e:
            print(f"[ERROR] TxFhssView import failed: {e}")
            self.landing.btnTxFhss.setEnabled(False)
            return

        try:
            self.txFhssPage = TxFhssView()
            self.TX_FHSS = self.stack.addWidget(self.txFhssPage)
            self.txFhssPage.sig_back.connect(self.show_home)
            print("[DEBUG] TxFhssView instance created & added to stack.")
        except Exception as e:
            print(f"[ERROR] TxFhssView init error: {e}")
            self.txFhssPage = None
            self.landing.btnTxFhss.setEnabled(False)
            return

        try:
            from controllers.tx_fhss_controller import TxFhssController
        except Exception as e:
            print(f"[ERROR] TxFhssController import failed: {e}")
            self.landing.btnTxFhss.setEnabled(False)
            return

        try:
            try:
                self.txFhssController = TxFhssController(self.txFhssPage)
            except TypeError:
                self.txFhssController = TxFhssController()
                if hasattr(self.txFhssController, "bind_view"):
                    self.txFhssController.bind_view(self.txFhssPage)
            print("[DEBUG] TxFhssController instance created for TxFhssView.")
        except Exception as e:
            print(f"[ERROR] FHSS controller init/bind error: {e}")
            self.txFhssController = None
            self.landing.btnTxFhss.setEnabled(False)

    # ---------- Lazy FHSS kurulum: RX ----------
    def _ensure_rx_fhss_page(self):
        if self.rxFhssPage is not None:
            return

        try:
            from ui.views.rx_fhss_view import RxFhssView
        except Exception as e:
            print(f"[ERROR] RxFhssView import failed: {e}")
            self.landing.btnRxFhss.setEnabled(False)
            return

        try:
            self.rxFhssPage = RxFhssView()
            self.RX_FHSS = self.stack.addWidget(self.rxFhssPage)
            self.rxFhssPage.sig_back.connect(self.show_home)
            print("[DEBUG] RxFhssView instance created & added to stack.")
        except Exception as e:
            print(f"[ERROR] RxFhssView init error: {e}")
            self.rxFhssPage = None
            self.landing.btnRxFhss.setEnabled(False)
            return

        try:
            from controllers.rx_fhss_controller import RxFhssController
        except Exception as e:
            print(f"[ERROR] RxFhssController import failed: {e}")
            self.landing.btnRxFhss.setEnabled(False)
            return

        try:
            try:
                self.rxFhssController = RxFhssController(self.rxFhssPage)
            except TypeError:
                self.rxFhssController = RxFhssController()
                if hasattr(self.rxFhssController, "bind_view"):
                    self.rxFhssController.bind_view(self.rxFhssPage)
            print("[DEBUG] RxFhssController instance created for RxFhssView.")
        except Exception as e:
            print(f"[ERROR] RX FHSS controller init/bind error: {e}")
            self.rxFhssController = None
            self.landing.btnRxFhss.setEnabled(False)

    # ---------- Navigation helpers ----------
    def show_home(self):
        self.stack.setCurrentIndex(self.HOME)

    def show_tx(self):
        self.stack.setCurrentIndex(self.TX)

    def show_tx_fhss(self):
        self._ensure_tx_fhss_page()
        if self.TX_FHSS != -1:
            self.stack.setCurrentIndex(self.TX_FHSS)

    def show_rx(self):
        self.stack.setCurrentIndex(self.RX)

    def show_rx_fhss(self):
        self._ensure_rx_fhss_page()
        if self.RX_FHSS != -1:
            self.stack.setCurrentIndex(self.RX_FHSS)
