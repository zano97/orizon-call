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

from PyQt6.QtCore import QSettings, QStandardPaths

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
    "hide_from_screen_share": True,  # widget visible to me, not to who sees my screen
    "call_detection": "propose",     # off | propose | auto
    "call_detection_ignored": [],    # apps whose calls are never proposed
}

CALL_DETECTION_MODES = ("off", "propose", "auto")


def make_qsettings() -> QSettings:
    return QSettings(_ORG, _APP)


def default_downloads_dir() -> Path:
    """The user's real Downloads folder: Qt resolves the XDG user dir on
    Linux (~/Scaricati on an Italian desktop), the relocatable known folder
    on Windows (OneDrive, other drive) and ~/Downloads on macOS. Falls back
    to ~/Downloads if the platform reports nothing."""
    try:
        location = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DownloadLocation)
    except Exception:
        location = ""
    return Path(location) if location else Path.home() / "Downloads"


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


def _to_str_list(value) -> list:
    """QSettings hands lists back as None (empty, ini), a bare str (one
    item, ini) or a list."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v]
    return []


def load_settings(qs: Optional[QSettings] = None) -> dict:
    """Saved settings merged over DEFAULTS. Unknown/corrupt values fall
    back to their default instead of raising."""
    qs = qs or make_qsettings()
    s = dict(DEFAULTS)
    s["call_detection_ignored"] = []
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
        s["hide_from_screen_share"] = _to_bool(
            qs.value("hide_from_screen_share"), s["hide_from_screen_share"])
        mode = qs.value("call_detection", s["call_detection"])
        if mode in CALL_DETECTION_MODES:
            s["call_detection"] = mode
        s["call_detection_ignored"] = _to_str_list(qs.value("call_detection_ignored"))
    finally:
        qs.endGroup()
    return s


def save_settings(values: dict, qs: Optional[QSettings] = None) -> None:
    qs = qs or make_qsettings()
    qs.beginGroup(_GROUP)
    try:
        for key in DEFAULTS:
            if key in values:
                value = values[key]
                if isinstance(value, (list, tuple)):
                    value = list(value)
                qs.setValue(key, value)
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
    if getattr(args, "call_detection", None) in CALL_DETECTION_MODES:
        s["call_detection"] = args.call_detection
    if getattr(args, "show_in_screen_share", False):
        s["hide_from_screen_share"] = False
    return s


def apply_to_recorder(recorder, s: dict) -> None:
    """Push the effective settings into an (idle) AudioRecorder. The
    folder is always explicit (the platform Downloads folder when unset)
    so the recorder, the /files API and 'open folder' all agree."""
    recorder.set_output_format(s["output_format"])
    recorder.set_output_directory(effective_output_dir(s))
    recorder.set_mix_mode(not s["dual_track"])
    recorder.set_auto_balance(s["auto_balance"])
    recorder.set_normalize_lufs(s["normalize_lufs"] if s["normalize"] else None)
    recorder.set_system_audio_enabled(s["system_audio"])


def effective_output_dir(s: dict) -> Path:
    return Path(s["output_dir"]).expanduser() if s["output_dir"] else default_downloads_dir()
