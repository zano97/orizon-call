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
import socket
import sys
from pathlib import Path

from orizon_logging import setup_logging, get_logger

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
    # Allow rebinding over TIME_WAIT remnants of a previous run (instant
    # app restart). Does not weaken the single-instance check: binding
    # over a LIVE listener still fails without SO_REUSEPORT.
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
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
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5) as conn:
            conn.sendall(b"GET /health HTTP/1.0\r\nHost: localhost\r\n\r\n")
            data = conn.recv(256)
        return b"200" in data and b"ok" in data
    except (OSError, socket.timeout):
        return False


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
        default="wav",
        help="Output audio format (default: wav). MP3 requires ffmpeg.",
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
             "without a number: -16. Requires ffmpeg; falls back to peak "
             "normalize otherwise.",
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

    setup_logging(verbose=args.verbose, quiet=args.quiet)

    # PyQt6 aborts the whole process (qFatal) on unhandled Python exceptions
    # raised inside Qt slots. Log them instead: a failed button click must
    # never kill a recording in progress.
    def _excepthook(exc_type, exc_value, exc_tb):
        log.error("Unhandled exception", exc_info=(exc_type, exc_value, exc_tb))
    sys.excepthook = _excepthook

    from PyQt6.QtWidgets import QApplication, QMessageBox

    # Single instance check (try-bind on the API port). The bound socket is
    # handed to the API server directly — no close/rebind race.
    bound_socket = _try_bind_or_explain(args.api_port)
    if bound_socket is None:
        sys.exit(1)

    app = QApplication(sys.argv)
    app.setApplicationName("Orizon Call")
    app.setQuitOnLastWindowClosed(True)

    icon_path = Path(__file__).resolve().parent / "assets" / "icons" / "orizon-call-256.png"
    if icon_path.exists():
        from PyQt6.QtGui import QIcon
        app.setWindowIcon(QIcon(str(icon_path)))

    if sys.platform == 'darwin':
        _hide_dock_icon_macos()

    # Create recorder and detect devices
    from audio_recorder import AudioRecorder
    from floating_widget import FloatingRecorderWidget

    recorder = AudioRecorder()

    if args.output_dir:
        recorder.set_output_directory(args.output_dir)

    recorder.set_output_format(args.format)
    # Default: combined mix. --dual-track opts into L=mic / R=sys layout.
    recorder.set_mix_mode(not args.dual_track)
    recorder.set_preroll_seconds(args.preroll)
    recorder.set_auto_balance(not args.no_auto_balance)
    recorder.set_normalize_lufs(args.normalize)
    recorder.set_system_audio_enabled(not args.no_system_audio)

    mic_ok, sys_ok, guidance = recorder.detect_devices()

    if not mic_ok:
        QMessageBox.critical(
            None,
            "Orizon Call — Errore",
            "Nessun microfono trovato.\n\n"
            "Collega un microfono e riavvia l'applicazione.",
        )
        sys.exit(1)

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

    if not args.no_system_audio and not sys_ok:
        log.info("System audio not available. Mic only.\n%s", guidance)

    widget.show()

    # Start local API server for web app integration, reusing the
    # already-bound probe socket (single-instance check without TOCTOU).
    from api_server import start_api_server
    api_server = start_api_server(
        widget,
        port=args.api_port,
        output_dir=args.output_dir,
        cors_origin=args.cors_origin,
        require_auth=not args.no_auth,
        bound_socket=bound_socket,
    )

    exit_code = app.exec()
    if api_server:
        api_server.shutdown()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
