"""
Streaming resampler tests: chunked resampling must be continuous across
block boundaries (no clicks) and must not drift over time.
"""

import numpy as np
import pytest

from audio_recorder import _StreamingResampler


def _sine(n, freq, sr, phase0=0.0):
    t = (np.arange(n) + phase0) / sr
    return np.sin(2 * np.pi * freq * t).astype(np.float32)


def _chunked(rs, signal, chunk=1024):
    out = []
    for i in range(0, len(signal), chunk):
        block = rs.process(signal[i:i + chunk])
        if block.shape[0]:
            out.append(block)
    return np.concatenate(out) if out else np.zeros(0, dtype=np.float32)


class TestContinuity:

    @pytest.mark.parametrize("from_rate", [44100, 24000, 96000])
    def test_no_clicks_at_block_boundaries(self, from_rate):
        """A pure sine resampled in chunks must stay smooth: the max
        sample-to-sample step never exceeds the sine's own slope bound."""
        freq = 440.0
        sig = _sine(from_rate, freq, from_rate)  # 1 second
        rs = _StreamingResampler(from_rate, 48000, 1)
        out = _chunked(rs, sig)

        assert out.shape[0] > 0
        # Theoretical max derivative of sin(2πft) sampled at 48k, +25% slack.
        max_step = 2 * np.pi * freq / 48000 * 1.25
        steps = np.abs(np.diff(out[100:-100]))
        assert float(steps.max()) < max_step, (
            f"discontinuity: step {steps.max():.4f} > bound {max_step:.4f}")

    def test_linear_fallback_is_continuous_too(self):
        freq = 200.0
        sig = _sine(44100, freq, 44100)
        rs = _StreamingResampler(44100, 48000, 1)
        rs._soxr = None  # force the linear fallback path
        out = _chunked(rs, sig)

        max_step = 2 * np.pi * freq / 48000 * 1.25
        steps = np.abs(np.diff(out[100:-100]))
        assert float(steps.max()) < max_step

    def test_stereo_shape_preserved(self):
        sig = np.random.default_rng(1).standard_normal((4096, 2)).astype(np.float32)
        rs = _StreamingResampler(44100, 48000, 2)
        out = _chunked(rs, sig)
        assert out.ndim == 2 and out.shape[1] == 2


class TestDrift:

    def test_no_cumulative_drift(self):
        """100 chunks of 1024 @ 44.1k -> output length within a handful of
        samples of the exact ratio (soxr keeps some latency in-flight)."""
        rs = _StreamingResampler(44100, 48000, 1)
        total_in = 0
        total_out = 0
        rng = np.random.default_rng(7)
        for _ in range(100):
            block = rng.standard_normal(1024).astype(np.float32)
            total_in += block.shape[0]
            total_out += rs.process(block).shape[0]
        expected = total_in * 48000 / 44100
        # Allow soxr's internal latency (~few hundred samples), but rule out
        # the old per-block truncation drift (~1.8 s/hour ≈ 60 samples/sec).
        assert abs(total_out - expected) < 2000

    def test_linear_fallback_no_drift(self):
        rs = _StreamingResampler(44100, 48000, 1)
        rs._soxr = None
        total_in = 0
        total_out = 0
        for _ in range(200):
            block = np.zeros(1024, dtype=np.float32)
            total_in += block.shape[0]
            total_out += rs.process(block).shape[0]
        expected = total_in * 48000 / 44100
        assert abs(total_out - expected) < 16

    def test_noop_when_rates_equal(self):
        rs = _StreamingResampler(48000, 48000, 1)
        data = np.ones(1000, dtype=np.float32)
        out = rs.process(data)
        assert np.array_equal(out, data)
