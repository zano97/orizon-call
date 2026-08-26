#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  Orizon Call — installer per macOS e Linux
#
#  Installazione con un solo comando:
#
#    curl -fsSL https://raw.githubusercontent.com/zano97/orizon-call/master/install.sh | bash
#
#  Cosa fa:
#    1. Controlla i prerequisiti (Python ≥ 3.10, git; su Linux PortAudio)
#       e dove possibile li installa da solo.
#    2. Scarica l'app da GitHub in ~/.orizon-call/app (o la aggiorna).
#    3. Crea un ambiente Python isolato in ~/.orizon-call/venv con tutte
#       le dipendenze: non tocca il Python di sistema.
#    4. Installa il comando `orizon-call` in ~/.local/bin, una app
#       "Orizon Call" doppio-cliccabile (macOS: ~/Applications,
#       Linux: menu applicazioni).
#
#  Altri usi:
#    install.sh --uninstall      rimuove tutto (le registrazioni restano)
#    ORIZON_CALL_REF=<branch>    installa da un branch diverso da master
#
#  Per aggiornare basta rilanciare lo stesso comando: `orizon-call update`
#  fa la stessa cosa.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO="${ORIZON_CALL_REPO:-zano97/orizon-call}"
REF="${ORIZON_CALL_REF:-master}"
BASE_DIR="$HOME/.orizon-call"
APP_DIR="$BASE_DIR/app"
VENV_DIR="$BASE_DIR/venv"
BIN_DIR="$HOME/.local/bin"
LAUNCHER="$BIN_DIR/orizon-call"

OS="$(uname -s)"

say()  { printf '\033[1;32m•\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m⚠\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; exit 1; }

# Chiede conferma sul terminale anche quando lo script arriva via pipe
# (curl … | bash). Senza terminale risponde "no" senza bloccarsi.
ask() {
    local prompt="$1" reply=""
    if [ -r /dev/tty ]; then
        printf '%s [s/N] ' "$prompt" > /dev/tty
        read -r reply < /dev/tty || true
    fi
    case "$reply" in [sSyY]*) return 0 ;; *) return 1 ;; esac
}

# ── Disinstallazione ─────────────────────────────────────────────────────────

uninstall() {
    say "Rimuovo Orizon Call…"
    rm -rf "$BASE_DIR/app" "$BASE_DIR/venv"
    rm -f "$LAUNCHER"
    rm -rf "$HOME/Applications/Orizon Call.app"
    rm -f "$HOME/.local/share/applications/orizon-call.desktop"
    rm -f "$HOME/.local/share/icons/hicolor/256x256/apps/orizon-call.png" 2>/dev/null || true
    say "Fatto. Le registrazioni e i log in ~/.orizon-call e ~/Downloads NON sono stati toccati."
    say "Per rimuovere anche token e log: rm -rf ~/.orizon-call"
    exit 0
}

[ "${1:-}" = "--uninstall" ] && uninstall

case "$OS" in
    Darwin|Linux) ;;
    MINGW*|MSYS*|CYGWIN*)
        die "Su Windows usa PowerShell: irm https://raw.githubusercontent.com/$REPO/$REF/install.ps1 | iex" ;;
    *) die "Sistema operativo non supportato: $OS" ;;
esac

# ── 1. Prerequisiti ──────────────────────────────────────────────────────────

find_python() {
    local cand
    for cand in python3.13 python3.12 python3.11 python3.10 python3; do
        if command -v "$cand" >/dev/null 2>&1 &&
           "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
            command -v "$cand"
            return 0
        fi
    done
    return 1
}

pkg_install() {
    # Prova a installare pacchetti di sistema con il gestore disponibile.
    # Non fallisce mai: al peggio l'utente li installa a mano.
    local apt_pkgs="$1" dnf_pkgs="$2" pacman_pkgs="$3" zypper_pkgs="$4"
    local SUDO=""
    [ "$(id -u)" -ne 0 ] && SUDO="sudo"
    if command -v apt-get >/dev/null 2>&1; then
        $SUDO apt-get update -qq && $SUDO apt-get install -y -qq $apt_pkgs
    elif command -v dnf >/dev/null 2>&1; then
        $SUDO dnf install -y -q $dnf_pkgs
    elif command -v pacman >/dev/null 2>&1; then
        $SUDO pacman -S --noconfirm --needed $pacman_pkgs
    elif command -v zypper >/dev/null 2>&1; then
        $SUDO zypper install -y $zypper_pkgs
    else
        return 1
    fi
}

PY="$(find_python || true)"
if [ -z "$PY" ]; then
    warn "Python ≥ 3.10 non trovato."
    if [ "$OS" = "Darwin" ]; then
        if command -v brew >/dev/null 2>&1 && ask "Lo installo con Homebrew (brew install python)?"; then
            brew install python
            PY="$(find_python || true)"
        else
            die "Installa Python 3: https://www.python.org/downloads/  (o: brew install python) e rilancia l'installer."
        fi
    else
        if ask "Provo a installarlo con il gestore pacchetti della tua distribuzione?"; then
            pkg_install "python3 python3-venv python3-pip" "python3" "python" "python3" || true
            PY="$(find_python || true)"
        fi
        [ -z "$PY" ] && die "Installa Python 3 (es. sudo apt install python3 python3-venv) e rilancia l'installer."
    fi
fi
say "Python trovato: $("$PY" --version) ($PY)"

if [ "$OS" = "Linux" ]; then
    # PortAudio è la libreria di cattura audio usata dall'app (sounddevice).
    if ! ldconfig -p 2>/dev/null | grep -q libportaudio; then
        warn "Libreria PortAudio non trovata: senza, l'app non può registrare."
        if ask "La installo adesso (serve sudo)?"; then
            pkg_install "libportaudio2" "portaudio" "portaudio" "portaudio" ||
                warn "Installazione automatica fallita: installa 'libportaudio2' (Debian/Ubuntu) o 'portaudio' a mano."
        else
            warn "Ricordati di installarla: sudo apt install libportaudio2  (Debian/Ubuntu)"
        fi
    fi
    if ! command -v ffmpeg >/dev/null 2>&1; then
        warn "ffmpeg non trovato (opzionale: serve solo per l'export MP3 e la normalizzazione del volume)."
    fi
fi

# ── 2. Scarica / aggiorna il codice ──────────────────────────────────────────

mkdir -p "$BASE_DIR"
if command -v git >/dev/null 2>&1; then
    if [ -d "$APP_DIR/.git" ]; then
        say "Aggiorno Orizon Call ($REF)…"
        git -C "$APP_DIR" fetch --depth 1 origin "$REF"
        git -C "$APP_DIR" checkout -q FETCH_HEAD 2>/dev/null || true
        git -C "$APP_DIR" reset --hard -q FETCH_HEAD
    else
        say "Scarico Orizon Call da GitHub…"
        rm -rf "$APP_DIR"
        git clone --depth 1 --branch "$REF" "https://github.com/$REPO" "$APP_DIR"
    fi
else
    say "git non trovato: scarico l'archivio da GitHub…"
    TMP_TGZ="$(mktemp)"
    curl -fsSL "https://codeload.github.com/$REPO/tar.gz/refs/heads/$REF" -o "$TMP_TGZ"
    rm -rf "$APP_DIR.new"
    mkdir -p "$APP_DIR.new"
    tar -xzf "$TMP_TGZ" -C "$APP_DIR.new" --strip-components=1
    rm -f "$TMP_TGZ"
    rm -rf "$APP_DIR"
    mv "$APP_DIR.new" "$APP_DIR"
fi

# ── 3. Ambiente Python isolato ───────────────────────────────────────────────

if [ ! -x "$VENV_DIR/bin/python" ]; then
    say "Creo l'ambiente Python isolato…"
    if ! "$PY" -m venv "$VENV_DIR" 2>/dev/null; then
        # Debian/Ubuntu senza python3-venv
        warn "Il modulo venv non è disponibile."
        if [ "$OS" = "Linux" ] && ask "Installo python3-venv (serve sudo)?"; then
            pkg_install "python3-venv" "python3" "python" "python3" || true
        fi
        "$PY" -m venv "$VENV_DIR" || die "Impossibile creare il virtualenv. Installa python3-venv e rilancia."
    fi
fi

say "Installo le dipendenze (la prima volta può richiedere qualche minuto)…"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

# Helper audio di sistema per macOS: nel repo c'è il binario precompilato;
# se manca o non parte si può ricompilare con Xcode CLT (start.sh lo fa da solo).
if [ "$OS" = "Darwin" ] && [ ! -x "$APP_DIR/helpers/system_audio_capture" ]; then
    if command -v swiftc >/dev/null 2>&1; then
        say "Compilo l'helper per l'audio di sistema…"
        (cd "$APP_DIR/helpers" && ./build.sh) || warn "Compilazione helper fallita: partirà in modalità solo-microfono."
    else
        warn "Helper audio di sistema mancante e swiftc assente: audio di sistema non disponibile."
        warn "Per abilitarlo: xcode-select --install  e poi rilancia l'installer."
    fi
fi

# ── 4. Comando `orizon-call` + icona nel menu / Applications ────────────────

mkdir -p "$BIN_DIR"
cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
# Lancia Orizon Call. Generato da install.sh — le modifiche andranno perse.
case "\${1:-}" in
    update)    exec bash "$APP_DIR/install.sh" ;;
    uninstall) exec bash "$APP_DIR/install.sh" --uninstall ;;
esac
exec "$VENV_DIR/bin/python" "$APP_DIR/main.py" "\$@"
EOF
chmod +x "$LAUNCHER"

if [ "$OS" = "Darwin" ]; then
    # Mini bundle .app: doppio click da Launchpad/Applications, niente Terminale.
    APP_BUNDLE="$HOME/Applications/Orizon Call.app"
    mkdir -p "$APP_BUNDLE/Contents/MacOS" "$APP_BUNDLE/Contents/Resources"
    cat > "$APP_BUNDLE/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
    <key>CFBundleName</key><string>Orizon Call</string>
    <key>CFBundleDisplayName</key><string>Orizon Call</string>
    <key>CFBundleIdentifier</key><string>com.orizon.call</string>
    <key>CFBundleExecutable</key><string>orizon-call</string>
    <key>CFBundleIconFile</key><string>orizon-call</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>LSUIElement</key><false/>
    <key>NSMicrophoneUsageDescription</key>
    <string>Orizon Call registra il microfono durante le chiamate.</string>
</dict></plist>
PLIST
    cat > "$APP_BUNDLE/Contents/MacOS/orizon-call" <<EOF
#!/usr/bin/env bash
exec "$VENV_DIR/bin/python" "$APP_DIR/main.py"
EOF
    chmod +x "$APP_BUNDLE/Contents/MacOS/orizon-call"
    [ -f "$APP_DIR/assets/icons/orizon-call.icns" ] &&
        cp "$APP_DIR/assets/icons/orizon-call.icns" "$APP_BUNDLE/Contents/Resources/orizon-call.icns"
    say "App installata in ~/Applications → \"Orizon Call\""
else
    # Voce nel menu applicazioni (GNOME/KDE/…)
    ICON_DIR="$HOME/.local/share/icons/hicolor/256x256/apps"
    mkdir -p "$ICON_DIR" "$HOME/.local/share/applications"
    [ -f "$APP_DIR/assets/icons/orizon-call-256.png" ] &&
        cp "$APP_DIR/assets/icons/orizon-call-256.png" "$ICON_DIR/orizon-call.png"
    cat > "$HOME/.local/share/applications/orizon-call.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Orizon Call
Comment=Registratore di chiamate (microfono + audio di sistema)
Exec=$LAUNCHER
Icon=orizon-call
Terminal=false
Categories=AudioVideo;Audio;Recorder;
EOF
    command -v update-desktop-database >/dev/null 2>&1 &&
        update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
    say "Voce aggiunta al menu applicazioni → \"Orizon Call\""
fi

# PATH: assicura che ~/.local/bin sia raggiungibile.
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *)
        warn "$BIN_DIR non è nel tuo PATH."
        RC=""
        case "${SHELL:-}" in
            */zsh)  RC="$HOME/.zshrc" ;;
            */bash) RC="$HOME/.bashrc" ;;
        esac
        if [ -n "$RC" ] && ask "Aggiungo $BIN_DIR al PATH in $RC?"; then
            printf '\n# Orizon Call\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$RC"
            say "Fatto: apri un nuovo terminale (o esegui: source $RC)."
        else
            warn "Aggiungi tu questa riga al tuo profilo shell:  export PATH=\"\$HOME/.local/bin:\$PATH\""
        fi
        ;;
esac

echo
say "Installazione completata! 🎉"
say "Avvia con:  orizon-call        (oppure dall'icona \"Orizon Call\")"
say "Opzioni:    orizon-call --help"
say "Aggiorna:   orizon-call update      Disinstalla: orizon-call uninstall"
if [ "$OS" = "Darwin" ]; then
    say "Al primo avvio macOS chiederà i permessi per Microfono e Registrazione Schermo"
    say "(quest'ultima serve per catturare l'audio di sistema)."
fi
