"""
Writer-loop alignment tests: the mic and system sources must be written
sample-aligned, with silence injected ONLY when a source is genuinely
starved or at the final drain — never as per-iteration zero-stuffing.
"""

import queue
import time

import numpy as np
import pytest
import soundfile as sf

from audio_recorder import (
    AudioRecorder,
    RecordingState,
    SAMPLE_RATE,
    STARVATION_FRAMES,
)


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


def _mic_chunk(n=1024, value=0.5):
    return np.full((n, 1), value, dtype=np.float32)


def _sys_chunk(n=1024, value=0.25):
    return np.full((n, 2), value, dtype=np.float32)


def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class TestAlignment:

    def test_uneven_arrival_no_zero_stuffing(self, recorder):
        """3 mic chunks vs 2 sys chunks: the aligned overlap is mixed, the
        mic tail is written at the END (final drain), never interleaved
        with stuffed zeros."""
        recorder._has_system_audio = True
        recorder._sys_device = "fake"
        path = recorder.start()
        assert recorder._session_has_sys

        for _ in range(3):
            recorder._mic_queue.put_nowait((_mic_chunk(), SAMPLE_RATE))
        for _ in range(2):
            recorder._sys_queue.put_nowait((_sys_chunk(), SAMPLE_RATE))

        time.sleep(0.4)
        recorder.stop()

        data, sr = sf.read(str(path), always_2d=True)
        assert sr == SAMPLE_RATE
        assert data.shape[0] == 3 * 1024
        # First 2048 frames: 0.5*0.5 + 0.5*0.25 = 0.375 on both channels.
        assert np.allclose(data[:2048], 0.375, atol=0.01)
        # Mic tail mixed against silence: 0.5*0.5 = 0.25.
        assert np.allclose(data[2048:], 0.25, atol=0.01)
        # No silent gaps anywhere.
        assert float(np.min(np.abs(data[:, 0]))) > 0.2

    def test_starvation_pads_dead_source(self, recorder):
        """If the system source dies mid-recording, mic audio keeps being
        written (padded with silence) instead of stalling forever."""
        recorder._has_system_audio = True
        recorder._sys_device = "fake"
        path = recorder.start()

        n_chunks = STARVATION_FRAMES // 1024 + 2
        for _ in range(n_chunks):
            recorder._mic_queue.put_nowait((_mic_chunk(), SAMPLE_RATE))

        # The writer must flush the starved-aligned audio while still
        # recording (not only at stop).
        assert _wait_for(lambda: recorder._samples_in_segment >= n_chunks * 1024)
        recorder.stop()

        data, _ = sf.read(str(path), always_2d=True)
        assert data.shape[0] == n_chunks * 1024
        assert np.allclose(data, 0.25, atol=0.01)  # mic/2, sys silent

    def test_mic_only_session_writes_everything(self, recorder):
        path = recorder.start()
        assert not recorder._session_has_sys
        for _ in range(4):
            recorder._mic_queue.put_nowait((_mic_chunk(value=0.4), SAMPLE_RATE))
        time.sleep(0.3)
        recorder.stop()
        data, _ = sf.read(str(path), always_2d=True)
        assert data.shape[0] == 4 * 1024
        assert np.allclose(data, 0.4, atol=0.01)

    def test_resampled_source_aligns(self, recorder):
        """A 44.1 kHz mic must be resampled to 48 kHz continuously and
        aligned against the 48 kHz system source."""
        recorder._has_system_audio = True
        recorder._sys_device = "fake"
        recorder._mic_samplerate = 44100.0
        path = recorder.start()

        n = 10
        for _ in range(n):
            recorder._mic_queue.put_nowait(
                (np.full((1024, 1), 0.5, dtype=np.float32), 44100.0))
        # Equivalent duration of sys audio at 48k.
        sys_frames = int(n * 1024 * 48000 / 44100)
        for _ in range(sys_frames // 1024 + 1):
            recorder._sys_queue.put_nowait((_sys_chunk(), SAMPLE_RATE))

        time.sleep(0.5)
        recorder.stop()
        data, _ = sf.read(str(path), always_2d=True)
        # Both sources present for nearly the whole file -> mixed level.
        mixed_region = data[1024:n * 1024 - 4096]
        assert np.allclose(mixed_region, 0.375, atol=0.02)


class TestDropAccounting:

    def test_queue_overflow_is_counted(self, recorder):
        recorder._state = RecordingState.RECORDING
        recorder._mic_queue = queue.Queue(maxsize=2)
        for _ in range(5):
            recorder._route_chunk(np.zeros((4, 1), dtype=np.float32),
                                  SAMPLE_RATE, recorder._mic_queue,
                                  recorder._mic_ring)
        assert recorder.dropped_chunks == 3
        recorder._state = RecordingState.IDLE


class TestAutoStop:

    def test_writer_fatal_transitions_to_idle(self, recorder, monkeypatch):
        """Disk-full style write errors must auto-stop the recording and
        leave a finalized, playable file — no zombie RECORDING state."""
        path = recorder.start()
        recorder._mic_queue.put_nowait((_mic_chunk(), SAMPLE_RATE))
        assert _wait_for(lambda: recorder._samples_in_segment >= 1024)

        # Make every subsequent write explode like a full disk.
        def boom(*a, **k):
            raise OSError("No space left on device")
        monkeypatch.setattr(recorder._output_file, "write", boom)
        recorder._mic_queue.put_nowait((_mic_chunk(), SAMPLE_RATE))

        assert _wait_for(lambda: recorder.state == RecordingState.IDLE)
        data, _ = sf.read(str(path), always_2d=True)
        assert data.shape[0] >= 1024  # audio up to the failure is saved


class TestNoDoubleResample:

    def test_mix_frames_never_resamples(self, recorder):
        """Regression: writer data is already 48 kHz when _mix_frames runs;
        a non-48k device rate must NOT trigger a second resample (it would
        stretch the audio ~9% and zero-pad the system channel)."""
        recorder._mic_samplerate = 44100.0
        recorder._sys_samplerate = 44100.0
        mic = np.full(48000, 0.5, dtype=np.float32)
        sys_ = np.full((48000, 2), 0.25, dtype=np.float32)
        out = recorder._mix_frames(mic, sys_)
        assert out.shape[0] == 48000, (
            f"_mix_frames changed the frame count: {out.shape[0]}")
        out_mic_only = recorder._mix_frames(mic, None)
        assert out_mic_only.shape[0] == 48000

    def test_end_to_end_44100_mic_correct_duration(self, recorder):
        """A 44.1 kHz mic session must produce a file whose frame count
        matches the captured duration exactly once-resampled."""
        recorder._mic_samplerate = 44100.0
        path = recorder.start()
        n = 20
        for _ in range(n):
            recorder._mic_queue.put_nowait(
                (np.full((1024, 1), 0.5, dtype=np.float32), 44100.0))
        time.sleep(0.4)
        recorder.stop()
        data, _ = sf.read(str(path), always_2d=True)
        expected = n * 1024 * 48000 / 44100
        # Streaming-resampler flush recovers the filter delay; allow a
        # few samples of rounding.
        assert abs(data.shape[0] - expected) < 64, (
            f"got {data.shape[0]} frames, expected ~{expected:.0f}")
        assert np.allclose(data[100:-100], 0.5, atol=0.02)
