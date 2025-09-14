# -*- coding: utf-8 -*-
"""
scripts/jammer_detection_runner.py

Temiz stop: UDP 'STOP' -> terminate (grace) -> (Windows) taskkill /T /F -> kill
- Kesinlikle CTRL_C / CTRL_BREAK / AttachConsole KULLANMAZ.
- Varsayılan: create_new_process_group=False, new_console=False (GUI log için PIPE).
"""

import os, re, socket, time, threading, subprocess, locale
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List
from PyQt5.QtCore import QObject, pyqtSignal

IS_WIN = (os.name == 'nt')
ENC = locale.getpreferredencoding(False) or "utf-8"

def _default_exe() -> str:
    try:
        import paths
        return str(Path(paths.dir_scripts()) / "jammer_detect.exe")
    except Exception:
        return str(Path(__file__).resolve().parent / "jammer_detect.exe")

@dataclass
class JammerDetectConfig:
    exe_path: str = field(default_factory=_default_exe)
    cwd: Optional[str] = None
    create_new_process_group: bool = False   # <<< DEĞİŞTİ: default False
    new_console: bool = False               # GUI log için False önerilir
    no_window: bool = False                 # yalnız Windows'ta CREATE_NO_WINDOW

    # --- RX / Pluto ---
    uri: str = "ip:192.168.2.1"
    freq_hz: float = 2.402e9
    samp_hz: float = 4e6
    rfbw_hz: float = 4e6
    gain_db: int = -20
    frame_size: int = 4096

    # --- Calib / Detect (kendi C++ aracına göre) ---
    calib_secs: float = 10.0
    calib_dummy: int = 10
    calib_probes: int = 20
    calib_clean: int = 10
    remove_dc: bool = True
    dc_alpha: float = 0.01
    floor_watt: float = 1e-15
    calib_db: float = 0.0
    p_low: float = 1.0
    p_high: float = 99.0
    gmm_eps: float = 1e-6
    gmm_iters: int = 200
    detect_consec: int = 5
    detect_max: int = 5000

    def build_args(self) -> List[str]:
        a: List[str] = [self.exe_path,
            "--uri", self.uri,
            "--freq", str(self.freq_hz),
            "--samp", str(self.samp_hz),
            "--rfbw", str(self.rfbw_hz),
            "--gain", str(self.gain_db),
            "--framesize", str(self.frame_size),
            "--calib-secs", str(self.calib_secs),
            "--calib-dummy", str(self.calib_dummy),
            "--calib-probes", str(self.calib_probes),
            "--calib-clean", str(self.calib_clean),
        ]
        if not self.remove_dc:
            a += ["--no-dc"]
        a += [
            "--dc-alpha", str(self.dc_alpha),
            "--floor-watt", str(self.floor_watt),
            "--calib-db", str(self.calib_db),
            "--p-low", str(self.p_low),
            "--p-high", str(self.p_high),
            "--gmm-eps", str(self.gmm_eps),
            "--gmm-iters", str(self.gmm_iters),
            "--detect-consec", str(self.detect_consec),
            "--detect-max", str(self.detect_max),
        ]
        return a

class JammerDetectionRunner(QObject):
    # lifecycle
    started = pyqtSignal(int)
    stopped = pyqtSignal(int, str)  # exit_code, reason
    # logs
    log_line = pyqtSignal(str)
    warn = pyqtSignal(str)
    error = pyqtSignal(str)
    # parsed telemetry
    info_pluto_config = pyqtSignal(dict)
    calibration = pyqtSignal(dict)
    detected = pyqtSignal(int)
    ctrl_listening = pyqtSignal(str, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc: Optional[subprocess.Popen] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._stop_reader = threading.Event()
        self._cfg: Optional[JammerDetectConfig] = None
        self._closing = False  # reentrancy guard

        # regexler
        self._re_info_cfg = re.compile(
            r"\[INFO\]\s+Pluto URI=(?P<uri>\S+)\s+\|\s+Freq=(?P<freq>\d+)"
            r"\s+\|\s+Samp=(?P<samp>\d+)\s+\|\s+RFBW=(?P<rfbw>\d+)"
            r"\s+\|\s+Gain=(?P<gain>-?\d+)\s+\|\s+Frame=(?P<frame>\d+)"
        )
        self._re_calib = re.compile(
            r"\[INFO\]\s+Threshold\(dBm\)=(?P<thr>[-0-9.]+)\s+\|\s+clean=(?P<clean>yes|no)"
            r"\s+\|\s+mean_rx_ms=(?P<rx>[0-9.]+)\s+\|\s+mean_frame_ms=(?P<frm>[0-9.]+)\s+\|\s+frames_used=(?P<used>\d+)"
        )
        self._re_detected = re.compile(r"\[INFO\]\s+Jammer bulundu, sayaç basladi \(seq=(?P<seq>\d+)\)")
        self._re_ctrl = re.compile(r"\[CTRL\]\s+UDP control listening on\s+(?P<h>[\d\.]+):(?P<p>\d+)\s+\(send 'STOP'\)\.")
        self._re_err = re.compile(r"\[ERR\]", re.IGNORECASE)
        self._re_warn = re.compile(r"\[WARN\]", re.IGNORECASE)

    # --- state ---
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # --- start ---
    def start(self, cfg: JammerDetectConfig) -> None:
        if self.is_running():
            self._emit_log("[JD] Already running.")
            return

        self._cfg = cfg
        exe = cfg.exe_path or _default_exe()
        if not os.path.isfile(exe):
            self.error.emit(f"[JD] exe not found: {exe}")
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
            exe_dir = os.path.dirname(exe)
            env["PATH"] = exe_dir + os.pathsep + env.get("PATH", "")

        # creation flags (Windows)
        creationflags = 0
        if IS_WIN:
            if cfg.new_console:
                creationflags |= subprocess.CREATE_NEW_CONSOLE
            if cfg.no_window:
                # yalnız yeni pencere açmıyorsan anlamlıdır
                creationflags |= subprocess.CREATE_NO_WINDOW
            # Bilerek create_new_process_group KULLANMIYORUZ (CTRL yayılımını istemiyoruz)

        try:
            if cfg.new_console and IS_WIN:
                # ayrı pencere, log GUI'ye gelmez
                self._proc = subprocess.Popen(
                    args, cwd=cwd, stdout=None, stderr=None, stdin=subprocess.DEVNULL,
                    env=env, creationflags=creationflags
                )
            else:
                # PIPE modu: GUI log + parsing
                self._proc = subprocess.Popen(
                    args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, bufsize=0, universal_newlines=False,
                    env=env, creationflags=creationflags
                )
        except Exception as e:
            self.error.emit(f"[JD] start failed: {e}")
            self.stopped.emit(-1, f"error: {e}")
            return

        self.started.emit(self._proc.pid)
        self._emit_log(f"[JD] started pid={self._proc.pid} uri={cfg.uri} f={cfg.freq_hz} sr={cfg.samp_hz} bw={cfg.rfbw_hz} gain={cfg.gain_db}")

        if self._proc.stdout is not None:
            self._stop_reader.clear()
            self._reader_thread = threading.Thread(target=self._stdout_loop, name="jd_reader", daemon=True)
            self._reader_thread.start()

    # --- stop ---
    def stop(self, grace_ms: int = 700) -> None:
        if not self.is_running():
            self.stopped.emit(0, "normal")
            return
        if self._closing:
            return
        self._closing = True

        # 1) UDP STOP (best-effort)
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.15)
            s.sendto(b"STOP", ("127.0.0.1", 25000))
            s.close()
        except Exception:
            pass

        p = self._proc
        reason = "terminated"

        # 2) Kibar sonlandır
        try:
            p.terminate()
        except Exception:
            pass

        # kısa bekle
        t_end = time.time() + max(0, grace_ms) / 1000.0
        while time.time() < t_end:
            if p.poll() is not None:
                break
            time.sleep(0.03)

        # 3) Hâlâ yaşıyorsa: Windows -> taskkill /T /F, diğer -> kill
        if p.poll() is None:
            if IS_WIN:
                try:
                    subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                                   capture_output=True, check=False)
                except Exception as e:
                    self.warn.emit(f"[JD] taskkill failed: {e}")
            try:
                p.kill()
            except Exception:
                pass

        # reader kapat
        self._stop_reader.set()
        if self._reader_thread and self._reader_thread.is_alive():
            try:
                self._reader_thread.join(timeout=1.0)
            except Exception:
                pass

        exit_code = p.poll()
        if exit_code is None:
            exit_code = -9

        self._proc = None
        self._closing = False
        self.stopped.emit(exit_code, reason)

    # --- stdout parse ---
    def _stdout_loop(self):
        assert self._proc and self._proc.stdout
        buf = b""
        stream = self._proc.stdout
        while not self._stop_reader.is_set():
            ch = stream.read(1)
            if not ch:
                break
            buf += ch
            if ch in (b"\n", b"\r"):
                line = buf.decode(ENC, errors="replace").strip()
                buf = b""
                if line:
                    self._handle_line(line)
        if buf:
            line = buf.decode(ENC, errors="replace").strip()
            if line:
                self._handle_line(line)

    def _handle_line(self, line: str):
        self._emit_log(line)
        if self._re_warn.search(line): self.warn.emit(line)
        if self._re_err.search(line):  self.error.emit(line)

        m = self._re_info_cfg.search(line)
        if m:
            self.info_pluto_config.emit({
                "uri": m.group("uri"),
                "freq": int(m.group("freq")),
                "samp": int(m.group("samp")),
                "rfbw": int(m.group("rfbw")),
                "gain": int(m.group("gain")),
                "frame": int(m.group("frame")),
            }); return

        m = self._re_calib.search(line)
        if m:
            self.calibration.emit({
                "threshold_dbm": float(m.group("thr")),
                "clean": (m.group("clean") == "yes"),
                "mean_rx_ms": float(m.group("rx")),
                "mean_frame_ms": float(m.group("frm")),
                "frames_used": int(m.group("used")),
            }); return

        m = self._re_detected.search(line)
        if m:
            self.detected.emit(int(m.group("seq"))); return

        m = self._re_ctrl.search(line)
        if m:
            self.ctrl_listening.emit(m.group("h"), int(m.group("p"))); return

    def _emit_log(self, s: str):
        try:
            self.log_line.emit(s)
        except Exception:
            pass
