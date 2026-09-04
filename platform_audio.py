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

# Virtual/loopback devices that must never be used as the "microphone":
# picking one would record system audio onto the mic track.
_VIRTUAL_INPUT_KEYWORDS = ('blackhole', 'soundflower', 'loopback', 'monitor')

# Linux only: when system audio goes through the ALSA 'pulse' plugin device
# (distro PortAudio has no PulseAudio host API, so monitor sources are not
# PortAudio devices), this is the PulseAudio/PipeWire source name to capture.
# audio_recorder exports it as PULSE_SOURCE while opening that stream.
linux_monitor_source: Optional[str] = None


def _is_virtual_input(name: str) -> bool:
    name_lower = name.lower()
    return any(kw in name_lower for kw in _VIRTUAL_INPUT_KEYWORDS)


def _mic_settings_supported(index: int, samplerate: float) -> bool:
    """Ask PortAudio whether the exact stream we are going to open (mono
    float32 at the device's default rate) is supported. A device that is
    listed but cannot actually be opened (unplugged, exclusive-mode busy,
    broken driver) is skipped so we pick a working mic up front instead
    of failing at start()."""
    try:
        sd.check_input_settings(device=index, channels=1, dtype='float32',
                                samplerate=samplerate)
        return True
    except Exception:
        return False


def _sys_settings_supported(index: int, channels: int, samplerate: float) -> bool:
    """Same check for a system-audio candidate (stereo float32 at its
    default rate): a stale virtual device or a monitor with an unusable
    rate is skipped instead of failing later inside _open_streams."""
    try:
        sd.check_input_settings(device=index, channels=min(int(channels), 2),
                                dtype='float32', samplerate=samplerate)
        return True
    except Exception:
        return False


def _windows_wasapi_default_input() -> Optional[int]:
    """On Windows PortAudio's global default input is the legacy MME
    device (31-char names, fixed 44.1 kHz, worse latency/overflow
    behaviour). The same microphone through the WASAPI host API is the
    modern path — and system audio already uses WASAPI."""
    try:
        for api in sd.query_hostapis():
            if str(api.get('name', '')).lower().find('wasapi') >= 0:
                idx = api.get('default_input_device', -1)
                if idx is not None and idx >= 0:
                    return int(idx)
    except Exception:
        pass
    return None


def detect_mic_device() -> Tuple[Optional[int], int, float]:
    """
    Detect the default microphone (input) device.

    Returns:
        (device_index, max_input_channels, default_samplerate)
        or (None, 0, 0) if no mic found.
    """
    if sys.platform == 'win32':
        try:
            idx = _windows_wasapi_default_input()
            if idx is not None:
                info = sd.query_devices(idx)
                if (info['max_input_channels'] > 0
                        and not _is_virtual_input(info['name'])
                        and _mic_settings_supported(idx, info['default_samplerate'])):
                    return (idx, info['max_input_channels'], info['default_samplerate'])
        except Exception:
            pass

    try:
        default_input = sd.default.device[0]
        if default_input is not None and default_input >= 0:
            info = sd.query_devices(default_input)
            # The default input can itself be a virtual/loopback device
            # (e.g. the user routed audio through BlackHole) — skip it and
            # fall through to the scan in that case.
            if (info['max_input_channels'] > 0
                    and not _is_virtual_input(info['name'])
                    and _mic_settings_supported(int(default_input), info['default_samplerate'])):
                return (int(default_input), info['max_input_channels'], info['default_samplerate'])
    except Exception:
        pass

    # Fallback: scan all devices for an input device
    devices = sd.query_devices()
    for i, dev in enumerate(devices):
        if dev['max_input_channels'] > 0:
            if _is_virtual_input(dev['name']):
                continue
            if not _mic_settings_supported(i, dev['default_samplerate']):
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
            "2. Installa il plugin ALSA per PulseAudio/PipeWire (Debian/Ubuntu: "
            "libasound2-plugins, Fedora: alsa-plugins-pulseaudio o pipewire-alsa, "
            "Arch: pipewire-alsa)\n"
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
            if (any(kw in name_lower for kw in virtual_keywords)
                    and _sys_settings_supported(i, dev['max_input_channels'], dev['default_samplerate'])):
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
        dev = _windows_pick_loopback(p, pyaudio)
        if dev is not None and dev.get('maxInputChannels', 0) > 0:
            return (dev, dev['maxInputChannels'], dev['defaultSampleRate'])
    except Exception:
        pass
    finally:
        if p is not None:
            try:
                p.terminate()
            except Exception:
                pass

    return (None, None, None)


def _windows_pick_loopback(p, pyaudio) -> Optional[dict]:
    """Pick the WASAPI loopback device to capture, preferring the
    loopback twin of the *default* output device (what the user hears).

    1. PyAudioWPatch's official helper get_default_wasapi_loopback()
       (raises OSError without WASAPI, LookupError without a loopback).
    2. The official generator over every loopback device.
    3. Manual scan, for releases predating those helpers.
    """
    get_default = getattr(p, 'get_default_wasapi_loopback', None)
    if callable(get_default):
        try:
            dev = get_default()
            if dev and dev.get('maxInputChannels', 0) > 0:
                return dev
        except (OSError, LookupError):
            pass  # no WASAPI / no loopback for the default device: keep looking
        except Exception:
            pass

    gen = getattr(p, 'get_loopback_device_info_generator', None)
    if callable(gen):
        try:
            for dev in gen():
                if dev.get('maxInputChannels', 0) > 0:
                    return dev
        except Exception:
            pass

    # Manual scan (older PyAudioWPatch): match the default speakers' name.
    try:
        wasapi_info = p.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_speakers = p.get_device_info_by_index(wasapi_info['defaultOutputDevice'])
        fallback = None
        for i in range(p.get_device_count()):
            dev = p.get_device_info_by_index(i)
            if not dev.get('isLoopbackDevice', False) or dev['maxInputChannels'] <= 0:
                continue
            if dev['name'].startswith(default_speakers['name']):
                return dev
            fallback = fallback or dev
        return fallback
    except Exception:
        return None


# ---------- Linux ----------

def _detect_linux() -> Tuple[Optional[int], Optional[int], Optional[float]]:
    """Detect PulseAudio/PipeWire monitor source on Linux."""
    global linux_monitor_source
    linux_monitor_source = None
    devices = sd.query_devices()

    # Method 1: pulsectl for precise detection. A PortAudio built with the
    # PulseAudio host API exposes pulse sources under their *description*
    # ("Monitor of Built-in Audio ..."), not their internal name
    # ("alsa_output...monitor"), so we match on both, case-insensitively.
    pulse_names = _detect_linux_pulsectl()

    if pulse_names:
        for candidate in pulse_names:
            cand_lower = candidate.lower()
            for i, dev in enumerate(devices):
                dev_lower = dev['name'].lower()
                if dev['max_input_channels'] > 0 and (
                        cand_lower in dev_lower or dev_lower in cand_lower):
                    if _sys_settings_supported(i, dev['max_input_channels'], dev['default_samplerate']):
                        return (i, dev['max_input_channels'], dev['default_samplerate'])

    # Method 2: fallback - scan for any device with 'monitor' in name
    for i, dev in enumerate(devices):
        if (dev['max_input_channels'] > 0 and 'monitor' in dev['name'].lower()
                and _sys_settings_supported(i, dev['max_input_channels'], dev['default_samplerate'])):
            return (i, dev['max_input_channels'], dev['default_samplerate'])

    # Method 3: the common case with distro PortAudio (ALSA host API only,
    # e.g. Debian/Ubuntu/Fedora libportaudio2): Pulse sources never show
    # up as devices. The ALSA 'pulse' plugin device does, and it captures
    # whatever PULSE_SOURCE names — the monitor of the default sink.
    if pulse_names:
        monitor_name = pulse_names[-1]  # internal name (description comes first)
        for i, dev in enumerate(devices):
            if (dev['name'].strip().lower() == 'pulse' and dev['max_input_channels'] > 0
                    and _sys_settings_supported(i, dev['max_input_channels'], dev['default_samplerate'])):
                linux_monitor_source = monitor_name
                return (i, min(dev['max_input_channels'], 2), dev['default_samplerate'])

    return (None, None, None)


def _detect_linux_pulsectl() -> list:
    """Use pulsectl to find the monitor source of the default sink.
    Returns candidate identifiers (description first, then name) for the
    best monitor source, or an empty list."""
    try:
        import pulsectl
    except ImportError:
        return []

    try:
        with pulsectl.Pulse('orizon-call-detect') as pulse:
            default_sink_name = _as_str(pulse.server_info().default_sink_name)
            sources = pulse.source_list()

            # 1. The server tells us which sink each monitor belongs to
            #    (monitor_of_sink_name / monitor_of_sink): exact, no naming
            #    assumptions. Use it to find the default sink's monitor.
            default_sink_index = None
            for sink in pulse.sink_list():
                if _as_str(sink.name) == default_sink_name:
                    default_sink_index = sink.index
                    break
            for source in sources:
                if not _is_monitor_source(source):
                    continue
                if (_as_str(getattr(source, 'monitor_of_sink_name', None)) == default_sink_name
                        or (default_sink_index is not None
                            and getattr(source, 'monitor_of_sink', None) == default_sink_index)):
                    return _source_identifiers(source)

            # 2. Naming convention <sink>.monitor (older servers).
            target_monitor = f"{default_sink_name}.monitor"
            for source in sources:
                if _as_str(source.name) == target_monitor:
                    return _source_identifiers(source)

            # 3. Any monitor source at all.
            for source in sources:
                if _is_monitor_source(source):
                    return _source_identifiers(source)
    except Exception:
        pass

    return []


_PA_INVALID_INDEX = 0xFFFFFFFF


def _as_str(value) -> str:
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='replace')
    return value if isinstance(value, str) else ''


def _is_monitor_source(source) -> bool:
    """True for PulseAudio/PipeWire monitor sources. monitor_of_sink is
    PA_INVALID_INDEX (uint32 max) for real inputs; also honour the
    '.monitor' naming convention as a belt-and-braces check."""
    mos = getattr(source, 'monitor_of_sink', None)
    if isinstance(mos, int) and 0 <= mos < _PA_INVALID_INDEX:
        return True
    return _as_str(getattr(source, 'name', '')).endswith('.monitor')


def _source_identifiers(source) -> list:
    return [s for s in (_as_str(getattr(source, 'description', None)),
                        _as_str(getattr(source, 'name', None))) if s]


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
