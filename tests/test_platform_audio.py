from unittest.mock import patch

from platform_audio import detect_system_audio_device


def test_detect_system_audio_device_macos():
    with patch("sys.platform", "darwin"), patch("platform_audio._detect_macos", return_value=("macos_device", 2, 48000.0)) as mock_macos:
        result = detect_system_audio_device()
        mock_macos.assert_called_once()
        assert result == ("macos_device", 2, 48000.0)


def test_detect_system_audio_device_windows():
    with patch("sys.platform", "win32"), patch("platform_audio._detect_windows", return_value=({"name": "windows_device"}, 2, 44100.0)) as mock_windows:
        result = detect_system_audio_device()
        mock_windows.assert_called_once()
        assert result == ({"name": "windows_device"}, 2, 44100.0)


def test_detect_system_audio_device_linux():
    with patch("sys.platform", "linux"), patch("platform_audio._detect_linux", return_value=(3, 2, 48000.0)) as mock_linux:
        result = detect_system_audio_device()
        mock_linux.assert_called_once()
        assert result == (3, 2, 48000.0)


def test_detect_system_audio_device_unknown_platform():
    with patch("sys.platform", "freebsd"), patch("platform_audio._detect_macos") as mock_macos, patch("platform_audio._detect_windows") as mock_windows, patch("platform_audio._detect_linux") as mock_linux:
        result = detect_system_audio_device()
        mock_macos.assert_not_called()
        mock_windows.assert_not_called()
        mock_linux.assert_not_called()
        assert result == (None, None, None)
