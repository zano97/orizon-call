#!/usr/bin/env bash
# Compile the ScreenCaptureKit-based system-audio helper.
# Output: helpers/system_audio_capture (arm64 on Apple Silicon, x86_64 on Intel).
set -euo pipefail
cd "$(dirname "$0")"
swiftc -O -o system_audio_capture SystemAudioCapture.swift \
    -framework ScreenCaptureKit \
    -framework CoreMedia \
    -framework AVFoundation \
    -framework CoreGraphics
echo "Built: $(pwd)/system_audio_capture"
