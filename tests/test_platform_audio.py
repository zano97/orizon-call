import pytest
from unittest.mock import patch

from platform_audio import detect_mic_device

def test_detect_mic_device_default_valid():
    """Test when the default device is valid."""
    with patch('platform_audio.sd') as mock_sd:
        mock_sd.default.device = [0, 1]
        mock_sd.query_devices.return_value = {
            'name': 'MacBook Pro Microphone',
            'max_input_channels': 1,
            'default_samplerate': 44100.0
        }

        result = detect_mic_device()
        assert result == (0, 1, 44100.0)
        mock_sd.query_devices.assert_called_with(0)

def test_detect_mic_device_default_virtual_fallback():
    """Test when default device is virtual, causing fallback to scan."""
    with patch('platform_audio.sd') as mock_sd:
        mock_sd.default.device = [0, 1]

        def side_effect(*args):
            if args == (0,):
                # The default device query
                return {
                    'name': 'BlackHole 16ch',
                    'max_input_channels': 16,
                    'default_samplerate': 48000.0
                }
            elif len(args) == 0:
                # The scan all devices query
                return [
                    {
                        'name': 'BlackHole 16ch',
                        'max_input_channels': 16,
                        'default_samplerate': 48000.0
                    },
                    {
                        'name': 'External Microphone',
                        'max_input_channels': 2,
                        'default_samplerate': 44100.0
                    }
                ]
            raise ValueError("Unexpected args")

        mock_sd.query_devices.side_effect = side_effect

        result = detect_mic_device()
        assert result == (1, 2, 44100.0)

def test_detect_mic_device_default_exception_fallback():
    """Test when fetching default device throws, triggering fallback scan."""
    with patch('platform_audio.sd') as mock_sd:
        # Simulate an exception when accessing the default device array index
        mock_sd.default.device.__getitem__.side_effect = Exception("Device error")

        # Fallback to scanning devices
        mock_sd.query_devices.return_value = [
            {
                'name': 'Valid Microphone',
                'max_input_channels': 1,
                'default_samplerate': 48000.0
            }
        ]

        result = detect_mic_device()
        assert result == (0, 1, 48000.0)

def test_detect_mic_device_no_valid_devices():
    """Test when no valid input devices are found."""
    with patch('platform_audio.sd') as mock_sd:
        mock_sd.default.device = [None, None]

        mock_sd.query_devices.return_value = [
            {
                'name': 'Speakers',
                'max_input_channels': 0,
                'default_samplerate': 44100.0
            },
            {
                'name': 'Soundflower (2ch)',
                'max_input_channels': 2,
                'default_samplerate': 48000.0
            }
        ]

        result = detect_mic_device()
        assert result == (None, 0, 0)
