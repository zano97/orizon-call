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
    echo "✗ Python 3 non trovato. Installalo con: brew install python3" >&2
    exit 1
fi

if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "✗ Serve Python 3.10 o superiore (trovato: $("$PY" --version))." >&2
    exit 1
fi

VENV=.venv
if [ ! -x "$VENV/bin/python" ]; then
    echo "• Primo avvio: creo l'ambiente Python (.venv)…"
    "$PY" -m venv "$VENV"
fi

# Reinstalla le dipendenze solo se requirements.txt è cambiato.
STAMP="$VENV/.deps-ok"
REQ_HASH=$(shasum -a 256 requirements.txt | cut -d' ' -f1)
if [ ! -f "$STAMP" ] || [ "$(cat "$STAMP" 2>/dev/null)" != "$REQ_HASH" ]; then
    echo "• Installo le dipendenze (può richiedere qualche minuto la prima volta)…"
    "$VENV/bin/pip" install --quiet --upgrade pip
    "$VENV/bin/pip" install --quiet -r requirements.txt
    echo "$REQ_HASH" > "$STAMP"
fi

# Helper audio di sistema (macOS): compila solo se manca.
if [ "$(uname)" = "Darwin" ] && [ ! -x helpers/system_audio_capture ]; then
    if command -v swiftc >/dev/null 2>&1; then
        echo "• Compilo l'helper per l'audio di sistema…"
        (cd helpers && ./build.sh)
    else
        echo "⚠ swiftc non trovato: l'audio di sistema non sarà disponibile (solo microfono)." >&2
        echo "  Per abilitarlo: xcode-select --install   e poi rilancia questo script." >&2
    fi
fi

echo "• Avvio Orizon Call… (widget in basso a destra; tasto destro per il menu)"
exec "$VENV/bin/python" main.py "$@"
