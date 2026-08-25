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
  two sources, and finalizes the file itself when it exits. Zero-padding
  is applied only when a source is genuinely starved (e.g. the system
  audio helper died) or at the very end of a recording.
- A watchdog thread restarts dead sources; every restart is guarded by
  a recovery lock that stop() also takes, so a stop can never race a
  restart and leave an orphaned live capture.
- stop() is synchronous (capture teardown + optional ffmpeg
  post-processing). UI code must call it from a worker thread; the state
  is STOPPING for the whole finalization so /status never lies.
"""

import atexit
import collections
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Deque, List, Optional, Tuple

import numpy as np
import sounddevice as sd
import soundfile as sf

from orizon_logging import get_logger
from platform_audio import detect_mic_device, detect_system_audio_device, get_system_audio_guidance

log = get_logger("recorder")

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

# libsndfile command: keep the RIFF header's data-size field updated on
# every write, so a hard kill (SIGKILL, power loss) leaves a playable file.
_SFC_SET_UPDATE_HEADER_AUTO = 0x1061


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
        if self._soxr is not None:
            try:
                return self._soxr.resample_chunk(data).astype(np.float32, copy=False)
            except Exception as e:
                log.debug("soxr resample_chunk failed, switching to linear: %s", e)
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
        self._init_state_and_locks()
        self._init_streams_and_queues()
        self._init_writer_and_timing()
        self._init_output_and_devices()
        self._init_audio_processing()
        self._init_crash_recovery()

    def _init_state_and_locks(self) -> None:
        self._state = RecordingState.IDLE
        self._lock = threading.Lock()
        # Serializes watchdog recovery against stop()/teardown.
        self._recovery_lock = threading.Lock()

    def _init_streams_and_queues(self) -> None:
        # Streams
        self._mic_stream: Optional[sd.InputStream] = None
        self._sys_stream: Optional[sd.InputStream] = None
        self._wasapi_stream: Any = None
        self._wasapi_thread: Optional[threading.Thread] = None
        self._pyaudio_instance: Any = None
        self._sck_source: Any = None

        # Per-session queues of (chunk, native_rate) tuples. Recreated on
        # every start() so a zombie writer from a stuck previous session
        # can never consume the new session's audio.
        self._mic_queue: queue.Queue = queue.Queue(maxsize=QUEUE_MAXSIZE)
        self._sys_queue: queue.Queue = queue.Queue(maxsize=QUEUE_MAXSIZE)

    def _init_writer_and_timing(self) -> None:
        # Writer + watchdog
        self._output_file: Optional[sf.SoundFile] = None
        self._writer_thread: Optional[threading.Thread] = None
        self._watchdog_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()  # replaced per session
        self._pause_event = threading.Event()
        self._pause_event.set()
        self._finalizing = False  # True while stop()/emergency save finalizes
        self._zombie_writer: Optional[threading.Thread] = None  # stuck writer

        # Timing
        self._elapsed_seconds: float = 0.0
        self._recording_start_time: Optional[float] = None

    def _init_output_and_devices(self) -> None:
        # Output
        self._output_path: Optional[Path] = None
        self._output_dir: Optional[Path] = None
        self._output_format: str = 'wav'  # wav, flac, mp3
        self._segment_paths: List[Path] = []
        self._segment_index: int = 0
        self._samples_in_segment: int = 0

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

    def _init_audio_processing(self) -> None:
        # Audio levels (read by UI, written by callbacks)
        self._mic_level: float = 0.0
        self._sys_level: float = 0.0

        # Dropped-chunk accounting (queue overflow)
        self._dropped_chunks: int = 0

        # Error reporting
        self._error_callback: Optional[Callable[[str], None]] = None

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
        self._ab_smoothing: float = 0.04        # fraction moved toward the
                                                # desired gain per block
        self._ab_min_rms: float = 0.005         # ignore silence (no gain change)
        self._ab_max_gain: float = 6.0          # never amplify > +15.5 dB
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

    def set_output_directory(self, path: Path) -> None:
        self._output_dir = path

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
        self._mic_muted = bool(muted)

    def set_system_audio_enabled(self, enabled: bool) -> None:
        """Disable to record microphone only (--no-system-audio)."""
        self._system_audio_enabled = bool(enabled)

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
        mic_idx, mic_ch, mic_sr = detect_mic_device()
        if mic_idx is not None:
            self._mic_device = mic_idx
            self._mic_channels = min(mic_ch, 1)
            self._mic_samplerate = mic_sr
        else:
            self._mic_device = None

        if not self._system_audio_enabled:
            self._sys_device = None
            self._has_system_audio = False
            return (self._mic_device is not None, False, "")

        sys_dev, sys_ch, sys_sr = detect_system_audio_device()
        if sys_dev is not None:
            self._sys_device = sys_dev
            self._sys_channels = min(sys_ch, 2)
            self._sys_samplerate = sys_sr
            self._has_system_audio = True
        else:
            self._sys_device = None
            self._has_system_audio = False

        guidance = ""
        if not self._has_system_audio:
            guidance = get_system_audio_guidance()

        return (self._mic_device is not None, self._has_system_audio, guidance)

    # ---------- Properties ----------

    @property
    def state(self) -> RecordingState:
        return self._state

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
    def segment_paths(self) -> List[Path]:
        return list(self._segment_paths)

    # ---------- Public API ----------

    def start(self) -> Path:
        with self._lock:
            if self._state != RecordingState.IDLE:
                raise RuntimeError(f"Cannot start from state {self._state}")
            if self._mic_device is None:
                raise RuntimeError("No microphone available.")
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

            # Open output file
            self._open_output_file(self._output_path)

            # Fresh per-session queues and stop event: a zombie writer from
            # a stuck previous session keeps its own references and can
            # neither consume our audio nor be revived by our state.
            self._mic_queue = queue.Queue(maxsize=QUEUE_MAXSIZE)
            self._sys_queue = queue.Queue(maxsize=QUEUE_MAXSIZE)
            self._stop_event = threading.Event()
            self._pause_event.set()
            self._elapsed_seconds = 0.0
            self._mic_level = 0.0
            self._sys_level = 0.0
            self._dropped_chunks = 0
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
                self._state = RecordingState.RECORDING

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
            self._pause_event.clear()
            self._state = RecordingState.PAUSED

    def resume(self) -> None:
        with self._lock:
            if self._state != RecordingState.PAUSED:
                raise RuntimeError(f"Cannot resume from state {self._state}")
            self._drain_queue(self._mic_queue)
            self._drain_queue(self._sys_queue)
            self._recording_start_time = time.monotonic()
            self._pause_event.set()
            self._state = RecordingState.RECORDING

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
            self._state = RecordingState.STOPPING
            self._finalizing = True
            stop_event = self._stop_event
            self._pause_event.set()
            stop_event.set()

        # The writer finalizes (flushes + closes) the output file itself
        # before exiting — it is the only thread that touches the file.
        writer = self._writer_thread
        writer_stuck = False
        if writer is not None:
            writer.join(timeout=10.0)
            if writer.is_alive():
                # Wedged (e.g. blocked on a hung volume). The file is still
                # open and owned by it: remember the zombie so start() can
                # refuse a new session that would share the file attributes.
                writer_stuck = True
                self._zombie_writer = writer
                self._report_error("Writer thread did not stop in time.")
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
            # Loudness normalization first, so the LUFS-normalised PCM is
            # what gets encoded to MP3.
            if self._normalize_lufs is not None:
                self._loudness_normalize_segments(self._normalize_lufs)
            if self._output_format == 'mp3':
                self._convert_to_mp3()

        with self._lock:
            self._state = RecordingState.IDLE
            self._finalizing = False

        return self._output_path

    # ---------- Audio Callbacks ----------

    def _mic_callback(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        chunk = indata.copy()
        if self._mic_muted:
            chunk[:] = 0
        # RMS level for VU meter (always — even when muted, so the user sees "0")
        self._mic_level = 0.0 if self._mic_muted else float(np.sqrt(np.mean(indata * indata)))
        self._route_chunk(chunk, self._mic_samplerate, self._mic_queue, self._mic_ring)

    def _sys_callback(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        chunk = indata.copy()
        self._sys_level = float(np.sqrt(np.mean(chunk * chunk)))
        self._route_chunk(chunk, self._sys_samplerate, self._sys_queue, self._sys_ring)

    def _route_chunk(self, chunk: np.ndarray, rate: float,
                     q: queue.Queue, ring: Deque) -> None:
        """Send to the writer queue when recording, to the pre-roll ring when idle."""
        state = self._state
        if state == RecordingState.RECORDING:
            try:
                q.put_nowait((chunk, rate))
            except queue.Full:
                self._dropped_chunks += 1
                if self._dropped_chunks == 1 or self._dropped_chunks % 100 == 0:
                    log.warning("Audio queue full — dropped %d chunks so far.",
                                self._dropped_chunks)
        elif (self._preroll_active and self._preroll_seconds > 0
                and state in (RecordingState.IDLE, RecordingState.STOPPING)):
            self._append_to_ring(chunk, rate, ring, q)
        # PAUSED: drop — paused recordings should not capture audio.

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
                except queue.Full:
                    self._dropped_chunks += 1
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
        if self._streams_open:
            return
        self._preroll_active = True
        try:
            self._open_streams()
        except Exception as e:
            log.warning("Pre-roll capture failed to start: %s", e)
            self._preroll_active = False
            self._close_streams()

    def disable_preroll_capture(self) -> None:
        if not self._preroll_active:
            return
        self._preroll_active = False
        # Only close streams if not currently recording (otherwise stop() will).
        if self._state == RecordingState.IDLE:
            self._close_streams()
            with self._ring_lock:
                self._mic_ring.clear()
                self._sys_ring.clear()

    # ---------- Stream Management ----------

    def _open_streams(self) -> None:
        if self._streams_open:
            return
        try:
            self._mic_stream = sd.InputStream(
                samplerate=self._mic_samplerate,
                blocksize=BLOCKSIZE,
                device=self._mic_device,
                channels=1,
                dtype=DTYPE,
                callback=self._mic_callback,
            )
            self._mic_stream.start()
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
                    self._sys_stream = sd.InputStream(
                        samplerate=self._sys_samplerate,
                        blocksize=BLOCKSIZE,
                        device=self._sys_device,
                        channels=self._sys_channels,
                        dtype=DTYPE,
                        callback=self._sys_callback,
                    )
                    self._sys_stream.start()
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
        import pyaudiowpatch as pyaudio
        dev_info = self._sys_device
        self._pyaudio_instance = pyaudio.PyAudio()
        channels = dev_info['maxInputChannels']
        rate = int(dev_info['defaultSampleRate'])
        self._sys_channels = min(channels, 2)
        self._sys_samplerate = rate
        self._wasapi_stream = self._pyaudio_instance.open(
            format=pyaudio.paFloat32,
            channels=channels,
            rate=rate,
            input=True,
            input_device_index=dev_info['index'],
            frames_per_buffer=BLOCKSIZE,
        )
        self._wasapi_thread = threading.Thread(
            target=self._wasapi_reader_loop, daemon=True, name='wasapi-reader'
        )
        self._wasapi_thread.start()

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

    def _wasapi_reader_loop(self) -> None:
        try:
            stream = self._wasapi_stream
            channels = self._sys_device['maxInputChannels']
            while stream is not None and stream is self._wasapi_stream:
                data = stream.read(BLOCKSIZE, exception_on_overflow=False)
                arr = np.frombuffer(data, dtype=np.float32).reshape(-1, channels)
                if channels > 2:
                    arr = arr[:, :2]
                # Route via the shared sys callback so pre-roll/recording
                # state is respected uniformly.
                self._sys_callback(arr, arr.shape[0], None, None)
        except Exception:
            # Stream was closed (or genuine error) — exit cleanly.
            pass

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
        if self._wasapi_thread is not None:
            self._wasapi_thread.join(timeout=2.0)
            self._wasapi_thread = None
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

        # WAV sidecar JSON with the same info we'd embed for other formats.
        if (self._output_format != 'flac'
                and segment_path is not None
                and segment_path.exists()):
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

    def _write_sidecar_json(self, audio_path: Path, frames: int) -> None:
        try:
            import json as _json
            payload = {
                "software": "Orizon Call",
                "recorded_at": datetime.now().isoformat(timespec="seconds"),
                "sample_rate": SAMPLE_RATE,
                "channels": CHANNELS,
                "dual_track": not self._mix_mode,
                "channel_map": (["mic", "system"] if not self._mix_mode else ["mixed", "mixed"]),
                "duration_seconds": round(frames / SAMPLE_RATE, 2),
                "segment_index": self._segment_index,
                "segments": [str(p) for p in self._segment_paths],
            }
            self._sidecar_path(audio_path).write_text(
                _json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as e:
            log.debug("Sidecar JSON write failed: %s", e)

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
                resamplers: dict, to_mono: bool) -> None:
        """Move queued (chunk, rate) tuples into the pending buffer:
        normalize shape, resample to 48 kHz with per-rate stateful
        resamplers (no boundary clicks, no drift)."""
        while True:
            try:
                chunk, rate = q.get_nowait()
            except queue.Empty:
                break
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
        starvation_logged = False
        was_paused = False

        def write_mixed(mic_b: Optional[np.ndarray], sys_b: Optional[np.ndarray]) -> bool:
            """Mix + write one aligned block. Returns False on fatal error."""
            mixed = self._mix_frames(mic_b, sys_b)
            n_samples = mixed.shape[0]
            if self._samples_in_segment + n_samples > MAX_SAMPLES_PER_SEGMENT:
                self._split_file()
            if self._output_file is not None:
                self._output_file.write(mixed)
                self._samples_in_segment += n_samples
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
                self._pause_event.wait(timeout=0.1)
                if stop_event.is_set():
                    break
                if not self._pause_event.is_set():
                    if not was_paused:
                        # Entering pause: discard in-flight audio so resume
                        # starts realigned from a clean slate.
                        mic_pending.clear()
                        sys_pending.clear()
                        was_paused = True
                    self._drain_queue(mic_q)
                    self._drain_queue(sys_q)
                    continue
                was_paused = False

                self._ingest(mic_q, mic_pending, resamplers, to_mono=True)
                if session_has_sys:
                    self._ingest(sys_q, sys_pending, resamplers, to_mono=False)

                wrote = False
                if session_has_sys:
                    n = min(self._pending_frames(mic_pending),
                            self._pending_frames(sys_pending))
                    if n > 0:
                        wrote = write_mixed(self._take_frames(mic_pending, n),
                                            self._take_frames(sys_pending, n))
                        starvation_logged = False
                    else:
                        # One source silent: pad it only after a real gap so
                        # transient scheduling jitter never injects zeros.
                        m_av = self._pending_frames(mic_pending)
                        s_av = self._pending_frames(sys_pending)
                        if m_av >= STARVATION_FRAMES and s_av == 0:
                            if not starvation_logged:
                                starvation_logged = True
                                log.warning("System audio starved — padding with silence.")
                            wrote = write_mixed(
                                self._take_frames(mic_pending, m_av),
                                np.zeros((m_av, CHANNELS), dtype=np.float32))
                        elif s_av >= STARVATION_FRAMES and m_av == 0:
                            if not starvation_logged:
                                starvation_logged = True
                                log.warning("Microphone starved — padding with silence.")
                            wrote = write_mixed(
                                np.zeros(s_av, dtype=np.float32),
                                self._take_frames(sys_pending, s_av))
                else:
                    m_av = self._pending_frames(mic_pending)
                    if m_av > 0:
                        wrote = write_mixed(self._take_frames(mic_pending, m_av), None)

                if not wrote:
                    time.sleep(0.01)

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
            self._writer_fatal(stop_event, f"Recording stopped: {e}")

    def _writer_fatal(self, stop_event: threading.Event, message: str) -> None:
        """
        Writer hit an unrecoverable error (disk full, I/O error, split
        failure). Finalize what we have, transition to IDLE ourselves so
        the UI/API can reconcile, and stop companion threads.
        """
        log.error("%s", message)
        stop_event.set()           # releases the watchdog
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
        acquired = self._lock.acquire(timeout=2.0)
        try:
            if self._state != RecordingState.IDLE:
                self._elapsed_seconds = self.elapsed_time
                self._recording_start_time = None
                self._state = RecordingState.IDLE
        finally:
            if acquired:
                self._lock.release()
        self._report_error(message + " File salvato fino all'interruzione.")
        log.info("Recording auto-stopped; partial file saved: %s", self._output_path)

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
            stop_event.wait(timeout=2.0)
            if stop_event.is_set():
                break

            # 1. Writer thread health
            if not writer.is_alive():
                if self._state in (RecordingState.RECORDING, RecordingState.PAUSED):
                    self._report_error("Recording thread died unexpectedly! Saving file.")
                    self._emergency_save()
                break

            # 2. Mic stream health (sounddevice). active=False after device
            #    disappears (e.g. user unplugged headphones).
            mic_dead = (
                (self._mic_stream is not None
                 and not getattr(self._mic_stream, "active", True))
                or (self._mic_stream is None and mic_attempts > 0)
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

            # 4. WASAPI reader health (Windows): the thread exits when the
            #    stream errors, so a dead reader means the stream must be
            #    fully reopened (a bare thread restart would just die again).
            wasapi_dead = (
                (self._wasapi_thread is not None
                 and not self._wasapi_thread.is_alive()
                 and self._wasapi_stream is not None)
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
        self._report_error(message)
        stop_event.set()
        self._pause_event.set()
        writer = self._writer_thread
        if writer is not None and writer.is_alive():
            writer.join(timeout=5.0)
        if self._finalizing:
            return  # a user stop() owns the rest of the teardown
        with self._recovery_lock:
            if not self._preroll_active:
                self._close_streams()
        acquired = self._lock.acquire(timeout=2.0)
        try:
            if self._state != RecordingState.IDLE:
                self._elapsed_seconds = self.elapsed_time
                self._recording_start_time = None
                self._state = RecordingState.IDLE
        finally:
            if acquired:
                self._lock.release()
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
            # newly plugged device can be found. Only safe when we hold no
            # other PortAudio stream (SCK/WASAPI don't use PortAudio).
            if self._sys_stream is None:
                try:
                    sd._terminate()
                    sd._initialize()
                except Exception:
                    pass

            # Re-detect: the user may have plugged in a different device.
            idx, ch, sr = detect_mic_device()
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
                callback=self._mic_callback,
            )
            self._mic_stream.start()
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
            self._wasapi_thread = None

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
        self._emergency_save()
        sys.exit(0)

    def _emergency_save(self) -> None:
        if self._state == RecordingState.IDLE or self._finalizing:
            return
        self._finalizing = True
        try:
            self._stop_event.set()
            self._pause_event.set()
            writer = self._writer_thread
            if writer is not None and writer.is_alive():
                writer.join(timeout=2.0)
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
            log.warning("Emergency save: writer still alive, file left to it.")

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
            self._state = RecordingState.IDLE
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
            new_gain = running_gain + self._ab_smoothing * (desired - running_gain)
            ramp = np.linspace(running_gain, new_gain, data.shape[0], dtype=np.float32)
            if data.ndim == 2:
                ramp = ramp[:, None]
            return (data * ramp).astype(np.float32, copy=False), new_gain

        if mic_data is not None:
            mic_data, self._mic_running_gain = _gain_for(mic_data, self._mic_running_gain)
        if sys_data is not None:
            sys_data, self._sys_running_gain = _gain_for(sys_data, self._sys_running_gain)
        return mic_data, sys_data

    def _frames_mixed(self, mic_data: Optional[np.ndarray], sys_data: Optional[np.ndarray]) -> np.ndarray:
        """Default: 50/50 combined mix into both stereo channels."""
        if mic_data is not None and sys_data is not None:
            mic_s = self._to_stereo(mic_data)
            sys_s = self._to_stereo(sys_data)
            mic_s, sys_s = self._pad_to_equal(mic_s, sys_s)
            return np.clip(0.5 * mic_s + 0.5 * sys_s, -1.0, 1.0).astype(np.float32)
        if mic_data is not None:
            return self._to_stereo(mic_data).astype(np.float32)
        if sys_data is not None:
            return self._to_stereo(sys_data).astype(np.float32)
        return np.zeros((BLOCKSIZE, CHANNELS), dtype=np.float32)

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
            return np.clip(out, -1.0, 1.0)
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
        ffmpeg = shutil.which('ffmpeg')
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
            try:
                filter_arg = (
                    f"loudnorm=I={target_lufs}:LRA=11:TP=-1.5:print_format=summary"
                )
                result = subprocess.run(
                    [ffmpeg, '-y', '-i', str(path), '-af', filter_arg,
                     '-ar', str(SAMPLE_RATE), '-c:a', 'pcm_s16le',
                     str(tmp_out)],
                    capture_output=True, timeout=600,
                )
                if result.returncode == 0 and tmp_out.exists() and tmp_out.stat().st_size > 0:
                    tmp_out.replace(path)
                    log.info("Loudness-normalised: %s (target %.1f LUFS)", path, target_lufs)
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
                    for block in fin.blocks(blocksize=SAMPLE_RATE * 10, always_2d=True):
                        fout.write(np.clip(block * gain, -1.0, 1.0))
            tmp_out.replace(path)
            log.info("Peak-normalised: %s (gain %.2f)", path, gain)
        except Exception as e:
            log.warning("Peak normalize failed for %s: %s", path, e)

    def _convert_to_mp3(self) -> None:
        ffmpeg = shutil.which('ffmpeg')
        if not ffmpeg:
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
        for wav_path in self._segment_paths:
            mp3_path = wav_path.with_suffix('.mp3')
            try:
                result = subprocess.run(
                    [ffmpeg, '-y', '-i', str(wav_path), '-q:a', '2',
                     *meta_args, str(mp3_path)],
                    capture_output=True, timeout=300,
                )
                # Delete the WAV only after a verified successful encode:
                # a partial .mp3 from a failed run must never replace the
                # original audio.
                if (result.returncode == 0 and mp3_path.exists()
                        and mp3_path.stat().st_size > 0):
                    wav_path.unlink(missing_ok=True)
                    self._sidecar_path(wav_path).unlink(missing_ok=True)
                    new_paths.append(mp3_path)
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
                self._report_error("Conversione MP3 fallita — file salvato come WAV.")
                new_paths.append(wav_path)

        self._segment_paths = new_paths
        if new_paths:
            self._output_path = new_paths[0]

    # ---------- Utilities ----------

    def _drain_queue(self, q: queue.Queue) -> None:
        while True:
            try:
                q.get_nowait()
            except queue.Empty:
                break

    def _check_disk_space(self, raise_on_low: bool = False) -> bool:
        try:
            path = self._output_dir or (Path.home() / 'Downloads')
            if not path.exists():
                path = Path.home()
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

    def _generate_output_path(self) -> Path:
        if self._output_dir is not None:
            downloads = self._output_dir
        elif sys.platform == 'win32':
            downloads = Path(os.environ.get('USERPROFILE', str(Path.home()))) / 'Downloads'
        else:
            downloads = Path.home() / 'Downloads'

        if not downloads.exists() or not downloads.is_dir():
            downloads = Path.home()

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
