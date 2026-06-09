"""
Output-file integrity tests: filename collisions, per-segment sidecar
metadata, and crash-consistent WAV headers.
"""

import json
import shutil
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

import audio_recorder
from audio_recorder import AudioRecorder, SAMPLE_RATE


class _FakeStream:
    active = True

    def start(self):
        pass

    def stop(self):
        pass

    def close(self):
        pass


@pytest.fixture()
def recorder(tmp_path, monkeypatch):
    r = AudioRecorder()
    r._mic_device = 0
    r._mic_samplerate = SAMPLE_RATE
    r.set_output_directory(tmp_path)
    r.set_output_format("wav")

    def fake_open():
        r._mic_stream = _FakeStream()
        r._streams_open = True

    def fake_close():
        r._mic_stream = None
        r._streams_open = False

    monkeypatch.setattr(r, "_open_streams", fake_open)
    monkeypatch.setattr(r, "_close_streams", fake_close)
    return r


class TestFilenameCollision:

    def test_existing_file_is_never_truncated(self, recorder, tmp_path, monkeypatch):
        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 6, 9, 12, 0, 0)

        monkeypatch.setattr(audio_recorder, "datetime", FrozenDatetime)

        first = recorder._generate_output_path()
        first.write_bytes(b"precious previous recording")
        second = recorder._generate_output_path()

        assert second != first
        assert second.name == "recording_20260609_120000_2.wav"
        assert first.read_bytes() == b"precious previous recording"


class TestSidecar:

    def test_sidecar_written_with_real_duration(self, recorder, tmp_path):
        path = recorder.start()
        chunk = np.full((1024, 1), 0.4, dtype=np.float32)
        for _ in range(5):
            recorder._mic_queue.put_nowait((chunk, SAMPLE_RATE))
        deadline = time.monotonic() + 3
        while recorder._samples_in_segment < 5 * 1024 and time.monotonic() < deadline:
            time.sleep(0.02)
        recorder.stop()

        sidecar = Path(str(path) + ".json")
        assert sidecar.exists()
        meta = json.loads(sidecar.read_text())
        assert meta["software"] == "Orizon Call"
        assert meta["sample_rate"] == SAMPLE_RATE
        assert meta["dual_track"] is False
        assert meta["channel_map"] == ["mixed", "mixed"]
        # Duration from frames actually written, not wall-clock guesses.
        assert meta["duration_seconds"] == pytest.approx(5 * 1024 / SAMPLE_RATE, abs=0.01)
        assert meta["segments"] == [str(path)]

    def test_failed_start_leaves_no_orphans(self, recorder, tmp_path, monkeypatch):
        def explode():
            raise RuntimeError("no streams today")
        monkeypatch.setattr(recorder, "_open_streams", explode)

        with pytest.raises(RuntimeError):
            recorder.start()

        assert recorder.output_path is None
        assert recorder.segment_paths == []
        assert list(tmp_path.glob("recording_*")) == []


class TestCrashConsistentHeader:

    def test_wav_readable_without_close(self, recorder, tmp_path):
        """Simulates a hard kill: a copy of the file taken while it is
        still open must be playable (header auto-update enabled)."""
        target = tmp_path / "recording_live.wav"
        recorder._output_path = target
        recorder._segment_paths = [target]
        recorder._open_output_file(target)
        recorder._output_file.write(np.full((SAMPLE_RATE, 2), 0.2, dtype=np.float32))
        recorder._output_file.flush()

        snapshot = tmp_path / "snapshot.wav"
        shutil.copy(target, snapshot)
        data, sr = sf.read(str(snapshot), always_2d=True)
        assert sr == SAMPLE_RATE
        assert data.shape[0] == SAMPLE_RATE, (
            "header must already announce the written frames")

        recorder._output_file.close()
        recorder._output_file = None
