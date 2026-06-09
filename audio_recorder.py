"""
Audio recording engine with dual-stream capture (mic + system audio),
real-time mixing, WAV/FLAC/MP3 output, file splitting, crash recovery.
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
from typing import Any, Callable, Deque, List, Optional

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


class AudioRecorder:
    """
    Manages dual-stream audio recording (microphone + system audio),
    real-time mixing, file output with crash recovery and watchdog.
    """

    def __init__(self) -> None:
        self._state = RecordingState.IDLE
        self._lock = threading.Lock()

        # Streams
        self._mic_stream: Optional[sd.InputStream] = None
        self._sys_stream: Optional[sd.InputStream] = None
        self._wasapi_stream: Any = None
        self._wasapi_thread: Optional[threading.Thread] = None
        self._pyaudio_instance: Any = None
        self._sck_source: Any = None

        # Bounded queues
        self._mic_queue: queue.Queue = queue.Queue(maxsize=QUEUE_MAXSIZE)
        self._sys_queue: queue.Queue = queue.Queue(maxsize=QUEUE_MAXSIZE)

        # Writer + watchdog
        self._output_file: Optional[sf.SoundFile] = None
        self._writer_thread: Optional[threading.Thread] = None
        self._watchdog_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()

        # Timing
        self._elapsed_seconds: float = 0.0
        self._recording_start_time: Optional[float] = None

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

        # Audio levels (read by UI, written by callbacks)
        self._mic_level: float = 0.0
        self._sys_level: float = 0.0

        # Error reporting
        self._error_callback: Optional[Callable[[str], None]] = None

        # Runtime toggles
        self._mic_muted: bool = False
        # True (default) = single combined stereo file where both channels
        # carry mic+system mixed together — natural playback.
        # False = dual-track (L=mic, R=system) for downstream speaker tagging.
        self._mix_mode: bool = True
        self._preroll_seconds: float = 0.0

        # Auto-balance: per-source slow-attack RMS gain matching applied
        # before summing. Default ON because raw 50/50 sounds bad when mic
        # and system loudness differ (the typical case for calls).
        self._auto_balance: bool = True
        self._ab_target_rms: float = 0.12       # target ~ -18 dBFS RMS
        self._ab_smoothing: float = 0.02        # 0=instant, 1=never (per block)
        self._ab_min_rms: float = 0.005         # ignore silence (no gain change)
        self._ab_max_gain: float = 6.0          # never amplify > +15.5 dB
        self._mic_running_gain: float = 1.0
        self._sys_running_gain: float = 1.0

        # Post-stop loudness normalization (LUFS). None = disabled.
        self._normalize_lufs: Optional[float] = None

        # Pre-roll
        self._preroll_active: bool = False  # streams kept open across recordings
        self._streams_open: bool = False
        self._mic_ring: Deque[np.ndarray] = collections.deque()
        self._sys_ring: Deque[np.ndarray] = collections.deque()
        self._ring_lock = threading.Lock()

        # Crash recovery — install handlers for the signals we can on this
        # platform. On Windows SIGTERM doesn't exist, but SIGBREAK (Ctrl+Break)
        # and SIGINT (Ctrl+C) do; on POSIX we cover SIGTERM and SIGHUP too.
        atexit.register(self._emergency_save)
        for sig_name in ("SIGTERM", "SIGINT", "SIGHUP", "SIGBREAK"):
            sig = getattr(signal, sig_name, None)
            if sig is None:
                continue
            try:
                # Only install if no custom handler is already in place.
                current = signal.getsignal(sig)
                if current in (signal.SIG_DFL, signal.SIG_IGN, None):
                    signal.signal(sig, self._signal_handler)
            except (ValueError, OSError):
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
            self._error_callback(message)

    # ---------- Device Detection ----------

    def detect_devices(self) -> tuple[bool, bool, str]:
        mic_idx, mic_ch, mic_sr = detect_mic_device()
        if mic_idx is not None:
            self._mic_device = mic_idx
            self._mic_channels = min(mic_ch, 1)
            self._mic_samplerate = mic_sr
        else:
            self._mic_device = None

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
        if self._state == RecordingState.PAUSED:
            return self._elapsed_seconds
        if self._state == RecordingState.RECORDING and self._recording_start_time is not None:
            return self._elapsed_seconds + (time.monotonic() - self._recording_start_time)
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
    def segment_paths(self) -> List[Path]:
        return list(self._segment_paths)

    # ---------- Public API ----------

    def start(self) -> Path:
        with self._lock:
            if self._state != RecordingState.IDLE:
                raise RuntimeError(f"Cannot start from state {self._state}")
            if self._mic_device is None:
                raise RuntimeError("No microphone available.")

            # Check disk space
            self._check_disk_space(raise_on_low=True)

            # Generate output path
            self._output_path = self._generate_output_path()
            self._segment_paths = [self._output_path]
            self._segment_index = 0
            self._samples_in_segment = 0

            # Open output file
            self._open_output_file(self._output_path)

            # Clear queues and events (preserve ring buffer — we'll flush it
            # into the queues below as the pre-roll prelude).
            self._drain_queue(self._mic_queue)
            self._drain_queue(self._sys_queue)
            self._stop_event.clear()
            self._pause_event.set()
            self._elapsed_seconds = 0.0
            self._mic_level = 0.0
            self._sys_level = 0.0

            # Open audio streams unless pre-roll mode already has them open.
            # On failure, close the output file and delete the empty placeholder.
            try:
                self._open_streams()
            except Exception:
                self._close_output_file()
                try:
                    if self._output_path is not None:
                        self._output_path.unlink(missing_ok=True)
                except Exception:
                    pass
                raise

            # Flush the pre-roll ring buffers into the writer queues so the
            # first samples of the recording are the captured pre-roll.
            if self._preroll_active and self._preroll_seconds > 0:
                self._drain_ring_to_queue(self._mic_ring, self._mic_queue)
                self._drain_ring_to_queue(self._sys_ring, self._sys_queue)

            # Start writer thread
            self._writer_thread = threading.Thread(
                target=self._writer_loop, daemon=True, name='audio-writer'
            )
            self._writer_thread.start()

            # Start watchdog thread
            self._watchdog_thread = threading.Thread(
                target=self._watchdog_loop, daemon=True, name='watchdog'
            )
            self._watchdog_thread.start()

            self._recording_start_time = time.monotonic()
            self._state = RecordingState.RECORDING

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
        with self._lock:
            if self._state == RecordingState.IDLE:
                return None
            if self._state == RecordingState.RECORDING and self._recording_start_time is not None:
                self._elapsed_seconds += time.monotonic() - self._recording_start_time
                self._recording_start_time = None
            self._pause_event.set()
            self._stop_event.set()

        if self._writer_thread is not None:
            self._writer_thread.join(timeout=5.0)
            if self._writer_thread.is_alive():
                self._report_error("Writer thread did not stop in time.")
            self._writer_thread = None

        if self._watchdog_thread is not None:
            self._watchdog_thread.join(timeout=2.0)
            self._watchdog_thread = None

        # Keep streams open if pre-roll mode is active — they must continue to
        # feed the ring buffer between recordings.
        if not self._preroll_active:
            self._close_streams()
        self._close_output_file()

        # Post-stop loudness normalization (before MP3 conversion so the
        # LUFS-normalised PCM is what gets encoded).
        if self._normalize_lufs is not None:
            self._loudness_normalize_segments(self._normalize_lufs)

        # Convert to MP3 if needed
        if self._output_format == 'mp3':
            self._convert_to_mp3()

        with self._lock:
            self._state = RecordingState.IDLE

        return self._output_path

    # ---------- Audio Callbacks ----------

    def _mic_callback(self, indata: np.ndarray, frames: int, time_info, status: sd.CallbackFlags) -> None:
        chunk = indata.copy()
        if self._mic_muted:
            chunk[:] = 0
        # RMS level for VU meter (always — even when muted, so the user sees "0")
        self._mic_level = 0.0 if self._mic_muted else float(np.sqrt(np.mean(indata * indata)))
        self._route_chunk(chunk, self._mic_queue, self._mic_ring)

    def _sys_callback(self, indata: np.ndarray, frames: int, time_info, status: sd.CallbackFlags) -> None:
        chunk = indata.copy()
        self._sys_level = float(np.sqrt(np.mean(chunk * chunk)))
        self._route_chunk(chunk, self._sys_queue, self._sys_ring)

    def _route_chunk(self, chunk: np.ndarray, q: queue.Queue, ring: "Deque[np.ndarray]") -> None:
        """Send to the writer queue when recording, to the pre-roll ring when idle."""
        if self._state == RecordingState.RECORDING:
            try:
                q.put_nowait(chunk)
            except queue.Full:
                pass
        elif self._preroll_active and self._preroll_seconds > 0:
            self._append_to_ring(chunk, ring)
        # PAUSED: drop — paused recordings should not capture audio.

    def _append_to_ring(self, chunk: np.ndarray, ring: "Deque[np.ndarray]") -> None:
        """Append to ring buffer trimming to preroll_seconds worth of audio."""
        # Determine target sample count per ring (rough — close enough for trim).
        target_samples = int(self._preroll_seconds * SAMPLE_RATE) + chunk.shape[0]
        with self._ring_lock:
            ring.append(chunk)
            total = sum(c.shape[0] for c in ring)
            while ring and total > target_samples:
                dropped = ring.popleft()
                total -= dropped.shape[0]

    def _drain_ring_to_queue(self, ring: "Deque[np.ndarray]", q: queue.Queue) -> None:
        with self._ring_lock:
            chunks = list(ring)
            ring.clear()
        for c in chunks:
            try:
                q.put_nowait(c)
            except queue.Full:
                break

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
            channels = self._sys_device['maxInputChannels']
            while True:
                data = self._wasapi_stream.read(BLOCKSIZE, exception_on_overflow=False)
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

        # FLAC supports embedded tags via soundfile (libsndfile). WAV does not
        # carry useful metadata for us, so we write a sidecar .json for it.
        if sf_format == 'FLAC':
            try:
                self._output_file.title = path.stem
                self._output_file.software = "Orizon Call"
                self._output_file.date = datetime.now().isoformat(timespec="seconds")
                self._output_file.comment = (
                    f"dual_track={'no' if self._mix_mode else 'yes'} "
                    f"channels=L:mic,R:sys"
                )
            except Exception as e:
                log.debug("FLAC metadata write failed: %s", e)

    def _close_output_file(self) -> None:
        if self._output_file is not None:
            try:
                self._output_file.flush()
                self._output_file.close()
            except Exception:
                pass
            self._output_file = None

        # WAV sidecar JSON with the same info we'd embed for other formats.
        if (self._output_format == 'wav'
                and self._output_path is not None
                and self._output_path.exists()):
            self._write_sidecar_json(self._output_path)

    def _write_sidecar_json(self, audio_path: Path) -> None:
        try:
            import json as _json
            sidecar = audio_path.with_suffix(audio_path.suffix + ".json")
            payload = {
                "software": "Orizon Call",
                "recorded_at": datetime.now().isoformat(timespec="seconds"),
                "sample_rate": SAMPLE_RATE,
                "channels": CHANNELS,
                "dual_track": not self._mix_mode,
                "channel_map": (["mic", "system"] if not self._mix_mode else ["mixed", "mixed"]),
                "duration_seconds": round(self._elapsed_seconds, 2),
                "segments": [str(p) for p in self._segment_paths],
            }
            sidecar.write_text(_json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as e:
            log.debug("Sidecar JSON write failed: %s", e)

    def _split_file(self) -> None:
        """Close current segment and open a new one for long recordings."""
        self._close_output_file()
        self._segment_index += 1
        base = self._output_path
        stem = base.stem
        suffix = base.suffix
        new_path = base.parent / f"{stem}_part{self._segment_index + 1}{suffix}"
        self._segment_paths.append(new_path)
        self._samples_in_segment = 0
        try:
            self._open_output_file(new_path)
        except Exception as e:
            self._report_error(f"Cannot open new segment: {e}")
            self._stop_event.set()

    # ---------- Writer Thread ----------

    def _writer_loop(self) -> None:
        last_disk_check = time.monotonic()
        try:
            while not self._stop_event.is_set():
                self._pause_event.wait(timeout=0.1)
                if self._stop_event.is_set():
                    break
                if not self._pause_event.is_set():
                    self._drain_queue(self._mic_queue)
                    self._drain_queue(self._sys_queue)
                    continue

                mic_block = self._collect_queue(self._mic_queue)
                sys_block = self._collect_queue(self._sys_queue)

                if mic_block is not None or sys_block is not None:
                    mixed = self._mix_frames(mic_block, sys_block)
                    n_samples = mixed.shape[0]

                    # Check if we need to split the file
                    if self._samples_in_segment + n_samples > MAX_SAMPLES_PER_SEGMENT:
                        self._split_file()

                    # Write with error handling
                    try:
                        if self._output_file is not None:
                            self._output_file.write(mixed)
                            self._samples_in_segment += n_samples
                    except OSError as e:
                        self._report_error(f"Write error (disk full?): {e}")
                        self._stop_event.set()
                        break
                else:
                    time.sleep(0.01)

                # Periodic disk space check
                now = time.monotonic()
                if now - last_disk_check > DISK_CHECK_SECONDS:
                    last_disk_check = now
                    if not self._check_disk_space(raise_on_low=False):
                        self._report_error("Disk space low! Stopping recording.")
                        self._stop_event.set()
                        break

            # Final drain
            mic_block = self._collect_queue(self._mic_queue)
            sys_block = self._collect_queue(self._sys_queue)
            if mic_block is not None or sys_block is not None:
                mixed = self._mix_frames(mic_block, sys_block)
                try:
                    if self._output_file is not None:
                        self._output_file.write(mixed)
                except OSError:
                    pass

        except Exception as e:
            self._report_error(f"Recording error: {e}")

    # ---------- Watchdog ----------

    def _watchdog_loop(self) -> None:
        # Track consecutive recovery attempts per source to implement backoff.
        mic_attempts = 0
        sys_attempts = 0
        MAX_ATTEMPTS = 3

        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=2.0)
            if self._stop_event.is_set():
                break

            # 1. Writer thread health
            if self._writer_thread and not self._writer_thread.is_alive():
                self._report_error("Recording thread died unexpectedly! Saving file.")
                self._emergency_save()
                break

            # 2. Mic stream health (sounddevice). active=False after device
            #    disappears (e.g. user unplugged headphones).
            mic_dead = (
                self._mic_stream is not None
                and not getattr(self._mic_stream, "active", True)
            )
            if mic_dead:
                if mic_attempts >= MAX_ATTEMPTS:
                    self._report_error(
                        "Microphone disconnected and could not be recovered. Stopping."
                    )
                    self._stop_event.set()
                    break
                mic_attempts += 1
                log.warning("Mic stream inactive — recovery attempt %d/%d",
                            mic_attempts, MAX_ATTEMPTS)
                if self._restart_mic_stream():
                    mic_attempts = 0
                else:
                    time.sleep(min(2.0 * mic_attempts, 6.0))
            else:
                mic_attempts = 0

            # 3. SCK helper process health (macOS system audio)
            sck = self._sck_source
            if sck is not None and not sck.is_running():
                if sys_attempts >= MAX_ATTEMPTS:
                    self._report_error(
                        "System audio helper crashed and could not be recovered."
                    )
                    self._has_system_audio = False
                    sys_attempts = MAX_ATTEMPTS + 1  # don't keep trying
                else:
                    sys_attempts += 1
                    log.warning("SCK helper inactive — restart attempt %d/%d",
                                sys_attempts, MAX_ATTEMPTS)
                    if self._restart_sck_source():
                        sys_attempts = 0
                    else:
                        time.sleep(min(2.0 * sys_attempts, 6.0))
            elif sck is not None:
                sys_attempts = 0

            # 4. WASAPI reader health (Windows)
            if (self._wasapi_thread is not None
                    and not self._wasapi_thread.is_alive()
                    and self._wasapi_stream is not None):
                log.warning("WASAPI reader thread died — attempting restart.")
                self._restart_wasapi_reader()

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

            # Re-detect: the user may have plugged in a different device.
            from platform_audio import detect_mic_device
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

    def _restart_wasapi_reader(self) -> None:
        try:
            self._wasapi_thread = threading.Thread(
                target=self._wasapi_reader_loop, daemon=True, name='wasapi-reader'
            )
            self._wasapi_thread.start()
        except Exception as e:
            log.warning("WASAPI reader restart failed: %s", e)

    # ---------- Crash Recovery ----------

    def _signal_handler(self, signum, frame) -> None:
        self._emergency_save()
        sys.exit(0)

    def _emergency_save(self) -> None:
        if self._state == RecordingState.IDLE:
            return
        try:
            self._stop_event.set()
            self._pause_event.set()
            if self._writer_thread and self._writer_thread.is_alive():
                self._writer_thread.join(timeout=2.0)
        except Exception as e:
            log.warning("Emergency save: writer join failed: %s", e)

        # Make sure the output file is finalized BEFORE we close streams —
        # the WAV/FLAC header is rewritten on close(), so this is what
        # makes the partial recording playable.
        try:
            self._close_output_file()
        except Exception as e:
            log.warning("Emergency save: close_output_file failed: %s", e)

        try:
            self._close_streams()
        except Exception as e:
            log.warning("Emergency save: close_streams failed: %s", e)

        with self._lock:
            self._state = RecordingState.IDLE
        log.info("Emergency save complete: %s", self._output_path)

    # ---------- Audio Mixing ----------

    def _mix_frames(self, mic_data: Optional[np.ndarray], sys_data: Optional[np.ndarray]) -> np.ndarray:
        if mic_data is not None and self._mic_samplerate != SAMPLE_RATE:
            mic_data = self._resample(mic_data, self._mic_samplerate, SAMPLE_RATE)
        if sys_data is not None and self._sys_samplerate != SAMPLE_RATE:
            sys_data = self._resample(sys_data, self._sys_samplerate, SAMPLE_RATE)

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
        apply them. Slow exponential smoothing avoids pumping; we skip
        gain updates when the source is too quiet to be meaningful
        (avoids amplifying background noise during silences).
        """

        def _gain_for(data: np.ndarray, running_gain: float) -> tuple[np.ndarray, float]:
            rms = float(np.sqrt(np.mean(data * data)))
            if rms < self._ab_min_rms:
                # Source is essentially silent — keep last gain, don't update.
                return (data * running_gain).astype(np.float32, copy=False), running_gain
            desired = min(self._ab_max_gain, self._ab_target_rms / rms)
            # Exponential smoothing toward desired gain.
            new_gain = (1.0 - self._ab_smoothing) * desired + self._ab_smoothing * running_gain
            return (data * new_gain).astype(np.float32, copy=False), new_gain

        if mic_data is not None:
            mic_data, self._mic_running_gain = _gain_for(mic_data, self._mic_running_gain)
        if sys_data is not None:
            sys_data, self._sys_running_gain = _gain_for(sys_data, self._sys_running_gain)
        return mic_data, sys_data

    def _frames_mixed(self, mic_data: Optional[np.ndarray], sys_data: Optional[np.ndarray]) -> np.ndarray:
        """Legacy 50/50 mix into both stereo channels."""
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
        """Default: stereo with L = mic, R = system audio (downmixed to mono)."""
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
        """Resample to `to_rate`. Uses soxr (high quality) when available, falling
        back to linear interpolation (legacy path) otherwise."""
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

    # ---------- MP3 Conversion ----------

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
            tmp_out = path.with_suffix(path.suffix + ".norm.tmp")
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
                if result.returncode == 0 and tmp_out.exists():
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
        target peak. No ffmpeg required."""
        try:
            data, sr = sf.read(str(path), always_2d=True)
            peak = float(np.max(np.abs(data)))
            if peak < 1e-6:
                return
            target_linear = 10 ** (target_peak_dbfs / 20.0)
            gain = target_linear / peak
            data = np.clip(data * gain, -1.0, 1.0).astype(np.float32)
            sf.write(str(path), data, sr, subtype=OUTPUT_SUBTYPE)
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
            '-metadata', f'title=Orizon Call recording',
            '-metadata', f'artist=Orizon Call',
            '-metadata', f'date={datetime.now().isoformat(timespec="seconds")}',
            '-metadata', f'comment=dual_track={"no" if self._mix_mode else "yes"} '
                          f'channels=L:mic,R:sys',
        ]

        new_paths = []
        for wav_path in self._segment_paths:
            mp3_path = wav_path.with_suffix('.mp3')
            try:
                subprocess.run(
                    [ffmpeg, '-y', '-i', str(wav_path), '-q:a', '2',
                     *meta_args, str(mp3_path)],
                    capture_output=True, timeout=300,
                )
                if mp3_path.exists():
                    wav_path.unlink(missing_ok=True)
                    # Sidecar JSON written for WAV no longer applies — remove it.
                    sidecar = wav_path.with_suffix(wav_path.suffix + ".json")
                    sidecar.unlink(missing_ok=True)
                    new_paths.append(mp3_path)
                else:
                    new_paths.append(wav_path)
            except Exception as e:
                log.warning("MP3 conversion failed: %s", e)
                new_paths.append(wav_path)

        self._segment_paths = new_paths
        if new_paths:
            self._output_path = new_paths[0]

    # ---------- Utilities ----------

    def _collect_queue(self, q: queue.Queue) -> Optional[np.ndarray]:
        chunks = []
        while True:
            try:
                chunks.append(q.get_nowait())
            except queue.Empty:
                break
        if chunks:
            return np.concatenate(chunks, axis=0)
        return None

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
        return downloads / f"recording_{timestamp}.{ext}"
