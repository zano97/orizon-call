"""
Post-processing safety tests: a failed ffmpeg run must NEVER destroy the
original recording, and the loudnorm temp file must keep a real audio
extension (ffmpeg infers the muxer from it).
"""

import os
import stat
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from audio_recorder import AudioRecorder, SAMPLE_RATE


pytestmark = pytest.mark.skipif(sys.platform == "win32",
                                reason="fake-ffmpeg shell scripts are POSIX")


@pytest.fixture()
def recorder() -> AudioRecorder:
    r = AudioRecorder()
    r._mic_samplerate = SAMPLE_RATE
    r._sys_samplerate = SAMPLE_RATE
    return r


def _make_wav(path: Path, seconds: float = 0.2) -> None:
    data = np.full((int(SAMPLE_RATE * seconds), 2), 0.3, dtype=np.float32)
    sf.write(str(path), data, SAMPLE_RATE, subtype="PCM_16")


def _fake_ffmpeg(tmp_path: Path, script_body: str, monkeypatch) -> Path:
    """Install a fake `ffmpeg` at the front of PATH. The script receives
    the real ffmpeg CLI args; $@ / ${@: -1} give access to them."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "ffmpeg"
    script.write_text("#!/bin/bash\n" + script_body, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    return script


class TestMp3Conversion:

    def test_failed_ffmpeg_keeps_wav(self, recorder, tmp_path, monkeypatch):
        wav = tmp_path / "recording_x.wav"
        _make_wav(wav)
        _fake_ffmpeg(tmp_path, "exit 1\n", monkeypatch)

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
        # Writes garbage to the output (last arg) then fails.
        _fake_ffmpeg(tmp_path, 'echo garbage > "${@: -1}"\nexit 1\n', monkeypatch)

        recorder._segment_paths = [wav]
        recorder._output_path = wav
        recorder._convert_to_mp3()

        assert wav.exists()
        assert not wav.with_suffix(".mp3").exists()
        assert recorder._segment_paths == [wav]

    def test_successful_ffmpeg_replaces_wav(self, recorder, tmp_path, monkeypatch):
        wav = tmp_path / "recording_z.wav"
        _make_wav(wav)
        _fake_ffmpeg(tmp_path, 'echo mp3data > "${@: -1}"\nexit 0\n', monkeypatch)

        recorder._segment_paths = [wav]
        recorder._output_path = wav
        recorder._convert_to_mp3()

        mp3 = wav.with_suffix(".mp3")
        assert mp3.exists()
        assert not wav.exists()
        assert recorder._segment_paths == [mp3]
        assert recorder._output_path == mp3


class TestLoudnorm:

    def test_temp_output_keeps_wav_extension(self, recorder, tmp_path, monkeypatch):
        """ffmpeg picks the muxer from the output extension: the loudnorm
        temp file must end in .wav, not .tmp."""
        wav = tmp_path / "recording_n.wav"
        _make_wav(wav)
        args_log = tmp_path / "args.txt"
        _fake_ffmpeg(tmp_path,
                     f'echo "$@" >> "{args_log}"\n'
                     'echo data > "${@: -1}"\nexit 0\n',
                     monkeypatch)

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
        _fake_ffmpeg(tmp_path, "exit 1\n", monkeypatch)

        recorder._segment_paths = [wav]
        recorder._loudness_normalize_segments(-16.0)

        assert wav.exists()
        assert wav.read_bytes() == original
        # No temp litter left behind.
        assert list(tmp_path.glob("*.norm.*")) == []
