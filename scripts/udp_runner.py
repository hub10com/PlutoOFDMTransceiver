# -*- coding: utf-8 -*-
"""
services/udp_runner.py

UDP dump runner for Windows (portable):

- Runs '<scripts>/udp_dump+.exe' resolved via paths.dir_scripts(), fallback to repo layout.
- Args: <bind_ip> <port> <out_file>   e.g. 0.0.0.0 2000 out.bitwrap
- Reads stdout in raw bytes; treats both '\\n' and '\\r' as "line" terminators (our exe uses \\r).
- Emits:
    started(pid:int)
    stopped(exit_code:int, reason:str in {"normal","ctrl","killed","error"})
    log_line(str)                          # each parsed stdout "line"
    stats(dict)                            # {'pkts','bytes','mb','mbps','queue_mb','drops', ...aliases}
    listening(ip:str, port:int)

UI-compat notes:
- Adds aliases so older/newer controllers can both work:
    d["queue"] = d["queue_mb"]
    d["drop"]  = d["drops"]
    d["rate_MBps"] = d["mbps"]/8.0      # OPTIONAL; do not break existing 'rate' usage
- Keeps original keys intact (pkts/bytes/mb/mbps/queue_mb/drops).

Portable PATH:
- Tries to prepend paths.portable_library_bin(), paths.dir_dll(), and exe dir to PATH
  so MinGW/runtime DLLs resolve without absolute paths.

Stop behavior:
- Tries CTRL_BREAK where possible, then hard kill as a fallback.
"""

import os
import re
import time
import threading
import subprocess
import locale
from dataclasses import dataclass
from typing import Optional, Dict
from pathlib import Path

from PyQt5.QtCore import QObject, pyqtSignal

# ---------------- Windows flags / ctypes ----------------
IS_WIN = (os.name == 'nt')
if IS_WIN:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

    CTRL_C_EVENT    = 0
    CTRL_BREAK_EVENT = 1

    CREATE_NEW_PROCESS_GROUP = 0x00000200
    DETACHED_PROCESS         = 0x00000008
    CREATE_NEW_CONSOLE       = 0x00000010
    CREATE_NO_WINDOW         = 0x08000000

    kernel32.AttachConsole.argtypes = [wintypes.DWORD]
    kernel32.AttachConsole.restype  = wintypes.BOOL

    kernel32.FreeConsole.argtypes = []
    kernel32.FreeConsole.restype  = wintypes.BOOL

    kernel32.GenerateConsoleCtrlEvent.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.GenerateConsoleCtrlEvent.restype  = wintypes.BOOL

    kernel32.SetConsoleCtrlHandler.argtypes = [wintypes.LPVOID, wintypes.BOOL]

    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype  = wintypes.HANDLE

    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype  = wintypes.BOOL

    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype  = wintypes.BOOL

    PROCESS_TERMINATE = 0x0001
    ATTACH_PARENT_PROCESS = wintypes.DWORD(-1).value

# ---------------- Path resolution ----------------
def _default_udp_exe() -> str:
    """Prefer scripts/ via paths.dir_scripts(); fallback to repo layout."""
    try:
        import paths  # noqa
        p = Path(paths.dir_scripts()) / "udp_dump+.exe"
        return str(p)
    except Exception:
        pass
    return str(Path(__file__).resolve().parent.parent / "scripts" / "udp_dump+.exe")

DEFAULT_EXE = _default_udp_exe()

# ---------------- Regexes (tolerant) ----------------
STATS_RE = re.compile(
    r"pkts=(?P<pkts>\d+)\s+"
    r"bytes=(?P<bytes>\d+)\s+\((?P<mb>[0-9.]+)\s+MB\)\s+"
    r"rate=(?P<mbps>[0-9.]+)\s+M[bB](?:it)?/s\s+"
    r"queue=(?P<qmb>[0-9.]+)\s+MB\s+"
    r"drops=(?P<drops>\d+)",
    re.IGNORECASE
)

LISTEN_RE = re.compile(
    r"Listening\s+UDP\s+(?P<ip>\d+\.\d+\.\d+\.\d+):(?P<port>\d+)",
    re.IGNORECASE
)

ENC = locale.getpreferredencoding(False) or "utf-8"

# ---------------- Dataclass config ----------------
@dataclass
class UdpRunnerConfig:
    exe_path: str = DEFAULT_EXE
    bind_ip: str = "0.0.0.0"
    port: int = 2000
    out_file: str = "out.bitwrap"
    cwd: Optional[str] = None

    # Console/termination behavior
    create_new_process_group: bool = True
    detached_process: bool = False
    new_console: bool = False
    no_window: bool = True   # default: hidden window

# ---------------- Runner ----------------
class UdpRunner(QObject):
    started   = pyqtSignal(int)                 # pid
    stopped   = pyqtSignal(int, str)            # (exit_code, reason)
    log_line  = pyqtSignal(str)                 # raw parsed line
    stats     = pyqtSignal(dict)                # KPI dict
    listening = pyqtSignal(str, int)            # ip, port

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc: Optional[subprocess.Popen] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._stop_reader = threading.Event()
        self._last_stats: Optional[Dict] = None
        self._cfg: Optional[UdpRunnerConfig] = None

    # ---------- Public API ----------
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def last_stats(self) -> Optional[Dict]:
        return self._last_stats

    def start(self, cfg: UdpRunnerConfig) -> None:
        if self.is_running():
            self.log_line.emit("[udp_runner] Already running; stop() before start().")
            return

        self._cfg = cfg
        exe = cfg.exe_path or DEFAULT_EXE
        if not os.path.isfile(exe):
            self.log_line.emit(f"[udp_runner][ERROR] exe not found: {exe}")
            self.stopped.emit(-1, "error")
            return

        args = [exe, cfg.bind_ip, str(cfg.port), cfg.out_file]
        cwd = cfg.cwd or os.path.dirname(exe) or None

        # Env (portable)
        env = os.environ.copy()
        try:
            import paths  # noqa
            env = paths.subprocess_env_with_portable_paths(env)
            dll_dir = str(paths.dir_dll())
            exe_dir = os.path.dirname(exe)
            env["PATH"] = dll_dir + os.pathsep + exe_dir + os.pathsep + env.get("PATH", "")
        except Exception:
            exe_dir = os.path.dirname(exe)
            env["PATH"] = exe_dir + os.pathsep + env.get("PATH", "")

        # Creation flags
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
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                bufsize=0,                 # raw bytes
                universal_newlines=False,  # we handle CR/LF
                env=env,
                creationflags=creationflags
            )
        except Exception as e:
            self.log_line.emit(f"[udp_runner][ERROR] start failed: {e}")
            self.stopped.emit(-1, "error")
            return

        self.started.emit(self._proc.pid)

        # Start reader
        self._stop_reader.clear()
        self._reader_thread = threading.Thread(
            target=self._read_stdout_loop,
            name="udp_runner_reader",
            daemon=True
        )
        self._reader_thread.start()

        self.log_line.emit(f"[udp_runner] started pid={self._proc.pid} → {cfg.bind_ip}:{cfg.port} → {cfg.out_file}")

    def stop(self, grace_ms: int = 600) -> None:
        if not self.is_running():
            self.stopped.emit(0, "normal")
            return

        reason = "ctrl"
        pid = self._proc.pid

        sent_ctrl = False
        if IS_WIN:
            # Attempt A: attach console & send CTRL_BREAK (often tricky in GUI apps)
            try:
                if kernel32.AttachConsole(pid):
                    kernel32.SetConsoleCtrlHandler(None, True)
                    if kernel32.GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, pid):
                        sent_ctrl = True
                    time.sleep(grace_ms / 1000.0)
                kernel32.FreeConsole()
                kernel32.SetConsoleCtrlHandler(None, False)
            except Exception:
                pass

            # Attempt B: process-group CTRL_BREAK
            if (not sent_ctrl) and self._cfg and self._cfg.create_new_process_group:
                try:
                    os.kill(pid, 0x1D)  # CTRL_BREAK_EVENT
                    sent_ctrl = True
                    time.sleep(grace_ms / 1000.0)
                except Exception:
                    pass

        # Force kill if still alive
        if self._proc.poll() is None:
            reason = "killed"
            if IS_WIN:
                try:
                    h = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
                    if h:
                        kernel32.TerminateProcess(h, 1)
                        kernel32.CloseHandle(h)
                    else:
                        subprocess.call(["taskkill", "/PID", str(pid), "/T", "/F"])
                except Exception:
                    subprocess.call(["taskkill", "/PID", str(pid), "/T", "/F"])
            else:
                self._proc.terminate()

        # Stop reader thread
        self._stop_reader.set()
        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=1.0)

        exit_code = self._proc.poll()
        if exit_code is None:
            try:
                self._proc.kill()
                exit_code = self._proc.poll()
            except Exception:
                exit_code = -9

        self._proc = None
        self.stopped.emit(exit_code or 0, reason)

    # ---------- Internal: stdout reading & parsing ----------
    def _read_stdout_loop(self):
        assert self._proc and self._proc.stdout
        stream = self._proc.stdout
        buf = b""

        while not self._stop_reader.is_set():
            chunk = stream.read(1)
            if not chunk:
                break
            buf += chunk

            # Treat both \n and \r as "end-of-line" (exe prints progress on \r)
            if chunk in (b"\n", b"\r"):
                line = buf.decode(ENC, errors="replace").strip()
                buf = b""
                if line:
                    self._handle_line(line)

        # trailing partial
        if buf:
            line = buf.decode(ENC, errors="replace").strip()
            if line:
                self._handle_line(line)

    def _handle_line(self, line: str):
        # Always emit raw line
        self.log_line.emit(line)

        # "Listening UDP a.b.c.d:port"
        m = LISTEN_RE.search(line)
        if m:
            ip = m.group("ip")
            port = int(m.group("port"))
            self.listening.emit(ip, port)
            return

        # KPI line
        m = STATS_RE.search(line)
        if m:
            d = {
                "pkts": int(m.group("pkts")),
                "bytes": int(m.group("bytes")),
                "mb": float(m.group("mb")),
                "mbps": float(m.group("mbps")),        # megabits per second (as printed by exe)
                "queue_mb": float(m.group("qmb")),
                "drops": int(m.group("drops")),
            }

            # -------- Aliases for UI compatibility --------
            d["queue"] = d["queue_mb"]                # some controllers read "queue"
            d["drop"]  = d["drops"]                   # some controllers read "drop"
            # Optional convenience: MB/s (do NOT overwrite existing 'rate' if UI handles itself)
            try:
                d["rate_MBps"] = d["mbps"] / 8.0
            except Exception:
                pass
            # ---------------------------------------------

            self._last_stats = d
            self.stats.emit(d)
            return

        # Could add handling for "Done." etc., if needed
