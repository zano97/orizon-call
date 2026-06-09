"""
Unit tests for the audio framing/mixing logic in AudioRecorder.

These tests do not touch any audio hardware — they instantiate the
recorder and call the private mixing helpers directly with synthesised
numpy arrays.
"""

import numpy as np
import pytest

from audio_recorder import AudioRecorder, SAMPLE_RATE, CHANNELS, BLOCKSIZE


@pytest.fixture()
def recorder() -> AudioRecorder:
    r = AudioRecorder()
    r._mic_samplerate = SAMPLE_RATE
    r._sys_samplerate = SAMPLE_RATE
    # These tests cover framing/mixing only — turn off auto-balance so the
    # expected sample values are deterministic. Auto-balance has its own
    # tests in test_audio_processing.py.
    r.set_auto_balance(False)
    return r


def _mic_chunk(n: int = 1024, value: float = 0.3) -> np.ndarray:
    return np.full((n,), value, dtype=np.float32)


def _sys_chunk(n: int = 1024, l: float = 0.5, r: float = -0.5) -> np.ndarray:
    arr = np.zeros((n, 2), dtype=np.float32)
    arr[:, 0] = l
    arr[:, 1] = r
    return arr


# ---------- Dual-track mode (default) ----------

class TestDualTrack:

    def test_both_sources_present(self, recorder):
        recorder.set_mix_mode(False)
        mic = _mic_chunk(1024, 0.3)
        sys = _sys_chunk(1024, 0.5, -0.5)
        out = recorder._mix_frames(mic, sys)
        assert out.shape == (1024, 2)
        # Left channel = mic (constant 0.3)
        assert np.allclose(out[:, 0], 0.3)
        # Right channel = downmix of sys (mean of 0.5 and -0.5 = 0)
        assert np.allclose(out[:, 1], 0.0)

    def test_mic_only_duplicates_to_stereo(self, recorder):
        recorder.set_mix_mode(False)
        mic = _mic_chunk(512, 0.4)
        out = recorder._mix_frames(mic, None)
        assert out.shape == (512, 2)
        assert np.allclose(out[:, 0], 0.4)
        assert np.allclose(out[:, 1], 0.4)

    def test_sys_only_preserves_true_stereo(self, recorder):
        recorder.set_mix_mode(False)
        sys = _sys_chunk(512, 0.7, -0.2)
        out = recorder._mix_frames(None, sys)
        assert out.shape == (512, 2)
        assert np.allclose(out[:, 0], 0.7)
        assert np.allclose(out[:, 1], -0.2)

    def test_uneven_lengths_zero_padded(self, recorder):
        recorder.set_mix_mode(False)
        mic = _mic_chunk(1000, 0.1)
        sys = _sys_chunk(800, 0.4, -0.4)
        out = recorder._mix_frames(mic, sys)
        assert out.shape == (1000, 2)
        # Trailing 200 samples on right channel = padded with 0
        assert np.allclose(out[800:, 1], 0.0)

    def test_neither_source_returns_silent_block(self, recorder):
        recorder.set_mix_mode(False)
        out = recorder._mix_frames(None, None)
        assert out.shape == (BLOCKSIZE, CHANNELS)
        assert np.all(out == 0)


# ---------- Mix mode (legacy) ----------

class TestMixMode:

    def test_50_50_mix(self, recorder):
        recorder.set_mix_mode(True)
        mic = _mic_chunk(1024, 0.4)
        sys = _sys_chunk(1024, 0.4, 0.4)
        out = recorder._mix_frames(mic, sys)
        # Both channels = 0.5*0.4 + 0.5*0.4 = 0.4 — but mic is mono duplicated.
        assert out.shape == (1024, 2)
        assert np.allclose(out, 0.4)

    def test_clips_to_unit_range(self, recorder):
        recorder.set_mix_mode(True)
        mic = _mic_chunk(256, 1.5)
        sys = _sys_chunk(256, 1.5, 1.5)
        out = recorder._mix_frames(mic, sys)
        assert out.max() <= 1.0
        assert out.min() >= -1.0


# ---------- Helpers ----------

class TestHelpers:

    def test_to_mono_passthrough(self, recorder):
        x = np.arange(10, dtype=np.float32)
        assert np.array_equal(recorder._to_mono(x), x)

    def test_to_mono_averages_stereo(self, recorder):
        x = np.column_stack([np.ones(8), np.full(8, -1.0)]).astype(np.float32)
        out = recorder._to_mono(x)
        assert np.allclose(out, 0.0)

    def test_to_stereo_duplicates_mono(self, recorder):
        x = np.arange(4, dtype=np.float32)
        out = recorder._to_stereo(x)
        assert out.shape == (4, 2)
        assert np.array_equal(out[:, 0], x)
        assert np.array_equal(out[:, 1], x)


# ---------- Resampling ----------

class TestResampling:

    def test_no_op_when_rates_equal(self, recorder):
        data = np.random.randn(512).astype(np.float32)
        out = recorder._resample(data, SAMPLE_RATE, SAMPLE_RATE)
        # Should return the array unchanged.
        assert np.array_equal(out, data)

    def test_upsample_doubles_length(self, recorder):
        data = np.sin(np.linspace(0, np.pi, 1000)).astype(np.float32)
        out = recorder._resample(data, 24000, 48000)
        # Allow a tiny tolerance because resampling may produce ±1 samples.
        assert abs(out.shape[0] - 2000) <= 4
        assert out.dtype == np.float32

    def test_downsample_halves_length(self, recorder):
        data = np.sin(np.linspace(0, np.pi, 2000)).astype(np.float32)
        out = recorder._resample(data, 48000, 24000)
        assert abs(out.shape[0] - 1000) <= 4

    def test_preserves_stereo_shape(self, recorder):
        data = np.random.randn(2000, 2).astype(np.float32)
        out = recorder._resample(data, 44100, 48000)
        assert out.ndim == 2
        assert out.shape[1] == 2
