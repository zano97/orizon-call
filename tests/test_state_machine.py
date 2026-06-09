"""
State-machine tests for AudioRecorder.

Audio streams are monkey-patched out so the tests don't need a microphone
or any platform audio support.
"""

from pathlib import Path
import time

import numpy as np
import pytest

from audio_recorder import AudioRecorder, RecordingState


class _FakeStream:
    """Minimal stand-in for sounddevice.InputStream."""

    def __init__(self):
        self.active = True
        self.started = False
        self.closed = False

    def start(self):
        self.started = True

    def stop(self):
        self.active = False

    def close(self):
        self.closed = True


@pytest.fixture()
def recorder(tmp_path, monkeypatch):
    r = AudioRecorder()

    # Pretend we found a mic and no system audio.
    r._mic_device = 0
    r._mic_channels = 1
    r._mic_samplerate = 48000
    r._sys_device = None
    r._has_system_audio = False

    # Replace _open_streams / _close_streams so they don't touch real hardware
    # but still set the bookkeeping flags the recorder relies on.
    def fake_open_streams():
        if r._streams_open:
            return
        r._mic_stream = _FakeStream()
        r._streams_open = True

    def fake_close_streams():
        if r._mic_stream is not None:
            r._mic_stream.stop()
            r._mic_stream.close()
        r._mic_stream = None
        r._streams_open = False

    monkeypatch.setattr(r, "_open_streams", fake_open_streams)
    monkeypatch.setattr(r, "_close_streams", fake_close_streams)

    r.set_output_directory(tmp_path)
    r.set_output_format("wav")
    return r


# ---------- Transitions ----------

def test_initial_state_is_idle(recorder):
    assert recorder.state == RecordingState.IDLE


def test_start_transitions_to_recording(recorder):
    path = recorder.start()
    assert recorder.state == RecordingState.RECORDING
    assert isinstance(path, Path)
    assert path.parent.exists()
    recorder.stop()


def test_pause_then_resume(recorder):
    recorder.start()
    recorder.pause()
    assert recorder.state == RecordingState.PAUSED
    recorder.resume()
    assert recorder.state == RecordingState.RECORDING
    recorder.stop()


def test_stop_returns_to_idle(recorder):
    recorder.start()
    recorder.stop()
    assert recorder.state == RecordingState.IDLE


def test_double_start_raises(recorder):
    recorder.start()
    with pytest.raises(RuntimeError):
        recorder.start()
    recorder.stop()


def test_pause_from_idle_raises(recorder):
    with pytest.raises(RuntimeError):
        recorder.pause()


def test_resume_from_recording_raises(recorder):
    recorder.start()
    with pytest.raises(RuntimeError):
        recorder.resume()
    recorder.stop()


def test_stop_from_idle_is_noop(recorder):
    assert recorder.stop() is None
    assert recorder.state == RecordingState.IDLE


# ---------- Mute ----------

def test_mute_default_off(recorder):
    assert recorder.is_mic_muted is False


def test_mute_toggle(recorder):
    recorder.set_mic_muted(True)
    assert recorder.is_mic_muted is True
    recorder.set_mic_muted(False)
    assert recorder.is_mic_muted is False


# ---------- Toggles ----------

def test_mix_mode_default_is_combined(recorder):
    # Default: single combined mix in both channels (one file, natural playback).
    assert recorder._mix_mode is True


def test_mix_mode_setter_can_switch_to_dual_track(recorder):
    recorder.set_mix_mode(False)
    assert recorder._mix_mode is False
    recorder.set_mix_mode(True)
    assert recorder._mix_mode is True


def test_preroll_setter_clamps_negative(recorder):
    recorder.set_preroll_seconds(-3.0)
    assert recorder._preroll_seconds == 0.0


def test_preroll_setter_accepts_positive(recorder):
    recorder.set_preroll_seconds(7.5)
    assert recorder._preroll_seconds == 7.5


# ---------- Elapsed time ----------

def test_elapsed_is_zero_when_idle(recorder):
    assert recorder.elapsed_time == 0.0


def test_elapsed_grows_during_recording(recorder):
    recorder.start()
    time.sleep(0.2)
    assert recorder.elapsed_time > 0.1
    recorder.stop()


def test_elapsed_frozen_while_paused(recorder):
    recorder.start()
    time.sleep(0.1)
    recorder.pause()
    paused_at = recorder.elapsed_time
    time.sleep(0.2)
    assert abs(recorder.elapsed_time - paused_at) < 0.01
    recorder.resume()
    recorder.stop()
