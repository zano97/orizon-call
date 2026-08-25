import sys
import time
from unittest.mock import MagicMock

# Mock sounddevice completely
mock_sd = MagicMock()
sys.modules['sounddevice'] = mock_sd

import platform_audio

class MockDevice(dict):
    pass

devices_mock = [
    {'name': 'alsa_output.pci-0000_00_1f.3.analog-stereo.monitor', 'max_input_channels': 2, 'default_samplerate': 44100},
    {'name': 'some_other_device', 'max_input_channels': 2, 'default_samplerate': 44100}
]

def mock_query_devices(*args, **kwargs):
    time.sleep(0.01) # Simulate slow syscall
    return devices_mock

mock_sd.query_devices.side_effect = mock_query_devices

# Mock pulsectl to return a non-matching name to ensure both query_devices() run
platform_audio._detect_linux_pulsectl = lambda: ['some_non_existent_device_to_force_fallback']

start_time = time.time()
for _ in range(100):
    platform_audio._detect_linux()
end_time = time.time()

print(f"Elapsed time for 100 iterations (baseline): {end_time - start_time:.4f} seconds")
