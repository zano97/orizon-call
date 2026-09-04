"""
Post-processing safety tests: a failed ffmpeg run must NEVER destroy the
original recording, and the loudnorm temp file must keep a real audio
extension (ffmpeg infers the muxer from it). Runs on every OS through
tests/fake_ffmpeg.py.
"""

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from audio_recorder import AudioRecorder, SAMPLE_RATE
from tests import fake_ffmpeg


@pytest.fixture()
def recorder() -> AudioRecorder:
    r = AudioRecorder()
    r._mic_samplerate = SAMPLE_RATE
    r._sys_samplerate = SAMPLE_RATE
    return r


def _make_wav(path: Path, seconds: float = 0.2) -> None:
    data = np.full((int(SAMPLE_RATE * seconds), 2), 0.3, dtype=np.float32)
    sf.write(str(path), data, SAMPLE_RATE, subtype="PCM_16")


class TestMp3Conversion:

    def test_failed_ffmpeg_keeps_wav(self, recorder, tmp_path, monkeypatch):
        wav = tmp_path / "recording_x.wav"
        _make_wav(wav)
        fake_ffmpeg.install(tmp_path, monkeypatch, rc=1, output="none")

        errors = []
        recorder.set_error_callback(errors.append)
        recorder._segment_paths = [wav]
        recorder._output_path = wav
        recorder._convert_to_mp3()

        assert wav.exists(), "original WAV must survive a failed conversion"
        assert recorder._segment_paths == [wav]
        assert recorder._output_path == wav
        assert errors, "the user must be told the conversion failed"

    def test_failed_ffmpeg_with_partial_mp3_keeps_wav(self, recorder, tmp_path, monkeypatch):
        """Even if a partial .mp3 is left behind, rc!=0 must keep the WAV
        and remove the partial output."""
        wav = tmp_path / "recording_y.wav"
        _make_wav(wav)
        fake_ffmpeg.install(tmp_path, monkeypatch, rc=1, output="write")

        recorder._segment_paths = [wav]
        recorder._output_path = wav
        recorder._convert_to_mp3()

        assert wav.exists()
        assert not wav.with_suffix(".mp3").exists()
        assert recorder._segment_paths == [wav]

    def test_successful_ffmpeg_replaces_wav(self, recorder, tmp_path, monkeypatch):
        wav = tmp_path / "recording_z.wav"
        _make_wav(wav)
        fake_ffmpeg.install(tmp_path, monkeypatch, rc=0)

        recorder._segment_paths = [wav]
        recorder._output_path = wav
        recorder._convert_to_mp3()

        mp3 = wav.with_suffix(".mp3")
        assert mp3.exists()
        assert not wav.exists()
        assert recorder._segment_paths == [mp3]
        assert recorder._output_path == mp3

    def test_second_candidate_used_when_first_fails(self, recorder, tmp_path, monkeypatch):
        """A system ffmpeg without libmp3lame fails; the bundled build is
        tried next and the recording still ends up as MP3."""
        import audio_recorder
        wav = tmp_path / "recording_fb.wav"
        _make_wav(wav)
        failing = fake_ffmpeg.install(tmp_path, monkeypatch, rc=1, output="write")
        ok = fake_ffmpeg.install_always_ok(tmp_path)
        monkeypatch.setattr(audio_recorder, "_ffmpeg_candidates", lambda: [str(failing), str(ok)])

        recorder._segment_paths = [wav]
        recorder._output_path = wav
        recorder._convert_to_mp3()
        mp3 = wav.with_suffix(".mp3")
        assert mp3.exists() and not wav.exists()
        assert recorder._output_path == mp3


class TestLoudnorm:

    def test_temp_output_keeps_wav_extension(self, recorder, tmp_path, monkeypatch):
        """ffmpeg picks the muxer from the output extension: the loudnorm
        temp file must end in .wav, not .tmp."""
        wav = tmp_path / "recording_n.wav"
        _make_wav(wav)
        args_log = tmp_path / "args.txt"
        fake_ffmpeg.install(tmp_path, monkeypatch, rc=0, log=args_log)

        recorder._segment_paths = [wav]
        recorder._loudness_normalize_segments(-16.0)

        recorded_args = args_log.read_text()
        out_arg = recorded_args.strip().split()[-1]
        assert out_arg.endswith(".wav"), f"loudnorm temp must be .wav, got {out_arg}"
        assert wav.exists()

    def test_failed_loudnorm_keeps_original(self, recorder, tmp_path, monkeypatch):
        wav = tmp_path / "recording_f.wav"
        _make_wav(wav)
        original = wav.read_bytes()
        fake_ffmpeg.install(tmp_path, monkeypatch, rc=1, measure_rc=1)

        recorder._segment_paths = [wav]
        recorder._loudness_normalize_segments(-16.0)

        assert wav.exists()
        assert wav.read_bytes() == original
        # No temp litter left behind.
        assert list(tmp_path.glob("*.norm.*")) == []
