# -*- coding: utf-8 -*-
"""
scripts/pluto_cmdd_runner.py

Runner for pluto_udp_cmdd_bridge.exe (MinGW C++17).

• EXE'yi AYRI bir konsolda (CREATE_NEW_CONSOLE) başlatır.
• GUI'ye log pipe edilmez.
• stop(reboot_first=True): AUTOTX/AUTORX OFF → REBOOT (TCP, drop bekler) → process tree kill.
• Başlat/bitir öncesi UDP port (örn. 6000) serbestlik kontrolü yapılır.
"""

import os
import time
import socket
import subprocess
import locale
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List

from PyQt5.QtCore import QObject, pyqtSignal

# ---------------- OS helpers ----------------
IS_WIN = (os.name == 'nt')
if IS_WIN:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

    CREATE_NEW_PROCESS_GROUP = 0x00000200
    DETACHED_PROCESS         = 0x00000008
    CREATE_NEW_CONSOLE       = 0x00000010
    CREATE_NO_WINDOW         = 0x08000000

    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype  = wintypes.HANDLE
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype  = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype  = wintypes.BOOL

    PROCESS_TERMINATE = 0x0001

ENC = locale.getpreferredencoding(False) or "utf-8"


# ---------------- Path helpers ----------------
def _default_exe() -> str:
    try:
        import paths
        return str(Path(paths.dir_scripts()) / "pluto_udp_cmdd_bridge.exe")
    except Exception:
        return str(Path(__file__).resolve().parent / "pluto_udp_cmdd_bridge.exe")


# ---------------- UDP helpers ----------------
def _udp_port_free(port: int) -> bool:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("0.0.0.0", port))
        s.close()
        return True
    except Exception:
        return False


def _wait_udp_free(port: int, timeout_s: float = 2.0, step_s: float = 0.1) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if _udp_port_free(port):
            return True
        time.sleep(step_s)
    return _udp_port_free(port)


# ---------------- Robust CMDD TCP send ----------------
def _send_cmd_and_wait(host: str, port: int, line: str,
                       expect_any: bytes = b"OK",
                       wait_drop: bool = False,
                       timeout_s: float = 1.5) -> bool:
    """
    CMDD'ye tek satır komut gönder, kısa süre cevap/bağlantı kapanışı bekle.
    - TCP_NODELAY açılır.
    - 'expect_any' (örn. b"OK") görülürse ya da bağlantı düşerse True döner.
    - REBOOT gibi durumlarda 'wait_drop=True' başarı sayılır.
    """
    try:
        with socket.create_connection((host, port), timeout=0.8) as s:
            try:
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except Exception:
                pass

            if not line.endswith("\r\n"):
                line = line + "\r\n"
            s.sendall(line.encode("ascii", "ignore"))

            t0 = time.time()
            got_any = False
            buf = b""
            try:
                s.settimeout(0.3)
            except Exception:
                pass

            while time.time() - t0 < timeout_s:
                try:
                    chunk = s.recv(4096)
                    if not chunk:
                        # Karşı taraf kapattı → çoğu zaman komut işlendi demektir
                        return True if (wait_drop or got_any) else True
                    buf += chunk
                    got_any = True
                    if expect_any and (expect_any in buf or b"OK" in buf or b">" in buf):
                        break
                except socket.timeout:
                    time.sleep(0.05)
                except Exception:
                    break

            try:
                s.shutdown(socket.SHUT_WR)
            except Exception:
                pass
            return True
    except Exception:
        return False


def _best_effort_autotx_off(host: str = "192.168.2.1", port: int = 80) -> bool:
    ok = _send_cmd_and_wait(host, port, "AUTOTX_MODE OFF", expect_any=b"OK", timeout_s=0.9)
    if ok:
        print("[pluto_cmdd_runner] AUTOTX_MODE OFF sent (best-effort)")
    return ok


def _best_effort_autorx_off(host: str = "192.168.2.1", port: int = 80) -> bool:
    ok = _send_cmd_and_wait(host, port, "AUTORX_MODE OFF", expect_any=b"OK", timeout_s=0.9)
    if ok:
        print("[pluto_cmdd_runner] AUTORX_MODE OFF sent (best-effort)")
    return ok


def _best_effort_reboot(host: str = "192.168.2.1", port: int = 80) -> bool:
    # REBOOT'ta bağlantı düşmesi normal; drop'u da başarı kabul et.
    ok = _send_cmd_and_wait(host, port, "REBOOT", expect_any=b"OK", wait_drop=True, timeout_s=1.2)
    if ok:
        print("[pluto_cmdd_runner] REBOOT issued (best-effort)")
    return ok


# ---------------- Config ----------------
@dataclass
class PlutoCmddConfig:
    # EXE + console flags
    exe_path: str = field(default_factory=_default_exe)
    cwd: Optional[str] = None
    create_new_process_group: bool = True
    new_console: bool = True
    no_window: bool = False
    detached_process: bool = False

    # CLI options
    host: str = "192.168.2.1"
    tcp_port: int = 80
    udp_port: int = 6000

    # Trigger / command
    trigger: str = "4"
    cmd: str = "AUTOTX_MODE ON"
    off_cmd: str = "AUTOTX_MODE OFF"

    # JDX
    jdx_on_value: int = 4
    jdx_autodetect: bool = True
    jdx_stop_off: bool = True

    # UDP behavior
    udp_one_shot: bool = True

    # Delay options
    delay_trigger_value: int = 4
    delay_ms: int = 200

    def build_args(self) -> List[str]:
        a: List[str] = [self.exe_path]

        if self.host:                 a += ["--host", self.host]
        if self.tcp_port != 80:       a += ["--port", str(self.tcp_port)]
        if self.udp_port != 6000:     a += ["--udp", str(self.udp_port)]
        if self.trigger:              a += ["--trigger", self.trigger]
        if self.cmd != "AUTOTX_MODE ON":      a += ["--cmd", self.cmd]
        if self.off_cmd != "AUTOTX_MODE OFF": a += ["--off-cmd", self.off_cmd]
        if self.jdx_on_value:         a += ["--jdx-on", str(self.jdx_on_value)]
        if not self.jdx_autodetect:   a += ["--no-jdx"]
        if not self.jdx_stop_off:     a += ["--no-stop-off"]
        if not self.udp_one_shot:     a += ["--keep-udp"]
        if self.delay_trigger_value >= 0: a += ["--delay-trigger", str(self.delay_trigger_value)]
        if self.delay_ms > 0:             a += ["--delay-ms", str(self.delay_ms)]

        return a


# ---------------- Runner ----------------
class PlutoCmddRunner(QObject):
    """
    Not: Bu runner log okumaz. EXE ayrı bir konsolda çalışır.
    """
    started = pyqtSignal(int)          # pid
    stopped = pyqtSignal(int, str)     # exit_code, reason ("normal" | "killed" | "error: ...")

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc: Optional[subprocess.Popen] = None
        self._cfg: Optional[PlutoCmddConfig] = None

    # ---- Public API ----
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, cfg: PlutoCmddConfig) -> None:
        if self.is_running():
            return  # zaten açık

        self._cfg = cfg
        exe = cfg.exe_path or _default_exe()
        if not os.path.isfile(exe):
            self.stopped.emit(-1, "error: exe not found")
            return

        args = cfg.build_args()
        cwd = cfg.cwd or os.path.dirname(exe) or None

        # Portable PATH
        env = os.environ.copy()
        try:
            import paths
            env = paths.subprocess_env_with_portable_paths(env)
            dll_dir = str(paths.dir_dll())
            exe_dir = os.path.dirname(exe)
            env["PATH"] = dll_dir + os.pathsep + exe_dir + os.pathsep + env.get("PATH", "")
        except Exception:
            try:
                exe_dir = os.path.dirname(exe)
                env["PATH"] = exe_dir + os.pathsep + env.get("PATH", "")
            except Exception:
                pass

        # Start öncesi: UDP port boş mu? (örn. 6000)
        port = cfg.udp_port if hasattr(cfg, "udp_port") else 6000
        if not _udp_port_free(port):
            _wait_udp_free(port, timeout_s=1.0)
        if not _udp_port_free(port):
            msg = f"udp {port} in use"
            print(f"[pluto_cmdd_runner] start blocked: {msg}")
            self.stopped.emit(-1, f"error: {msg}")
            return

        # Process create flags
        creationflags = 0
        if IS_WIN:
            if cfg.create_new_process_group:
                creationflags |= CREATE_NEW_PROCESS_GROUP
            if cfg.new_console:
                creationflags |= CREATE_NEW_CONSOLE
            elif cfg.no_window:
                creationflags |= CREATE_NO_WINDOW
            elif cfg.detached_process:
                creationflags |= DETACHED_PROCESS

        try:
            self._proc = subprocess.Popen(
                args,
                cwd=cwd,
                stdout=None,
                stderr=None,
                stdin=subprocess.DEVNULL,
                env=env,
                creationflags=creationflags
            )
        except Exception as e:
            print(f"[pluto_cmdd_runner] start failed: {e}")
            self.stopped.emit(-1, f"error: {e}")
            return

        self.started.emit(self._proc.pid)

    def stop(self, grace_ms: int = 600, reboot_first: bool = False) -> None:
        """
        Durdurma sırası:
          1) AUTOTX/AUTORX OFF (best-effort)
          2) (opsiyonel) REBOOT (best-effort)  → bağlantı düşebilir, normaldir
          3) Process tree kill / terminate
          4) UDP serbestlik bekle
        """
        if not self.is_running():
            self.stopped.emit(0, "normal")
            return

        # 1) Komutlar (best-effort)
        try:
            host = (self._cfg.host if (self._cfg and hasattr(self._cfg, "host")) else "192.168.2.1")
            port = (self._cfg.tcp_port if (self._cfg and hasattr(self._cfg, "tcp_port")) else 80)
            _best_effort_autotx_off(host, port)
            _best_effort_autorx_off(host, port)
        except Exception:
            pass

        # 2) REBOOT
        if reboot_first:
            try:
                host = (self._cfg.host if (self._cfg and hasattr(self._cfg, "host")) else "192.168.2.1")
                port = (self._cfg.tcp_port if (self._cfg and hasattr(self._cfg, "tcp_port")) else 80)
                _best_effort_reboot(host, port)
                # Pluto'nun USB/Ethernet'i düşürmesi için çok kısa bir nefes
                time.sleep(0.8)
            except Exception:
                pass

        # 3) Process ağaç öldürme
        pid = self._proc.pid
        reason = "killed"

        if IS_WIN:
            try:
                subprocess.call(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
            except Exception:
                try:
                    h = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
                    if h:
                        kernel32.TerminateProcess(h, 1)
                        kernel32.CloseHandle(h)
                except Exception:
                    pass
        else:
            try:
                self._proc.terminate()
            except Exception:
                pass

        # 4) UDP serbestlik
        try:
            port_u = (self._cfg.udp_port if (self._cfg and hasattr(self._cfg, "udp_port")) else 6000)
            _wait_udp_free(port_u, timeout_s=2.0)
        except Exception:
            pass

        try:
            self._proc.wait(timeout=1.0)
        except Exception:
            pass

        exit_code = self._proc.poll()
        if exit_code is None:
            exit_code = -9

        self._proc = None
        self.stopped.emit(exit_code, reason)


# Dışa açılan yardımcılar
__all__ = [
    "PlutoCmddConfig",
    "PlutoCmddRunner",
    "_best_effort_autotx_off",
    "_best_effort_autorx_off",
]
