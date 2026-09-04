#!/usr/bin/env bash
# Avvio "tutto-in-uno" di Orizon Call.
#
#   ./start.sh                  → prepara l'ambiente (solo al primo avvio) e lancia l'app
#   ./start.sh --format flac    → ogni opzione viene passata all'app (vedi --help)
#
# Al primo avvio: crea il virtualenv .venv, installa le dipendenze e
# compila l'helper audio di sistema (macOS). Dai successivi parte subito.
set -euo pipefail
cd "$(dirname "$0")"

PY=python3
if ! command -v "$PY" >/dev/null 2>&1; then
    if [ "$(uname)" = "Darwin" ]; then
        echo "✗ Python 3 non trovato. Installalo con: brew install python3" >&2
    else
        echo "✗ Python 3 non trovato. Installalo con il gestore pacchetti (es. sudo apt install python3 python3-venv)." >&2
    fi
    exit 1
fi

if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "✗ Serve Python 3.10 o superiore (trovato: $("$PY" --version))." >&2
    exit 1
fi

VENV=.venv
STAMP="$VENV/.deps-ok"
if [ ! -x "$VENV/bin/python" ]; then
    echo "• Primo avvio: creo l'ambiente Python (.venv)…"
    # --clear: un venv rimasto orfano (Python di sistema aggiornato) viene
    # ricreato da zero, senza lasciare il vecchio timbro .deps-ok.
    "$PY" -m venv --clear "$VENV"
    rm -f "$STAMP"
fi

# Il timbro vale solo se i pacchetti sono davvero installati (controllo
# senza importarli: sounddevice fallirebbe senza PortAudio, che è un altro
# problema, segnalato più sotto, e non deve far reinstallare tutto).
if [ -f "$STAMP" ] && ! "$VENV/bin/python" -c 'import importlib.util as u, sys; sys.exit(0 if all(u.find_spec(m) for m in ("PyQt6", "sounddevice", "soundfile", "soxr", "numpy")) else 1)' >/dev/null 2>&1; then
    rm -f "$STAMP"
fi

# Reinstalla le dipendenze solo se requirements.txt è cambiato.
if command -v shasum >/dev/null 2>&1; then
    REQ_HASH=$(shasum -a 256 requirements.txt | cut -d' ' -f1)
else
    REQ_HASH=$(sha256sum requirements.txt | cut -d' ' -f1)
fi
if [ ! -f "$STAMP" ] || [ "$(cat "$STAMP" 2>/dev/null)" != "$REQ_HASH" ]; then
    echo "• Installo le dipendenze (può richiedere qualche minuto la prima volta)…"
    "$VENV/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade pip
    "$VENV/bin/python" -m pip install --quiet --disable-pip-version-check -r requirements.txt
    echo "$REQ_HASH" > "$STAMP"
fi

# ldconfig sta in /sbin, che su Debian non è nel PATH degli utenti normali.
LDCONFIG="$(command -v ldconfig 2>/dev/null || echo /sbin/ldconfig)"

# Linux: PortAudio è richiesto da sounddevice per la cattura audio.
if [ "$(uname)" = "Linux" ] && ! "$LDCONFIG" -p 2>/dev/null | grep -q libportaudio; then
    echo "⚠ Libreria PortAudio non trovata: la registrazione non funzionerà." >&2
    echo "  Installala con: sudo apt install libportaudio2   (Debian/Ubuntu)" >&2
fi
# Linux: il plugin grafico di Qt 6 richiede libxcb-cursor0 (e libEGL).
if [ "$(uname)" = "Linux" ] && ! "$LDCONFIG" -p 2>/dev/null | grep -q libxcb-cursor.so.0; then
    echo "⚠ Libreria libxcb-cursor0 non trovata: il widget potrebbe non avviarsi." >&2
    echo "  Installala con: sudo apt install libxcb-cursor0 libegl1 libxkbcommon-x11-0   (Debian/Ubuntu)" >&2
fi

# Helper audio di sistema (macOS): compila se manca o se il binario nel repo
# (Apple Silicon) non è per questa architettura (Mac Intel). lipo/swiftc sono
# shim che senza Xcode CLT aprono un prompt: si usa `file` e si compila solo
# con i CLT davvero installati.
_helper_ok() {
    [ -x helpers/system_audio_capture ] || return 1
    file helpers/system_audio_capture 2>/dev/null | grep -q "$(uname -m)" || return 1
    return 0
}
if [ "$(uname)" = "Darwin" ] && ! _helper_ok; then
    if xcode-select -p >/dev/null 2>&1 && command -v swiftc >/dev/null 2>&1; then
        echo "• Compilo l'helper per l'audio di sistema…"
        (cd helpers && ./build.sh)
    else
        echo "⚠ swiftc non trovato: l'audio di sistema non sarà disponibile (solo microfono)." >&2
        echo "  Per abilitarlo: xcode-select --install   e poi rilancia questo script." >&2
    fi
fi

echo "• Avvio Orizon Call… (widget in basso a destra; tasto destro per il menu)"
exec "$VENV/bin/python" main.py "$@"
