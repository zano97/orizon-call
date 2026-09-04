#!/usr/bin/env python3
"""
Diagnostic tool for the macOS ScreenCaptureKit system-audio helper.

Spawns helpers/system_audio_capture, prints every STATUS line from its
stderr (including the periodic 'heartbeat' lines), and measures the audio
levels of the PCM data coming back. Use this to confirm whether the
helper is actually capturing system audio.

How to read the output
----------------------
- 'STATUS ready ...'           → permission OK, stream started
- 'STATUS source_format ...'   → first audio buffer received, with the
                                 device's native format
- 'STATUS heartbeat buffers=N bytes=M peak=P' (every 5 s)
      buffers > 0  → SCK is delivering audio buffers
      peak > 0.001 → there is actual audible signal in the buffers
      peak ≈ 0     → buffers contain silence (audio is routed somewhere
                     SCK can't see — bug, or app is muted, or you simply
                     have nothing playing)
- 'STATUS error ...'           → fatal: read the message and fix.

Usage
-----
    python3 diag_sck.py              # runs for 20 seconds
    python3 diag_sck.py --seconds 60

While it's running, play any audio (YouTube, Music, a call). The
heartbeat should show non-zero peak.
"""

import argparse
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np


HELPER = Path(__file__).resolve().parent / "helpers" / "system_audio_capture"
BYTES_PER_SAMPLE = 4   # float32
CHANNELS = 2


def main() -> int:
    p = argparse.ArgumentParser(description="Diagnose SCK audio capture.")
    p.add_argument("--seconds", type=int, default=20,
                   help="How long to run before stopping (default: 20).")
    args = p.parse_args()

    if not HELPER.exists():
        print(f"[diag] Helper binary not found: {HELPER}", file=sys.stderr)
        print("[diag] Build it with: cd helpers && ./build.sh", file=sys.stderr)
        return 2

    print(f"[diag] Spawning {HELPER.name} for {args.seconds} s.")
    print("[diag] Play some audio (YouTube, Music, a call) while this runs.\n")

    proc = subprocess.Popen(
        [str(HELPER)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        bufsize=0,
    )

    stats = {"bytes": 0, "peak": 0.0, "buffers": 0}
    stats_lock = threading.Lock()

    def stderr_reader():
        if proc.stderr is None:
            return
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                print(f"  helper> {line}")

    def stdout_reader():
        if proc.stdout is None:
            return
        frame_size = CHANNELS * BYTES_PER_SAMPLE
        chunk_size = 1024 * frame_size
        while True:
            chunk = proc.stdout.read(chunk_size)
            if not chunk:
                break
            usable = (len(chunk) // frame_size) * frame_size
            if usable == 0:
                continue
            samples = np.frombuffer(chunk[:usable], dtype=np.float32)
            peak = float(np.max(np.abs(samples))) if samples.size else 0.0
            with stats_lock:
                stats["bytes"] += usable
                stats["buffers"] += 1
                if peak > stats["peak"]:
                    stats["peak"] = peak

    t_err = threading.Thread(target=stderr_reader, daemon=True)
    t_out = threading.Thread(target=stdout_reader, daemon=True)
    t_err.start()
    t_out.start()

    try:
        start = time.monotonic()
        last_report = start
        while time.monotonic() - start < args.seconds:
            time.sleep(1)
            now = time.monotonic()
            if now - last_report >= 5.0:
                last_report = now
                with stats_lock:
                    print(
                        f"[diag] {int(now - start):3d}s in: "
                        f"buffers_from_stdout={stats['buffers']:>5} "
                        f"bytes={stats['bytes']:>9} "
                        f"peak_seen={stats['peak']:.4f}"
                    )
                    stats["peak"] = 0.0  # reset window peak
    except KeyboardInterrupt:
        pass
    finally:
        try:
            proc.send_signal(signal.SIGINT)
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                proc.terminate()
                proc.wait(timeout=1)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    proc.kill()
                except OSError:
                    pass

    print()
    print("[diag] Helper exited.")
    print()
    print("Interpretation:")
    print("  - bytes > 0 and peak > 0.001 over time:")
    print("      System audio is being captured correctly. If the recorder")
    print("      still saves a mic-only file, the bug is in the Python side.")
    print("  - bytes > 0 but peak always ~0:")
    print("      SCK is delivering buffers but they're silent. Common causes:")
    print("      (1) Nothing is actually playing on the system right now;")
    print("      (2) The audio source uses a routing path SCK can't see")
    print("          (rare — Zoom/Meet/Teams normally do route through the mixer);")
    print("      (3) System output is muted.")
    print("  - bytes == 0 after seeing STATUS ready:")
    print("      SCK started but is delivering nothing. Likely cause:")
    print("      the macOS version handles SCK audio differently. Try the")
    print("      latest helper (the .swift file was patched to register a")
    print("      video output too — some SCK builds need that).")
    print("  - 'STATUS error start_failed ... user denied':")
    print("      Permission missing. Open System Settings → Privacy & Security")
    print("      → Screen Recording, enable the app that ran this script,")
    print("      then re-run after restarting that app.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
