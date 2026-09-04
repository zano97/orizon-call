#!/usr/bin/env python3
"""
Orizon Call - Floating Audio Recorder

A cross-platform floating widget that records both microphone input
and system audio output simultaneously.

Usage:
    python main.py
    python main.py --format flac
    python main.py --no-system-audio
    python main.py --output-dir /path/to/folder
    python main.py --api-port 19876
"""

import argparse
import json
import os
import socket
import sys
from pathlib import Path

from orizon_logging import setup_logging, get_logger, install_thread_excepthook

log = get_logger("main")


def _hide_dock_icon_macos() -> None:
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyAccessory
        )
    except Exception:
        pass


def _try_bind_or_explain(port: int) -> "socket.socket | None":
    """
    Attempt to bind a TCP socket on 127.0.0.1:<port>. If it fails with
    EADDRINUSE, probe the port: if it answers our /health endpoint we
    treat it as another Orizon Call instance; otherwise it's an unrelated
    service the user must move out of the way.

    Returns the bound (but not-yet-listening) socket on success, None on
    failure. Caller must close() the returned socket immediately before
    starting the real HTTP server.
    """
    import errno

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform != "win32":
            # Allow rebinding over TIME_WAIT remnants of a previous run
            # (instant app restart). Does not weaken the single-instance
            # check: binding over a LIVE listener still fails without
            # SO_REUSEPORT.
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Windows (Winsock): deliberately NO option. SO_REUSEADDR there
        # would let a second instance bind over the live listener, while
        # SO_EXCLUSIVEADDRUSE would refuse an instant restart while old
        # connections linger in TIME_WAIT. The default already rejects a
        # second listener and allows the restart.
        s.bind(("127.0.0.1", port))
        return s
    except OSError as e:
        s.close()
        if e.errno not in (errno.EADDRINUSE, errno.EACCES):
            log.error("Cannot bind 127.0.0.1:%d: %s", port, e)
            return None

    # Port is taken — figure out by whom.
    is_us = _probe_is_orizon_call(port)
    if is_us:
        log.error(
            "Another Orizon Call instance is already running on port %d. "
            "Use --api-port to pick a different port, or quit the other instance.",
            port,
        )
    else:
        log.error(
            "Port %d is in use by another service. Pick a different port with --api-port.",
            port,
        )
    return None


def _probe_is_orizon_call(port: int) -> bool:
    """Is the service on <port> another Orizon Call? Ask /health (HTTP/1.0:
    the server closes the connection after the reply) and read to EOF —
    the headers alone exceed 256 bytes, so a single short recv() would
    miss the body and misreport a running instance as a foreign service."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5) as conn:
            conn.sendall(b"GET /health HTTP/1.0\r\nHost: localhost\r\n\r\n")
            data = b""
            while len(data) < 4096:
                chunk = conn.recv(1024)
                if not chunk:
                    break
                data += chunk
    except (OSError, socket.timeout):
        return False
    head, _, body = data.partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n", 1)[0]
    if not status_line.startswith(b"HTTP/1.") or b" 200" not in status_line:
        return False
    try:
        return json.loads(body.decode("utf-8", errors="replace") or "null") == {"ok": True}
    except ValueError:
        return False


def _install_graceful_shutdown(app, widget, recorder) -> None:
    """
    Ctrl+C / SIGTERM / SIGHUP (terminal closed) / SIGBREAK become a normal
    "stop, save, quit" through the GUI: the file is finalized including
    MP3 conversion and normalization, the API server is shut down, Qt
    exits cleanly. A second signal while that is in progress does an
    emergency save and exits immediately.

    Python signal handlers only run while Python bytecode executes; the
    widget's 100 ms tick timer guarantees that inside the Qt event loop.
    """
    from PyQt6.QtCore import QTimer

    pending = {"count": 0}

    def on_signal(signum: int) -> None:
        pending["count"] += 1
        if pending["count"] == 1:
            log.info("Signal %s received: stopping the recording and quitting.", signum)
            QTimer.singleShot(0, widget.request_quit)
        else:
            log.warning("Second signal: emergency save and immediate exit.")
            recorder.emergency_save()
            os._exit(130)

    recorder.set_signal_callback(on_signal)
    app.aboutToQuit.connect(widget.shutdown)

    # OS logout / shutdown (Windows WM_QUERYENDSESSION, macOS, X11 session
    # managers): no time for a dialog — finalize the file right away.
    try:
        app.commitDataRequest.connect(lambda _manager: recorder.emergency_save())
    except Exception:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Orizon Call - Floating Audio Recorder"
    )
    parser.add_argument(
        "--no-system-audio",
        action="store_true",
        help="Record microphone only, skip system audio capture",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Override output directory (default: ~/Downloads)",
    )
    parser.add_argument(
        "--api-port",
        type=int,
        default=19876,
        help="Port for the local REST API (default: 19876)",
    )
    parser.add_argument(
        "--format",
        choices=["wav", "flac", "mp3"],
        default=None,
        help="Output audio format (default: the one chosen in the in-app "
             "settings, initially wav). MP3 uses the bundled ffmpeg.",
    )
    parser.add_argument("--verbose", action="store_true", help="DEBUG-level console logs.")
    parser.add_argument("--quiet", action="store_true", help="WARNING-level console logs.")
    parser.add_argument(
        "--dual-track",
        action="store_true",
        help="Output a stereo file with L=mic, R=system audio (instead of the "
             "default single combined mix where both channels carry both "
             "voices). Useful when feeding each speaker separately to "
             "transcription.",
    )
    parser.add_argument(
        "--no-auto-balance",
        action="store_true",
        help="Disable real-time per-source gain matching. Default: on — "
             "auto-balance keeps mic and system audio at similar loudness so "
             "neither overpowers the other in the mix.",
    )
    parser.add_argument(
        "--normalize",
        nargs="?",
        const=-16.0,
        type=float,
        default=None,
        metavar="LUFS",
        help="After stop, normalize the file to the given LUFS target "
             "(podcast=-16, streaming=-14). Default value when flag given "
             "without a number: -16. Uses the bundled ffmpeg; falls back to "
             "peak normalize if unavailable.",
    )
    parser.add_argument(
        "--preroll",
        type=float,
        default=0.0,
        help="Pre-roll buffer in seconds (default: 0 = off). Captures audio continuously while idle so the start of a recording isn't missed.",
    )
    parser.add_argument(
        "--cors-origin",
        default=None,
        help="Allowed Origin for the local API (default: only loopback). Pass the web app URL.",
    )
    parser.add_argument(
        "--no-auth",
        action="store_true",
        help="Disable token auth for the local API (NOT recommended).",
    )
    args = parser.parse_args()
    if not 1 <= args.api_port <= 65535:
        parser.error("--api-port must be between 1 and 65535")
    if args.preroll < 0:
        parser.error("--preroll must be >= 0")

    setup_logging(verbose=args.verbose, quiet=args.quiet)
    install_thread_excepthook()

    # Linux/Wayland: Wayland forbids clients from positioning, dragging
    # or stacking their own windows, which is everything a floating
    # always-on-top widget does. When XWayland is available prefer the
    # xcb backend (the widget then behaves exactly as on X11). Users can
    # still force a backend with QT_QPA_PLATFORM.
    if (sys.platform == "linux" and not os.environ.get("QT_QPA_PLATFORM")
            and os.environ.get("WAYLAND_DISPLAY") and os.environ.get("DISPLAY")):
        os.environ["QT_QPA_PLATFORM"] = "xcb"
        log.info("Wayland session with XWayland: using the xcb backend for the floating widget.")

    # PyQt6 aborts the whole process (qFatal) on unhandled Python exceptions
    # raised inside Qt slots. Log them instead: a failed button click must
    # never kill a recording in progress.
    def _excepthook(exc_type, exc_value, exc_tb):
        log.error("Unhandled exception", exc_info=(exc_type, exc_value, exc_tb))
    sys.excepthook = _excepthook

    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication

    # Single instance check (try-bind on the API port). The bound socket is
    # handed to the API server directly — no close/rebind race.
    bound_socket = _try_bind_or_explain(args.api_port)
    if bound_socket is None:
        sys.exit(1)

    app = QApplication(sys.argv)
    app.setApplicationName("Orizon Call")
    app.setApplicationDisplayName("Orizon Call")
    app.setOrganizationName("OrizonCall")
    if sys.platform == 'linux':
        # Lets Wayland/GNOME/KDE match the window and tray icon to the
        # orizon-call.desktop entry installed by install.sh.
        app.setDesktopFileName("orizon-call")
    app.setQuitOnLastWindowClosed(True)

    from floating_widget import app_icon
    icon = app_icon()
    if not icon.isNull():
        app.setWindowIcon(icon)

    if sys.platform == 'darwin':
        _hide_dock_icon_macos()

    # Create recorder and detect devices
    from audio_recorder import AudioRecorder
    from floating_widget import FloatingRecorderWidget

    recorder = AudioRecorder()

    # Saved in-app settings first, explicit CLI flags on top (one run only:
    # flags are never written back — the GUI settings stay as the user set
    # them from the widget's right-click menu → Impostazioni).
    import app_settings
    settings = app_settings.merge_cli_overrides(app_settings.load_settings(), args)
    app_settings.apply_to_recorder(recorder, settings)
    recorder.set_preroll_seconds(args.preroll)

    mic_ok, sys_ok, guidance = recorder.detect_devices()

    if not mic_ok:
        # Not fatal: with login autostart a USB/Bluetooth headset is often
        # enumerated seconds after the app. Devices are re-detected at
        # every recording start; just tell the user.
        log.warning("No microphone found at startup — will look again when a recording starts.")

    if args.preroll > 0:
        # Start streams continuously so the last N seconds are always buffered.
        # Note: this means the mic is "live" the whole time the app is open.
        log.info("Pre-roll enabled (%.1fs continuous capture).", args.preroll)
        recorder.enable_preroll_capture()

    # Create and show the floating widget
    widget = FloatingRecorderWidget(recorder)
    widget.recording_started.connect(
        lambda p: log.info("Recording started: %s", p)
    )
    widget.recording_stopped.connect(
        lambda p: log.info("Recording saved: %s", p)
    )

    if settings["system_audio"] and not sys_ok:
        log.info("System audio not available. Mic only.\n%s", guidance)

    _install_graceful_shutdown(app, widget, recorder)
    widget.show()
    if not mic_ok:
        QTimer.singleShot(500, lambda: widget.notify(
            "Nessun microfono trovato. Collegane uno: verrà cercato di nuovo "
            "all'avvio della registrazione.", kind="warn", duration_ms=8000))

    # Start local API server for web app integration, reusing the
    # already-bound probe socket (single-instance check without TOCTOU).
    from api_server import start_api_server, stop_api_server
    api_server = start_api_server(
        widget,
        port=args.api_port,
        output_dir=recorder.output_directory,
        cors_origin=args.cors_origin,
        require_auth=not args.no_auth,
        bound_socket=bound_socket,
    )

    exit_code = app.exec()
    stop_api_server(api_server)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
