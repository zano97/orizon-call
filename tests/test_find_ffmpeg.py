"""Tests for the ffmpeg discovery helper: system ffmpeg on PATH wins,
otherwise the static binary bundled by imageio-ffmpeg is used, and the
helper degrades to None (never raises) when neither is available."""

import os
import sys

import audio_recorder
from audio_recorder import _find_ffmpeg


def test_prefers_system_ffmpeg(monkeypatch):
    monkeypatch.setattr(audio_recorder.shutil, "which",
                        lambda name: "/fake/bin/ffmpeg")
    assert _find_ffmpeg() == "/fake/bin/ffmpeg"


def test_falls_back_to_bundled_binary(monkeypatch):
    monkeypatch.setattr(audio_recorder.shutil, "which", lambda name: None)
    exe = _find_ffmpeg()
    assert exe is not None
    assert os.path.isfile(exe)
    assert os.access(exe, os.X_OK)


def test_none_when_nothing_available(monkeypatch):
    monkeypatch.setattr(audio_recorder.shutil, "which", lambda name: None)
    # Make `import imageio_ffmpeg` raise ImportError.
    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", None)
    assert _find_ffmpeg() is None
