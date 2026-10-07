"""Screen-share privacy: the guard excludes every top-level window when it
is shown, follows runtime toggles, and is a no-op where unsupported."""

import pytest
from PyQt6.QtWidgets import QApplication, QWidget

import screen_privacy


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def calls(monkeypatch):
    log = []
    monkeypatch.setattr(screen_privacy, "is_supported", lambda: True)
    monkeypatch.setattr(screen_privacy, "set_window_excluded",
                        lambda w, excluded: log.append((w, excluded)) or True)
    return log


def test_windows_excluded_when_shown(qapp, calls):
    guard = screen_privacy.ScreenShareGuard(qapp, enabled=True)
    try:
        top = QWidget()
        child = QWidget(top)
        top.show()
        assert (top, True) in calls
        assert all(w is not child for w, _ in calls)
        top.close()
    finally:
        qapp.removeEventFilter(guard)


def test_disabled_guard_leaves_windows_alone(qapp, calls):
    guard = screen_privacy.ScreenShareGuard(qapp, enabled=False)
    try:
        top = QWidget()
        top.show()
        assert not any(w is top for w, _ in calls)
        # Turning it on applies to the windows already visible.
        guard.set_enabled(True)
        assert (top, True) in calls
        guard.set_enabled(False)
        assert (top, False) in calls
        top.close()
    finally:
        qapp.removeEventFilter(guard)


def test_unsupported_platform_is_noop(qapp, monkeypatch):
    monkeypatch.setattr(screen_privacy, "is_supported", lambda: False)
    w = QWidget()
    assert screen_privacy.set_window_excluded(w, True) is False
    guard = screen_privacy.ScreenShareGuard(qapp, enabled=True)
    guard.set_enabled(False)
    guard.set_enabled(True)   # must not raise


@pytest.mark.skipif(not screen_privacy.is_supported(), reason="Windows/macOS only")
def test_real_call_never_raises(qapp):
    w = QWidget()
    w.show()
    screen_privacy.set_window_excluded(w, True)
    screen_privacy.set_window_excluded(w, False)
    w.close()


def test_no_native_calls_without_native_platform_plugin(qapp, monkeypatch):
    """Under the offscreen plugin (tests, CI) winId() is not an NSView*/
    HWND: dereferencing it crashed the macOS test run. Nothing native may
    be touched there."""
    assert QApplication.platformName() not in ("windows", "cocoa")
    monkeypatch.setattr(screen_privacy, "is_supported", lambda: True)
    touched = []
    monkeypatch.setattr(screen_privacy, "_set_excluded_macos", lambda *a: touched.append(a))
    monkeypatch.setattr(screen_privacy, "_set_excluded_windows", lambda *a: touched.append(a))
    w = QWidget()
    assert screen_privacy.set_window_excluded(w, True) is False
    assert not touched

    import floating_widget
    monkeypatch.setattr(floating_widget.sys, "platform", "darwin")
    called = []
    monkeypatch.setattr(w, "winId", lambda: called.append(1) or 0)
    floating_widget.apply_macos_floating(w)
    assert not called
