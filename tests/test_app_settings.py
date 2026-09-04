"""Persistent settings: defaults, roundtrip, corrupt-value fallback, CLI
override precedence and application to the recorder."""

import argparse

import pytest
from PyQt6.QtCore import QSettings

import app_settings
from audio_recorder import AudioRecorder


@pytest.fixture()
def qs(tmp_path):
    """Isolated ini-backed QSettings so tests never touch real settings."""
    return QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)


def _args(**kwargs):
    base = dict(format=None, output_dir=None, dual_track=False,
                no_auto_balance=False, normalize=None, no_system_audio=False)
    base.update(kwargs)
    return argparse.Namespace(**base)


class TestLoadSave:

    def test_defaults_when_empty(self, qs):
        assert app_settings.load_settings(qs) == app_settings.DEFAULTS

    def test_roundtrip(self, qs, tmp_path):
        values = {
            "output_format": "mp3",
            "output_dir": str(tmp_path / "rec"),
            "dual_track": True,
            "auto_balance": False,
            "normalize": True,
            "normalize_lufs": -14.0,
            "system_audio": False,
        }
        app_settings.save_settings(values, qs)
        # Re-read through a fresh instance on the same ini file: values
        # survive the string round-trip of the ini format.
        qs2 = QSettings(qs.fileName(), QSettings.Format.IniFormat)
        assert app_settings.load_settings(qs2) == values

    def test_corrupt_values_fall_back_to_defaults(self, qs):
        qs.beginGroup("recording")
        qs.setValue("output_format", "ogg")        # not supported
        qs.setValue("normalize_lufs", "not-a-number")
        qs.setValue("dual_track", "maybe")
        qs.endGroup()
        s = app_settings.load_settings(qs)
        assert s["output_format"] == app_settings.DEFAULTS["output_format"]
        assert s["normalize_lufs"] == app_settings.DEFAULTS["normalize_lufs"]
        assert s["dual_track"] == app_settings.DEFAULTS["dual_track"]


class TestCliOverrides:

    def test_no_flags_keep_settings(self):
        s = dict(app_settings.DEFAULTS, output_format="mp3", dual_track=True)
        merged = app_settings.merge_cli_overrides(s, _args())
        assert merged == s

    def test_explicit_flags_win(self, tmp_path):
        s = dict(app_settings.DEFAULTS, output_format="mp3", system_audio=True)
        merged = app_settings.merge_cli_overrides(
            s, _args(format="flac", output_dir=tmp_path, dual_track=True,
                     no_auto_balance=True, normalize=-14.0,
                     no_system_audio=True))
        assert merged["output_format"] == "flac"
        assert merged["output_dir"] == str(tmp_path)
        assert merged["dual_track"] is True
        assert merged["auto_balance"] is False
        assert merged["normalize"] is True
        assert merged["normalize_lufs"] == -14.0
        assert merged["system_audio"] is False

    def test_overrides_do_not_mutate_input(self):
        s = dict(app_settings.DEFAULTS)
        app_settings.merge_cli_overrides(s, _args(format="mp3"))
        assert s["output_format"] == app_settings.DEFAULTS["output_format"]


class TestApplyToRecorder:

    def test_apply(self, tmp_path):
        r = AudioRecorder()
        app_settings.apply_to_recorder(r, {
            "output_format": "mp3",
            "output_dir": str(tmp_path),
            "dual_track": True,
            "auto_balance": False,
            "normalize": True,
            "normalize_lufs": -14.0,
            "system_audio": False,
        })
        assert r._output_format == "mp3"
        assert r._output_dir == tmp_path
        assert r._mix_mode is False
        assert r._auto_balance is False
        assert r._normalize_lufs == -14.0
        assert r._system_audio_enabled is False

    def test_apply_defaults(self):
        r = AudioRecorder()
        app_settings.apply_to_recorder(r, dict(app_settings.DEFAULTS))
        assert r._output_format == "wav"
        assert r._output_dir == app_settings.default_downloads_dir()
        assert r._mix_mode is True
        assert r._normalize_lufs is None
        assert r._system_audio_enabled is True


def test_effective_output_dir(tmp_path):
    assert app_settings.effective_output_dir(
        dict(app_settings.DEFAULTS)) == app_settings.default_downloads_dir()
    assert app_settings.default_downloads_dir().is_absolute()
    s = dict(app_settings.DEFAULTS, output_dir=str(tmp_path))
    assert app_settings.effective_output_dir(s) == tmp_path
