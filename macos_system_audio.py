"""
macOS system-audio source backed by a Swift ScreenCaptureKit helper.

Spawns helpers/system_audio_capture as a subprocess, reads raw
interleaved Float32 stereo PCM @ 48 kHz from its stdout, and exposes
the same producer pattern the rest of audio_recorder.py expects
(numpy chunks pushed into a queue, RMS level updated, errors reported).
"""

import os
import platform
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from orizon_logging import get_logger

log = get_logger("sck")


SAMPLE_RATE = 48_000
CHANNELS = 2
BYTES_PER_SAMPLE = 4  # float32
FRAMES_PER_READ = 1024
BYTES_PER_READ = FRAMES_PER_READ * CHANNELS * BYTES_PER_SAMPLE


def _helper_binary_path() -> Path:
    return Path(__file__).resolve().parent / "helpers" / "system_audio_capture"


# Mach-O cputype values (CPU_ARCH_ABI64 flag included) per host machine.
_MACHO_CPUTYPES = {"arm64": 0x0100000C, "x86_64": 0x01000007}
_MACHO_MAGIC_64_LE = b"\xcf\xfa\xed\xfe"
_MACHO_MAGIC_64_BE = b"\xfe\xed\xfa\xcf"
_FAT_MAGICS = (b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca")
_arch_warned = False


def _process_is_translated() -> bool:
    """True when this Python runs under Rosetta 2 on Apple Silicon (an
    x86_64 Homebrew/conda interpreter): the host CPU is arm64 and a native
    arm64 helper can be spawned just fine."""
    if sys.platform != "darwin":
        return False
    try:
        out = subprocess.run(["/usr/sbin/sysctl", "-n", "sysctl.proc_translated"],
                             capture_output=True, timeout=2)
        return out.returncode == 0 and out.stdout.strip() == b"1"
    except Exception:
        return False


def binary_matches_host(path: Path, machine: Optional[str] = None,
                        translated: Optional[bool] = None) -> bool:
    """True if the Mach-O at ``path`` can run on this CPU. The committed
    helper is a thin arm64 binary: on an Intel Mac execv would fail with
    'Bad CPU type in executable' at every recording start, so the helper
    must be reported unavailable there (mic-only / BlackHole fallback,
    with a hint to rebuild) instead of aborting each start(). A Rosetta-
    translated interpreter on Apple Silicon can still run the arm64 helper."""
    machine = machine or platform.machine()
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError:
        return False
    if len(head) < 8:
        return False
    magic = head[:4]
    if magic in _FAT_MAGICS:
        return True  # universal binary
    want = _MACHO_CPUTYPES.get(machine)
    if want is None:
        return True  # unknown host architecture: let exec decide
    runnable = {want}
    if machine == "x86_64":
        if translated is None:
            translated = _process_is_translated()
        if translated:
            runnable.add(_MACHO_CPUTYPES["arm64"])
    if magic == _MACHO_MAGIC_64_LE:
        cputype = int.from_bytes(head[4:8], "little")
    elif magic == _MACHO_MAGIC_64_BE:
        cputype = int.from_bytes(head[4:8], "big")
    else:
        return True  # not a Mach-O header we recognise (script wrapper?)
    return cputype in runnable


def is_available() -> bool:
    """True if we are on macOS 13+ and the helper binary is present,
    executable and built for this CPU architecture."""
    global _arch_warned
    if sys.platform != "darwin":
        return False
    try:
        major = int(platform.mac_ver()[0].split(".")[0])
        if major < 13:
            return False
    except Exception:
        return False
    binary = _helper_binary_path()
    if not (binary.is_file() and os.access(binary, os.X_OK)):
        return False
    if not binary_matches_host(binary):
        if not _arch_warned:
            _arch_warned = True
            log.warning(
                "System audio helper %s is not built for this CPU (%s). "
                "Rebuild it with: cd helpers && ./build.sh (Xcode Command Line Tools).",
                binary, platform.machine())
        return False
    return True


class SCKAudioSource:
    """Lifecycle wrapper around the Swift ScreenCaptureKit helper."""

    def __init__(
        self,
        on_audio: Callable[[np.ndarray], None],
        on_level: Optional[Callable[[float], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        on_ready: Optional[Callable[[], None]] = None,
    ) -> None:
        self._on_audio = on_audio
        self._on_level = on_level
        self._on_error = on_error
        self._on_ready = on_ready

        self._proc: Optional[subprocess.Popen] = None
        self._stdout_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()

    @property
    def sample_rate(self) -> int:
        return SAMPLE_RATE

    @property
    def channels(self) -> int:
        return CHANNELS

    def is_running(self) -> bool:
        """True iff the helper subprocess is alive and emitting audio."""
        proc = self._proc
        return proc is not None and proc.poll() is None and self._ready_event.is_set()

    def start(self, ready_timeout: float = 5.0) -> None:
        """Spawn the helper. Returns once it reports STATUS ready or raises."""
        binary = _helper_binary_path()
        if not is_available():
            raise RuntimeError("ScreenCaptureKit helper not available on this system.")

        self._stop_event.clear()
        self._ready_event.clear()

        try:
            self._proc = subprocess.Popen(
                [str(binary)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                bufsize=0,
            )
        except OSError as e:
            raise RuntimeError(f"Cannot launch helper: {e}") from e

        self._stderr_thread = threading.Thread(
            target=self._stderr_loop, daemon=True, name="sck-stderr"
        )
        self._stderr_thread.start()

        # Wait for "STATUS ready" or an early error / exit.
        if not self._ready_event.wait(timeout=ready_timeout):
            # No ready signal in time. Inspect: did the process die?
            if self._proc.poll() is not None:
                self._cleanup_proc()
                raise RuntimeError(
                    "System audio helper exited before becoming ready. "
                    "Most likely the 'Screen Recording' permission was denied. "
                    "Grant it in System Settings → Privacy & Security → Screen Recording."
                )
            self.stop()
            raise RuntimeError("System audio helper did not become ready in time.")

        self._stdout_thread = threading.Thread(
            target=self._stdout_loop, daemon=True, name="sck-stdout"
        )
        self._stdout_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGINT)
            except OSError:
                pass
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                try:
                    proc.terminate()
                    proc.wait(timeout=1.0)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        self._cleanup_proc()

        for t in (self._stdout_thread, self._stderr_thread):
            if t is not None:
                t.join(timeout=1.5)
        self._stdout_thread = None
        self._stderr_thread = None

    # ---------- Internals ----------

    def _cleanup_proc(self) -> None:
        proc = self._proc
        if proc is None:
            return
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
        self._proc = None

    def _stdout_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        stdout = proc.stdout
        buf = bytearray()
        try:
            while not self._stop_event.is_set():
                chunk = stdout.read(BYTES_PER_READ)
                if not chunk:
                    break
                buf.extend(chunk)
                # Process complete frames only (each frame = CHANNELS * BYTES_PER_SAMPLE).
                frame_size = CHANNELS * BYTES_PER_SAMPLE
                usable = (len(buf) // frame_size) * frame_size
                if usable == 0:
                    continue
                samples = np.frombuffer(bytes(buf[:usable]), dtype=np.float32)
                del buf[:usable]
                stereo = samples.reshape(-1, CHANNELS)
                try:
                    self._on_audio(stereo)
                except Exception as e:
                    self._report_error(f"system audio sink: {e}")
                if self._on_level is not None:
                    try:
                        self._on_level(float(np.sqrt(np.mean(stereo * stereo))))
                    except Exception:
                        pass
        except Exception as e:
            self._report_error(f"system audio reader: {e}")

    def _stderr_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        stderr = proc.stderr
        try:
            while not self._stop_event.is_set():
                line = stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if not text:
                    continue
                if text.startswith("STATUS "):
                    self._handle_status(text[len("STATUS "):])
                else:
                    # Unstructured stderr noise (frameworks, os_log spill):
                    # diagnostics only — never a user-facing error.
                    log.debug("helper stderr: %s", text)
        except Exception:
            pass

    def _handle_status(self, payload: str) -> None:
        # Payload format: "<key> <rest...>"
        parts = payload.split(" ", 1)
        key = parts[0]
        rest = parts[1] if len(parts) > 1 else ""
        if key == "ready":
            log.info("SCK ready: %s", rest)
            self._ready_event.set()
            if self._on_ready is not None:
                try:
                    self._on_ready()
                except Exception:
                    pass
        elif key == "error":
            log.warning("SCK error: %s", rest)
            self._report_error(f"system audio: {rest}")
            # Errors before ready: surface them and let the start() timeout / poll logic act.
            # We don't auto-stop here; audio_recorder owns the lifecycle.
        elif key == "heartbeat":
            # buffers=N bytes=M peak=P — surface so it shows up in the log file.
            # Heartbeat at peak=0.0000 means SCK is delivering buffers but they
            # are silent (audio is muted, or routed somewhere SCK can't see).
            log.info("SCK %s: %s", key, rest)
        elif key in ("source_format", "decoded_format"):
            log.info("SCK source format: %s", rest)
            # The helper promises 48 kHz output; warn loudly if the decoded
            # source ever disagrees so a pitch/speed bug is diagnosable.
            for part in rest.split():
                if part.startswith("sr=") and part[3:] != str(SAMPLE_RATE):
                    log.warning("SCK source rate %s != expected %d", part[3:], SAMPLE_RATE)
        elif key == "stopped":
            log.info("SCK stopped: %s", rest)
        else:
            log.debug("SCK %s: %s", key, rest)

    def _report_error(self, message: str) -> None:
        if self._on_error is not None:
            try:
                self._on_error(message)
            except Exception:
                pass


def get_permission_guidance() -> str:
    return (
        "Per registrare l'audio di sistema (la voce degli altri partecipanti) "
        "macOS richiede il permesso 'Registrazione schermo' per l'applicazione "
        "che esegue Orizon Call (es. Terminale, oppure il bundle .app).\n\n"
        "1. Apri Impostazioni di Sistema → Privacy e Sicurezza → Registrazione schermo\n"
        "2. Abilita l'applicazione (Terminale / iTerm / Orizon Call)\n"
        "3. Riavvia la registrazione\n\n"
        "Nessun driver né configurazione audio aggiuntiva è necessaria."
    )
