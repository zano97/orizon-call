# Pubblicare una nuova versione

Le versioni installabili (macOS `.dmg`, Windows `.exe`, Linux `.AppImage`)
le costruisce GitHub Actions ([release.yml](.github/workflows/release.yml))
quando spingi un tag. Le app installate trovano la nuova versione da sole e
propongono l'aggiornamento.

## Ogni volta (5 minuti)

1. **Numero di versione** in [`version.py`](version.py), es. `__version__ = "1.1.0"`.
   Regola: `1.0.0 → 1.0.1` correzioni, `→ 1.1.0` novità, `→ 2.0.0` cambi grossi.
2. **Novità** in [`CHANGELOG.md`](CHANGELOG.md): una sezione `## 1.1.0` con
   un elenco puntato scritto per chi usa l'app. Le prime 3 righe compaiono
   nella proposta di aggiornamento, tutta la sezione nella pagina della release.
3. Commit su `master`, poi il tag:

   ```bash
   git tag v1.1.0
   git push origin master v1.1.0
   ```

4. In **Actions → Release** segui la build (circa 15–25 minuti): un job per
   macOS Apple Silicon, macOS Intel, Windows e Linux, ognuno con il
   **self-test** dell'app impacchettata (audio, ffmpeg, interfaccia,
   componenti di sistema). Se uno fallisce, la release **non** viene
   pubblicata.
5. A build finita la release è su
   `https://github.com/<RELEASES_REPO>/releases` con i file, `SHA256SUMS.txt`
   e le note. Entro 6 ore (o subito con **Controlla aggiornamenti**) le app
   installate la propongono.

Il workflow rifiuta un tag che non corrisponde a `version.py` o una versione
senza sezione nel CHANGELOG.

**Provare senza pubblicare**: Actions → Release → *Run workflow* costruisce
tutto e lascia i file come *artifact* del run (14 giorni), senza creare la
release. Utile per provare l'installer su un computer vero prima del tag.

## Una volta: dove vengono pubblicate le versioni

`RELEASES_REPO` in [`version.py`](version.py) è il repository da cui le app
scaricano gli aggiornamenti. Oggi è `zano97/orizon-call`, che è **privato**:
in questo caso solo chi ha accesso al repository (e la GitHub CLI collegata)
può scaricare l'installer e ricevere gli aggiornamenti.

Per distribuire l'app a chiunque, senza account GitHub:

1. crea un repository **pubblico** vuoto, es. `zano97/orizon-call-releases`
   (con almeno un commit, ad esempio un README);
2. crea un token (*fine-grained*, permesso **Contents: Read and write** solo
   su quel repository) e salvalo nei secret di questo repository come
   `RELEASES_TOKEN`;
3. imposta `RELEASES_REPO = "zano97/orizon-call-releases"` in `version.py`
   **prima** della versione che distribuisci: le app installate continuano
   a leggere il valore con cui sono state costruite.

Il codice resta privato: nel repository pubblico finiscono solo gli installer.

## Una volta: firma del codice (consigliata)

Senza firma le build funzionano, ma:

- **macOS** al primo avvio chiede di confermare in *Impostazioni → Privacy e
  sicurezza → Apri comunque*, e i permessi **Microfono** e **Registrazione
  schermo** vanno ridati dopo ogni aggiornamento (macOS li lega alla firma);
- **Windows** al primo avvio dell'installer mostra «Windows ha protetto il PC».

Gli aggiornamenti dall'app funzionano comunque su tutti e tre i sistemi.

### macOS — Apple Developer ID (99 $/anno)

1. Iscriviti all'[Apple Developer Program](https://developer.apple.com/programs/).
2. Crea un certificato **Developer ID Application**, esportalo dal Portachiavi
   come `.p12` con una password.
3. Crea una *app-specific password* su [account.apple.com](https://account.apple.com)
   (serve per la notarizzazione).
4. Secret del repository (Settings → Secrets and variables → Actions):

   | Secret | Valore |
   |---|---|
   | `MACOS_CERT_P12` | il `.p12` in base64 (`base64 -i cert.p12 \| pbcopy`) |
   | `MACOS_CERT_PASSWORD` | la password del `.p12` |
   | `MACOS_SIGN_IDENTITY` | `Developer ID Application: Nome Cognome (TEAMID)` |
   | `APPLE_ID` | l'email dell'account sviluppatore |
   | `APPLE_TEAM_ID` | il Team ID (10 caratteri) |
   | `APPLE_APP_PASSWORD` | la app-specific password |

Dalla release successiva app e `.dmg` sono firmati, notarizzati e "graffettati"
(si aprono senza avvisi, anche offline). Gli aggiornamenti verificano che la
nuova versione sia firmata dallo **stesso** Team ID prima di installarla.

> Il passaggio da non firmata a firmata cambia l'identità dell'app per
> macOS: al primo avvio della versione firmata i permessi vanno ridati una
> volta, poi restano.

### Windows — certificato di firma del codice

Con un certificato **OV/EV Code Signing** in formato `.pfx`:

| Secret | Valore |
|---|---|
| `WINDOWS_CERT_PFX` | il `.pfx` in base64 (`[Convert]::ToBase64String([IO.File]::ReadAllBytes("cert.pfx"))`) |
| `WINDOWS_CERT_PASSWORD` | la password del `.pfx` |

Vengono firmati sia `Orizon Call.exe` sia l'installer. Un certificato nuovo
può far comparire ancora SmartScreen per qualche settimana, finché il
certificato non accumula reputazione (un certificato EV la ha subito).

## Costruire in locale

```bash
pip install -r requirements.txt -r packaging/requirements-build.txt
python packaging/build.py          # → dist/release/
```

Sul sistema in uso produce il pacchetto di quel sistema ed esegue il
self-test. Servono: macOS → Xcode Command Line Tools; Windows → [Inno
Setup 6](https://jrsoftware.org/isinfo.php); Linux → `build-essential`,
`libasound2-dev` e le librerie `libxcb-*` elencate in `release.yml`.

## Cosa succede durante un aggiornamento

| Sistema | Pacchetto scaricato | Installazione |
|---|---|---|
| macOS | `…-macos-<arch>.zip` | l'app viene sostituita in Applicazioni (firma verificata), poi riaperta |
| Windows | `…-windows-x64-setup.exe` | installer eseguito in modo silenzioso nella stessa cartella, poi l'app riparte |
| Linux | `…-linux-<arch>.AppImage` | il file AppImage viene sostituito, poi riaperto |

Sempre: download in background, verifica SHA-256 con `SHA256SUMS.txt`,
proposta all'utente solo a file pronto, mai durante una registrazione, esito
in `~/.orizon-call/update_status` e log in `~/.orizon-call/logs/update.log`.
