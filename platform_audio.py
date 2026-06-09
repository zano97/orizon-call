"""
Platform-specific system audio device detection.

Detects loopback/monitor devices for capturing system audio output on:
- macOS: BlackHole, Soundflower, or other virtual audio devices
- Windows: WASAPI loopback via PyAudioWPatch
- Linux: PulseAudio/PipeWire monitor sources
"""

import sys
from typing import Any, Optional, Tuple

import sounddevice as sd


def detect_mic_device() -> Tuple[Optional[int], int, float]:
    """
    Detect the default microphone (input) device.

    Returns:
        (device_index, max_input_channels, default_samplerate)
        or (None, 0, 0) if no mic found.
    """
    try:
        default_input = sd.default.device[0]
        if default_input is not None and default_input >= 0:
            info = sd.query_devices(default_input)
            if info['max_input_channels'] > 0:
                return (int(default_input), info['max_input_channels'], info['default_samplerate'])
    except Exception:
        pass

    # Fallback: scan all devices for an input device
    devices = sd.query_devices()
    for i, dev in enumerate(devices):
        if dev['max_input_channels'] > 0:
            name_lower = dev['name'].lower()
            # Skip virtual/loopback devices
            if any(kw in name_lower for kw in ('blackhole', 'soundflower', 'loopback', 'monitor')):
                continue
            return (i, dev['max_input_channels'], dev['default_samplerate'])

    return (None, 0, 0)


def detect_system_audio_device() -> Tuple[Optional[Any], Optional[int], Optional[float]]:
    """
    Detect a device capable of capturing system audio output.

    Returns:
        - macOS/Linux: (device_index: int, channels: int, samplerate: float)
        - Windows: (wasapi_device_info: dict, channels: int, samplerate: float)
        - Not found: (None, None, None)
    """
    if sys.platform == 'darwin':
        return _detect_macos()
    elif sys.platform == 'win32':
        return _detect_windows()
    elif sys.platform == 'linux':
        return _detect_linux()
    return (None, None, None)


def get_system_audio_guidance() -> str:
    """
    Return platform-specific guidance for enabling system audio capture.
    """
    if sys.platform == 'darwin':
        try:
            from macos_system_audio import get_permission_guidance
            return get_permission_guidance()
        except Exception:
            return (
                "Helper di cattura audio non disponibile. "
                "Compila helpers/system_audio_capture eseguendo helpers/build.sh "
                "(richiede Xcode Command Line Tools)."
            )
    elif sys.platform == 'win32':
        return (
            "L'audio di sistema su Windows verrà catturato automaticamente "
            "tramite WASAPI loopback. Assicurati che PyAudioWPatch sia installato: "
            "pip install PyAudioWPatch"
        )
    elif sys.platform == 'linux':
        return (
            "Per registrare l'audio di sistema su Linux:\n"
            "1. Assicurati che PulseAudio o PipeWire sia in esecuzione\n"
            "2. Installa pulsectl: pip install pulsectl\n"
            "3. L'app utilizzerà automaticamente il monitor source del sink predefinito"
        )
    return "Piattaforma non supportata per la cattura dell'audio di sistema."


# ---------- macOS ----------

def _detect_macos() -> Tuple[Optional[Any], Optional[int], Optional[float]]:
    """
    On macOS prefer the bundled ScreenCaptureKit helper (no driver install,
    no audio routing changes). Fall back to BlackHole/Soundflower/Loopback
    only if the helper isn't available.
    """
    try:
        from macos_system_audio import SAMPLE_RATE as SCK_RATE, CHANNELS as SCK_CH, is_available
        if is_available():
            return ("sck", SCK_CH, float(SCK_RATE))
    except Exception:
        pass

    virtual_keywords = ('blackhole', 'soundflower', 'loopback', 'virtual')
    devices = sd.query_devices()

    for i, dev in enumerate(devices):
        if dev['max_input_channels'] > 0:
            name_lower = dev['name'].lower()
            if any(kw in name_lower for kw in virtual_keywords):
                return (i, dev['max_input_channels'], dev['default_samplerate'])

    return (None, None, None)


# ---------- Windows ----------

def _detect_windows() -> Tuple[Optional[Any], Optional[int], Optional[float]]:
    """Detect WASAPI loopback device on Windows via PyAudioWPatch."""
    try:
        import pyaudiowpatch as pyaudio
    except ImportError:
        return (None, None, None)

    p = None
    try:
        p = pyaudio.PyAudio()

        # Get WASAPI host API info
        wasapi_info = p.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_speakers = p.get_device_info_by_index(
            wasapi_info['defaultOutputDevice']
        )

        # Find the loopback device matching the default speakers
        for i in range(p.get_device_count()):
            dev = p.get_device_info_by_index(i)
            if (dev['name'].startswith(default_speakers['name'])
                    and dev['maxInputChannels'] > 0
                    and dev.get('isLoopbackDevice', False)):
                return (
                    dev,
                    dev['maxInputChannels'],
                    dev['defaultSampleRate'],
                )

        # Fallback: any loopback device
        for i in range(p.get_device_count()):
            dev = p.get_device_info_by_index(i)
            if dev.get('isLoopbackDevice', False) and dev['maxInputChannels'] > 0:
                return (
                    dev,
                    dev['maxInputChannels'],
                    dev['defaultSampleRate'],
                )

    except Exception:
        pass
    finally:
        if p is not None:
            try:
                p.terminate()
            except Exception:
                pass

    return (None, None, None)


# ---------- Linux ----------

def _detect_linux() -> Tuple[Optional[int], Optional[int], Optional[float]]:
    """Detect PulseAudio/PipeWire monitor source on Linux."""
    # Method 1: try pulsectl for precise detection
    monitor_name = _detect_linux_pulsectl()

    if monitor_name:
        # Map the PulseAudio monitor source name to a sounddevice index
        devices = sd.query_devices()
        for i, dev in enumerate(devices):
            if dev['max_input_channels'] > 0 and monitor_name in dev['name']:
                return (i, dev['max_input_channels'], dev['default_samplerate'])

    # Method 2: fallback - scan for any device with 'monitor' in name
    devices = sd.query_devices()
    for i, dev in enumerate(devices):
        if dev['max_input_channels'] > 0 and 'monitor' in dev['name'].lower():
            return (i, dev['max_input_channels'], dev['default_samplerate'])

    return (None, None, None)


def _detect_linux_pulsectl() -> Optional[str]:
    """Use pulsectl to find the monitor source of the default sink."""
    try:
        import pulsectl
    except ImportError:
        return None

    try:
        with pulsectl.Pulse('orizon-call-detect') as pulse:
            server_info = pulse.server_info()
            default_sink_name = server_info.default_sink_name
            # The monitor source is typically named <sink_name>.monitor
            target_monitor = f"{default_sink_name}.monitor"

            sources = pulse.source_list()
            for source in sources:
                if source.name == target_monitor:
                    return source.name

            # Fallback: any monitor source
            for source in sources:
                if source.name.endswith('.monitor'):
                    return source.name
    except Exception:
        pass

    return None


if __name__ == '__main__':
    # Quick test: print detected devices
    print("=== Mic Device ===")
    mic = detect_mic_device()
    if mic[0] is not None:
        info = sd.query_devices(mic[0])
        print(f"  Device {mic[0]}: {info['name']}")
        print(f"  Channels: {mic[1]}, Sample Rate: {mic[2]}")
    else:
        print("  No microphone found!")

    print("\n=== System Audio Device ===")
    sys_dev = detect_system_audio_device()
    if sys_dev[0] is not None:
        if isinstance(sys_dev[0], dict):
            print(f"  WASAPI Device: {sys_dev[0]['name']}")
        else:
            info = sd.query_devices(sys_dev[0])
            print(f"  Device {sys_dev[0]}: {info['name']}")
        print(f"  Channels: {sys_dev[1]}, Sample Rate: {sys_dev[2]}")
    else:
        print("  No system audio device found!")
        print(f"\n{get_system_audio_guidance()}")
