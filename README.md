# Orizon Call

Registratore audio flottante, companion desktop della web app Orizon.
Cattura **microfono + audio di sistema** (la voce degli altri partecipanti
a una call) in un unico file, con un widget sempre in primo piano e una
REST API locale per il controllo dalla web app.

## Caratteristiche

- **Widget flottante** sopra tutte le finestre (anche app fullscreen su
  macOS): cerchio quando inattivo, "pillola" con timer, VU meter, mute,
  pausa e stop durante la registrazione. Salvataggio asincrono: la UI non
  si blocca mai.
- **Audio di sistema senza driver**: su macOS 13+ usa un helper Swift
  basato su ScreenCaptureKit (niente BlackHole), su Windows WASAPI
  loopback, su Linux i monitor source PulseAudio/PipeWire.
- **Mix allineato al campione**: le due sorgenti sono scritte solo nella
  parte sovrapposta realmente catturata; mai zeri inseriti a casaccio,
  mai tracce sfasate. Ricampionamento streaming (soxr) senza click.
- **Auto-bilanciamento** del volume mic/sistema (attacco lento, gain
  rampato per blocco: niente pompaggio).
- **Robustezza**: header WAV sempre consistente su disco (un crash duro
  lascia un file riproducibile), watchdog con recovery di mic/helper,
  salvataggio d'emergenza su SIGINT/SIGTERM, auto-stop pulito a disco
  pieno, mai perdita del WAV se la conversione MP3 fallisce.
- **API locale sicura**: bind solo su 127.0.0.1, bearer token (mai nei
  log), validazione Host anti DNS-rebinding, CORS ristretto, SSE per gli
  aggiornamenti in tempo reale.

## Requisiti

- Python 3.10+
- `pip install -r requirements.txt`
- macOS: permesso "Registrazione schermo" per l'app che lancia Orizon
  Call (Terminale/iTerm o il bundle .app) — richiesto una sola volta.
- MP3 e normalizzazione loudness richiedono `ffmpeg` (`brew install ffmpeg`).

## Avvio

```bash
python3 main.py                 # WAV in ~/Downloads, API su :19876
python3 main.py --format flac   # FLAC lossless
python3 main.py --dual-track    # L=mic, R=sistema (per trascrizione)
python3 main.py --help          # tutte le opzioni
```

## Helper audio di sistema (macOS)

Il binario è in `helpers/system_audio_capture`. Per ricompilarlo
(richiede Xcode Command Line Tools):

```bash
cd helpers && ./build.sh
```

Diagnostica della cattura: `python3 diag_sck.py` (riproduci dell'audio
mentre gira e controlla che `peak` sia > 0).

## Test

```bash
pip install -r requirements-dev.txt
python3 -m pytest tests/
```

## Integrazione con la web app

Tutta la documentazione dell'API REST (endpoint, auth, SSE, esempi
JavaScript) è in [INTEGRATION_GUIDE.txt](INTEGRATION_GUIDE.txt).

## File di output

`recording_YYYYMMDD_HHMMSS.{wav,flac,mp3}` in `~/Downloads` (o
`--output-dir`). I WAV hanno un sidecar `.json` con i metadati
(durata, layout canali, segmenti). Le registrazioni oltre ~5 ore vengono
divise in segmenti `_part2`, `_part3`, …
