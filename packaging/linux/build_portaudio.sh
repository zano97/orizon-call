#!/usr/bin/env bash
# Build PortAudio for the Linux package: ALSA only (no JACK, no OSS), so
# the bundled library depends on nothing but the host's libasound and libc.
# The app reaches PulseAudio/PipeWire through ALSA's pulse plugin, exactly
# as it does with the distribution's PortAudio.
#
# Usage: packaging/linux/build_portaudio.sh <output-dir>
# Needs: a C compiler, make, libasound2-dev (Debian/Ubuntu).
set -euo pipefail

OUT="${1:?usage: build_portaudio.sh <output-dir>}"
VERSION="v19.7.0"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

git clone --quiet --depth 1 --branch "$VERSION" https://github.com/PortAudio/portaudio.git "$WORK/src"
cd "$WORK/src"
./configure --quiet --without-jack --without-oss --without-asihpi --disable-static >/dev/null
make -j"$(nproc)" >/dev/null

mkdir -p "$OUT"
cp -L lib/.libs/libportaudio.so.2 "$OUT/libportaudio.so.2"
if ldd "$OUT/libportaudio.so.2" | grep -q -E 'libjack|not found'; then
    echo "unexpected dependencies:" >&2
    ldd "$OUT/libportaudio.so.2" >&2
    exit 1
fi
echo "$OUT/libportaudio.so.2"
