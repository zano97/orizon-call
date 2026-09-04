"""
Round-2/3 regression tests:

* writer alignment: dropped chunks become silence, gradual clock-drift
  correction, resync after a starved source returns, pause edge
* long-recording split, emergency save on a live session, watchdog loop
* post-processing on auto-stop, zombie writer parking
* device (re)detection: refresh at start, system-audio toggle, WASAPI
  callback mode, Linux 'pulse' fallback + PULSE_SOURCE, Windows WASAPI mic
* macOS helper architecture check, soxr dtype, ffmpeg candidates,
  FLAC metadata through peak-normalize
* API: ranges, HEAD, Content-Disposition, JSON errors, origin normalization,
  redaction, SSE statechange
* single-instance probe
* widget: quit during start, unmute at stop, pending settings, non-interactive quit
"""

import json
import os
import sys
import threading
import time
import types
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

import audio_recorder
import platform_audio
from audio_recorder import (
    AudioRecorder,
    RecordingState,
    SAMPLE_RATE,
    STARVATION_FRAMES,
    _pulse_source_env,
    _StreamingResampler,
)
from tests import fake_ffmpeg


class _FakeStream:
    def __init__(self):
        self.active = True

    def start(self):
        pass

    def stop(self):
        self.active = False

    def close(self):
        pass


@pytest.fixture()
def recorder(tmp_path, monkeypatch):
    r = AudioRecorder()
    r._mic_device = 0
    r._mic_channels = 1
    r._mic_samplerate = SAMPLE_RATE
    r._sys_device = None
    r._has_system_audio = False
    r.set_output_directory(tmp_path)
    r.set_output_format("wav")
    r.set_auto_balance(False)

    def fake_open_streams():
        if r._streams_open:
            return
        r._mic_stream = _FakeStream()
        if r._has_system_audio:
            r._sys_stream = _FakeStream()
        r._streams_open = True

    def fake_close_streams():
        r._mic_stream = None
        r._sys_stream = None
        r._streams_open = False

    monkeypatch.setattr(r, "_open_streams", fake_open_streams)
    monkeypatch.setattr(r, "_close_streams", fake_close_streams)
    return r


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _mic(n=1024, value=0.5):
    return np.full((n, 1), value, dtype=np.float32)


def _sys(n=1024, value=0.25):
    return np.full((n, 2), value, dtype=np.float32)


def _wait_for(predicate, timeout=4.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _count_near(data, value, atol=0.01):
    return int(np.sum(np.abs(data[:, 0] - value) < atol))


# ====================================================================
# Writer alignment
# ====================================================================

class TestAlignmentRound2:

    def test_dropped_chunk_becomes_silence_not_a_shift(self, recorder):
        recorder._has_system_audio = True
        recorder._sys_device = "fake"
        path = recorder.start()
        for _ in range(3):
            recorder._mic_queue.put_nowait((_mic(), SAMPLE_RATE))
            recorder._sys_queue.put_nowait((_sys(), SAMPLE_RATE))
        assert _wait_for(lambda: recorder._samples_in_segment >= 3072)

        # The mic callback had to drop one chunk (queue full).
        recorder._note_dropped_chunk(np.zeros((1024, 1), dtype=np.float32), SAMPLE_RATE,
                                     recorder._mic_ring)
        time.sleep(0.25)  # writer turns it into pending silence
        recorder._sys_queue.put_nowait((_sys(), SAMPLE_RATE))
        assert _wait_for(lambda: recorder._samples_in_segment >= 4096)
        for _ in range(2):
            recorder._mic_queue.put_nowait((_mic(), SAMPLE_RATE))
            recorder._sys_queue.put_nowait((_sys(), SAMPLE_RATE))
        assert _wait_for(lambda: recorder._samples_in_segment >= 6144)
        recorder.stop()

        data, _ = sf.read(str(path), always_2d=True)
        assert data.shape[0] == 6 * 1024
        assert np.allclose(data[:3072], 0.375, atol=0.01)     # mixed
        assert np.allclose(data[3072:4096], 0.125, atol=0.01)  # silence + sys/2
        assert np.allclose(data[4096:], 0.375, atol=0.01)      # realigned
        assert recorder.dropped_chunks == 1

    def test_returning_source_pairs_with_fresh_audio(self, recorder):
        """After system audio starved and comes back, stale mic backlog is
        padded and the returning frames pair with contemporaneous mic
        frames (no permanent 0.5 s lead)."""
        recorder._has_system_audio = True
        recorder._sys_device = "fake"
        path = recorder.start()
        for _ in range(30):
            recorder._mic_queue.put_nowait((_mic(value=0.5), SAMPLE_RATE))
        assert _wait_for(lambda: recorder._samples_in_segment >= 30 * 1024)  # starvation pad
        for _ in range(5):                                # stale mic while sys still dead
            recorder._mic_queue.put_nowait((_mic(value=0.5), SAMPLE_RATE))
        time.sleep(0.25)
        for _ in range(10):                               # system audio is back
            recorder._sys_queue.put_nowait((_sys(value=0.25), SAMPLE_RATE))
            recorder._mic_queue.put_nowait((_mic(value=0.8), SAMPLE_RATE))
        assert _wait_for(lambda: recorder._samples_in_segment >= 45 * 1024)
        recorder.stop()

        data, _ = sf.read(str(path), always_2d=True)
        assert data.shape[0] == 45 * 1024
        assert _count_near(data, 0.25) == 35 * 1024       # old mic against silence
        assert _count_near(data, 0.525) == 10 * 1024      # 0.5*0.8 + 0.5*0.25: fresh pairs
        assert _count_near(data, 0.375) == 0              # never old mic + new sys

    def test_clock_drift_is_corrected_in_small_steps(self, recorder, monkeypatch):
        monkeypatch.setattr(audio_recorder, "DRIFT_CONFIRM_SECONDS", 0.15)
        monkeypatch.setattr(audio_recorder, "DRIFT_STEP_INTERVAL", 0.05)
        recorder._has_system_audio = True
        recorder._sys_device = "fake"
        path = recorder.start()
        # Mic runs ahead by 5 chunks (> 100 ms) while both keep delivering.
        for _ in range(10):
            recorder._mic_queue.put_nowait((_mic(), SAMPLE_RATE))
        for _ in range(5):
            recorder._sys_queue.put_nowait((_sys(), SAMPLE_RATE))
        for _ in range(15):
            time.sleep(0.1)
            recorder._mic_queue.put_nowait((_mic(), SAMPLE_RATE))
            recorder._sys_queue.put_nowait((_sys(), SAMPLE_RATE))
        time.sleep(0.2)
        recorder.stop()

        data, _ = sf.read(str(path), always_2d=True)
        silent = np.abs(data[:, 0] - 0.25) < 0.01   # mic against inserted silence
        runs, current = [], 0
        for flag in silent:
            if flag:
                current += 1
            elif current:
                runs.append(current)
                current = 0
        if current:
            runs.append(current)
        # Many short 10 ms corrections (adjacent ones merge into one run of
        # a few steps), never a 0.5 s dropout; the backlog left for the
        # final drain is down at the 20 ms floor.
        step = audio_recorder.DRIFT_STEP_FRAMES
        assert len(runs) >= 3, runs
        assert all(r % step == 0 for r in runs[:-1]), runs
        assert max(runs) < STARVATION_FRAMES // 4, runs
        assert runs[-1] <= audio_recorder.DRIFT_FLOOR_FRAMES + 1024, runs
        assert 5120 - 1024 <= int(np.sum(silent)) <= 5120 + 1024

    def test_quick_pause_resume_discards_stale_audio(self, recorder):
        recorder._has_system_audio = True
        recorder._sys_device = "fake"
        path = recorder.start()
        recorder._mic_queue.put_nowait((_mic(value=0.5), SAMPLE_RATE))
        recorder._sys_queue.put_nowait((_sys(), SAMPLE_RATE))
        assert _wait_for(lambda: recorder._samples_in_segment >= 1024)
        # Two mic chunks with no system counterpart are left pending...
        recorder._mic_queue.put_nowait((_mic(value=0.5), SAMPLE_RATE))
        recorder._mic_queue.put_nowait((_mic(value=0.5), SAMPLE_RATE))
        time.sleep(0.25)
        # ...then a pause+resume too fast for the writer to observe PAUSED.
        recorder.pause()
        recorder.resume()
        for _ in range(2):
            recorder._mic_queue.put_nowait((_mic(value=0.8), SAMPLE_RATE))
            recorder._sys_queue.put_nowait((_sys(), SAMPLE_RATE))
        assert _wait_for(lambda: recorder._samples_in_segment >= 3072)
        recorder.stop()

        data, _ = sf.read(str(path), always_2d=True)
        assert data.shape[0] == 3072
        assert np.allclose(data[:1024], 0.375, atol=0.01)
        assert np.allclose(data[1024:], 0.525, atol=0.01), "stale mic must not pair with new sys"

    def test_pause_discards_and_resume_realigns(self, recorder):
        recorder._has_system_audio = True
        recorder._sys_device = "fake"
        path = recorder.start()
        for _ in range(2):
            recorder._mic_queue.put_nowait((_mic(), SAMPLE_RATE))
            recorder._sys_queue.put_nowait((_sys(), SAMPLE_RATE))
        assert _wait_for(lambda: recorder._samples_in_segment >= 2048)
        recorder.pause()
        for _ in range(3):
            recorder._mic_queue.put_nowait((_mic(value=0.9), SAMPLE_RATE))
        recorder._sys_queue.put_nowait((_sys(value=0.9), SAMPLE_RATE))
        time.sleep(0.3)
        recorder.resume()
        for _ in range(2):
            recorder._mic_queue.put_nowait((_mic(), SAMPLE_RATE))
            recorder._sys_queue.put_nowait((_sys(), SAMPLE_RATE))
        assert _wait_for(lambda: recorder._samples_in_segment >= 4096)
        recorder.stop()
        data, _ = sf.read(str(path), always_2d=True)
        assert data.shape[0] == 4096
        assert np.allclose(data, 0.375, atol=0.01)


class TestMixLaw:

    def test_unity_sum_with_soft_limiter_when_balanced(self):
        r = AudioRecorder()
        r.set_auto_balance(True)
        mic = np.full(1024, 0.05, dtype=np.float32)
        sys_ = np.full((1024, 2), 0.05, dtype=np.float32)
        out = r._mix_frames(mic.copy(), sys_.copy())
        # unity sum (gains start at 1.0): 0.1, not 0.05 as with 0.5/0.5
        assert out[0, 0] == pytest.approx(0.1, abs=0.005)
        loud = r._soft_limit(np.full((8, 2), 1.4, dtype=np.float32))
        assert np.all(loud < 1.0) and np.all(loud > 0.85)
        assert np.all(r._soft_limit(np.full((8, 2), 0.5, dtype=np.float32)) == 0.5)

    def test_raw_mix_kept_without_auto_balance(self):
        r = AudioRecorder()
        r.set_auto_balance(False)
        out = r._mix_frames(np.full(64, 0.4, dtype=np.float32),
                            np.full((64, 2), 0.4, dtype=np.float32))
        assert np.allclose(out, 0.4)

    def test_auto_balance_smoothing_is_time_based(self):
        r = AudioRecorder()
        r.set_auto_balance(True)
        quiet = np.full(48000, 0.02, dtype=np.float32)  # one second in one block
        r._mix_frames(quiet.copy(), None)
        one_second_gain = r._mic_running_gain
        r2 = AudioRecorder()
        r2.set_auto_balance(True)
        for _ in range(48000 // 1024):
            r2._mix_frames(quiet[:1024].copy(), None)
        assert one_second_gain == pytest.approx(r2._mic_running_gain, rel=0.15)


# ====================================================================
# Split / emergency / watchdog / auto-stop
# ====================================================================

class TestSplit:

    def test_long_recording_splits_and_sidecars_agree(self, recorder, monkeypatch):
        monkeypatch.setattr(audio_recorder, "MAX_SAMPLES_PER_SEGMENT", 3 * 1024)
        path = recorder.start()
        for _ in range(5):
            recorder._mic_queue.put_nowait((_mic(value=0.4), SAMPLE_RATE))
        assert _wait_for(lambda: len(recorder.segment_paths) == 2
                         and recorder._samples_in_segment >= 2048)
        recorder.stop()

        part2 = recorder.segment_paths[1]
        assert part2.name == f"{path.stem}_part2.wav"
        assert sf.read(str(path), always_2d=True)[0].shape[0] == 3072
        assert sf.read(str(part2), always_2d=True)[0].shape[0] == 2048
        meta1 = json.loads(Path(str(path) + ".json").read_text())
        meta2 = json.loads(Path(str(part2) + ".json").read_text())
        assert meta1["segments"] == meta2["segments"] == [str(path), str(part2)]
        assert (meta1["segment_index"], meta2["segment_index"]) == (0, 1)
        assert meta1["segment_count"] == 2
        assert meta1["duration_seconds"] == pytest.approx(3072 / SAMPLE_RATE, abs=0.01)
        assert meta2["duration_seconds"] == pytest.approx(2048 / SAMPLE_RATE, abs=0.01)


class TestEmergencySaveLive:

    def test_finalizes_without_post_processing(self, recorder, monkeypatch):
        recorder.set_output_format("mp3")
        monkeypatch.setattr(recorder, "_convert_to_mp3",
                            lambda: pytest.fail("post-processing must not run on emergency save"))
        path = recorder.start()
        for _ in range(3):
            recorder._mic_queue.put_nowait((_mic(), SAMPLE_RATE))
        assert _wait_for(lambda: recorder._samples_in_segment >= 3072)

        recorder.emergency_save()
        assert recorder.state == RecordingState.IDLE
        assert recorder._output_file is None
        assert recorder._streams_open is False
        assert sf.read(str(path), always_2d=True)[0].shape[0] >= 3072
        assert Path(str(path) + ".json").exists()
        assert recorder.stop() is None          # nothing left to stop
        recorder.emergency_save()               # idempotent


class TestAutoStopPaths:

    def test_auto_stop_runs_post_processing(self, recorder, tmp_path, monkeypatch):
        fake_ffmpeg.install(tmp_path, monkeypatch, rc=0)
        recorder.set_output_format("mp3")
        errors = []
        recorder.set_error_callback(errors.append)
        path = recorder.start()
        recorder._mic_queue.put_nowait((_mic(), SAMPLE_RATE))
        assert _wait_for(lambda: recorder._samples_in_segment >= 1024)

        recorder._auto_stop_session(recorder._stop_event, "Microfono scollegato")
        assert recorder.state == RecordingState.IDLE
        assert recorder.output_path == path.with_suffix(".mp3")
        assert recorder.output_path.exists()
        assert any("Microfono scollegato" in e for e in errors)

    def test_writer_fatal_non_disk_error_still_converts(self, recorder, tmp_path, monkeypatch):
        fake_ffmpeg.install(tmp_path, monkeypatch, rc=0)
        recorder.set_output_format("mp3")
        path = recorder.start()
        recorder._mic_queue.put_nowait((_mic(), SAMPLE_RATE))
        assert _wait_for(lambda: recorder._samples_in_segment >= 1024)

        def boom(*a, **k):
            raise ValueError("corrupt block")
        monkeypatch.setattr(recorder._output_file, "write", boom)
        recorder._mic_queue.put_nowait((_mic(), SAMPLE_RATE))
        assert _wait_for(lambda: recorder.state == RecordingState.IDLE)
        assert recorder.output_path == path.with_suffix(".mp3")

    def test_writer_fatal_disk_error_skips_conversion(self, recorder, tmp_path, monkeypatch):
        fake_ffmpeg.install(tmp_path, monkeypatch, rc=0)
        recorder.set_output_format("mp3")
        path = recorder.start()
        recorder._mic_queue.put_nowait((_mic(), SAMPLE_RATE))
        assert _wait_for(lambda: recorder._samples_in_segment >= 1024)

        def boom(*a, **k):
            raise OSError("No space left on device")
        monkeypatch.setattr(recorder._output_file, "write", boom)
        recorder._mic_queue.put_nowait((_mic(), SAMPLE_RATE))
        assert _wait_for(lambda: recorder.state == RecordingState.IDLE)
        assert recorder.output_path == path and path.exists()

    def test_stuck_writer_is_parked_and_blocks_new_start(self, recorder):
        release = threading.Event()
        stuck = threading.Thread(target=release.wait, daemon=True)
        stuck.start()
        try:
            assert recorder._park_writer_if_stuck(stuck) is True
            with pytest.raises(RuntimeError, match="non è ancora stata finalizzata"):
                recorder.start()
        finally:
            release.set()
            stuck.join(1)
        assert recorder._park_writer_if_stuck(stuck) is False


class TestWatchdog:

    def _fast_waits(self, r, monkeypatch):
        fake_wake = types.SimpleNamespace(
            wait=lambda timeout=None: time.sleep(0.005), clear=lambda: None,
            set=lambda: None, is_set=lambda: False)
        monkeypatch.setattr(r, "_watchdog_wake", fake_wake)
        stop_event = threading.Event()
        real_wait = stop_event.wait
        stop_event.wait = lambda timeout=None: (time.sleep(0.005), real_wait(0))[1]
        return stop_event

    def test_mic_recovered_resets_attempts(self, recorder, monkeypatch):
        stop_event = self._fast_waits(recorder, monkeypatch)
        recorder._state = RecordingState.RECORDING
        recorder._last_mic_callback = time.monotonic()
        dead = _FakeStream()
        dead.active = False
        recorder._mic_stream = dead
        calls = []

        def restart():
            calls.append(1)
            recorder._mic_stream = _FakeStream()
            recorder._last_mic_callback = time.monotonic()
            if len(calls) == 1:
                threading.Timer(0.05, stop_event.set).start()
            return True
        monkeypatch.setattr(recorder, "_restart_mic_stream", restart)
        monkeypatch.setattr(recorder, "_auto_stop_session",
                            lambda *a: pytest.fail("must not escalate"))
        writer = types.SimpleNamespace(is_alive=lambda: True)
        recorder._watchdog_loop(stop_event, writer)
        assert calls == [1]
        recorder._state = RecordingState.IDLE

    def test_unrecoverable_mic_escalates_after_three_attempts(self, recorder, monkeypatch):
        stop_event = self._fast_waits(recorder, monkeypatch)
        recorder._state = RecordingState.RECORDING
        recorder._last_mic_callback = time.monotonic()
        dead = _FakeStream()
        dead.active = False
        recorder._mic_stream = dead
        attempts = []
        monkeypatch.setattr(recorder, "_restart_mic_stream",
                            lambda: (attempts.append(1), False)[1])
        escalated = []

        def auto_stop(ev, message):
            escalated.append(message)
            ev.set()
        monkeypatch.setattr(recorder, "_auto_stop_session", auto_stop)
        writer = types.SimpleNamespace(is_alive=lambda: True)
        recorder._watchdog_loop(stop_event, writer)
        assert len(attempts) == 3
        assert escalated and "Microfono" in escalated[0]
        recorder._state = RecordingState.IDLE

    def test_silent_mic_callback_counts_as_dead(self, recorder, monkeypatch):
        stop_event = self._fast_waits(recorder, monkeypatch)
        recorder._state = RecordingState.RECORDING
        recorder._mic_stream = _FakeStream()          # active=True but silent
        recorder._last_mic_callback = time.monotonic() - audio_recorder.MIC_SILENCE_TIMEOUT - 1
        seen = []

        def restart():
            seen.append(1)
            recorder._last_mic_callback = time.monotonic()
            stop_event.set()
            return True
        monkeypatch.setattr(recorder, "_restart_mic_stream", restart)
        recorder._watchdog_loop(stop_event, types.SimpleNamespace(is_alive=lambda: True))
        assert seen == [1]
        recorder._state = RecordingState.IDLE

    def test_dead_writer_uses_auto_stop(self, recorder, monkeypatch):
        stop_event = self._fast_waits(recorder, monkeypatch)
        recorder._state = RecordingState.RECORDING
        called = []
        monkeypatch.setattr(recorder, "_auto_stop_session", lambda ev, m: called.append(m))
        monkeypatch.setattr(recorder, "_emergency_save",
                            lambda: pytest.fail("watchdog must not use the emergency path"))
        recorder._watchdog_loop(stop_event, types.SimpleNamespace(is_alive=lambda: False))
        assert called and "died" in called[0]
        recorder._state = RecordingState.IDLE


# ====================================================================
# Devices
# ====================================================================

class TestDeviceRefresh:

    def test_refresh_redetects_before_opening(self, monkeypatch):
        r = AudioRecorder()
        r._mic_device = None
        monkeypatch.setattr(audio_recorder.sd, "_terminate", lambda: None)
        monkeypatch.setattr(audio_recorder.sd, "_initialize", lambda: None)
        monkeypatch.setattr(audio_recorder, "detect_mic_device", lambda: (4, 2, 44100.0))
        assert r.has_microphone is False
        r._refresh_portaudio_devices()
        assert r.has_microphone and r._mic_device == 4 and r._mic_samplerate == 44100.0

    def test_open_streams_without_mic_raises_clearly(self, monkeypatch):
        r = AudioRecorder()
        monkeypatch.setattr(r, "_refresh_portaudio_devices", lambda: None)
        r._mic_device = None
        with pytest.raises(RuntimeError, match="Nessun microfono"):
            r._open_streams()

    def test_system_audio_toggle_takes_effect(self, monkeypatch):
        r = AudioRecorder()
        r._sys_device, r._has_system_audio = 7, True
        r.set_system_audio_enabled(False)
        assert r._sys_device is None and r.has_system_audio is False
        monkeypatch.setattr(audio_recorder, "detect_system_audio_device", lambda: (9, 2, 48000.0))
        r.set_system_audio_enabled(True)
        assert r._sys_device == 9 and r.has_system_audio is True

    def test_finished_callback_wakes_watchdog(self):
        r = AudioRecorder()
        r._state = RecordingState.RECORDING
        r._on_stream_finished()
        assert r._watchdog_wake.is_set()
        r._state = RecordingState.IDLE


class TestWasapiCallbackMode:

    def test_callback_routes_audio_and_counts_overflow(self, monkeypatch):
        captured = {}

        class FakeStream:
            def is_active(self):
                return True

            def stop_stream(self):
                pass

            def close(self):
                pass

        class FakePyAudio:
            def open(self, **kwargs):
                captured.update(kwargs)
                return FakeStream()

            def terminate(self):
                pass

        fake_module = types.SimpleNamespace(PyAudio=FakePyAudio, paFloat32=1,
                                            paInputOverflow=2, paContinue=0)
        monkeypatch.setitem(sys.modules, "pyaudiowpatch", fake_module)
        r = AudioRecorder()
        r._sys_device = {"index": 5, "maxInputChannels": 4, "defaultSampleRate": 48000.0}
        r._open_wasapi_loopback()

        assert captured["input_device_index"] == 5
        assert captured["channels"] == 4 and captured["rate"] == 48000
        assert callable(captured["stream_callback"]) and captured["start"] is True
        assert r._sys_channels == 2 and r._wasapi_stream_active()

        frames = np.full((1024, 4), 0.3, dtype=np.float32)
        result = captured["stream_callback"](frames.tobytes(), 1024, None, 2)
        assert result == (None, 0)
        assert r.input_overflows == 1
        assert r.sys_level == pytest.approx(0.3, abs=0.01)
        r._close_streams()
        assert r._wasapi_stream is None


class TestLinuxPulseFallback:

    def test_pulse_device_with_monitor_source(self, monkeypatch):
        from unittest.mock import patch
        monkeypatch.setattr(platform_audio, "_detect_linux_pulsectl",
                            lambda: ["Monitor of X", "alsa_output.x.monitor"])
        with patch("platform_audio.sd") as mock_sd:
            mock_sd.query_devices.return_value = [
                {"name": "default", "max_input_channels": 32, "default_samplerate": 44100.0},
                {"name": "pulse", "max_input_channels": 32, "default_samplerate": 44100.0},
            ]
            result = platform_audio._detect_linux()
        assert result == (1, 2, 44100.0)
        assert platform_audio.linux_monitor_source == "alsa_output.x.monitor"

    def test_no_pulse_device_means_no_system_audio(self, monkeypatch):
        from unittest.mock import patch
        monkeypatch.setattr(platform_audio, "_detect_linux_pulsectl",
                            lambda: ["Monitor of X", "alsa_output.x.monitor"])
        with patch("platform_audio.sd") as mock_sd:
            mock_sd.query_devices.return_value = [
                {"name": "hw:0,0", "max_input_channels": 2, "default_samplerate": 44100.0},
            ]
            assert platform_audio._detect_linux() == (None, None, None)
        assert platform_audio.linux_monitor_source is None

    def test_pulse_source_env_is_scoped(self, monkeypatch):
        monkeypatch.delenv("PULSE_SOURCE", raising=False)
        with _pulse_source_env("mon.monitor"):
            assert os.environ["PULSE_SOURCE"] == "mon.monitor"
        assert "PULSE_SOURCE" not in os.environ
        monkeypatch.setenv("PULSE_SOURCE", "user-choice")
        with _pulse_source_env("mon.monitor"):
            assert os.environ["PULSE_SOURCE"] == "mon.monitor"
        assert os.environ["PULSE_SOURCE"] == "user-choice"
        with _pulse_source_env(None):
            assert os.environ["PULSE_SOURCE"] == "user-choice"


class TestWindowsMic:

    def test_wasapi_default_input_preferred(self):
        from unittest.mock import patch
        with patch("sys.platform", "win32"), patch("platform_audio.sd") as mock_sd:
            mock_sd.query_hostapis.return_value = [
                {"name": "MME", "default_input_device": 1},
                {"name": "Windows WASAPI", "default_input_device": 3},
            ]
            mock_sd.default.device = [1, 2]
            devices = {
                1: {"name": "Mic (MME)", "max_input_channels": 2, "default_samplerate": 44100.0},
                3: {"name": "Mic (WASAPI)", "max_input_channels": 2, "default_samplerate": 48000.0},
            }
            mock_sd.query_devices.side_effect = lambda *a: devices[a[0]] if a else list(devices.values())
            assert platform_audio.detect_mic_device() == (3, 2, 48000.0)


class TestMacHelperArch:

    def _macho(self, cputype: int) -> bytes:
        return b"\xcf\xfa\xed\xfe" + cputype.to_bytes(4, "little") + b"\x00" * 24

    def test_architecture_check(self, tmp_path):
        from macos_system_audio import binary_matches_host
        arm = tmp_path / "arm"
        arm.write_bytes(self._macho(0x0100000C))
        intel = tmp_path / "intel"
        intel.write_bytes(self._macho(0x01000007))
        fat = tmp_path / "fat"
        fat.write_bytes(b"\xca\xfe\xba\xbe" + b"\x00" * 28)
        assert binary_matches_host(arm, machine="arm64")
        assert not binary_matches_host(arm, machine="x86_64")
        assert binary_matches_host(intel, machine="x86_64")
        assert not binary_matches_host(intel, machine="arm64")
        assert binary_matches_host(fat, machine="x86_64")
        assert not binary_matches_host(tmp_path / "missing", machine="arm64")


class TestMiscRecorder:

    def test_soxr_survives_float64_input(self):
        rs = _StreamingResampler(44100, 48000, 1)
        if rs._soxr is None:
            pytest.skip("soxr not available")
        out = rs.process(np.zeros(1024, dtype=np.float64))
        assert out.dtype == np.float32
        assert rs._soxr is not None

    def test_ffmpeg_candidate_order(self, tmp_path, monkeypatch):
        override = tmp_path / "my-ffmpeg"
        override.write_text("x")
        monkeypatch.setenv("IMAGEIO_FFMPEG_EXE", str(override))
        monkeypatch.setattr(audio_recorder.shutil, "which", lambda name: "/usr/bin/ffmpeg")
        cands = audio_recorder._ffmpeg_candidates()
        assert cands[0] == str(override) and cands[1] == "/usr/bin/ffmpeg"
        assert audio_recorder._find_ffmpeg() == str(override)

    def test_peak_normalize_keeps_flac_tags(self, tmp_path):
        path = tmp_path / "recording_t.flac"
        data = np.full((SAMPLE_RATE // 4, 2), 0.2, dtype=np.float32)
        with sf.SoundFile(str(path), "w", samplerate=SAMPLE_RATE, channels=2,
                          format="FLAC", subtype="PCM_16") as f:
            f.comment = "dual_track=yes channels=L:mic,R:sys"
            f.software = "Orizon Call"
            f.write(data)
        AudioRecorder()._peak_normalize_in_place(path, target_peak_dbfs=-1.0)
        with sf.SoundFile(str(path)) as f:
            assert f.comment == "dual_track=yes channels=L:mic,R:sys"
            assert f.software.startswith("Orizon Call")
        out, _ = sf.read(str(path), always_2d=True)
        assert float(np.max(np.abs(out))) == pytest.approx(0.891, abs=0.02)

    def test_change_sequence_wakes_waiters(self):
        r = AudioRecorder()
        seq = r.wait_for_state_change(timeout=0.0)
        r.set_mic_muted(True)
        t0 = time.monotonic()
        new_seq = r.wait_for_state_change(timeout=2.0, since=seq)
        assert new_seq == seq + 1
        assert time.monotonic() - t0 < 0.5  # returned immediately
        r.set_mic_muted(False)


# ====================================================================
# API
# ====================================================================

class TestApiRound2:

    @pytest.fixture()
    def server(self, qapp, tmp_path):
        import socket
        from tests.test_api_server import FakeWidget
        from api_server import RecorderAPIHandler, start_api_server, stop_api_server
        widget = FakeWidget()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        srv = start_api_server(widget, port=port, output_dir=tmp_path,
                               require_auth=True, bound_socket=sock)
        yield {"port": port, "widget": widget, "token": RecorderAPIHandler.auth_token,
               "dir": tmp_path}
        stop_api_server(srv)

    def _req(self, server, path, method="GET", headers=None, token=True):
        req = urllib.request.Request(f"http://127.0.0.1:{server['port']}{path}", method=method)
        if token:
            req.add_header("Authorization", f"Bearer {server['token']}")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def test_byte_ranges(self, server):
        f = server["dir"] / "recording_r.wav"
        f.write_bytes(b"0123456789")
        status, headers, body = self._req(server, "/files/recording_r.wav",
                                          headers={"Range": "bytes=2-5"})
        assert status == 206 and body == b"2345"
        assert headers["Content-Range"] == "bytes 2-5/10" and headers["Content-Length"] == "4"
        assert headers["Accept-Ranges"] == "bytes"
        status, _, body = self._req(server, "/files/recording_r.wav", headers={"Range": "bytes=-3"})
        assert status == 206 and body == b"789"
        status, _, body = self._req(server, "/files/recording_r.wav", headers={"Range": "bytes=7-"})
        assert status == 206 and body == b"789"
        status, headers, _ = self._req(server, "/files/recording_r.wav", headers={"Range": "bytes=50-60"})
        assert status == 416 and headers["Content-Range"] == "bytes */10"
        status, _, body = self._req(server, "/files/recording_r.wav")
        assert status == 200 and body == b"0123456789"

    def test_head_requests(self, server):
        f = server["dir"] / "recording_h.wav"
        f.write_bytes(b"abc")
        status, headers, body = self._req(server, "/files/recording_h.wav", method="HEAD")
        assert status == 200 and body == b"" and headers["Content-Length"] == "3"
        status, _, body = self._req(server, "/health", method="HEAD", token=False)
        assert status == 200 and body == b""
        status, headers, body = self._req(server, "/events", method="HEAD")
        assert status == 200 and headers["Content-Type"].startswith("text/event-stream") and body == b""

    def test_content_disposition_is_safe(self, server):
        f = server["dir"] / "recording_caffè.wav"
        f.write_bytes(b"x")
        status, headers, _ = self._req(server, "/files/" + urllib.request.quote("recording_caffè.wav"))
        assert status == 200
        cd = headers["Content-Disposition"]
        assert 'filename="recording_caff?.wav"' in cd
        assert "filename*=UTF-8''recording_caff%C3%A8.wav" in cd
        status, _, _ = self._req(server, "/files/" + urllib.request.quote('recording_a"b.wav'))
        assert status == 400
        status, _, _ = self._req(server, "/files/" + urllib.request.quote("recording_a\r\nX: y.wav"))
        assert status == 400

    def test_stdlib_errors_are_json_and_auth_challenge(self, server):
        status, headers, body = self._req(server, "/start", method="PUT")
        assert status == 501
        assert headers["Content-Type"].startswith("application/json")
        assert json.loads(body)["code"] == 501
        status, headers, _ = self._req(server, "/status", token=False)
        assert status == 401 and headers["WWW-Authenticate"].startswith("Bearer")
        assert "Python" not in headers.get("Server", "")
        status, _, _ = self._req(server, "/status", token=False,
                                 headers={"Authorization": f"bearer {server['token']}"})
        assert status == 200

    def test_cors_exposes_and_reflects_headers(self, server):
        req = urllib.request.Request(f"http://127.0.0.1:{server['port']}/status", method="OPTIONS")
        req.add_header("Origin", "http://localhost:3000")
        req.add_header("Access-Control-Request-Method", "GET")
        req.add_header("Access-Control-Request-Headers", "Authorization, X-Requested-With")
        with urllib.request.urlopen(req, timeout=5) as resp:
            headers = dict(resp.headers)
        assert headers["Access-Control-Allow-Headers"] == "Authorization, X-Requested-With"
        assert "Content-Disposition" in headers["Access-Control-Expose-Headers"]
        assert "HEAD" in headers["Access-Control-Allow-Methods"]

    def test_no_second_status_line_after_headers(self, server, monkeypatch):
        import socket
        from api_server import RecorderAPIHandler

        def half_response(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            raise RuntimeError("late failure")
        monkeypatch.setattr(RecorderAPIHandler, "_handle_status", half_response)
        with socket.create_connection(("127.0.0.1", server["port"]), timeout=5) as s:
            s.sendall(f"GET /status HTTP/1.0\r\nHost: 127.0.0.1\r\n"
                      f"Authorization: Bearer {server['token']}\r\n\r\n".encode())
            data = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
        assert data.count(b"HTTP/1.0 ") == 1 and data.startswith(b"HTTP/1.0 200")

    def test_token_redacted_in_error_log(self, server, monkeypatch, caplog):
        import logging
        from api_server import RecorderAPIHandler

        def boom(self):
            raise RuntimeError("sse exploded")
        monkeypatch.setattr(RecorderAPIHandler, "_handle_events_sse", boom)
        logger = logging.getLogger("orizon.api")
        monkeypatch.setattr(logger, "propagate", True)
        with caplog.at_level(logging.ERROR, logger="orizon.api"):
            status, _, _ = self._req(server, f"/events?token={server['token']}", token=False)
        assert status == 500
        assert server["token"] not in caplog.text
        assert "token=<redacted>" in caplog.text

    def test_sse_statechange_event(self, server):
        url = f"http://127.0.0.1:{server['port']}/events?token={server['token']}"
        with urllib.request.urlopen(urllib.request.Request(url), timeout=5) as resp:
            first = resp.readline().decode()
            assert first.startswith("retry:")
            seen_event = False
            deadline = time.monotonic() + 3
            got_first_data = False
            while time.monotonic() < deadline:
                line = resp.readline().decode()
                if line.startswith("event: statechange"):
                    seen_event = True
                    payload = json.loads(resp.readline().decode()[len("data: "):])
                    assert payload["state"] == "recording"
                    break
                if line.startswith("data: ") and not got_first_data:
                    got_first_data = True
                    server["widget"].state_name = "recording"
            server["widget"].state_name = "idle"
        assert got_first_data and seen_event

    def test_normalize_origin(self):
        from api_server import normalize_origin
        assert normalize_origin("https://App.Orizon.com/") == "https://app.orizon.com"
        assert normalize_origin("https://app.orizon.com:443/recorder") == "https://app.orizon.com"
        assert normalize_origin("http://localhost:3000") == "http://localhost:3000"
        assert normalize_origin("ftp://x") is None
        assert normalize_origin("") is None and normalize_origin(None) is None


class TestSingleInstance:

    def test_probe_recognises_orizon_call(self, qapp, tmp_path):
        import socket
        import main as main_mod
        from tests.test_api_server import FakeWidget
        from api_server import start_api_server, stop_api_server
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        srv = start_api_server(FakeWidget(), port=port, output_dir=tmp_path,
                               require_auth=True, bound_socket=sock)
        try:
            assert main_mod._probe_is_orizon_call(port) is True
            assert main_mod._try_bind_or_explain(port) is None
        finally:
            stop_api_server(srv)

    def test_probe_rejects_foreign_service(self):
        import socket
        import main as main_mod
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        def serve():
            try:
                conn, _ = listener.accept()
                conn.recv(1024)
                conn.sendall(b"HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\nhi")
                conn.close()
            except OSError:
                pass
        threading.Thread(target=serve, daemon=True).start()
        try:
            assert main_mod._probe_is_orizon_call(port) is False
        finally:
            listener.close()

    def test_free_port_binds(self):
        import main as main_mod
        sock = main_mod._try_bind_or_explain(0)
        assert sock is not None
        sock.close()


# ====================================================================
# Widget
# ====================================================================

class TestWidgetRound2:

    @pytest.fixture()
    def widget(self, qapp, tmp_path, monkeypatch):
        from floating_widget import FloatingRecorderWidget
        r = AudioRecorder()
        r.set_output_directory(tmp_path)
        w = FloatingRecorderWidget(r)
        yield w, r
        w.shutdown()
        w.close()

    def _pump(self, qapp, predicate, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            qapp.processEvents()
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def test_quit_during_start_stops_and_quits(self, qapp, widget, monkeypatch):
        import floating_widget
        w, r = widget
        path = r.output_directory / "recording_q.wav"

        def slow_start():
            time.sleep(0.2)
            r._state = RecordingState.RECORDING
            return path

        def fast_stop():
            r._state = RecordingState.IDLE
            return path
        monkeypatch.setattr(r, "start", slow_start)
        monkeypatch.setattr(r, "stop", fast_stop)
        quits = []
        monkeypatch.setattr(floating_widget.QApplication, "quit", staticmethod(lambda: quits.append(1)))

        w._start_recording(interactive=False)
        w.request_quit()                      # arrives while the start worker runs
        assert self._pump(qapp, lambda: bool(quits))
        assert r.state == RecordingState.IDLE
        assert w._quit_when_done is False    # flag consumed, not left armed

    def test_unmute_when_recording_ends(self, widget):
        w, r = widget
        w._set_mute(True)
        assert r.is_mic_muted
        w._on_stop_done(True, str(r.output_directory / "recording_a.wav"), False)
        assert r.is_mic_muted is False

    def test_pending_settings_applied_at_next_start(self, widget, monkeypatch):
        w, r = widget
        w._pending_settings = {
            "output_format": "flac", "output_dir": "", "dual_track": True,
            "auto_balance": True, "normalize": False, "normalize_lufs": -16.0,
            "system_audio": False,
        }
        monkeypatch.setattr(r, "start", lambda: (_ for _ in ()).throw(RuntimeError("no")))
        w._start_recording(interactive=False)
        assert r._output_format == "flac" and r._mix_mode is False
        assert w._pending_settings is None

    def test_non_interactive_quit_never_blocks_on_dialog(self, widget, monkeypatch):
        import floating_widget
        w, r = widget
        monkeypatch.setattr(w, "_exec_dialog", lambda box: pytest.fail("dialog shown"))
        quits = []
        monkeypatch.setattr(floating_widget.QApplication, "quit", staticmethod(lambda: quits.append(1)))
        w._quit_interactive = False
        w._on_stop_done(False, "disk exploded", True)
        assert quits == [1]

    def test_keyboard_toggles_recording(self, widget, monkeypatch):
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QKeyEvent
        w, _ = widget
        called = []
        monkeypatch.setattr(w, "toggle_recording", lambda: called.append("rec"))
        monkeypatch.setattr(w, "toggle_pause", lambda: called.append("pause"))
        for key in (Qt.Key.Key_Space, Qt.Key.Key_P):
            w.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier))
        assert called == ["rec", "pause"]
        assert w._mute_btn.accessibleName() == "Silenzia microfono"
