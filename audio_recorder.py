"""
Audio recording engine with dual-stream capture (mic + system audio),
sample-aligned real-time mixing, WAV/FLAC/MP3 output, file splitting,
crash recovery.

Threading model
---------------
- Audio callbacks (PortAudio / SCK reader / WASAPI reader) tag each chunk
  with its native sample rate and push it into per-session queues.
- A writer thread owns the output file: it ingests both queues into
  per-source pending buffers (resampled to 48 kHz with stateful,
  click-free resamplers), writes only the sample-aligned overlap of the
  two sources (batched to >= 10 ms blocks), and finalizes the file itself
  when it exits. Silence is injected only where audio is genuinely
  missing: a chunk the callback had to drop (queue full) becomes the same
  amount of silence in its own source; a source that delivers nothing
  for 0.5 s is padded and, when it comes back, realigned with the audio
  captured at the same moment; a persistent backlog with both sources
  alive (clock drift between the two devices) is trimmed 10 ms at a
  time down to a 20 ms floor. See _align_step.
- A watchdog thread restarts dead sources (woken at once by a stream's
  finished_callback, else every 2 s; a microphone silent for 5 s counts
  as dead); every restart is guarded by a recovery lock that stop() also
  takes, so a stop can never race a restart and leave an orphaned live
  capture. When capture is unrecoverable the session is stopped like a
  user stop, post-processing included.
- stop() is synchronous (capture teardown + optional ffmpeg
  post-processing). UI code must call it from a worker thread; the state
  is STOPPING for the whole finalization so /status never lies.
"""

import atexit
import collections
import contextlib
import json
import math
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
import types
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

import numpy as np
import sounddevice as sd
import soundfile as sf

import platform_audio
from orizon_logging import get_logger
from platform_audio import detect_mic_device, detect_system_audio_device, get_system_audio_guidance

log = get_logger("recorder")


def _ffmpeg_candidates() -> List[str]:
    """ffmpeg executables to try, in order: an explicit IMAGEIO_FFMPEG_EXE
    override (the user is explicit — honour it even with a system ffmpeg),
    the system ffmpeg on PATH, then the static binary bundled by
    imageio-ffmpeg (installed with the app, always built with libmp3lame),
    so post-processing works out of the box on every OS."""
    candidates: List[str] = []
    override = os.environ.get('IMAGEIO_FFMPEG_EXE')
    if override and os.path.isfile(override):
        candidates.append(override)
    path = shutil.which('ffmpeg')
    if path:
        candidates.append(path)
    try:
        import imageio_ffmpeg
        bundled = imageio_ffmpeg.get_ffmpeg_exe()
        if bundled:
            candidates.append(bundled)
    except Exception:
        pass
    unique: List[str] = []
    for c in candidates:
        if c not in unique:
            unique.append(c)
    return unique


def _find_ffmpeg() -> Optional[str]:
    """First usable ffmpeg (see _ffmpeg_candidates); None when none exists."""
    candidates = _ffmpeg_candidates()
    return candidates[0] if candidates else None


def _subprocess_kwargs() -> dict:
    """Extra Popen/run arguments for child processes. On Windows the app
    runs under pythonw (no console): without CREATE_NO_WINDOW every ffmpeg
    run would flash a black console window on top of the user's call."""
    if sys.platform == 'win32':
        return {'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0)}
    return {}


def _run_ffmpeg(ffmpeg: str, args: List[str], timeout: float,
                loglevel: str = 'error') -> subprocess.CompletedProcess:
    """Run ffmpeg non-interactively (-nostdin: never waits on a terminal,
    -nostats: no progress spam, banner hidden) with captured output."""
    cmd = [ffmpeg, '-hide_banner', '-nostdin', '-nostats',
           '-loglevel', loglevel, '-y', *args]
    return subprocess.run(cmd, capture_output=True, timeout=timeout,
                          **_subprocess_kwargs())


# EBU R128 loudness normalization (ffmpeg loudnorm). Two-pass "linear"
# mode is the documented best practice: pass 1 measures the file, pass 2
# applies one constant gain (plus a true-peak limiter) computed from those
# measurements — no pumping, no per-block dynamics, unlike single-pass mode.
_LOUDNORM_LRA = 11.0
_LOUDNORM_TP = -1.5
_LOUDNORM_MEASURE_KEYS = ('input_i', 'input_lra', 'input_tp', 'input_thresh', 'target_offset')


def _parse_loudnorm_stats(stderr_text: str) -> Optional[dict]:
    """Extract the loudnorm JSON block (print_format=json) from ffmpeg's
    stderr. Values are strings ("-20.06", "-inf"); returns floats or None
    if the block is missing/malformed."""
    start = stderr_text.rfind('{')
    end = stderr_text.rfind('}')
    if start < 0 or end < start:
        return None
    try:
        raw = json.loads(stderr_text[start:end + 1])
    except ValueError:
        return None
    stats = {}
    for key in _LOUDNORM_MEASURE_KEYS:
        try:
            stats[key] = float(raw[key])
        except (KeyError, TypeError, ValueError):
            return None
    return stats


def _measure_loudness(ffmpeg: str, path: Path, target_lufs: float,
                      timeout: float) -> Optional[dict]:
    """loudnorm pass 1: analyse only (-f null), JSON stats on stderr."""
    filter_arg = (f"loudnorm=I={target_lufs}:LRA={_LOUDNORM_LRA}:TP={_LOUDNORM_TP}"
                  ":print_format=json")
    result = _run_ffmpeg(ffmpeg, ['-i', str(path), '-af', filter_arg, '-f', 'null', '-'],
                         timeout=timeout, loglevel='info')
    if result.returncode != 0:
        return None
    return _parse_loudnorm_stats(result.stderr.decode('utf-8', errors='replace'))


def _loudnorm_filter(target_lufs: float, measured: Optional[dict]) -> str:
    """loudnorm pass 2 filter string; without measurements it degrades to
    the single-pass (dynamic) mode."""
    base = f"loudnorm=I={target_lufs}:LRA={_LOUDNORM_LRA}:TP={_LOUDNORM_TP}"
    if measured is None:
        return base + ":print_format=summary"
    return (base
            + f":measured_I={measured['input_i']:.2f}"
            + f":measured_LRA={measured['input_lra']:.2f}"
            + f":measured_TP={measured['input_tp']:.2f}"
            + f":measured_thresh={measured['input_thresh']:.2f}"
            + f":offset={measured['target_offset']:.2f}"
            + ":linear=true:print_format=summary")


def _ffmpeg_codec_args(path: Path) -> List[str]:
    """Encoder for an in-place rewrite of a recording segment: the FLAC
    muxer only accepts the flac codec (pcm_s16le would make every FLAC
    normalization fail), WAV keeps 16-bit PCM."""
    if path.suffix.lower() == '.flac':
        return ['-c:a', 'flac', '-sample_fmt', 's16']
    return ['-c:a', 'pcm_s16le']


def _audio_seconds(path: Path) -> float:
    """Rough duration from the file size (16-bit stereo @ 48 kHz), used
    to scale ffmpeg timeouts so a 5-hour segment is never cut short."""
    try:
        return path.stat().st_size / float(SAMPLE_RATE * CHANNELS * 2)
    except OSError:
        return 0.0

try:
    import soxr  # type: ignore
    _HAVE_SOXR = True
except ImportError:
    _HAVE_SOXR = False


class RecordingState(Enum):
    IDLE = auto()
    RECORDING = auto()
    PAUSED = auto()
    STOPPING = auto()  # capture stopped; finalization / ffmpeg in progress


# --- Constants ---

SAMPLE_RATE = 48000
CHANNELS = 2
DTYPE = 'float32'
BLOCKSIZE = 1024
OUTPUT_SUBTYPE = 'PCM_16'
QUEUE_MAXSIZE = 500           # ~10 seconds buffer at 48kHz/1024
MAX_SAMPLES_PER_SEGMENT = SAMPLE_RATE * 60 * 300  # 5 hours -> ~3.4 GB WAV
MIN_DISK_SPACE_BYTES = 100 * 1024 * 1024           # 100 MB minimum
DISK_CHECK_SECONDS = 30
HEADER_FLUSH_SECONDS = 5.0    # keep the WAV header crash-consistent
STARVATION_FRAMES = SAMPLE_RATE // 2  # pad a silent source after 0.5 s
STARVATION_SECONDS = 0.5      # ...and only if it delivered nothing for this long
# Alignment tuning (see _align_step):
MIN_WRITE_FRAMES = SAMPLE_RATE // 100       # batch writes to >= 10 ms blocks
DRIFT_THRESHOLD_FRAMES = SAMPLE_RATE // 10  # persistent 100 ms backlog = clock drift
DRIFT_CONFIRM_SECONDS = 5.0                 # backlog must persist this long
DRIFT_STEP_FRAMES = SAMPLE_RATE // 100      # correct 10 ms at a time...
DRIFT_STEP_INTERVAL = 1.0                   # ...at most once per second...
DRIFT_FLOOR_FRAMES = SAMPLE_RATE // 50      # ...down to a 20 ms residual
MIC_SILENCE_TIMEOUT = 5.0     # no mic callback for this long = stream dead

# libsndfile command: keep the RIFF header's data-size field updated on
# every write, so a hard kill (SIGKILL, power loss) leaves a playable file.
_SFC_SET_UPDATE_HEADER_AUTO = 0x1061


@contextlib.contextmanager
def _pulse_source_env(monitor_source: Optional[str]):
    """
    Linux, distro PortAudio (ALSA host API only): PulseAudio/PipeWire
    sources are not PortAudio devices, so the system-audio stream is the
    ALSA 'pulse' plugin device and PULSE_SOURCE=<sink monitor> tells the
    Pulse client which source to capture. The variable is set only while
    that one stream is being opened: the microphone stream (opened before,
    or reopened later by the watchdog) is never redirected to the monitor.
    """
    if not monitor_source:
        yield
        return
    previous = os.environ.get('PULSE_SOURCE')
    os.environ['PULSE_SOURCE'] = monitor_source
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop('PULSE_SOURCE', None)
        else:
            os.environ['PULSE_SOURCE'] = previous


class _AlignState:
    """Writer-side bookkeeping for keeping the two sources aligned."""

    def __init__(self, now: float) -> None:
        self.reset(now)

    def reset(self, now: float) -> None:
        # Wall-clock of the last frames received from each source.
        self.last_frames: Dict[str, float] = {'mic': now, 'sys': now}
        # True while a source is being padded with silence (dead/starved).
        self.starved: Dict[str, bool] = {'mic': False, 'sys': False}
        # When the *other* source's backlog first exceeded the drift
        # threshold while this one was empty (None = no backlog).
        self.backlog_since: Dict[str, Optional[float]] = {'mic': None, 'sys': None}
        # Drift confirmed for this (lagging) source: keep trimming the
        # other side's backlog down to the floor, one step per interval.
        self.drifting: Dict[str, bool] = {'mic': False, 'sys': False}
        self.last_step: Dict[str, float] = {'mic': 0.0, 'sys': 0.0}
        self.drift_steps: Dict[str, int] = {'mic': 0, 'sys': 0}


class _StreamingResampler:
    """
    Stateful block resampler: filter/phase state carries across blocks so
    chunked resampling produces the same continuous signal as a one-shot
    conversion (no boundary clicks). Uses soxr's streaming API when
    available, falling back to phase-continuous linear interpolation.
    """

    def __init__(self, from_rate: float, to_rate: float, channels: int) -> None:
        self.from_rate = float(from_rate)
        self.to_rate = float(to_rate)
        self.channels = channels
        self._soxr = None
        if _HAVE_SOXR and self.from_rate != self.to_rate:
            try:
                self._soxr = soxr.ResampleStream(
                    self.from_rate, self.to_rate, channels,
                    dtype='float32', quality='HQ',
                )
            except Exception as e:
                log.debug("soxr.ResampleStream unavailable, linear fallback: %s", e)
        # Linear-interpolation fallback state.
        self._tail: Optional[np.ndarray] = None
        self._frac: float = 0.0

    def process(self, data: np.ndarray) -> np.ndarray:
        if self.from_rate == self.to_rate or data.shape[0] == 0:
            return data
        # soxr's stream is bound to float32 and wants a contiguous array;
        # anything else raises and would silently demote the whole session
        # to linear interpolation.
        data = np.ascontiguousarray(data, dtype=np.float32)
        if self._soxr is not None:
            try:
                return self._soxr.resample_chunk(data).astype(np.float32, copy=False)
            except Exception as e:
                log.warning("soxr resampling failed (%s) — using linear interpolation "
                            "for the rest of this session.", e)
                self._soxr = None
        return self._linear(data)

    def flush(self) -> np.ndarray:
        """Drain the filter-delay samples soxr keeps in flight. Call once,
        at the end of a recording, so the last few milliseconds of audio
        are not lost."""
        empty = (np.zeros((0, self.channels), dtype=np.float32)
                 if self.channels > 1 else np.zeros(0, dtype=np.float32))
        if self._soxr is None:
            return empty
        try:
            out = self._soxr.resample_chunk(empty, last=True)
            return out.astype(np.float32, copy=False)
        except Exception:
            return empty

    def _linear(self, data: np.ndarray) -> np.ndarray:
        buf = data if self._tail is None else np.concatenate([self._tail, data], axis=0)
        n_in = buf.shape[0]
        if n_in < 2:
            self._tail = buf
            return buf[:0]
        step = self.from_rate / self.to_rate
        max_pos = float(n_in - 1)
        count = int((max_pos - self._frac) // step) + 1
        if count <= 0:
            self._tail = buf[-1:]
            self._frac -= max_pos
            return buf[:0]
        pos = self._frac + step * np.arange(count)
        src = np.arange(n_in, dtype=np.float64)
        if buf.ndim == 1:
            out = np.interp(pos, src, buf).astype(np.float32)
        else:
            out = np.empty((count, buf.shape[1]), dtype=np.float32)
            for ch in range(buf.shape[1]):
                out[:, ch] = np.interp(pos, src, buf[:, ch])
        self._frac = (self._frac + step * count) - max_pos
        self._tail = buf[-1:]
        return out


class AudioRecorder:
    """
    Manages dual-stream audio recording (microphone + system audio),
    sample-aligned real-time mixing, file output with crash recovery
    and watchdog.
    """

    def __init__(self) -> None:
        self._init_state()
        self._init_streams()
        self._init_queues_and_threads()
        self._init_timing_and_output()
        self._init_devices_and_levels()
        self._init_settings_and_processing()
        self._init_crash_recovery()

    def _init_state(self) -> None:
        self._state = RecordingState.IDLE
        self._lock = threading.Lock()
        self._state_cv = threading.Condition()
        # Bumped on every state / mute change so SSE waiters can tell
        # whether something happened since the snapshot they rendered
        # (a bare Condition.wait() loses a notify that lands in between).
        self._change_seq: int = 0
        # Serializes watchdog recovery against stop()/teardown.
        self._recovery_lock = threading.Lock()

    def _init_streams(self) -> None:
        # Streams
        self._mic_stream: Optional[sd.InputStream] = None
        self._sys_stream: Optional[sd.InputStream] = None
        self._wasapi_stream: Any = None
        self._wasapi_thread: Optional[threading.Thread] = None
        self._pyaudio_instance: Any = None
        self._sck_source: Any = None

    def _init_queues_and_threads(self) -> None:
        # Per-session queues of (chunk, native_rate) tuples. Recreated on
        # every start() so a zombie writer from a stuck previous session
        # can never consume the new session's audio.
        self._mic_queue: queue.Queue = queue.Queue(maxsize=QUEUE_MAXSIZE)
        self._sys_queue: queue.Queue = queue.Queue(maxsize=QUEUE_MAXSIZE)

        # Writer + watchdog
        self._output_file: Optional[sf.SoundFile] = None
        self._writer_thread: Optional[threading.Thread] = None
        self._watchdog_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()  # replaced per session
        self._pause_event = threading.Event()
        self._pause_event.set()
        self._data_event = threading.Event()
        # Woken by a stream's finished_callback (device vanished) and by
        # every stop path, so the watchdog reacts at once instead of at
        # its next 2 s tick.
        self._watchdog_wake = threading.Event()
        self._last_mic_callback: float = 0.0
        # Incremented by pause(): the writer detects pauses by this edge,
        # not by sampling _pause_event (a pause+resume within one writer
        # iteration would otherwise leave stale, misaligned audio pending).
        self._pause_epoch: int = 0
        self._finalizing = False  # True while stop()/emergency save finalizes
        self._zombie_writer: Optional[threading.Thread] = None  # stuck writer

    def _init_timing_and_output(self) -> None:
        # Timing
        self._elapsed_seconds: float = 0.0
        self._recording_start_time: Optional[float] = None

        # Output
        self._output_path: Optional[Path] = None
        self._output_dir: Optional[Path] = None
        self._resolved_output_dir: Optional[Path] = None
        self._session_started_at: Optional[datetime] = None
        self._output_format: str = 'wav'  # wav, flac, mp3
        self._segment_paths: List[Path] = []
        self._segment_index: int = 0
        self._samples_in_segment: int = 0
        self._segment_frames: Dict[int, int] = {}  # per-segment frame counts

    def _init_devices_and_levels(self) -> None:
        # Devices
        self._mic_device: Optional[int] = None
        self._mic_channels: int = 1
        self._mic_samplerate: float = SAMPLE_RATE
        self._sys_device: Optional[Any] = None
        self._sys_channels: int = 2
        self._sys_samplerate: float = SAMPLE_RATE
        self._has_system_audio: bool = False
        self._system_audio_enabled: bool = True
        self._session_has_sys: bool = False
        self._sys_monitor_source: Optional[str] = None  # Linux PULSE_SOURCE for the sys stream

        # Audio levels (read by UI, written by callbacks)
        self._mic_level: float = 0.0
        self._sys_level: float = 0.0

        # Dropped-chunk accounting (queue overflow). Frames dropped per
        # source (at 48 kHz) are replaced by silence at ingest time so a
        # drop never shifts one source against the other.
        self._dropped_chunks: int = 0
        self._dropped_frames: Dict[str, int] = {'mic': 0, 'sys': 0}
        self._drop_lock = threading.Lock()
        # PortAudio-side drops: the callback reported input_overflow
        # (samples lost inside the driver before we ever saw them).
        self._input_overflows: int = 0

        # Error reporting
        self._error_callback: Optional[Callable[[str], None]] = None

    def _init_settings_and_processing(self) -> None:
        # Runtime toggles
        self._mic_muted: bool = False
        # True (default) = single combined stereo file where both channels
        # carry mic+system mixed together — natural playback.
        # False = dual-track (L=mic, R=system) for downstream speaker tagging.
        self._mix_mode: bool = True
        self._preroll_seconds: float = 0.0

        # Auto-balance: per-source slow RMS gain matching applied before
        # summing. Default ON because raw 50/50 sounds bad when mic and
        # system loudness differ (the typical case for calls).
        self._auto_balance: bool = True
        self._ab_target_rms: float = 0.12       # target ~ -18 dBFS RMS
        self._ab_time_constant: float = 0.5     # seconds: gain smoothing is
                                                # time-based, not per-block
        self._ab_min_rms: float = 0.005         # ignore silence (no gain change)
        self._ab_max_gain: float = 6.0          # never amplify > +15.5 dB
        self._limiter_knee: float = 0.85        # soft limiter above this level
        self._mic_running_gain: float = 1.0
        self._sys_running_gain: float = 1.0

        # Post-stop loudness normalization (LUFS). None = disabled.
        self._normalize_lufs: Optional[float] = None

        # Pre-roll
        self._preroll_active: bool = False  # streams kept open across recordings
        self._streams_open: bool = False
        self._mic_ring: Deque[Tuple[np.ndarray, float]] = collections.deque()
        self._sys_ring: Deque[Tuple[np.ndarray, float]] = collections.deque()
        self._ring_lock = threading.Lock()

    def _init_crash_recovery(self) -> None:
        # Optional hook the GUI registers so a signal becomes a graceful
        # "stop, save (incl. post-processing), quit" instead of a bare
        # emergency save + sys.exit inside the Qt event loop.
        self._signal_callback: Optional[Callable[[int], None]] = None

        # Crash recovery — install handlers for the signals we can on this
        # platform. On Windows SIGTERM doesn't exist, but SIGBREAK (Ctrl+Break)
        # and SIGINT (Ctrl+C) do; on POSIX we cover SIGTERM and SIGHUP too.
        atexit.register(self._emergency_save)
        replaceable = {signal.SIG_DFL, signal.SIG_IGN, None,
                       signal.default_int_handler}
        for sig_name in ("SIGTERM", "SIGINT", "SIGHUP", "SIGBREAK"):
            sig = getattr(signal, sig_name, None)
            if sig is None:
                continue
            try:
                # Only install if no custom handler is already in place.
                # Python's own default for SIGINT is default_int_handler,
                # not SIG_DFL — it must count as replaceable too.
                current = signal.getsignal(sig)
                if current in replaceable:
                    signal.signal(sig, self._signal_handler)
            except (ValueError, OSError, TypeError):
                # signal can only be installed from the main thread; skip if not.
                pass

    def set_error_callback(self, callback: Callable[[str], None]) -> None:
        self._error_callback = callback

    def set_signal_callback(self, callback: Optional[Callable[[int], None]]) -> None:
        """Register what SIGINT/SIGTERM/SIGHUP/SIGBREAK should do. The GUI
        installs a graceful stop-and-quit; without a callback the recorder
        performs an emergency save and exits the process."""
        self._signal_callback = callback

    def set_output_directory(self, path: Optional[Path]) -> None:
        self._output_dir = Path(path).expanduser() if path is not None else None
        self._resolved_output_dir = None

    @property
    def output_directory(self) -> Path:
        """The folder recordings are written to right now (configured
        folder, created on demand; ~/Downloads or $HOME as fallbacks)."""
        return self._resolve_output_dir()

    def set_output_format(self, fmt: str) -> None:
        if fmt in ('wav', 'flac', 'mp3'):
            self._output_format = fmt

    def set_mix_mode(self, mix: bool) -> None:
        """True (default) = single combined mix in both channels;
        False = dual-track stereo L=mic R=sys."""
        self._mix_mode = bool(mix)

    def set_preroll_seconds(self, seconds: float) -> None:
        self._preroll_seconds = max(0.0, float(seconds))

    def set_mic_muted(self, muted: bool) -> None:
        new_muted = bool(muted)
        if self._mic_muted != new_muted:
            self._mic_muted = new_muted
            self._bump_change()

    def set_system_audio_enabled(self, enabled: bool) -> None:
        """Disable to record microphone only (--no-system-audio). Takes
        effect for the next recording: disabling forgets the device,
        enabling (re)detects it right away when idle, so the Settings
        checkbox never needs an app restart."""
        self._system_audio_enabled = bool(enabled)
        if not self._system_audio_enabled:
            self._sys_device = None
            self._has_system_audio = False
            self._sys_monitor_source = None
        elif self._sys_device is None and self._state == RecordingState.IDLE:
            self._detect_system_audio()
        # Pre-roll keeps the streams open between recordings: reopen them
        # so the next session really reflects the new choice.
        if self._preroll_active and self._streams_open and self._state == RecordingState.IDLE:
            with self._lock:
                with self._recovery_lock:
                    self._close_streams()
                    try:
                        self._open_streams()
                    except Exception as e:
                        log.warning("Pre-roll streams could not be reopened: %s", e)

    @property
    def is_mic_muted(self) -> bool:
        return self._mic_muted

    def set_auto_balance(self, enabled: bool) -> None:
        """Enable/disable per-source RMS gain matching before mixing."""
        self._auto_balance = bool(enabled)
        if not enabled:
            self._mic_running_gain = 1.0
            self._sys_running_gain = 1.0

    def set_normalize_lufs(self, target_lufs: Optional[float]) -> None:
        """Set the target loudness (LUFS) for post-stop normalization.
        Pass None to disable. Typical: -16 (podcast) or -14 (streaming)."""
        self._normalize_lufs = target_lufs

    def _report_error(self, message: str) -> None:
        if self._error_callback:
            try:
                self._error_callback(message)
            except Exception:
                log.exception("Error callback failed")

    # ---------- Device Detection ----------

    def detect_devices(self) -> tuple[bool, bool, str]:
        self._detect_mic()
        if not self._system_audio_enabled:
            self._sys_device = None
            self._has_system_audio = False
            return (self._mic_device is not None, False, "")
        self._detect_system_audio()
        guidance = "" if self._has_system_audio else get_system_audio_guidance()
        return (self._mic_device is not None, self._has_system_audio, guidance)

    def _detect_mic(self) -> bool:
        mic_idx, mic_ch, mic_sr = detect_mic_device()
        if mic_idx is not None:
            if mic_idx != self._mic_device:
                log.info("Microphone: device %s @ %.0f Hz", mic_idx, mic_sr)
            self._mic_device = mic_idx
            self._mic_channels = min(mic_ch, 1)
            self._mic_samplerate = mic_sr
        else:
            self._mic_device = None
        return self._mic_device is not None

    def _detect_system_audio(self) -> bool:
        sys_dev, sys_ch, sys_sr = detect_system_audio_device()
        if sys_dev is not None:
            self._sys_device = sys_dev
            self._sys_channels = min(sys_ch, 2)
            self._sys_samplerate = sys_sr
            self._has_system_audio = True
            # Linux 'pulse' fallback: the monitor to capture goes with the
            # device (snapshot, not the mutable module global).
            self._sys_monitor_source = (platform_audio.linux_monitor_source
                                        if sys.platform == 'linux' else None)
        else:
            self._sys_device = None
            self._has_system_audio = False
            self._sys_monitor_source = None
        return self._has_system_audio

    @property
    def has_microphone(self) -> bool:
        return self._mic_device is not None

    def _refresh_portaudio_devices(self) -> None:
        """PortAudio's device table is frozen at initialization: a headset
        plugged in after launch, or a default input changed in the OS, is
        invisible until PortAudio is re-initialized. Done right before the
        streams are opened, when no PortAudio stream exists (SCK and WASAPI
        capture do not go through this PortAudio instance)."""
        if self._mic_stream is not None or self._sys_stream is not None:
            return
        try:
            sd._terminate()
            sd._initialize()
        except Exception as e:
            log.debug("PortAudio re-initialization failed: %s", e)
        self._detect_mic()
        if self._system_audio_enabled and (
                self._sys_device is None or not isinstance(self._sys_device, (dict, str))):
            # Not found at launch (helper/permission/plugin fixed since?) or
            # a PortAudio device whose index may have moved: look again.
            had = self._has_system_audio
            self._detect_system_audio()
            if self._has_system_audio != had:
                log.info("System audio %s.", "available" if self._has_system_audio else "unavailable")

    # ---------- Properties ----------

    @property
    def state(self) -> RecordingState:
        return self._state

    def wait_for_state_change(self, timeout: float = 1.0,
                              since: Optional[int] = None) -> int:
        """Block until the state or mute flag changes (or timeout).
        ``since`` is the sequence number returned by a previous call: if a
        change already happened after it, return immediately (no lost
        wake-ups). Returns the current sequence number."""
        with self._state_cv:
            if since is None or self._change_seq == since:
                self._state_cv.wait(timeout)
            return self._change_seq

    def _bump_change(self) -> None:
        with self._state_cv:
            self._change_seq += 1
            self._state_cv.notify_all()

    def _set_state(self, new_state: RecordingState) -> None:
        if self._state != new_state:
            self._state = new_state
            self._bump_change()

    @property
    def elapsed_time(self) -> float:
        if self._state == RecordingState.IDLE:
            return 0.0
        if self._state in (RecordingState.PAUSED, RecordingState.STOPPING):
            return self._elapsed_seconds
        # Single read: pause()/stop() can null the attribute between the
        # check and the use (this property is read from API/UI threads).
        start = self._recording_start_time
        if self._state == RecordingState.RECORDING and start is not None:
            return self._elapsed_seconds + (time.monotonic() - start)
        return self._elapsed_seconds

    @property
    def output_path(self) -> Optional[Path]:
        return self._output_path

    @property
    def has_system_audio(self) -> bool:
        return self._has_system_audio

    @property
    def mic_level(self) -> float:
        return self._mic_level

    @property
    def sys_level(self) -> float:
        return self._sys_level

    @property
    def dropped_chunks(self) -> int:
        return self._dropped_chunks

    @property
    def input_overflows(self) -> int:
        """Number of PortAudio input-overflow reports this session."""
        return self._input_overflows

    @property
    def segment_paths(self) -> List[Path]:
        return list(self._segment_paths)

    # ---------- Public API ----------

    def start(self) -> Path:
        with self._lock:
            if self._state != RecordingState.IDLE:
                raise RuntimeError(f"Cannot start from state {self._state}")
            # A wedged writer from a previous session still owns the shared
            # file attributes; starting now would let it write into (and
            # close) the new session's file.
            zombie = self._zombie_writer
            if zombie is not None:
                if zombie.is_alive():
                    raise RuntimeError(
                        "La registrazione precedente non è ancora stata "
                        "finalizzata. Riprova tra qualche secondo.")
                self._zombie_writer = None

            # Check disk space
            self._check_disk_space(raise_on_low=True)

            # Generate output path
            self._output_path = self._generate_output_path()
            self._segment_paths = [self._output_path]
            self._segment_index = 0
            self._samples_in_segment = 0
            self._segment_frames = {}

            # Open output file
            self._open_output_file(self._output_path)

            # Fresh per-session queues and stop event: a zombie writer from
            # a stuck previous session keeps its own references and can
            # neither consume our audio nor be revived by our state.
            self._mic_queue = queue.Queue(maxsize=QUEUE_MAXSIZE)
            self._sys_queue = queue.Queue(maxsize=QUEUE_MAXSIZE)
            self._stop_event = threading.Event()
            self._pause_event.set()
            self._data_event.clear()
            self._elapsed_seconds = 0.0
            self._mic_level = 0.0
            self._sys_level = 0.0
            self._dropped_chunks = 0
            self._dropped_frames = {'mic': 0, 'sys': 0}
            self._input_overflows = 0
            self._session_started_at = datetime.now()
            self._last_mic_callback = time.monotonic()
            self._watchdog_wake.clear()
            self._mic_running_gain = 1.0
            self._sys_running_gain = 1.0
            self._finalizing = False

            # Open audio streams unless pre-roll mode already has them open.
            # On failure, close the output file and delete the empty placeholder.
            try:
                self._open_streams()
            except Exception:
                self._discard_output_file()
                raise

            self._session_has_sys = (
                self._sys_stream is not None
                or self._sck_source is not None
                or self._wasapi_stream is not None
            )

            # Snapshot the pre-roll rings and flip to RECORDING inside the
            # ring lock: no chunk can slip between snapshot and flip
            # (_append_to_ring re-checks the state under the same lock).
            with self._ring_lock:
                mic_prelude = list(self._mic_ring)
                sys_prelude = list(self._sys_ring)
                self._mic_ring.clear()
                self._sys_ring.clear()
                if not (self._preroll_active and self._preroll_seconds > 0):
                    mic_prelude = []
                    sys_prelude = []
                self._recording_start_time = time.monotonic()
                self._set_state(RecordingState.RECORDING)

            stop_event = self._stop_event
            self._writer_thread = threading.Thread(
                target=self._writer_loop,
                args=(stop_event, self._mic_queue, self._sys_queue,
                      mic_prelude, sys_prelude, self._session_has_sys),
                daemon=True, name='audio-writer',
            )
            self._writer_thread.start()

            self._watchdog_thread = threading.Thread(
                target=self._watchdog_loop,
                args=(stop_event, self._writer_thread),
                daemon=True, name='watchdog',
            )
            self._watchdog_thread.start()

        return self._output_path

    def pause(self) -> None:
        with self._lock:
            if self._state != RecordingState.RECORDING:
                raise RuntimeError(f"Cannot pause from state {self._state}")
            if self._recording_start_time is not None:
                self._elapsed_seconds += time.monotonic() - self._recording_start_time
                self._recording_start_time = None
            self._pause_epoch += 1
            self._pause_event.clear()
            self._set_state(RecordingState.PAUSED)

    def resume(self) -> None:
        with self._lock:
            if self._state != RecordingState.PAUSED:
                raise RuntimeError(f"Cannot resume from state {self._state}")
            self._drain_queue(self._mic_queue)
            self._drain_queue(self._sys_queue)
            self._recording_start_time = time.monotonic()
            self._pause_event.set()
            self._set_state(RecordingState.RECORDING)

    def stop(self) -> Optional[Path]:
        """
        Stop capture and finalize the file (plus optional ffmpeg
        post-processing). Synchronous and potentially slow — call from a
        worker thread in GUI code. State is STOPPING until everything,
        including MP3 conversion, is done.
        """
        with self._lock:
            if self._state in (RecordingState.IDLE, RecordingState.STOPPING):
                return None
            if self._state == RecordingState.RECORDING and self._recording_start_time is not None:
                self._elapsed_seconds += time.monotonic() - self._recording_start_time
                self._recording_start_time = None
            self._set_state(RecordingState.STOPPING)
            self._finalizing = True
            stop_event = self._stop_event
            self._pause_event.set()
            stop_event.set()
            self._data_event.set()
            self._watchdog_wake.set()

        # The writer finalizes (flushes + closes) the output file itself
        # before exiting — it is the only thread that touches the file.
        writer = self._writer_thread
        writer_stuck = False
        if writer is not None:
            writer.join(timeout=10.0)
            writer_stuck = self._park_writer_if_stuck(writer)
            self._writer_thread = None

        watchdog = self._watchdog_thread
        if watchdog is not None:
            watchdog.join(timeout=3.0)
            self._watchdog_thread = None

        # Wait for any in-flight watchdog recovery, then tear down streams.
        # Keep streams open if pre-roll mode is active — they must continue
        # to feed the ring buffer between recordings.
        with self._recovery_lock:
            if not self._preroll_active:
                self._close_streams()

        # Safety net: if the writer died without finalizing, do it here
        # (it is no longer running, so no concurrent access).
        if self._output_file is not None and not writer_stuck:
            self._finalize_segment()

        if self._dropped_chunks:
            log.warning("Recording dropped %d audio chunks (writer overloaded).",
                        self._dropped_chunks)

        # Post-processing must never run on a file a wedged writer still
        # holds open (MP3 conversion would even delete the WAV under it).
        if not writer_stuck:
            self._post_process()

        with self._lock:
            self._set_state(RecordingState.IDLE)
            self._finalizing = False

        return self._output_path

    def _park_writer_if_stuck(self, writer: Optional[threading.Thread]) -> bool:
        """A writer that did not exit within its join timeout still owns
        the open output file: remember it so start() refuses a new session
        that would share the file attributes with it. Used by every
        teardown path (stop, watchdog auto-stop, emergency save)."""
        if writer is not None and writer.is_alive():
            self._zombie_writer = writer
            self._report_error("Writer thread did not stop in time.")
            return True
        return False

    def _post_process(self) -> None:
        """Loudness normalization, then MP3 conversion (so the normalised
        PCM is what gets encoded). Runs after the file is closed, on
        whichever thread finalizes the session. Skipped, with a message,
        when the disk cannot hold the temporary copy ffmpeg needs."""
        if not self._segment_paths:
            return
        if self._normalize_lufs is None and self._output_format != 'mp3':
            self._rewrite_sidecars()
            return
        try:
            largest = max((p.stat().st_size for p in self._segment_paths if p.exists()),
                          default=0)
            free = shutil.disk_usage(str(self._segment_paths[0].parent)).free
            if free < largest + MIN_DISK_SPACE_BYTES:
                self._report_error(
                    "Spazio su disco insufficiente per la post-elaborazione: "
                    "file salvato senza normalizzazione/conversione.")
                log.warning("Post-processing skipped: %d MB free, largest segment %d MB.",
                            free // (1024 * 1024), largest // (1024 * 1024))
                self._rewrite_sidecars()
                return
        except OSError as e:
            log.debug("Disk check before post-processing failed: %s", e)
        if self._normalize_lufs is not None:
            self._loudness_normalize_segments(self._normalize_lufs)
        if self._output_format == 'mp3':
            self._convert_to_mp3()
        self._rewrite_sidecars()

    # ---------- Audio Callbacks ----------

    def _mic_callback(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        self._last_mic_callback = time.monotonic()
        if status:
            self._note_callback_status('Microphone', status)
        chunk = indata.copy()
        if self._mic_muted:
            chunk[:] = 0
        # RMS level for VU meter (always — even when muted, so the user sees "0")
        self._mic_level = 0.0 if self._mic_muted else float(np.sqrt(np.mean(indata * indata)))
        self._route_chunk(chunk, self._mic_samplerate, self._mic_queue, self._mic_ring)

    def _sys_callback(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        if status:
            self._note_callback_status('System audio', status)
        chunk = indata.copy()
        self._sys_level = float(np.sqrt(np.mean(chunk * chunk)))
        self._route_chunk(chunk, self._sys_samplerate, self._sys_queue, self._sys_ring)

    def _note_callback_status(self, source: str, status) -> None:
        """sounddevice passes CallbackFlags; input_overflow means PortAudio
        discarded samples before our callback ran (callback too slow, CPU
        starved). Count it (exposed via /status) and log it rate-limited,
        so a recording with gaps is diagnosable instead of silently short."""
        try:
            overflow = bool(getattr(status, 'input_overflow', False))
        except Exception:
            return
        if not overflow:
            return
        self._input_overflows += 1
        n = self._input_overflows
        if n == 1 or n % 100 == 0:
            log.warning("%s input overflow reported by PortAudio (%d so far).", source, n)

    def _route_chunk(self, chunk: np.ndarray, rate: float,
                     q: queue.Queue, ring: Deque) -> None:
        """Send to the writer queue when recording, to the pre-roll ring when idle."""
        state = self._state
        if state == RecordingState.RECORDING:
            try:
                q.put_nowait((chunk, rate))
                self._data_event.set()
            except queue.Full:
                self._note_dropped_chunk(chunk, rate, ring)
        elif (self._preroll_active and self._preroll_seconds > 0
                and state in (RecordingState.IDLE, RecordingState.STOPPING)):
            self._append_to_ring(chunk, rate, ring, q)
        # PAUSED: drop — paused recordings should not capture audio.

    def _note_dropped_chunk(self, chunk: np.ndarray, rate: float, ring: Deque) -> None:
        """Queue overflow: the writer is far behind. Count the chunk and
        remember how many 48 kHz frames it was worth, per source, so the
        writer can put an equal amount of silence in its place instead of
        letting the two sources slide apart by one chunk per drop."""
        source = 'mic' if ring is self._mic_ring else 'sys'
        frames = int(round(chunk.shape[0] * SAMPLE_RATE / float(rate))) if rate else chunk.shape[0]
        with self._drop_lock:
            self._dropped_chunks += 1
            self._dropped_frames[source] += frames
            n = self._dropped_chunks
        if n == 1 or n % 100 == 0:
            log.warning("Audio queue full — dropped %d chunks so far.", n)

    def _take_dropped_frames(self, source: str) -> int:
        with self._drop_lock:
            frames = self._dropped_frames.get(source, 0)
            self._dropped_frames[source] = 0
        return frames

    def _append_to_ring(self, chunk: np.ndarray, rate: float,
                        ring: Deque, q: queue.Queue) -> None:
        """Append to the ring, trimming to preroll_seconds of audio
        (duration computed at each chunk's native rate)."""
        with self._ring_lock:
            if self._state == RecordingState.RECORDING:
                # start() flipped the state while we waited on the lock:
                # the ring was already snapshotted, so route to the queue
                # to preserve sample order. Re-read the queue attribute —
                # the one passed in may belong to the previous session
                # (start() swaps queues before flipping the state).
                live_q = self._mic_queue if ring is self._mic_ring else self._sys_queue
                try:
                    live_q.put_nowait((chunk, rate))
                    self._data_event.set()
                except queue.Full:
                    self._note_dropped_chunk(chunk, rate, ring)
                return
            ring.append((chunk, rate))
            total_sec = sum(c.shape[0] / r for c, r in ring)
            while len(ring) > 1 and total_sec > self._preroll_seconds:
                c, r = ring.popleft()
                total_sec -= c.shape[0] / r

    def enable_preroll_capture(self) -> None:
        """
        Open the audio streams immediately and keep them running so that the
        first ``preroll_seconds`` of any subsequent recording are pre-captured.

        Call once, after detect_devices(), if preroll_seconds > 0.
        """
        if self._preroll_seconds <= 0:
            return
        # Serialized against start()/stop() and watchdog recovery so a
        # future settings toggle can never yank streams from under a
        # session that is being set up.
        with self._lock:
            if self._streams_open:
                return
            self._preroll_active = True
            with self._recovery_lock:
                try:
                    self._open_streams()
                except Exception as e:
                    log.warning("Pre-roll capture failed to start: %s", e)
                    self._preroll_active = False
                    self._close_streams()

    def disable_preroll_capture(self) -> None:
        with self._lock:
            if not self._preroll_active:
                return
            self._preroll_active = False
            # Only close streams if not currently recording (otherwise stop() will).
            if self._state == RecordingState.IDLE:
                with self._recovery_lock:
                    self._close_streams()
                with self._ring_lock:
                    self._mic_ring.clear()
                    self._sys_ring.clear()

    # ---------- Stream Management ----------

    def _open_streams(self) -> None:
        if self._streams_open:
            # Pre-roll keeps streams open between recordings with no
            # watchdog: a mic unplugged while idle would otherwise start a
            # session with no microphone data until the session watchdog
            # notices seconds later. Reopen if anything looks dead.
            mic_ok = (self._mic_stream is not None
                      and getattr(self._mic_stream, 'active', True))
            sck_ok = self._sck_source is None or self._sck_source.is_running()
            if mic_ok and sck_ok:
                return
            log.warning("Pre-roll streams unhealthy — reopening.")
            self._close_streams()
        self._refresh_portaudio_devices()
        if self._mic_device is None:
            raise RuntimeError("Nessun microfono disponibile. Collegane uno e riprova.")
        try:
            self._mic_stream = sd.InputStream(
                samplerate=self._mic_samplerate,
                blocksize=BLOCKSIZE,
                device=self._mic_device,
                channels=1,
                dtype=DTYPE,
                latency='high',  # robustness over latency: fewer overflows
                callback=self._mic_callback,
                finished_callback=self._on_stream_finished,
            )
            self._mic_stream.start()
            self._last_mic_callback = time.monotonic()
        except Exception as e:
            self._report_error(f"Microphone error: {e}")
            raise

        if self._has_system_audio and self._sys_device is not None:
            sck_in_use = (self._sys_device == "sck")
            try:
                if sys.platform == 'win32' and isinstance(self._sys_device, dict):
                    self._refresh_wasapi_device()
                    self._open_wasapi_loopback()
                elif sck_in_use:
                    self._open_sck_source()
                else:
                    self._open_portaudio_sys_stream()
            except Exception as e:
                # SCK failure is almost always a missing 'Screen Recording'
                # permission. Degrading to mic-only would silently reproduce the
                # exact bug we're trying to fix, so fail loudly and abort.
                if sck_in_use:
                    self._close_streams()  # clean up the mic we just opened
                    raise RuntimeError(str(e)) from e
                self._report_error(f"System audio unavailable: {e}")
                self._has_system_audio = False
                self._sys_stream = None

        self._streams_open = True

    def _open_portaudio_sys_stream(self) -> None:
        """System audio through PortAudio (Linux monitor source, macOS
        BlackHole-style virtual device)."""
        monitor = self._sys_monitor_source if sys.platform == 'linux' else None
        with _pulse_source_env(monitor):
            stream = sd.InputStream(
                samplerate=self._sys_samplerate,
                blocksize=BLOCKSIZE,
                device=self._sys_device,
                channels=self._sys_channels,
                dtype=DTYPE,
                latency='high',
                callback=self._sys_callback,
                finished_callback=self._on_stream_finished,
            )
            stream.start()
        self._sys_stream = stream

    def _on_stream_finished(self) -> None:
        """sounddevice finished_callback: the stream became inactive
        (device unplugged, host error) — poke the watchdog now."""
        if self._state in (RecordingState.RECORDING, RecordingState.PAUSED):
            self._watchdog_wake.set()

    def _refresh_wasapi_device(self) -> None:
        """Re-detect the WASAPI loopback device so we don't open a stale
        index after the user switched their default output."""
        try:
            dev, ch, sr = detect_system_audio_device()
            if dev is not None and isinstance(dev, dict):
                self._sys_device = dev
                self._sys_channels = min(int(ch), 2)
                self._sys_samplerate = float(sr)
        except Exception as e:
            log.debug("WASAPI re-detection failed, using cached device: %s", e)

    def _open_wasapi_loopback(self) -> None:
        """WASAPI loopback (Windows) in callback mode: PortAudio drives the
        callback from its own real-time thread and reports overflows via
        the status flags — a blocking read() from a Python thread could
        neither see them nor keep up under GIL contention."""
        import pyaudiowpatch as pyaudio
        dev_info = self._sys_device
        self._pyaudio_instance = pyaudio.PyAudio()
        channels = int(dev_info['maxInputChannels'])
        rate = int(dev_info['defaultSampleRate'])
        self._sys_channels = min(channels, 2)
        self._sys_samplerate = rate
        overflow_flag = int(getattr(pyaudio, 'paInputOverflow', 0))
        continue_flag = getattr(pyaudio, 'paContinue', 0)

        def _callback(in_data, frame_count, time_info, status):
            try:
                if overflow_flag and status and (int(status) & overflow_flag):
                    self._note_callback_status(
                        'System audio', types.SimpleNamespace(input_overflow=True))
                arr = np.frombuffer(in_data, dtype=np.float32).reshape(-1, channels)
                if channels > 2:
                    arr = arr[:, :2]
                # Route via the shared sys callback so pre-roll/recording
                # state is respected uniformly.
                self._sys_callback(arr, arr.shape[0], None, None)
            except Exception as e:
                log.debug("WASAPI callback error: %s", e)
            return (None, continue_flag)

        self._wasapi_stream = self._pyaudio_instance.open(
            format=pyaudio.paFloat32,
            channels=channels,
            rate=rate,
            input=True,
            input_device_index=dev_info['index'],
            frames_per_buffer=BLOCKSIZE,
            stream_callback=_callback,
            start=True,
        )

    def _wasapi_stream_active(self) -> bool:
        stream = self._wasapi_stream
        if stream is None:
            return False
        try:
            return bool(stream.is_active())
        except Exception:
            return False

    def _open_sck_source(self) -> None:
        from macos_system_audio import SCKAudioSource

        def _on_audio(chunk: np.ndarray) -> None:
            # Route through the shared sys callback so pre-roll state is
            # respected (and level is updated as a side effect).
            self._sys_callback(chunk, chunk.shape[0], None, None)

        source = SCKAudioSource(
            on_audio=_on_audio,
            on_error=self._report_error,
        )
        source.start()
        self._sck_source = source
        self._sys_samplerate = float(source.sample_rate)
        self._sys_channels = source.channels

    def _close_streams(self) -> None:
        for stream in (self._mic_stream, self._sys_stream):
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass
        self._mic_stream = None
        self._sys_stream = None

        if self._wasapi_stream is not None:
            try:
                self._wasapi_stream.stop_stream()
                self._wasapi_stream.close()
            except Exception:
                pass
            self._wasapi_stream = None
        if self._pyaudio_instance is not None:
            try:
                self._pyaudio_instance.terminate()
            except Exception:
                pass
            self._pyaudio_instance = None

        if self._sck_source is not None:
            try:
                self._sck_source.stop()
            except Exception:
                pass
            self._sck_source = None

        self._streams_open = False

    # ---------- Output File Management ----------

    def _open_output_file(self, path: Path) -> None:
        fmt = self._output_format
        if fmt == 'mp3':
            fmt = 'wav'  # Record as WAV, convert later

        sf_format = 'WAV' if fmt == 'wav' else 'FLAC'
        subtype = OUTPUT_SUBTYPE if fmt == 'wav' else 'PCM_16'

        self._output_file = sf.SoundFile(
            str(path), mode='w',
            samplerate=SAMPLE_RATE, channels=CHANNELS,
            format=sf_format, subtype=subtype,
        )

        if sf_format == 'WAV':
            # Keep the RIFF header consistent on every write: a recording
            # that dies hard (SIGKILL, power loss) stays playable up to the
            # last flushed sample. Best effort — uses soundfile internals.
            try:
                sf._snd.sf_command(  # type: ignore[attr-defined]
                    self._output_file._file, _SFC_SET_UPDATE_HEADER_AUTO,
                    sf._ffi.NULL, 1,  # type: ignore[attr-defined]
                )
            except Exception as e:
                log.debug("Header auto-update unavailable: %s", e)

        # FLAC supports embedded tags via soundfile (libsndfile). WAV does not
        # carry useful metadata for us, so we write a sidecar .json for it.
        if sf_format == 'FLAC':
            try:
                self._output_file.title = path.stem
                self._output_file.software = "Orizon Call"
                self._output_file.date = datetime.now().isoformat(timespec="seconds")
                self._output_file.comment = (
                    f"dual_track={'no' if self._mix_mode else 'yes'} "
                    f"channels={'mixed' if self._mix_mode else 'L:mic,R:sys'}"
                )
            except Exception as e:
                log.debug("FLAC metadata write failed: %s", e)

    def _finalize_segment(self) -> None:
        """Flush + close the current output file and write its sidecar."""
        segment_path = (self._segment_paths[self._segment_index]
                        if self._segment_index < len(self._segment_paths)
                        else self._output_path)
        frames = self._samples_in_segment
        if self._output_file is not None:
            try:
                self._output_file.flush()
                self._output_file.close()
            except Exception:
                pass
            self._output_file = None

        # Sidecar JSON next to every segment (also FLAC, which embeds tags
        # too: the sidecar is what carries the multi-segment layout).
        if segment_path is not None and segment_path.exists():
            self._segment_frames[self._segment_index] = frames
            self._write_sidecar_json(segment_path, frames)

    def _discard_output_file(self) -> None:
        """Failed start(): close and remove the empty placeholder file."""
        if self._output_file is not None:
            try:
                self._output_file.close()
            except Exception:
                pass
            self._output_file = None
        if self._output_path is not None:
            try:
                self._output_path.unlink(missing_ok=True)
                self._sidecar_path(self._output_path).unlink(missing_ok=True)
            except Exception:
                pass
        self._output_path = None
        self._segment_paths = []

    @staticmethod
    def _sidecar_path(audio_path: Path) -> Path:
        return audio_path.with_suffix(audio_path.suffix + ".json")

    def _sidecar_payload(self, audio_path: Path, frames: int, index: int) -> dict:
        started = self._session_started_at or datetime.now()
        return {
            "software": "Orizon Call",
            "recorded_at": started.isoformat(timespec="seconds"),
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "format": audio_path.suffix.lstrip('.').lower(),
            "sample_rate": SAMPLE_RATE,
            "channels": CHANNELS,
            "dual_track": not self._mix_mode,
            "channel_map": (["mic", "system"] if not self._mix_mode else ["mixed", "mixed"]),
            "duration_seconds": round(frames / SAMPLE_RATE, 2),
            "segment_index": index,
            "segment_count": len(self._segment_paths),
            "segments": [str(p) for p in self._segment_paths],
            "dropped_chunks": self._dropped_chunks,
            "input_overflows": self._input_overflows,
        }

    def _write_sidecar_json(self, audio_path: Path, frames: int,
                            index: Optional[int] = None) -> None:
        try:
            payload = self._sidecar_payload(
                audio_path, frames, self._segment_index if index is None else index)
            self._sidecar_path(audio_path).write_text(
                json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as e:
            log.debug("Sidecar JSON write failed: %s", e)

    def _rewrite_sidecars(self) -> None:
        """Once the session is final (all segments known, post-processing
        done) rewrite every segment's sidecar with the complete segment
        list — the one written at split time only knew the segments that
        existed then."""
        if len(self._segment_paths) < 2:
            return
        for index, path in enumerate(self._segment_paths):
            if not path.exists():
                continue
            frames = self._segment_frames.get(index)
            if frames is None:
                existing = self._read_sidecar_json(path)
                frames = int(round((existing or {}).get("duration_seconds", 0.0) * SAMPLE_RATE))
            self._write_sidecar_json(path, frames, index=index)

    def _read_sidecar_json(self, audio_path: Path) -> Optional[dict]:
        try:
            data = json.loads(self._sidecar_path(audio_path).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def _split_file(self) -> None:
        """Close current segment and open a new one for long recordings."""
        self._finalize_segment()
        self._segment_index += 1
        base = self._output_path
        stem = base.stem
        suffix = base.suffix
        new_path = base.parent / f"{stem}_part{self._segment_index + 1}{suffix}"
        self._segment_paths.append(new_path)
        self._samples_in_segment = 0
        self._open_output_file(new_path)

    # ---------- Writer Thread ----------

    @staticmethod
    def _pending_frames(pending: List[np.ndarray]) -> int:
        return sum(a.shape[0] for a in pending)

    @staticmethod
    def _take_frames(pending: List[np.ndarray], n: int) -> np.ndarray:
        """Pop exactly n frames from the front of the pending list."""
        taken: List[np.ndarray] = []
        remaining = n
        while remaining > 0 and pending:
            head = pending[0]
            if head.shape[0] <= remaining:
                taken.append(head)
                remaining -= head.shape[0]
                pending.pop(0)
            else:
                taken.append(head[:remaining])
                pending[0] = head[remaining:]
                remaining = 0
        return np.concatenate(taken, axis=0)

    def _ingest(self, q: queue.Queue, pending: List[np.ndarray],
                resamplers: dict, to_mono: bool) -> bool:
        """Move queued (chunk, rate) tuples into the pending buffer:
        normalize shape, resample to 48 kHz with per-rate stateful
        resamplers (no boundary clicks, no drift). Chunks the callback
        had to drop (queue full) are replaced by an equal amount of
        silence *after* the queued audio — they were lost while the queue
        held it — so the source keeps its length. Returns True if any
        frames were received."""
        source = 'mic' if to_mono else 'sys'
        got = False
        while True:
            # A drop happens only while the queue is full, i.e. after every
            # item present right now and before anything enqueued later:
            # take the count first, drain exactly those items, then place
            # the silence, then look again.
            lost = self._take_dropped_frames(source)
            n_items = q.qsize()
            if n_items == 0 and lost == 0:
                break
            for _ in range(n_items):
                try:
                    chunk, rate = q.get_nowait()
                except queue.Empty:
                    break
                got = True
                arr = np.asarray(chunk, dtype=np.float32)
                arr = self._to_mono(arr) if to_mono else self._to_stereo(arr)
                if rate != SAMPLE_RATE:
                    key = (to_mono, float(rate))
                    rs = resamplers.get(key)
                    if rs is None:
                        rs = _StreamingResampler(rate, SAMPLE_RATE,
                                                 1 if to_mono else 2)
                        resamplers[key] = rs
                    arr = rs.process(arr)
                if arr.shape[0]:
                    pending.append(arr)
            if lost > 0:
                pending.append(np.zeros(lost, dtype=np.float32) if to_mono
                               else np.zeros((lost, CHANNELS), dtype=np.float32))
                got = True
        return got

    def _writer_loop(self, stop_event: threading.Event,
                     mic_q: queue.Queue, sys_q: queue.Queue,
                     mic_prelude: List[Tuple[np.ndarray, float]],
                     sys_prelude: List[Tuple[np.ndarray, float]],
                     session_has_sys: bool) -> None:
        """
        Single owner of the output file. Aligns the two sources sample-by-
        sample: writes only the overlap that both have produced, keeping
        leftovers for the next iteration. Silence is injected only when a
        source is starved for >0.5 s (e.g. helper crashed) so the timeline
        of the healthy source is never stretched or chopped.
        """
        last_disk_check = time.monotonic()
        last_header_flush = time.monotonic()
        mic_pending: List[np.ndarray] = []
        sys_pending: List[np.ndarray] = []
        resamplers: dict = {}
        seen_pause_epoch = self._pause_epoch
        align = _AlignState(time.monotonic())

        def write_mixed(mic_b: Optional[np.ndarray], sys_b: Optional[np.ndarray]) -> bool:
            """Mix + write one aligned block, splitting the file exactly at
            the segment limit (a block may span the boundary)."""
            mixed = self._mix_frames(mic_b, sys_b)
            total = mixed.shape[0]
            offset = 0
            while offset < total:
                room = MAX_SAMPLES_PER_SEGMENT - self._samples_in_segment
                if room <= 0:
                    self._split_file()
                    continue
                take = min(room, total - offset)
                if self._output_file is not None:
                    self._output_file.write(mixed[offset:offset + take])
                    self._samples_in_segment += take
                offset += take
            return True

        # Pre-roll prelude goes straight into the pending buffers so it is
        # written before any live audio, in capture order.
        for chunk, rate in mic_prelude:
            arr = self._to_mono(np.asarray(chunk, dtype=np.float32))
            if rate != SAMPLE_RATE:
                key = (True, float(rate))
                rs = resamplers.setdefault(
                    key, _StreamingResampler(rate, SAMPLE_RATE, 1))
                arr = rs.process(arr)
            if arr.shape[0]:
                mic_pending.append(arr)
        for chunk, rate in sys_prelude:
            arr = self._to_stereo(np.asarray(chunk, dtype=np.float32))
            if rate != SAMPLE_RATE:
                key = (False, float(rate))
                rs = resamplers.setdefault(
                    key, _StreamingResampler(rate, SAMPLE_RATE, 2))
                arr = rs.process(arr)
            if arr.shape[0]:
                sys_pending.append(arr)

        try:
            while not stop_event.is_set():
                self._data_event.clear()
                self._pause_event.wait(timeout=0.1)
                if stop_event.is_set():
                    break
                if self._pause_epoch != seen_pause_epoch:
                    # A pause happened (maybe already resumed): discard the
                    # in-flight audio so the recording continues realigned
                    # from a clean slate.
                    seen_pause_epoch = self._pause_epoch
                    mic_pending.clear()
                    sys_pending.clear()
                    self._take_dropped_frames('mic')
                    self._take_dropped_frames('sys')
                    align.reset(time.monotonic())
                if not self._pause_event.is_set():
                    self._drain_queue(mic_q)
                    self._drain_queue(sys_q)
                    continue

                now = time.monotonic()
                mic_before = self._pending_frames(mic_pending)
                sys_before = self._pending_frames(sys_pending)
                if self._ingest(mic_q, mic_pending, resamplers, to_mono=True):
                    align.last_frames['mic'] = now
                if session_has_sys:
                    if self._ingest(sys_q, sys_pending, resamplers, to_mono=False):
                        align.last_frames['sys'] = now

                wrote = False
                if session_has_sys:
                    wrote = self._align_step(align, now, mic_pending, sys_pending, write_mixed,
                                             mic_before, sys_before)
                else:
                    m_av = self._pending_frames(mic_pending)
                    if m_av >= MIN_WRITE_FRAMES:
                        wrote = write_mixed(self._take_frames(mic_pending, m_av), None)

                if not wrote:
                    self._data_event.wait(timeout=0.05)

                now = time.monotonic()
                # Keep the on-disk header valid for crash recovery.
                if now - last_header_flush > HEADER_FLUSH_SECONDS:
                    last_header_flush = now
                    if self._output_file is not None:
                        try:
                            self._output_file.flush()
                        except Exception:
                            pass

                # Periodic disk space check
                if now - last_disk_check > DISK_CHECK_SECONDS:
                    last_disk_check = now
                    if not self._check_disk_space(raise_on_low=False):
                        raise OSError("Disk space low")

            # Final drain: align what's left, then pad the single
            # shorter tail with silence so no captured audio is lost.
            self._ingest(mic_q, mic_pending, resamplers, to_mono=True)
            if session_has_sys:
                self._ingest(sys_q, sys_pending, resamplers, to_mono=False)
            # Flush the resamplers' filter delay into the right buffer.
            for (is_mono, _rate), rs in resamplers.items():
                tail = rs.flush()
                if tail.shape[0]:
                    (mic_pending if is_mono else sys_pending).append(tail)

            if session_has_sys:
                n = min(self._pending_frames(mic_pending),
                        self._pending_frames(sys_pending))
                if n > 0:
                    write_mixed(self._take_frames(mic_pending, n),
                                self._take_frames(sys_pending, n))
                m_av = self._pending_frames(mic_pending)
                s_av = self._pending_frames(sys_pending)
                if m_av > 0:
                    write_mixed(self._take_frames(mic_pending, m_av),
                                np.zeros((m_av, CHANNELS), dtype=np.float32))
                elif s_av > 0:
                    write_mixed(np.zeros(s_av, dtype=np.float32),
                                self._take_frames(sys_pending, s_av))
            else:
                m_av = self._pending_frames(mic_pending)
                if m_av > 0:
                    write_mixed(self._take_frames(mic_pending, m_av), None)

            self._finalize_segment()

        except Exception as e:
            self._writer_fatal(stop_event, f"Recording stopped: {e}", exc=e)

    def _align_step(self, st: _AlignState, now: float,
                    mic_pending: List[np.ndarray], sys_pending: List[np.ndarray],
                    write_mixed: Callable[[Optional[np.ndarray], Optional[np.ndarray]], bool],
                    mic_before: int = 0, sys_before: int = 0) -> bool:
        """
        One writer iteration of the two-source alignment policy:

        1. A source that was being padded (dead/starved) delivers again:
           what the other source had pending *before this iteration*
           predates the returning source's first new frame, so that stale
           backlog is written against silence first (frames ingested in
           the same iteration are contemporaneous and pair normally).
           Otherwise the returning source would be paired with old audio
           and stay up to 0.5 s early for the rest of the file (typical
           after a watchdog restart).
        2. Write the aligned overlap, batched to >= 10 ms blocks (fewer
           header rewrites, audio-time-sized auto-balance updates).
        3. One side empty, the other backed up:
           - the empty side delivered nothing for STARVATION_SECONDS and
             the backlog reached 0.5 s -> it is dead: pad it (as before);
           - the empty side is alive but the backlog stays above 100 ms
             for DRIFT_CONFIRM_SECONDS -> the two devices' clocks drift:
             from then on insert 10 ms of silence into the slower side
             (at most once per second) whenever the backlog exceeds a
             20 ms floor, so alignment stays within a few tens of ms
             instead of swinging by 0.5 s (with a dropout) every hour or
             two.
        """
        m_av = self._pending_frames(mic_pending)
        s_av = self._pending_frames(sys_pending)
        wrote = False

        if st.starved['sys'] and s_av > 0:
            st.starved['sys'] = False
            log.info("System audio is back — realigning.")
            stale = min(m_av, mic_before)
            if stale > 0:
                wrote = write_mixed(self._take_frames(mic_pending, stale),
                                    np.zeros((stale, CHANNELS), dtype=np.float32))
                m_av -= stale
        if st.starved['mic'] and m_av > 0:
            st.starved['mic'] = False
            log.info("Microphone is back — realigning.")
            stale = min(s_av, sys_before)
            if stale > 0:
                wrote = write_mixed(np.zeros(stale, dtype=np.float32),
                                    self._take_frames(sys_pending, stale))
                s_av -= stale

        # Batch small overlaps only while both sides are short: once one
        # side has pulled ahead, flush even a tiny overlap so the lagging
        # side can run empty and the starvation/drift handling below can
        # see it (a dying source leaving 1..479 residual frames must not
        # freeze the writer forever).
        n = min(m_av, s_av)
        if n > 0 and (n >= MIN_WRITE_FRAMES or abs(m_av - s_av) >= MIN_WRITE_FRAMES):
            wrote = write_mixed(self._take_frames(mic_pending, n),
                                self._take_frames(sys_pending, n))
            m_av -= n
            s_av -= n

        if m_av > 0 and s_av == 0:
            wrote = self._handle_lag(st, now, 'sys', m_av, mic_pending, sys_pending, write_mixed) or wrote
        elif s_av > 0 and m_av == 0:
            wrote = self._handle_lag(st, now, 'mic', s_av, sys_pending, mic_pending, write_mixed) or wrote
        else:
            st.backlog_since['mic'] = None
            st.backlog_since['sys'] = None
        return wrote

    def _handle_lag(self, st: _AlignState, now: float, lagging: str, backlog: int,
                    lead_pending: List[np.ndarray], lag_pending: List[np.ndarray],
                    write_mixed) -> bool:
        """``lagging`` has nothing pending while the other source has
        ``backlog`` frames waiting. Decide between starvation padding and
        gradual drift correction (see _align_step)."""
        other = 'mic' if lagging == 'sys' else 'sys'
        st.backlog_since[other] = None
        lag_is_mic = lagging == 'mic'

        def silence(n: int) -> np.ndarray:
            return (np.zeros(n, dtype=np.float32) if lag_is_mic
                    else np.zeros((n, CHANNELS), dtype=np.float32))

        def pair(lead_block: np.ndarray, lag_block: np.ndarray) -> bool:
            return (write_mixed(lag_block, lead_block) if lag_is_mic
                    else write_mixed(lead_block, lag_block))

        silent_for = now - st.last_frames[lagging]
        if backlog >= STARVATION_FRAMES and silent_for >= STARVATION_SECONDS:
            if not st.starved[lagging]:
                st.starved[lagging] = True
                log.warning("%s starved — padding with silence.",
                            "Microphone" if lag_is_mic else "System audio")
            st.backlog_since[lagging] = None
            return pair(self._take_frames(lead_pending, backlog), silence(backlog))

        if silent_for >= STARVATION_SECONDS:
            return False  # dead but not yet worth padding: wait

        # The lagging source is alive: a persistent backlog is clock drift.
        if backlog >= DRIFT_THRESHOLD_FRAMES:
            since = st.backlog_since[lagging]
            if since is None:
                st.backlog_since[lagging] = now
            elif not st.drifting[lagging] and now - since >= DRIFT_CONFIRM_SECONDS:
                st.drifting[lagging] = True
                log.info("Clock drift confirmed: %s lags by %d ms — correcting %d ms at a time.",
                         "microphone" if lag_is_mic else "system audio",
                         backlog * 1000 // SAMPLE_RATE, DRIFT_STEP_FRAMES * 1000 // SAMPLE_RATE)
        elif backlog < DRIFT_THRESHOLD_FRAMES // 2:
            st.backlog_since[lagging] = None

        if (st.drifting[lagging] and backlog > DRIFT_FLOOR_FRAMES
                and now - st.last_step[lagging] >= DRIFT_STEP_INTERVAL):
            st.last_step[lagging] = now
            st.drift_steps[lagging] += 1
            steps = st.drift_steps[lagging]
            if steps in (1, 10, 100) or steps % 1000 == 0:
                log.info("Clock drift: %s %d ms behind — %d corrections so far.",
                         "microphone" if lag_is_mic else "system audio",
                         backlog * 1000 // SAMPLE_RATE, steps)
            lag_pending.append(silence(DRIFT_STEP_FRAMES))
            return pair(self._take_frames(lead_pending, DRIFT_STEP_FRAMES),
                        self._take_frames(lag_pending, DRIFT_STEP_FRAMES))
        return False

    def _writer_fatal(self, stop_event: threading.Event, message: str,
                      exc: Optional[BaseException] = None) -> None:
        """
        Writer hit an unrecoverable error (disk full, I/O error, split
        failure). Finalize what we have, transition to IDLE ourselves so
        the UI/API can reconcile, and stop companion threads. Unless the
        failure is disk-related, the configured post-processing (MP3,
        normalization) still runs so the user gets the format they chose.
        """
        log.error("%s", message)
        stop_event.set()           # releases the watchdog
        self._watchdog_wake.set()
        self._pause_event.set()
        try:
            self._finalize_segment()
        except Exception:
            pass
        if self._finalizing:
            # A user-initiated stop() is mid-flight and owns the teardown
            # and the STOPPING→IDLE transition; flipping state from here
            # would let a new start() race the ffmpeg post-processing.
            return
        # Tear down capture unless pre-roll wants the streams alive.
        # recovery_lock orders this against any in-flight watchdog restart.
        try:
            with self._recovery_lock:
                if not self._preroll_active:
                    self._close_streams()
        except Exception:
            pass
        self._finish_unattended(message + " File salvato fino all'interruzione.",
                                post_process=not isinstance(exc, OSError))
        log.info("Recording auto-stopped; partial file saved: %s", self._output_path)

    def _finish_unattended(self, message: str, post_process: bool) -> None:
        """Shared tail of the non-user teardown paths (writer fatal,
        watchdog auto-stop): take ownership of the finalization atomically
        (a user stop() that got in first owns it — never two ffmpeg runs
        on the same file), freeze the timer, run post-processing while the
        state says STOPPING, then IDLE so the UI/API reconcile."""
        acquired = self._lock.acquire(timeout=2.0)
        try:
            if (self._finalizing
                    or self._state in (RecordingState.IDLE, RecordingState.STOPPING)):
                return  # somebody else (stop(), emergency save) owns the teardown
            self._finalizing = True
            self._elapsed_seconds = self.elapsed_time
            self._recording_start_time = None
            self._set_state(RecordingState.STOPPING)
        finally:
            if acquired:
                self._lock.release()
        try:
            if post_process:
                try:
                    self._post_process()
                except Exception:
                    log.exception("Post-processing after auto-stop failed")
        finally:
            acquired = self._lock.acquire(timeout=2.0)
            try:
                self._set_state(RecordingState.IDLE)
                self._finalizing = False
            finally:
                if acquired:
                    self._lock.release()
        self._report_error(message)

    # ---------- Watchdog ----------

    def _watchdog_loop(self, stop_event: threading.Event,
                       writer: threading.Thread) -> None:
        # Consecutive failed recoveries per source. A failed restart leaves
        # the stream object None, so the attempt counter (not the object)
        # is what keeps the retry loop alive until MAX_ATTEMPTS.
        mic_attempts = 0
        sck_attempts = 0
        wasapi_attempts = 0
        MAX_ATTEMPTS = 3

        while not stop_event.is_set():
            # Regular 2 s tick, or immediately when a stream reports it
            # finished / a stop path fires.
            self._watchdog_wake.wait(timeout=2.0)
            self._watchdog_wake.clear()
            if stop_event.is_set():
                break
            now = time.monotonic()

            # 1. Writer thread health. A writer that died without going
            #    through _writer_fatal (should not happen) is handled like
            #    any other unrecoverable condition: proper session stop.
            if not writer.is_alive():
                if self._state in (RecordingState.RECORDING, RecordingState.PAUSED):
                    self._auto_stop_session(
                        stop_event, "Recording thread died unexpectedly! Saving file.")
                break

            # 2. Mic stream health (sounddevice). active=False after device
            #    disappears (e.g. user unplugged headphones).
            # CoreAudio may keep 'active' True after a device vanished
            # while the callback simply stops firing: a silent callback
            # for MIC_SILENCE_TIMEOUT while recording counts as dead too.
            mic_dead = (
                (self._mic_stream is not None
                 and not getattr(self._mic_stream, "active", True))
                or (self._mic_stream is None and mic_attempts > 0)
                or (self._mic_stream is not None
                    and self._state == RecordingState.RECORDING
                    and now - self._last_mic_callback > MIC_SILENCE_TIMEOUT)
            )
            if mic_dead:
                if mic_attempts >= MAX_ATTEMPTS:
                    self._auto_stop_session(
                        stop_event,
                        "Microfono scollegato e non recuperabile. "
                        "Registrazione interrotta.")
                    break
                mic_attempts += 1
                log.warning("Mic stream inactive — recovery attempt %d/%d",
                            mic_attempts, MAX_ATTEMPTS)
                with self._recovery_lock:
                    if stop_event.is_set():
                        break
                    recovered = self._restart_mic_stream()
                if recovered:
                    mic_attempts = 0
                else:
                    stop_event.wait(timeout=min(2.0 * mic_attempts, 6.0))
            else:
                mic_attempts = 0

            # 3. SCK helper process health (macOS system audio)
            sck = self._sck_source
            sck_dead = (
                (sck is not None and not sck.is_running())
                or (sck is None and sck_attempts > 0)
            )
            if sck_dead:
                if sck_attempts >= MAX_ATTEMPTS:
                    self._report_error(
                        "Audio di sistema perso (helper non recuperabile). "
                        "La registrazione continua solo col microfono.")
                    self._has_system_audio = False
                    sck_attempts = 0  # give up: predicate stays False now
                    with self._recovery_lock:
                        if self._sck_source is not None:
                            try:
                                self._sck_source.stop()
                            except Exception:
                                pass
                            self._sck_source = None
                else:
                    sck_attempts += 1
                    log.warning("SCK helper inactive — restart attempt %d/%d",
                                sck_attempts, MAX_ATTEMPTS)
                    with self._recovery_lock:
                        if stop_event.is_set():
                            break
                        recovered = self._restart_sck_source()
                    if recovered:
                        sck_attempts = 0
                    else:
                        stop_event.wait(timeout=min(2.0 * sck_attempts, 6.0))
            elif sck is not None:
                sck_attempts = 0

            # 4. WASAPI stream health (Windows): PortAudio marks the stream
            #    inactive when the device goes away; reopen it fully.
            wasapi_dead = (
                (self._wasapi_stream is not None and not self._wasapi_stream_active())
                or (self._wasapi_stream is None and wasapi_attempts > 0)
            )
            if wasapi_dead:
                if wasapi_attempts >= MAX_ATTEMPTS:
                    self._report_error(
                        "Audio di sistema perso (WASAPI non recuperabile). "
                        "La registrazione continua solo col microfono.")
                    self._has_system_audio = False
                    wasapi_attempts = 0  # give up: predicate stays False now
                else:
                    wasapi_attempts += 1
                    log.warning("WASAPI reader died — reopen attempt %d/%d",
                                wasapi_attempts, MAX_ATTEMPTS)
                    with self._recovery_lock:
                        if stop_event.is_set():
                            break
                        if self._restart_wasapi():
                            wasapi_attempts = 0
                        else:
                            stop_event.wait(timeout=min(2.0 * wasapi_attempts, 6.0))

    def _auto_stop_session(self, stop_event: threading.Event, message: str) -> None:
        """
        Watchdog escalation: capture is unrecoverable. Stop the session the
        same way a user stop would: the writer drains and finalizes the
        file, streams are torn down, state reaches IDLE so the UI/API can
        reconcile (no zombie RECORDING state).
        """
        stop_event.set()
        self._watchdog_wake.set()
        self._pause_event.set()
        writer = self._writer_thread
        if writer is not None and writer.is_alive():
            writer.join(timeout=5.0)
        stuck = self._park_writer_if_stuck(writer)
        if self._finalizing:
            self._report_error(message)
            return  # a user stop() owns the rest of the teardown
        with self._recovery_lock:
            if not self._preroll_active:
                self._close_streams()
        if not stuck and self._output_file is not None:
            self._finalize_segment()  # writer died before closing the file
        self._finish_unattended(message, post_process=not stuck)
        log.info("Session auto-stopped: %s", self._output_path)

    def _restart_mic_stream(self) -> bool:
        """Close + reopen the microphone stream. Returns True on success."""
        try:
            try:
                if self._mic_stream is not None:
                    self._mic_stream.stop()
                    self._mic_stream.close()
            except Exception:
                pass
            self._mic_stream = None

            # PortAudio's device list is frozen at init: refresh it so a
            # newly plugged device can be found. That requires holding no
            # PortAudio stream, so a PortAudio system-audio stream (Linux
            # monitor, BlackHole) is closed for the refresh and reopened
            # right after (the writer pads the short gap). SCK/WASAPI do
            # not use PortAudio and are unaffected.
            sys_was_open = self._sys_stream is not None
            if sys_was_open:
                try:
                    self._sys_stream.stop()
                    self._sys_stream.close()
                except Exception:
                    pass
                self._sys_stream = None
            try:
                sd._terminate()
                sd._initialize()
            except Exception:
                pass

            # Re-detect: the user may have plugged in a different device.
            idx, ch, sr = detect_mic_device()
            if sys_was_open:
                # Indices may have changed with the refresh. A transient
                # detection failure (PulseAudio restarting — often the same
                # event that killed the mic) must keep the previous identity:
                # opening the Linux 'pulse' device without its monitor source
                # would capture the microphone as "system audio".
                previous = (self._sys_device, self._sys_channels, self._sys_samplerate,
                            self._sys_monitor_source)
                try:
                    if not self._detect_system_audio():
                        (self._sys_device, self._sys_channels, self._sys_samplerate,
                         self._sys_monitor_source) = previous
                        self._has_system_audio = True
                        log.warning("System audio re-detection failed — reopening the previous device.")
                    self._open_portaudio_sys_stream()
                except Exception as e:
                    log.warning("System audio reopen after mic recovery failed: %s", e)
                    self._sys_stream = None
            if idx is None:
                return False
            self._mic_device = idx
            self._mic_channels = min(ch, 1)
            self._mic_samplerate = sr

            self._mic_stream = sd.InputStream(
                samplerate=self._mic_samplerate,
                blocksize=BLOCKSIZE,
                device=self._mic_device,
                channels=1,
                dtype=DTYPE,
                latency='high',
                callback=self._mic_callback,
                finished_callback=self._on_stream_finished,
            )
            self._mic_stream.start()
            self._last_mic_callback = time.monotonic()
            log.info("Microphone stream recovered.")
            return True
        except Exception as e:
            log.warning("Mic recovery failed: %s", e)
            return False

    def _restart_sck_source(self) -> bool:
        try:
            try:
                if self._sck_source is not None:
                    self._sck_source.stop()
            except Exception:
                pass
            self._sck_source = None
            self._open_sck_source()
            log.info("SCK system audio recovered.")
            return True
        except Exception as e:
            log.warning("SCK recovery failed: %s", e)
            return False

    def _restart_wasapi(self) -> bool:
        """Fully reopen the WASAPI loopback (stream + instance + reader)."""
        try:
            if self._wasapi_stream is not None:
                try:
                    self._wasapi_stream.stop_stream()
                    self._wasapi_stream.close()
                except Exception:
                    pass
                self._wasapi_stream = None
            if self._pyaudio_instance is not None:
                try:
                    self._pyaudio_instance.terminate()
                except Exception:
                    pass
                self._pyaudio_instance = None

            self._refresh_wasapi_device()
            if not isinstance(self._sys_device, dict):
                return False
            self._open_wasapi_loopback()
            log.info("WASAPI loopback recovered.")
            return True
        except Exception as e:
            log.warning("WASAPI recovery failed: %s", e)
            return False

    # ---------- Crash Recovery ----------

    def _signal_handler(self, signum, frame) -> None:
        callback = self._signal_callback
        if callback is not None:
            try:
                callback(signum)
                return
            except Exception:
                log.exception("Signal callback failed — falling back to emergency save")
        self._emergency_save()
        sys.exit(0)

    def emergency_save(self) -> None:
        """Last-resort finalization of the current file (no post-processing).
        Idempotent; safe from any thread and from signal handlers."""
        self._emergency_save()

    def _emergency_save(self) -> None:
        if self._state == RecordingState.IDLE or self._finalizing:
            return
        self._finalizing = True
        try:
            self._stop_event.set()
            self._watchdog_wake.set()
            self._pause_event.set()
            # The writer may still have to drain ~10 s of queued audio,
            # flush the resamplers and close the file (for FLAC the close
            # writes the STREAMINFO that makes the file readable at all):
            # we are exiting anyway, so a bounded but generous wait is
            # worth it.
            for thread in (self._writer_thread, self._zombie_writer):
                if thread is not None and thread.is_alive():
                    thread.join(timeout=8.0)
        except Exception as e:
            log.warning("Emergency save: writer join failed: %s", e)

        # The writer finalizes the file on its way out. Only close it here
        # if the writer is truly gone, to avoid closing mid-write.
        writer = self._writer_thread
        if writer is None or not writer.is_alive():
            try:
                self._finalize_segment()
            except Exception as e:
                log.warning("Emergency save: finalize failed: %s", e)
        else:
            self._park_writer_if_stuck(writer)
            log.error("Emergency save: writer still busy — %s may be truncated.",
                      self._output_path)

        # Serialize against in-flight watchdog restarts (bounded: a couple
        # of seconds at most), with a timeout so a signal handler can never
        # hang here.
        got_recovery = self._recovery_lock.acquire(timeout=5.0)
        try:
            self._close_streams()
        except Exception as e:
            log.warning("Emergency save: close_streams failed: %s", e)
        finally:
            if got_recovery:
                self._recovery_lock.release()

        # Non-blocking state transition: the signal handler runs on the main
        # thread, which may already hold self._lock (it is not reentrant).
        acquired = self._lock.acquire(timeout=1.0)
        try:
            self._set_state(RecordingState.IDLE)
        finally:
            if acquired:
                self._lock.release()
        self._finalizing = False
        log.info("Emergency save complete: %s", self._output_path)

    # ---------- Audio Mixing ----------

    def _mix_frames(self, mic_data: Optional[np.ndarray], sys_data: Optional[np.ndarray]) -> np.ndarray:
        # Inputs are ALWAYS already at SAMPLE_RATE: the writer's ingest stage
        # resamples every chunk with the per-rate streaming resamplers.
        # Resampling here again (by the live device rate) would stretch the
        # audio a second time for any non-48 kHz device.
        if self._auto_balance:
            mic_data, sys_data = self._apply_auto_balance(mic_data, sys_data)

        if self._mix_mode:
            return self._frames_mixed(mic_data, sys_data)
        return self._frames_dual_track(mic_data, sys_data)

    def _apply_auto_balance(
        self,
        mic_data: Optional[np.ndarray],
        sys_data: Optional[np.ndarray],
    ) -> tuple[Optional[np.ndarray], Optional[np.ndarray]]:
        """
        Update per-source target gains based on each source's RMS, then
        apply them. The gain moves only a small fraction toward the target
        per block (slow attack, no pumping) and is ramped linearly across
        the block so consecutive blocks never have audible gain steps.
        Gain updates are skipped while a source is essentially silent
        (avoids amplifying background noise during silences).
        """

        def _gain_for(data: np.ndarray, running_gain: float) -> tuple[np.ndarray, float]:
            rms = float(np.sqrt(np.mean(data * data)))
            if rms < self._ab_min_rms:
                # Source is essentially silent — keep last gain, don't update.
                return (data * running_gain).astype(np.float32, copy=False), running_gain
            desired = min(self._ab_max_gain, self._ab_target_rms / rms)
            # Time-based smoothing: the same attack whatever the block
            # size the writer happened to align (1 frame or 1 second).
            alpha = 1.0 - math.exp(-data.shape[0] / (SAMPLE_RATE * self._ab_time_constant))
            new_gain = running_gain + alpha * (desired - running_gain)
            ramp = np.linspace(running_gain, new_gain, data.shape[0], dtype=np.float32)
            if data.ndim == 2:
                ramp = ramp[:, None]
            return (data * ramp).astype(np.float32, copy=False), new_gain

        if mic_data is not None:
            mic_data, self._mic_running_gain = _gain_for(mic_data, self._mic_running_gain)
        if sys_data is not None:
            sys_data, self._sys_running_gain = _gain_for(sys_data, self._sys_running_gain)
        return mic_data, sys_data

    def _soft_limit(self, data: np.ndarray) -> np.ndarray:
        """Gentle limiter for the auto-balanced paths: samples above the
        knee are compressed smoothly towards 1.0 (tanh) instead of being
        hard-clipped into flat tops when two loud voices overlap."""
        knee = self._limiter_knee
        span = 1.0 - knee
        mag = np.abs(data)
        over = mag > knee
        if not np.any(over):
            return data.astype(np.float32, copy=False)
        out = data.astype(np.float32, copy=True)
        excess = (mag[over] - knee) / span
        out[over] = np.sign(data[over]) * (knee + span * np.tanh(excess))
        return out

    def _frames_mixed(self, mic_data: Optional[np.ndarray], sys_data: Optional[np.ndarray]) -> np.ndarray:
        """Default: combined mix into both stereo channels.

        With auto-balance each source already sits near the target level
        and only one side talks at a time on a call, so the sources are
        summed at unity through a soft limiter: a mic-only recording, a
        mixed one, and the passages where the other side died all keep
        the same loudness. Without auto-balance the raw 50/50 sum is kept.
        """
        if mic_data is not None and sys_data is not None:
            mic_s = self._to_stereo(mic_data)
            sys_s = self._to_stereo(sys_data)
            mic_s, sys_s = self._pad_to_equal(mic_s, sys_s)
            if self._auto_balance:
                return self._soft_limit(mic_s + sys_s)
            return np.clip(0.5 * mic_s + 0.5 * sys_s, -1.0, 1.0).astype(np.float32)
        if mic_data is not None:
            single = self._to_stereo(mic_data).astype(np.float32)
        elif sys_data is not None:
            single = self._to_stereo(sys_data).astype(np.float32)
        else:
            return np.zeros((BLOCKSIZE, CHANNELS), dtype=np.float32)
        return self._soft_limit(single) if self._auto_balance else np.clip(single, -1.0, 1.0)

    def _frames_dual_track(self, mic_data: Optional[np.ndarray], sys_data: Optional[np.ndarray]) -> np.ndarray:
        """Opt-in (--dual-track): stereo with L = mic, R = system audio
        (downmixed to mono)."""
        if mic_data is not None and sys_data is not None:
            mic_m = self._to_mono(mic_data)
            sys_m = self._to_mono(sys_data)
            n = max(len(mic_m), len(sys_m))
            if len(mic_m) < n:
                mic_m = np.pad(mic_m, (0, n - len(mic_m)))
            if len(sys_m) < n:
                sys_m = np.pad(sys_m, (0, n - len(sys_m)))
            out = np.empty((n, 2), dtype=np.float32)
            out[:, 0] = mic_m
            out[:, 1] = sys_m
            return self._soft_limit(out) if self._auto_balance else np.clip(out, -1.0, 1.0)
        if mic_data is not None:
            # No system audio captured — duplicate mic onto both channels.
            return self._to_stereo(mic_data).astype(np.float32)
        if sys_data is not None:
            # No mic — preserve true system stereo.
            return self._to_stereo(sys_data).astype(np.float32)
        return np.zeros((BLOCKSIZE, CHANNELS), dtype=np.float32)

    @staticmethod
    def _pad_to_equal(a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        n = max(a.shape[0], b.shape[0])
        if a.shape[0] < n:
            a = np.pad(a, ((0, n - a.shape[0]), (0, 0)))
        if b.shape[0] < n:
            b = np.pad(b, ((0, n - b.shape[0]), (0, 0)))
        return a, b

    def _to_stereo(self, data: np.ndarray) -> np.ndarray:
        if data.ndim == 1:
            return np.column_stack([data, data])
        if data.shape[1] == 1:
            return np.column_stack([data[:, 0], data[:, 0]])
        return data[:, :2]

    @staticmethod
    def _to_mono(data: np.ndarray) -> np.ndarray:
        if data.ndim == 1:
            return data
        if data.shape[1] == 1:
            return data[:, 0]
        # Equal-power-ish downmix: simple average is fine for voice.
        return data.mean(axis=1)

    def _resample(self, data: np.ndarray, from_rate: float, to_rate: float) -> np.ndarray:
        """One-shot resample (whole-array). The writer path uses
        _StreamingResampler instead; this remains for offline helpers."""
        if from_rate == to_rate:
            return data
        if _HAVE_SOXR:
            # soxr accepts 1-D or (N, ch) arrays; preserve dtype.
            try:
                out = soxr.resample(data, from_rate, to_rate, quality="HQ")
                return out.astype(np.float32, copy=False)
            except Exception as e:
                log.debug("soxr.resample failed, falling back: %s", e)

        # Fallback: linear interpolation (cheap, low quality).
        ratio = to_rate / from_rate
        n_out = int(data.shape[0] * ratio)
        if n_out == 0:
            return data
        if data.ndim == 1:
            indices = np.linspace(0, len(data) - 1, n_out)
            return np.interp(indices, np.arange(len(data)), data).astype(np.float32)
        result = np.zeros((n_out, data.shape[1]), dtype=np.float32)
        indices = np.linspace(0, data.shape[0] - 1, n_out)
        for ch in range(data.shape[1]):
            result[:, ch] = np.interp(indices, np.arange(data.shape[0]), data[:, ch])
        return result

    # ---------- Post-processing (normalize / MP3) ----------

    def _loudness_normalize_segments(self, target_lufs: float) -> None:
        """
        Run ffmpeg's `loudnorm` filter on each segment in-place. ffmpeg is
        the only realistic way to do EBU R128 / ITU BS.1770 loudness
        normalization in Python without large dependencies. If ffmpeg is
        unavailable, we fall back to a naive peak normalize.
        """
        ffmpeg = _find_ffmpeg()
        if not ffmpeg:
            log.info("ffmpeg not found — falling back to peak normalization.")
            for path in self._segment_paths:
                self._peak_normalize_in_place(path, target_peak_dbfs=-1.0)
            return

        for path in self._segment_paths:
            # The temp file must keep the audio extension: ffmpeg infers the
            # output muxer from it (a bare ".tmp" suffix has no muxer and
            # makes every loudnorm run fail).
            tmp_out = path.with_name(path.stem + ".norm" + path.suffix)
            timeout = max(600.0, _audio_seconds(path))
            try:
                # Pass 1: measure. A silent file (-inf LUFS) has nothing to
                # normalize; a failed measurement degrades to single-pass.
                measured = _measure_loudness(ffmpeg, path, target_lufs, timeout)
                if measured is not None and not all(
                        math.isfinite(v) for v in measured.values()):
                    log.info("Loudness normalize skipped for %s: silent file.", path)
                    continue
                if measured is None:
                    log.warning("loudnorm measurement failed for %s — single-pass mode.", path)
                # Pass 2: apply (linear gain + true-peak limiter).
                result = _run_ffmpeg(
                    ffmpeg,
                    ['-i', str(path), '-af', _loudnorm_filter(target_lufs, measured),
                     '-ar', str(SAMPLE_RATE), *_ffmpeg_codec_args(path), str(tmp_out)],
                    timeout=timeout,
                )
                if result.returncode == 0 and tmp_out.exists() and tmp_out.stat().st_size > 0:
                    tmp_out.replace(path)
                    log.info("Loudness-normalised: %s (target %.1f LUFS, %s)", path, target_lufs,
                             "two-pass linear" if measured is not None else "single-pass")
                else:
                    log.warning(
                        "loudnorm failed for %s — keeping original. ffmpeg stderr: %s",
                        path, result.stderr.decode('utf-8', errors='replace')[-400:],
                    )
                    tmp_out.unlink(missing_ok=True)
            except Exception as e:
                log.warning("Loudness normalize crashed for %s: %s", path, e)
                tmp_out.unlink(missing_ok=True)

    def _peak_normalize_in_place(self, path: Path, target_peak_dbfs: float = -1.0) -> None:
        """Fallback: scale the whole file so the loudest sample sits at the
        target peak. Block-wise two-pass — never loads hours of audio in RAM."""
        try:
            peak = 0.0
            with sf.SoundFile(str(path)) as fin:
                for block in fin.blocks(blocksize=SAMPLE_RATE * 10, always_2d=True):
                    if block.size:
                        peak = max(peak, float(np.max(np.abs(block))))
            if peak < 1e-6:
                return
            target_linear = 10 ** (target_peak_dbfs / 20.0)
            gain = target_linear / peak
            tmp_out = path.with_name(path.stem + ".norm" + path.suffix)
            with sf.SoundFile(str(path)) as fin:
                with sf.SoundFile(str(tmp_out), mode='w',
                                  samplerate=fin.samplerate, channels=fin.channels,
                                  format=fin.format, subtype=OUTPUT_SUBTYPE) as fout:
                    # Keep the tags written at open time (FLAC: title,
                    # software, date, channel-layout comment).
                    try:
                        for key, value in fin.copy_metadata().items():
                            if value:
                                setattr(fout, key, value)
                    except Exception as e:
                        log.debug("Metadata copy failed: %s", e)
                    for block in fin.blocks(blocksize=SAMPLE_RATE * 10, always_2d=True):
                        fout.write(np.clip(block * gain, -1.0, 1.0))
            tmp_out.replace(path)
            log.info("Peak-normalised: %s (gain %.2f)", path, gain)
        except Exception as e:
            log.warning("Peak normalize failed for %s: %s", path, e)

    def _convert_to_mp3(self) -> None:
        candidates = _ffmpeg_candidates()
        if not candidates:
            self._report_error("ffmpeg not found. File saved as WAV.")
            return

        # ID3 tags embedded at conversion time.
        meta_args = [
            '-metadata', 'title=Orizon Call recording',
            '-metadata', 'artist=Orizon Call',
            '-metadata', f'date={datetime.now().isoformat(timespec="seconds")}',
            '-metadata', f'comment=dual_track={"no" if self._mix_mode else "yes"} '
                         f'channels={"mixed" if self._mix_mode else "L:mic,R:sys"}',
        ]

        new_paths = []
        sidecars: List[Tuple[dict, Path]] = []
        for wav_path in self._segment_paths:
            if wav_path.suffix.lower() == '.mp3':
                new_paths.append(wav_path)  # already converted
                continue
            mp3_path = wav_path.with_suffix('.mp3')
            try:
                # A system ffmpeg without libmp3lame fails the encode; the
                # bundled static build always has it — try each in turn.
                result = None
                for ffmpeg in candidates:
                    result = _run_ffmpeg(
                        ffmpeg,
                        ['-i', str(wav_path), '-q:a', '2',
                         # ID3v2.3: the version Windows Explorer / older players read.
                         '-id3v2_version', '3', *meta_args, str(mp3_path)],
                        timeout=max(300.0, _audio_seconds(wav_path)),
                    )
                    if result.returncode == 0:
                        break
                    log.warning("MP3 encode with %s failed (rc=%s): %s", ffmpeg, result.returncode,
                                result.stderr.decode('utf-8', errors='replace')[-200:])
                    mp3_path.unlink(missing_ok=True)
                # Delete the WAV only after a verified successful encode:
                # a partial .mp3 from a failed run must never replace the
                # original audio.
                if (result.returncode == 0 and mp3_path.exists()
                        and mp3_path.stat().st_size > 0):
                    payload = self._read_sidecar_json(wav_path)
                    wav_path.unlink(missing_ok=True)
                    self._sidecar_path(wav_path).unlink(missing_ok=True)
                    new_paths.append(mp3_path)
                    if payload is not None:
                        sidecars.append((payload, mp3_path))
                else:
                    log.warning(
                        "MP3 conversion failed for %s (rc=%s) — keeping WAV. stderr: %s",
                        wav_path, result.returncode,
                        result.stderr.decode('utf-8', errors='replace')[-400:],
                    )
                    mp3_path.unlink(missing_ok=True)
                    self._report_error("Conversione MP3 fallita — file salvato come WAV.")
                    new_paths.append(wav_path)
            except Exception as e:
                log.warning("MP3 conversion failed: %s", e)
                mp3_path.unlink(missing_ok=True)  # never leave a truncated .mp3 behind
                self._report_error("Conversione MP3 fallita — file salvato come WAV.")
                new_paths.append(wav_path)

        self._segment_paths = new_paths
        if new_paths:
            self._output_path = new_paths[0]
        # The metadata sidecar follows the audio: recording_x.mp3.json with
        # the final segment paths, so downstream tools (transcription) keep
        # the channel layout / duration info whatever the output format.
        for payload, mp3_path in sidecars:
            payload["format"] = "mp3"
            payload["segments"] = [str(p) for p in new_paths]
            try:
                self._sidecar_path(mp3_path).write_text(
                    json.dumps(payload, indent=2), encoding="utf-8")
            except OSError as e:
                log.debug("MP3 sidecar write failed: %s", e)

    # ---------- Utilities ----------

    def _drain_queue(self, q: queue.Queue) -> None:
        while True:
            try:
                q.get_nowait()
            except queue.Empty:
                break

    def _check_disk_space(self, raise_on_low: bool = False) -> bool:
        try:
            path = self._resolve_output_dir()
            usage = shutil.disk_usage(str(path))
            if usage.free < MIN_DISK_SPACE_BYTES:
                msg = f"Low disk space: {usage.free // (1024*1024)} MB remaining."
                if raise_on_low:
                    raise RuntimeError(msg)
                return False
        except RuntimeError:
            raise
        except Exception:
            pass
        return True

    def _resolve_output_dir(self) -> Path:
        """Folder recordings are written to. The configured folder is
        created on demand (previously a missing folder silently sent the
        file to $HOME); if it cannot be created or written (unmounted
        drive, permissions) fall back to ~/Downloads, then to $HOME."""
        candidates: List[Path] = []
        if self._output_dir is not None:
            candidates.append(Path(self._output_dir).expanduser())
        if sys.platform == 'win32':
            candidates.append(Path(os.environ.get('USERPROFILE', str(Path.home()))) / 'Downloads')
        else:
            candidates.append(Path.home() / 'Downloads')
        candidates.append(Path.home())

        chosen = Path.home()
        for i, folder in enumerate(candidates):
            try:
                folder.mkdir(parents=True, exist_ok=True)
                if folder.is_dir() and os.access(str(folder), os.W_OK):
                    chosen = folder
                    break
            except OSError as e:
                log.debug("Output folder %s unusable: %s", folder, e)
        if chosen != self._resolved_output_dir:
            if self._output_dir is not None and chosen != candidates[0]:
                log.warning("Output folder %s is not usable — saving to %s instead.",
                            candidates[0], chosen)
            self._resolved_output_dir = chosen
        return chosen

    def _generate_output_path(self) -> Path:
        downloads = self._resolve_output_dir()

        fmt = self._output_format
        ext = 'wav' if fmt == 'mp3' else fmt  # MP3 records as WAV first
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = downloads / f"recording_{timestamp}.{ext}"
        # Never truncate an existing recording (two starts in one second,
        # or a file left by a previous run).
        n = 2
        while path.exists():
            path = downloads / f"recording_{timestamp}_{n}.{ext}"
            n += 1
        return path
