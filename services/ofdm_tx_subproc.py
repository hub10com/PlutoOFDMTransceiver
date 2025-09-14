# -*- coding: utf-8 -*-
"""
services/ofdm_tx_subproc.py
- Runs GNU Radio OFDM TX in a SEPARATE PROCESS.
- Reads stdout/stderr live → forwards to GUI log.
- Detects 'u'/'U' underrun characters and reports them throttled.
- Compatible with Windows and POSIX (Windows: CREATE_NO_WINDOW for hidden console).

Notes:
- Default runner: current python (sys.executable) or paths.exe_python() if available.
- tx_runner.py is expected in the project's scripts folder (paths.dir_scripts() preferred).
"""

import os
import sys
import time
import threading
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

# --- Optional centralized paths (portable) ---
try:
    import paths  # should expose dir_scripts(), exe_python()
except Exception:
    paths = None


@dataclass
class TxConfig:
    bitwrap_path: str
    center: float = 2.4e9
    samp: float = 2e6
    rfbw: Optional[float] = None
    atten: float = 10.0
    buffer: int = 32768
    amp: float = 0.03
    pkt: int = 512
    roll: int = 0
    mod: str = "qpsk"

    # Executable / command paths
    python_exe: Optional[str] = None      # None -> paths.exe_python() or sys.executable
    runner_path: Optional[str] = None     # None -> paths.dir_scripts()/tx_runner.py or repo fallback

    # Console behavior
    show_console: bool = False            # True → external console window (stdout not captured)
    cwd: Optional[str] = None


class OfdmTxServiceSubproc:
    """
    start(config) → launch subprocess
    stop()        → gracefully stop (SIGINT/terminate → kill)
    running       → bool
    pid           → int | None
    """

    def __init__(self, on_log: Optional[Callable[[str], None]] = None):
        self._on_log = on_log or (lambda s: None)
        self._p: Optional[subprocess.Popen] = None
        self._th: Optional[threading.Thread] = None
        self._stop_req = False

        # underrun tracking
        self._u_count = 0
        self._u_last_emit_ts = 0.0
        self._u_emit_interval = 0.8  # seconds

    # ----- public props -----
    @property
    def running(self) -> bool:
        return self._p is not None and self._p.poll() is None

    @property
    def pid(self) -> Optional[int]:
        return None if self._p is None else self._p.pid

    # ----- helpers -----
    def _emit(self, msg: str):
        try:
            self._on_log(msg)
        except Exception:
            pass

    def _runner_file(self, cfg: TxConfig) -> Path:
        # 1) explicit
        if cfg.runner_path:
            return Path(cfg.runner_path).resolve()
        # 2) paths.py (preferred)
        if paths and hasattr(paths, "dir_scripts"):
            try:
                p = Path(paths.dir_scripts()) / "tx_runner.py"
                return p.resolve()
            except Exception:
                pass
        # 3) repo fallback: services/ -> ../scripts/tx_runner.py
        here = Path(__file__).resolve().parent.parent
        return (here / "scripts" / "tx_runner.py").resolve()

    def _python_exe(self, cfg: TxConfig) -> str:
        # 1) explicit
        if cfg.python_exe:
            return cfg.python_exe
        # 2) paths.py radioconda / portable python
        if paths and hasattr(paths, "exe_python"):
            try:
                p = Path(paths.exe_python())
                if p.exists():
                    return str(p)
            except Exception:
                pass
        # 3) fallback to current interpreter
        return sys.executable

    def _build_cmd(self, cfg: TxConfig) -> list:
        py = self._python_exe(cfg)
        runner = str(self._runner_file(cfg))

        amp_val = max(0.0, min(1.0, float(cfg.amp)))
        amp_str = f"{amp_val:.6f}"

        args = [
            py, "-u", runner,
            "--bitwrap", cfg.bitwrap_path,
            "--center", str(cfg.center),
            "--samp", str(cfg.samp),
            "--atten", str(cfg.atten),
            "--buffer", str(cfg.buffer),
            "--amp", amp_str,
            "--pkt", str(cfg.pkt),
            "--roll", str(cfg.roll),
            "--mod", cfg.mod,
        ]
        if cfg.rfbw is not None:
            args.extend(["--rfbw", str(cfg.rfbw)])
        return args

    def start(self, cfg: TxConfig):
        if self.running:
            self._emit("[TX] Already running (pid={}).".format(self.pid))
            return

        cmd = self._build_cmd(cfg)
        self._stop_req = False
        self._u_count = 0
        self._u_last_emit_ts = 0.0

        self._emit(
            "[TX] Subprocess starting… "
            f"center={cfg.center} samp={cfg.samp} rfbw={cfg.rfbw or cfg.samp} "
            f"atten={cfg.atten} buf={cfg.buffer} pkt={cfg.pkt} mod={cfg.mod} "
            f"amp={max(0.0, min(1.0, float(cfg.amp))):.6f}"
        )

        popen_kwargs = dict(
            cwd=cfg.cwd or Path(self._runner_file(cfg)).parent,
            env=os.environ.copy(),
        )

        if cfg.show_console:
            if os.name == "nt":
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE  # type: ignore[attr-defined]
            self._p = subprocess.Popen(cmd, **popen_kwargs)
        else:
            if os.name == "nt":
                popen_kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
            self._p = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                universal_newlines=True,
                **popen_kwargs,
            )
            self._th = threading.Thread(target=self._pump_stdout, daemon=True)
            self._th.start()

        self._emit("[TX] Subprocess started (pid={}).".format(self.pid))

    def _pump_stdout(self):
        assert self._p is not None and self._p.stdout is not None
        for line in self._p.stdout:
            if line is None:
                break
            s = line.rstrip("\r\n")
            if not s:
                continue

            # 1) Pure underrun bursts (only u/U) → don't echo, count, and emit periodically
            if s.strip("uU") == "":
                u_local = len(s)
                if u_local > 0:
                    self._u_count += u_local
                    now = time.time()
                    if now - self._u_last_emit_ts >= self._u_emit_interval:
                        self._emit(f"[TX] Underrun: +{self._u_count} (total)")
                        self._u_count = 0
                        self._u_last_emit_ts = now
                continue

            # 2) Mixed lines with u/U → count and still log
            u_mixed = s.count("u") + s.count("U")
            if u_mixed > 0:
                self._u_count += u_mixed
                now = time.time()
                if now - self._u_last_emit_ts >= self._u_emit_interval:
                    self._emit(f"[TX] Underrun: +{self._u_count} (total)")
                    self._u_count = 0
                    self._u_last_emit_ts = now

            # 3) Avoid double-tagging lines that already start with [TX]
            if s.startswith("[TX]"):
                self._emit(s)
            else:
                self._emit("[TX] " + s)

        code = self._p.poll()
        self._emit(f"[TX] Subprocess exited with code {code}.")

    def stop(self, timeout: float = 2.0):
        if not self.running:
            self._emit("[TX] No running process.")
            return

        self._stop_req = True
        p = self._p
        self._emit("[TX] Stop requested… (SIGINT/terminate)")

        try:
            if os.name == "nt":
                p.terminate()
            else:
                p.send_signal(subprocess.signal.SIGINT)  # type: ignore[attr-defined]
        except Exception:
            pass

        try:
            p.wait(timeout=timeout)
        except Exception:
            self._emit("[TX] Did not exit gracefully, forcing kill…")
            try:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(p.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                    )
                else:
                    p.kill()
            except Exception:
                pass

        self._p = None
        self._th = None
        self._emit("[TX] Stopped.")
