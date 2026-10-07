"""
``orizon-call --self-test``: check that this copy of the app has everything
it needs, without opening the widget, the API port or any audio device.

The release workflow runs it on every packaged build (macOS, Windows,
Linux) so a library PyInstaller failed to bundle — PortAudio, libsndfile,
ffmpeg, the Qt SVG plugin, the macOS helper — fails the release instead
of reaching users. Also handy for support: the report lists versions and
paths.
"""

import os
import platform
import sys
import tempfile
import time
import traceback
from pathlib import Path


_APP = None


def _offscreen_available() -> bool:
    from PyQt6.QtCore import QLibraryInfo
    plugins = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath)) / "platforms"
    return plugins.is_dir() and any("offscreen" in p.name for p in plugins.iterdir())


def _check_qt(report) -> None:
    # Headless when possible (CI, no display); otherwise the native
    # platform plugin, which is what users run anyway.
    if not os.environ.get("QT_QPA_PLATFORM") and _offscreen_available():
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PyQt6.QtCore import QT_VERSION_STR
    from PyQt6.QtWidgets import QApplication
    global _APP
    # Module-level reference: a QApplication collected while Qt objects
    # still exist aborts the process.
    app = _APP = QApplication.instance() or QApplication(["orizon-call-self-test"])
    from floating_widget import _get_logo_renderer, app_icon
    if app_icon().isNull():
        raise RuntimeError("app icon not found in the bundle")
    if _get_logo_renderer() is None:
        raise RuntimeError("SVG logo cannot be rendered (QtSvg missing?)")
    report(f"Qt {QT_VERSION_STR}, platform plugin {app.platformName()}")


def _check_portaudio(report) -> None:
    import sounddevice as sd
    devices = sd.query_devices()
    report(f"{sd.get_portaudio_version()[1]} — {len(devices)} device(s)")


def _check_soundfile(report) -> None:
    import numpy as np
    import soundfile as sf
    import soxr
    tone = (0.2 * np.sin(np.linspace(0, 440 * 2 * np.pi, 48000))).astype("float32")
    resampled = soxr.resample(tone, 48000, 44100)
    with tempfile.TemporaryDirectory() as tmp:
        for ext in ("wav", "flac"):
            path = Path(tmp) / f"t.{ext}"
            sf.write(str(path), resampled, 44100)
            data, rate = sf.read(str(path), dtype="float32")
            if rate != 44100 or len(data) != len(resampled):
                raise RuntimeError(f"{ext} round trip mismatch")
    report(f"libsndfile {sf.__libsndfile_version__}, soxr {soxr.__version__}")


def _check_ffmpeg(report) -> None:
    import numpy as np
    import soundfile as sf
    import imageio_ffmpeg
    from audio_recorder import _run_ffmpeg
    # The bundled binary specifically: a system ffmpeg on the build
    # machine's PATH must not hide a missing one.
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    if not exe or not os.path.isfile(exe):
        raise RuntimeError("bundled ffmpeg not found (imageio-ffmpeg binary not collected?)")
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "t.wav"
        mp3 = Path(tmp) / "t.mp3"
        sf.write(str(wav), np.zeros((4800, 2), dtype="float32"), 48000)
        result = _run_ffmpeg(exe, ["-i", str(wav), "-codec:a", "libmp3lame", str(mp3)], timeout=60)
        if result.returncode != 0 or not mp3.exists() or mp3.stat().st_size == 0:
            raise RuntimeError(f"MP3 encode failed: {result.stderr[-300:]!r}")
        report(f"MP3 encode OK ({mp3.stat().st_size} bytes)")
    report(exe)


def _check_platform(report) -> None:
    if sys.platform == "darwin":
        import AppKit  # noqa: F401
        from macos_system_audio import _helper_binary_path, binary_matches_host
        helper = _helper_binary_path()
        if not helper.exists():
            raise RuntimeError(f"system-audio helper missing: {helper}")
        if not os.access(helper, os.X_OK):
            raise RuntimeError(f"system-audio helper not executable: {helper}")
        if not binary_matches_host(helper):
            raise RuntimeError(f"system-audio helper not built for {platform.machine()}")
        report(f"helper {helper}")
    elif sys.platform == "win32":
        import pyaudiowpatch  # noqa: F401
        import ctypes
        ctypes.WinDLL("user32").SetWindowDisplayAffinity  # screen-share privacy API
        report("WASAPI loopback (PyAudioWPatch) available")
    else:
        import importlib.util
        if importlib.util.find_spec("pulsectl") is None:
            raise RuntimeError("pulsectl not bundled")
        try:
            import pulsectl  # noqa: F401
            report("pulsectl available")
        except OSError as e:
            # The package is there; the host lacks libpulse (pure ALSA
            # system): the app still records, without PulseAudio extras.
            report(f"pulsectl bundled, libpulse not on this system ({e})")


def _check_app_modules(report) -> None:
    import api_server  # noqa: F401
    import app_settings  # noqa: F401
    import call_detection  # noqa: F401
    import screen_privacy  # noqa: F401
    import settings_dialog  # noqa: F401
    import updater
    report(f"install kind: {updater.install_kind()}")


CHECKS = [
    ("Moduli dell'app", _check_app_modules),
    ("Interfaccia (Qt, icone, logo SVG)", _check_qt),
    ("PortAudio (microfono)", _check_portaudio),
    ("libsndfile + soxr (WAV/FLAC, ricampionamento)", _check_soundfile),
    ("ffmpeg (MP3, normalizzazione)", _check_ffmpeg),
    ("Componenti di sistema", _check_platform),
]


def run(report_path=None) -> int:
    from version import __version__
    lines = [f"Orizon Call {__version__} — self-test",
             f"{platform.platform()} ({platform.machine()}), Python {platform.python_version()}",
             f"frozen={getattr(sys, 'frozen', False)} executable={sys.executable}", ""]
    failures = 0
    for name, check in CHECKS:
        details = []
        start = time.monotonic()
        try:
            check(details.append)
            status = "OK  "
        except Exception as e:
            failures += 1
            status = "FAIL"
            details.append(f"{type(e).__name__}: {e}")
            details.append(traceback.format_exc().strip().splitlines()[-1])
        lines.append(f"[{status}] {name} ({time.monotonic() - start:.1f}s)")
        lines.extend(f"         {d}" for d in details)
    lines.append("")
    lines.append("RESULT: " + ("OK" if not failures else f"{failures} check(s) failed"))
    text = "\n".join(lines) + "\n"
    if report_path:
        Path(report_path).write_text(text, encoding="utf-8")
    if sys.stdout is not None:
        try:
            sys.stdout.write(text)
            sys.stdout.flush()
        except Exception:
            pass
    return 0 if not failures else 1
