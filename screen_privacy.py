"""
Keep Orizon Call's own windows out of screen sharing and screenshots.

The floating widget, its toasts, menus and dialogs stay visible on the
user's monitor, but are excluded from what other participants see when
the screen is shared in Meet/Zoom/Teams (and from screen recordings):

- Windows 10 2004+: ``SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)``,
  the window simply does not exist for capture APIs. Older builds fall
  back to ``WDA_MONITOR`` (the window is captured as a black rectangle:
  the content is still hidden).
- macOS: ``NSWindow.sharingType = NSWindowSharingNone``. Honoured by the
  legacy capture APIs; on macOS 15+ some apps that capture through
  ScreenCaptureKit may still show the window (Apple's choice, there is no
  public API to opt out there).
- Linux (X11 and Wayland): there is no protocol to hide a window from
  capture — the guard is a no-op and ``is_supported()`` returns False.

``ScreenShareGuard`` applies the exclusion to every top-level window the
application shows (Qt creates a fresh native window for each toast, menu
and dialog), so nothing slips through.
"""

import sys

from PyQt6.QtCore import QEvent, QObject
from PyQt6.QtWidgets import QApplication, QWidget

from orizon_logging import get_logger

log = get_logger("privacy")

# SetWindowDisplayAffinity values.
WDA_NONE = 0x00
WDA_MONITOR = 0x01
WDA_EXCLUDEFROMCAPTURE = 0x11

# NSWindowSharingType values.
NS_WINDOW_SHARING_NONE = 0
NS_WINDOW_SHARING_READ_ONLY = 1

# First Windows 10 build with WDA_EXCLUDEFROMCAPTURE (version 2004).
_WIN_EXCLUDE_MIN_BUILD = 19041


def _windows_build() -> int:
    try:
        return sys.getwindowsversion().build
    except Exception:
        return 0


def is_supported() -> bool:
    """Can this platform hide a window from screen capture at all?"""
    return sys.platform in ("win32", "darwin")


def unsupported_reason() -> str:
    if sys.platform.startswith("linux"):
        return ("Su Linux il sistema non permette di nascondere una finestra "
                "dalla condivisione dello schermo.")
    return ""


def _set_excluded_windows(hwnd: int, excluded: bool) -> bool:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
    user32.SetWindowDisplayAffinity.restype = wintypes.BOOL

    if not excluded:
        return bool(user32.SetWindowDisplayAffinity(hwnd, WDA_NONE))
    if _windows_build() >= _WIN_EXCLUDE_MIN_BUILD:
        if user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE):
            return True
        log.debug("WDA_EXCLUDEFROMCAPTURE failed (error %d), trying WDA_MONITOR",
                  ctypes.get_last_error())
    if user32.SetWindowDisplayAffinity(hwnd, WDA_MONITOR):
        return True
    log.debug("SetWindowDisplayAffinity failed (error %d)", ctypes.get_last_error())
    return False


def _set_excluded_macos(widget: QWidget, excluded: bool) -> bool:
    import objc
    view = objc.objc_object(c_void_p=int(widget.winId()))
    window = view.window()
    if window is None:
        return False
    window.setSharingType_(NS_WINDOW_SHARING_NONE if excluded
                           else NS_WINDOW_SHARING_READ_ONLY)
    return True


def set_window_excluded(widget: QWidget, excluded: bool) -> bool:
    """Exclude (or re-include) one top-level window from screen capture.
    Returns True when the platform accepted the change. Never raises."""
    if not is_supported() or widget is None or not widget.isWindow():
        return False
    try:
        if sys.platform == "win32":
            return _set_excluded_windows(int(widget.winId()), excluded)
        return _set_excluded_macos(widget, excluded)
    except Exception:
        log.debug("Could not change screen-capture visibility", exc_info=True)
        return False


class ScreenShareGuard(QObject):
    """Application-wide event filter: every top-level window is excluded
    from screen capture right when it is shown, while ``enabled``."""

    def __init__(self, app: QApplication, enabled: bool = True):
        super().__init__(app)
        self._app = app
        self._enabled = bool(enabled)
        self._warned = False
        if is_supported():
            app.installEventFilter(self)
        elif enabled:
            log.info("Hiding windows from screen sharing is not available on this platform.")

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        """Toggle at runtime: windows already on screen follow at once."""
        enabled = bool(enabled)
        if enabled == self._enabled:
            return
        self._enabled = enabled
        if not is_supported():
            return
        for w in self._app.topLevelWidgets():
            if w.isVisible():
                set_window_excluded(w, enabled)

    def eventFilter(self, obj, event) -> bool:
        if (self._enabled and event.type() == QEvent.Type.Show
                and isinstance(obj, QWidget) and obj.isWindow()):
            if not set_window_excluded(obj, True) and not self._warned:
                self._warned = True
                log.warning("Could not hide a window from screen sharing on this system.")
        return False
