"""Tests for auto-balance gain matching and loudness normalization fallback."""

import shutil
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from audio_recorder import AudioRecorder, SAMPLE_RATE


@pytest.fixture()
def recorder() -> AudioRecorder:
    r = AudioRecorder()
    r._mic_samplerate = SAMPLE_RATE
    r._sys_samplerate = SAMPLE_RATE
    return r


# ---------- Auto-balance ----------

class TestAutoBalance:

    def test_disabled_passes_through(self, recorder):
        recorder.set_auto_balance(False)
        mic = np.full(1024, 0.02, dtype=np.float32)  # very quiet
        sys_ = np.zeros((1024, 2), dtype=np.float32)
        sys_[:] = 0.8                                  # loud
        out = recorder._mix_frames(mic, sys_)
        # Mic contribution to the mix: 0.5 * 0.02 = 0.01 → essentially inaudible
        # vs sys contribution 0.5 * 0.8 = 0.4. Strong imbalance preserved.
        # Use samples in the middle to ensure we're past any startup transient.
        avg_l = float(np.mean(out[200:, 0]))
        assert avg_l == pytest.approx(0.01 + 0.4, abs=0.01)

    def test_enabled_lifts_quiet_source(self, recorder):
        recorder.set_auto_balance(True)
        # Run many blocks so the smoothing converges.
        mic = np.full(1024, 0.02, dtype=np.float32)
        sys_ = np.full((1024, 2), 0.5, dtype=np.float32)
        for _ in range(200):
            out = recorder._mix_frames(mic.copy(), sys_.copy())
        # After convergence, the quiet mic should have been amplified close
        # to the target RMS (~0.12), the loud sys gently turned down.
        # Mic running gain should be > 1 (amplification).
        assert recorder._mic_running_gain > 2.0
        # Sys gain should be < 1 (attenuation).
        assert recorder._sys_running_gain < 1.0

    def test_silent_source_skips_gain_update(self, recorder):
        recorder.set_auto_balance(True)
        mic = np.zeros(1024, dtype=np.float32)
        # Without any signal, gain should stay at its initial value (1.0).
        recorder._mix_frames(mic, None)
        assert recorder._mic_running_gain == 1.0

    def test_gain_clamped_to_max(self, recorder):
        recorder.set_auto_balance(True)
        recorder._ab_max_gain = 4.0
        # A signal so quiet that the desired gain would be huge.
        mic = np.full(1024, 0.0001, dtype=np.float32) + 0.01  # barely above min_rms
        for _ in range(500):
            recorder._mix_frames(mic.copy(), None)
        assert recorder._mic_running_gain <= 4.0 + 1e-6


# ---------- Peak normalize fallback ----------

class TestPeakNormalize:

    def test_scales_to_target_peak(self, recorder, tmp_path):
        path = tmp_path / "test.wav"
        data = np.full((SAMPLE_RATE, 2), 0.2, dtype=np.float32)
        sf.write(str(path), data, SAMPLE_RATE, subtype="PCM_16")

        recorder._peak_normalize_in_place(path, target_peak_dbfs=-1.0)

        # Reload and inspect
        out, _ = sf.read(str(path), always_2d=True)
        peak = float(np.max(np.abs(out)))
        # Target = -1 dBFS = 10^(-1/20) ≈ 0.891
        # int16 quantisation introduces small rounding; allow a tolerance.
        assert peak == pytest.approx(0.891, abs=0.02)

    def test_silent_file_unchanged(self, recorder, tmp_path):
        path = tmp_path / "silent.wav"
        data = np.zeros((SAMPLE_RATE, 2), dtype=np.float32)
        sf.write(str(path), data, SAMPLE_RATE, subtype="PCM_16")
        recorder._peak_normalize_in_place(path, target_peak_dbfs=-1.0)
        out, _ = sf.read(str(path), always_2d=True)
        assert np.all(out == 0.0)


# ---------- Loudness normalize (only runs if ffmpeg installed) ----------

@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
class TestLoudnessNormalize:

    def test_loudnorm_runs_without_error(self, recorder, tmp_path):
        path = tmp_path / "test.wav"
        # 1 second of pink-ish noise at moderate level
        rng = np.random.default_rng(42)
        data = (rng.standard_normal((SAMPLE_RATE, 2)) * 0.1).astype(np.float32)
        sf.write(str(path), data, SAMPLE_RATE, subtype="PCM_16")
        recorder._segment_paths = [path]
        recorder._loudness_normalize_segments(target_lufs=-16.0)
        # File should still exist and be playable.
        out, _ = sf.read(str(path), always_2d=True)
        assert out.shape[0] > 0
