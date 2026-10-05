"""Settings dialog (offscreen): widget → values mapping, persistence via
app_settings, and application to the recorder on save."""

import pytest
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication, QDialog

import app_settings
from audio_recorder import AudioRecorder


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def isolated_settings(monkeypatch, tmp_path):
    """Route app_settings persistence to a temp ini file."""
    path = str(tmp_path / "settings.ini")
    monkeypatch.setattr(
        app_settings, "make_qsettings",
        lambda: QSettings(path, QSettings.Format.IniFormat))
    return path


def test_dialog_reflects_saved_settings(qapp, isolated_settings, tmp_path):
    app_settings.save_settings({
        "output_format": "mp3",
        "output_dir": str(tmp_path),
        "normalize": True,
        "normalize_lufs": -14.0,
        "dual_track": True,
        "auto_balance": False,
        "system_audio": False,
        "hide_from_screen_share": False,
        "call_detection": "auto",
        "call_detection_ignored": ["Zoom"],
    })
    from settings_dialog import SettingsDialog
    dlg = SettingsDialog()
    values = dlg.values()
    assert values["hide_from_screen_share"] is False
    assert values["call_detection"] == "auto"
    assert values["call_detection_ignored"] == ["Zoom"]
    assert values["output_format"] == "mp3"
    assert values["output_dir"] == str(tmp_path)
    assert values["normalize"] is True
    assert values["normalize_lufs"] == -14.0
    assert values["dual_track"] is True
    assert values["auto_balance"] is False
    assert values["system_audio"] is False


def test_dialog_save_persists_and_applies(qapp, isolated_settings):
    from settings_dialog import SettingsDialog, open_settings
    dlg = SettingsDialog()
    SettingsDialog._select_data(dlg._format, "mp3")
    dlg._normalize.setChecked(True)
    dlg._dual_track.setChecked(True)
    dlg.save()

    reloaded = app_settings.load_settings()
    assert reloaded["output_format"] == "mp3"
    assert reloaded["normalize"] is True
    assert reloaded["dual_track"] is True

    # open_settings applies the saved values to the recorder on accept.
    recorder = AudioRecorder()

    class AutoAcceptDialog(SettingsDialog):
        def exec(self):
            SettingsDialog._select_data(self._format, "flac")
            return QDialog.DialogCode.Accepted

    import settings_dialog as sd
    orig = sd.SettingsDialog
    sd.SettingsDialog = AutoAcceptDialog
    try:
        values = open_settings(None, recorder)
        assert values and values["output_format"] == "flac"
    finally:
        sd.SettingsDialog = orig
    assert recorder._output_format == "flac"


def test_lufs_combo_follows_normalize_checkbox(qapp, isolated_settings):
    from settings_dialog import SettingsDialog
    dlg = SettingsDialog()
    assert dlg._lufs.isEnabled() is False
    dlg._normalize.setChecked(True)
    assert dlg._lufs.isEnabled() is True


def test_reset_ignored_apps(qapp, isolated_settings):
    app_settings.save_settings({"call_detection_ignored": ["Zoom", "Slack"]})
    from settings_dialog import SettingsDialog
    dlg = SettingsDialog()
    assert "Zoom" in dlg._reset_ignored.text()
    dlg._reset_ignored.click()
    dlg.save()
    assert app_settings.load_settings()["call_detection_ignored"] == []


def test_defaults_privacy_on_and_propose(qapp, isolated_settings):
    from settings_dialog import SettingsDialog
    values = SettingsDialog().values()
    assert values["hide_from_screen_share"] is True
    assert values["call_detection"] == "propose"
    assert values["call_detection_ignored"] == []
