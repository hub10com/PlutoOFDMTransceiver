# -*- coding: utf-8 -*-
"""
ui/style.py
Koyu, modern tema. Tüm uygulamaya apply() ile uygulanır.
"""

THEME_QSS = """
QMainWindow { background: #0f1115; }

/* Başlık niteliğinde label kullanmak için: lbl.setObjectName("title") */
QLabel#title {
    color: #e6edf3;
    font-size: 22px;
    font-weight: 600;
}

/* Metinler */
QLabel { color: #cfd8e3; }
QRadioButton, QCheckBox { color: #cfd8e3; }

/* Butonlar (nötr) */
QPushButton {
    background: #1f232a;
    color: #e6edf3;
    border: 1px solid #30363d;
    border-radius: 12px;
    padding: 8px 14px;
}
QPushButton:hover  { background: #2a2f37; }
QPushButton:pressed{ background: #161b22; }

/* Kart tarzı büyük butonlar */
QPushButton[class="card"] {
    font-size: 20px;
    padding: 28px;
    min-width: 240px; min-height: 220px;
    border-radius: 18px;
}

/* Gruplar */
QGroupBox {
    color: #e6edf3;
    border: 1px solid #30363d;
    border-radius: 10px;
    margin-top: 10px;
    padding: 8px 10px 10px 10px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px; top: -4px;
    padding: 0 4px;
    color: #9fb3c8;
}

/* Girişler */
QDoubleSpinBox, QSpinBox, QLineEdit, QComboBox {
    background: #0f1115;
    color: #e6edf3;
    border: 1px solid #30363d;
    border-radius: 10px;
    padding: 6px 10px;
    min-height: 34px;
}
QLineEdit[readOnly="true"] {
    background: #0d0f13;
    color: #9fb3c8;
}

/* Log alanı */
QTextEdit {
    background: #141821;
    color: #e6edf3;
    border: 1px solid #30363d;
    border-radius: 10px;
}

/* ProgressBar */
QProgressBar {
    background: #1a1f29;
    border: 1px solid #30363d;
    border-radius: 10px;
    height: 22px;
    text-align: center;
    color: #c7d5e0;
    font-weight: 600;
}
QProgressBar::chunk {
    background: #2f81f7;
    border-radius: 10px;
}

/* Slider */
QSlider::groove:horizontal { height: 6px; background: #30363d; border-radius: 3px; }
QSlider::handle:horizontal { width: 16px; height: 16px; margin: -5px 0; border-radius: 8px; background: #8ab4f8; }

/* Radio button'ları "chip" gibi göster */
QRadioButton {
    padding: 4px 10px;
    border: 1px solid #30363d;
    border-radius: 12px;
    margin-right: 6px;
}
QRadioButton::indicator { width: 14px; height: 14px; }

/* Alt footer kartı */
#footerCard {
    background: #0f1115;
    border: 1px solid #30363d;
    border-radius: 12px;
    padding: 10px 12px;
}

/* Birincil ve uyarı butonları */
QPushButton#primary {
    background: #2f81f7;
    color: white;
    border: 1px solid #2f81f7;
}
QPushButton#primary:hover  { background: #3b8bf9; }
QPushButton#primary:pressed{ background: #2a6fe0; }

QPushButton#danger {
    background: #2a1f22;
    color: #ffb4b4;
    border: 1px solid #6b2e33;
}
QPushButton#danger:hover  { background: #3a2a2d; }
QPushButton#danger:pressed{ background: #251a1d; }

/* =========================
   FHSS PANEL STİLLERİ
   ========================= */

/* FHSS kartı — Log ile aynı arka plan */
QFrame#fhssCard {
    background: #141821;              /* Log ile birebir */
    border: 1px solid #30363d;
    border-radius: 10px;
}

/* İsteğe bağlı başlık görünümü */
QLabel#fhssTitle {
    color: #cfe2ff;                   /* hafif vurgulu başlık */
    font-weight: 600;
}

/* FHSS hücre kutusu varsayılan (pasif) */
QFrame#fhssBox {
    background: rgba(255,255,255,0.06);
    border: 1px solid #30363d;
    border-radius: 10px;
}

/* FHSS hücre kutusu aktif (controller view'e property yazar) */
QFrame#fhssBox[active="true"] {
    background: #2f81f7;          /* aktif: mavi */
    border: 1px solid #3b8bf9;    /* biraz daha koyu mavi çerçeve */
}
"""

def apply(app_or_widget):
    app_or_widget.setStyleSheet(THEME_QSS)
