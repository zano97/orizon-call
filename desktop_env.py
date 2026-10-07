"""
Desktop integration details that differ between a source checkout and the
packaged app (PyInstaller bundle / .app / AppImage).

- ``clean_child_env()``: environment for processes we start that are not
  part of the bundle (file manager, update helper, the relaunched app).
  PyInstaller's bootloader and Qt hooks export variables (``_PYI_*``,
  ``QT_PLUGIN_PATH``…) that would make another program load this
  bundle's libraries, or make a freshly started copy of the app believe
  it is a child of this one.
- ``open_path()``: "open the recordings folder" without leaking that
  environment into the file manager.
- Linux AppImage: menu entry + icon pointing at the AppImage, refreshed
  when the file moves, so the app is in the applications menu like any
  installed program.
- macOS: detect an app started from the disk image or from a quarantined
  download (App Translocation) and move it to Applications — updates can
  only replace an app that lives in a writable, stable place.
"""

import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from orizon_logging import get_logger

log = get_logger("desktop")

FROZEN = bool(getattr(sys, "frozen", False))

_PYI_PREFIXES = ("_PYI_", "_MEIPASS")
_BUNDLE_VARS = ("QT_PLUGIN_PATH", "QML2_IMPORT_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH",
                "QTWEBENGINEPROCESS_PATH", "PYTHONHOME", "PYTHONPATH")


def bundle_dir() -> Optional[Path]:
    return Path(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))) if FROZEN else None


def clean_child_env(env: Optional[dict] = None) -> dict:
    """A copy of ``env`` (default: ours) without the variables that tie a
    child process to this bundle. A no-op when running from source."""
    env = dict(os.environ if env is None else env)
    if not FROZEN:
        return env
    bundle = str(bundle_dir() or "")
    for key in list(env):
        if key.startswith(_PYI_PREFIXES):
            env.pop(key, None)
        elif key in _BUNDLE_VARS and (not bundle or bundle in env[key]):
            env.pop(key, None)
    # PyInstaller >= 6.9: a program started from here is a new instance,
    # not a sub-process sharing our unpacked files.
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def open_path(path) -> bool:
    """Open a folder/file with the desktop's default application."""
    path = str(path)
    if FROZEN and sys.platform.startswith("linux"):
        try:
            subprocess.Popen(["xdg-open", path], env=clean_child_env(), start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return True
        except OSError:
            log.debug("xdg-open failed", exc_info=True)
    from PyQt6.QtCore import QUrl
    from PyQt6.QtGui import QDesktopServices
    return QDesktopServices.openUrl(QUrl.fromLocalFile(path))


def open_url(url: str) -> bool:
    if FROZEN and sys.platform.startswith("linux"):
        try:
            subprocess.Popen(["xdg-open", url], env=clean_child_env(), start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return True
        except OSError:
            pass
    from PyQt6.QtCore import QUrl
    from PyQt6.QtGui import QDesktopServices
    return QDesktopServices.openUrl(QUrl(url))


# ---------- Linux AppImage ----------

def appimage_path() -> Optional[Path]:
    path = os.environ.get("APPIMAGE")
    if FROZEN and sys.platform.startswith("linux") and path and os.path.isfile(path):
        return Path(path)
    return None


_EXEC_RESERVED = set(' \t\n"\'\\><~|&;$*?#()`')


def desktop_exec_quote(arg: str) -> str:
    """Quote one Exec= argument per the Desktop Entry spec: double quotes,
    with ", `, $ and \\ backslash-escaped — then every backslash doubled,
    because the whole value is also an escaped string."""
    if arg and not (_EXEC_RESERVED & set(arg)):
        return arg
    inner = "".join("\\" + c if c in '"`$\\' else c for c in arg)
    return ('"' + inner + '"').replace("\\", "\\\\")


def desktop_entry_text(appimage: Path) -> str:
    exe = desktop_exec_quote(str(appimage))
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Orizon Call\n"
        "Comment=Registratore di chiamate (microfono + audio di sistema)\n"
        f"Exec={exe}\n"
        "Icon=orizon-call\n"
        "Terminal=false\n"
        "Categories=AudioVideo;Audio;Recorder;\n"
        "StartupWMClass=orizon-call\n"
        "X-AppImage-Integrated=true\n"
    )


def ensure_linux_desktop_entry(appimage: Optional[Path] = None,
                               data_home: Optional[Path] = None) -> bool:
    """Write/refresh ~/.local/share/applications/orizon-call.desktop (and
    the icon) for the running AppImage. Returns True when it changed."""
    appimage = appimage or appimage_path()
    if appimage is None:
        return False
    data_home = data_home or Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    entry = data_home / "applications" / "orizon-call.desktop"
    text = desktop_entry_text(appimage)
    try:
        if entry.is_file() and entry.read_text(encoding="utf-8") == text:
            return False
        entry.parent.mkdir(parents=True, exist_ok=True)
        icon_src = Path(__file__).resolve().parent / "assets" / "icons" / "orizon-call-256.png"
        icon_dst = data_home / "icons" / "hicolor" / "256x256" / "apps" / "orizon-call.png"
        if icon_src.is_file():
            icon_dst.parent.mkdir(parents=True, exist_ok=True)
            icon_dst.write_bytes(icon_src.read_bytes())
        tmp = entry.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, entry)
        log.info("Menu entry created for %s", appimage)
        return True
    except OSError:
        log.debug("Cannot write the desktop entry", exc_info=True)
        return False


# ---------- macOS: where the .app lives ----------

def macos_bundle_path() -> Optional[Path]:
    """/Applications/Orizon Call.app when running from a packaged bundle."""
    if not (FROZEN and sys.platform == "darwin"):
        return None
    exe = Path(sys.executable).resolve()
    for parent in exe.parents:
        if parent.suffix == ".app":
            return parent
    return None


def macos_location_problem(bundle: Optional[Path]) -> Optional[str]:
    """Why this copy cannot be updated in place: 'dmg' (started from the
    disk image), 'translocated' (quarantined download run from a random
    read-only path) or 'readonly'. None = fine."""
    if bundle is None:
        return None
    text = str(bundle)
    if "/AppTranslocation/" in text:
        return "translocated"
    if text.startswith("/Volumes/"):
        return "dmg"
    if not os.access(bundle.parent, os.W_OK) or not os.access(bundle, os.W_OK):
        return "readonly"
    return None


def macos_applications_target(name: str = "Orizon Call.app") -> Path:
    system = Path("/Applications")
    if os.access(system, os.W_OK):
        return system / name
    return Path.home() / "Applications" / name


def build_move_helper(pid: int, src: Path, dest: Path) -> str:
    q = shlex.quote
    return f"""#!/bin/bash
# Orizon Call: move the app to Applications (temporary file).
rm -f "$0"
while kill -0 {pid} 2>/dev/null; do sleep 0.3; done
mkdir -p {q(str(dest.parent))}
rm -rf {q(str(dest))}.tmp-move
if /usr/bin/ditto {q(str(src))} {q(str(dest))}.tmp-move; then
    rm -rf {q(str(dest))}
    mv {q(str(dest))}.tmp-move {q(str(dest))}
    /usr/bin/xattr -dr com.apple.quarantine {q(str(dest))} 2>/dev/null
    /usr/bin/open {q(str(dest))}
else
    rm -rf {q(str(dest))}.tmp-move
    /usr/bin/open {q(str(src))}
fi
"""


def launch_move_to_applications(bundle: Path) -> Path:
    """Copy the running bundle to Applications once we have quit, then
    open the copy. Returns the destination."""
    dest = macos_applications_target(bundle.name)
    fd, path = tempfile.mkstemp(prefix="orizon-move-", suffix=".sh")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(build_move_helper(os.getpid(), bundle, dest))
    os.chmod(path, 0o700)
    subprocess.Popen(["/bin/bash", path], env=clean_child_env(), start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True)
    return dest
