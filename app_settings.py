"""
Persistent user settings for Orizon Call.

Stored via QSettings under the same OrizonCall scope the widget already
uses for its position, so everything lives in one place per platform
(plist on macOS, registry on Windows, ini under ~/.config on Linux).

Precedence: defaults < saved settings < explicit CLI flags. The GUI
settings dialog reads and writes these; CLI flags override for a single
run without being written back.
"""

from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QSettings

_ORG = "OrizonCall"
_APP = "OrizonCall"
_GROUP = "recording"

DEFAULTS = {
    "output_format": "wav",   # wav | flac | mp3
    "output_dir": "",         # "" = ~/Downloads
    "dual_track": False,      # False = combined mix, True = L=mic R=system
    "auto_balance": True,     # per-source gain matching
    "normalize": False,       # post-stop loudness normalization
    "normalize_lufs": -16.0,  # target when normalize is on
    "system_audio": True,     # capture system audio alongside the mic
}


def make_qsettings() -> QSettings:
    return QSettings(_ORG, _APP)


def _to_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        if value.lower() in ("true", "1", "yes"):
            return True
        if value.lower() in ("false", "0", "no"):
            return False
        return default
    if isinstance(value, int):
        return bool(value)
    return default


def _to_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load_settings(qs: Optional[QSettings] = None) -> dict:
    """Saved settings merged over DEFAULTS. Unknown/corrupt values fall
    back to their default instead of raising."""
    qs = qs or make_qsettings()
    s = dict(DEFAULTS)
    qs.beginGroup(_GROUP)
    try:
        fmt = qs.value("output_format", s["output_format"])
        if fmt in ("wav", "flac", "mp3"):
            s["output_format"] = fmt
        out_dir = qs.value("output_dir", s["output_dir"])
        s["output_dir"] = str(out_dir) if out_dir else ""
        s["dual_track"] = _to_bool(qs.value("dual_track"), s["dual_track"])
        s["auto_balance"] = _to_bool(qs.value("auto_balance"), s["auto_balance"])
        s["normalize"] = _to_bool(qs.value("normalize"), s["normalize"])
        s["normalize_lufs"] = _to_float(qs.value("normalize_lufs"), s["normalize_lufs"])
        s["system_audio"] = _to_bool(qs.value("system_audio"), s["system_audio"])
    finally:
        qs.endGroup()
    return s


def save_settings(values: dict, qs: Optional[QSettings] = None) -> None:
    qs = qs or make_qsettings()
    qs.beginGroup(_GROUP)
    try:
        for key in DEFAULTS:
            if key in values:
                qs.setValue(key, values[key])
    finally:
        qs.endGroup()
    qs.sync()


def merge_cli_overrides(s: dict, args) -> dict:
    """Apply explicitly-passed CLI flags on top of the saved settings.
    Flags left at their 'not passed' default do not touch the settings."""
    s = dict(s)
    if getattr(args, "format", None):
        s["output_format"] = args.format
    if getattr(args, "output_dir", None):
        s["output_dir"] = str(args.output_dir)
    if getattr(args, "dual_track", False):
        s["dual_track"] = True
    if getattr(args, "no_auto_balance", False):
        s["auto_balance"] = False
    if getattr(args, "normalize", None) is not None:
        s["normalize"] = True
        s["normalize_lufs"] = float(args.normalize)
    if getattr(args, "no_system_audio", False):
        s["system_audio"] = False
    return s


def apply_to_recorder(recorder, s: dict) -> None:
    """Push the effective settings into an (idle) AudioRecorder."""
    recorder.set_output_format(s["output_format"])
    recorder.set_output_directory(Path(s["output_dir"]) if s["output_dir"] else None)
    recorder.set_mix_mode(not s["dual_track"])
    recorder.set_auto_balance(s["auto_balance"])
    recorder.set_normalize_lufs(s["normalize_lufs"] if s["normalize"] else None)
    recorder.set_system_audio_enabled(s["system_audio"])


def effective_output_dir(s: dict) -> Path:
    return Path(s["output_dir"]) if s["output_dir"] else Path.home() / "Downloads"
