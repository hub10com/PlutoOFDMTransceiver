# -*- coding: utf-8 -*-
"""
main.py — PlutoOFDMTransceiver (PyQt5)

• Adds project root (app_root) to sys.path and sets working dir
• Prepares PATH / DLL search path for portable Radioconda (python\)
• (Windows) Sets AppUserModelID (for proper taskbar grouping)
• Enables high DPI scaling and pixmap scaling (before QApplication)
• Launches the application and shows MainWindow
• Logs uncaught exceptions to console and (if possible) to a dialog
"""

import os
import sys
import traceback
from pathlib import Path

# --- Import paths helper -----------------------------------------------------
from paths import app_root, prepare_dll_search_path, portable_library_bin

# --- Add project root to sys.path and set working directory ------------------
PROJECT_ROOT = app_root()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.chdir(str(PROJECT_ROOT))

# --- Prepare DLL / PATH for portable Radioconda ------------------------------
os.environ["PATH"] = str(portable_library_bin()) + os.pathsep + os.environ.get("PATH", "")
prepare_dll_search_path()

print("[DEBUG] APP PYTHON:", sys.executable)

# --- (Windows) Taskbar grouping via AppUserModelID ---------------------------
if sys.platform.startswith("win"):
    try:
        import ctypes  # noqa: E402
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PlutoOFDMTransceiver")
    except Exception:
        pass

# --- Qt attributes (must be set before QApplication) -------------------------
from PyQt5.QtCore import Qt, QCoreApplication  # noqa: E402
QCoreApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
QCoreApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

from PyQt5.QtWidgets import QApplication, QMessageBox  # noqa: E402

# --- Main window import ------------------------------------------------------
from ui.views.main_window import MainWindow  # noqa: E402


def build_services():
    """
    Hook to initialize RS, Bitwrap, OFDM TX/RX services.
    Currently returns a stub.
    """
    return object()


def install_excepthook():
    """Catch uncaught exceptions, print to stderr and show in a dialog if possible."""
    def _hook(etype, value, tb):
        text = "".join(traceback.format_exception(etype, value, tb))
        try:
            # Log to console
            print(text, file=sys.stderr, flush=True)
        except Exception:
            pass
        # If a QApplication is running, show a dialog
        app = QApplication.instance()
        if app is not None:
            try:
                QMessageBox.critical(None, "Unexpected Error", text)
            except Exception:
                pass
        # Exit safely
        sys.exit(1)

    sys.excepthook = _hook


def main() -> int:
    install_excepthook()

    app = QApplication(sys.argv)
    app.setApplicationName("PlutoOFDMTransceiver")
    app.setOrganizationName("HUB10")

    win = MainWindow(services=build_services())
    win.show()

    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
