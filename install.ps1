# ─────────────────────────────────────────────────────────────────────────────
#  Orizon Call — installer per Windows 10/11
#
#  Installazione con un solo comando (PowerShell; repository privato: serve
#  la GitHub CLI autenticata, `gh auth login`):
#
#    gh api -H "Accept: application/vnd.github.raw" repos/zano97/orizon-call/contents/install.ps1 | Out-String | iex
#
#  Cosa fa:
#    1. Controlla Python ≥ 3.10 (se manca prova a installarlo con winget).
#    2. Scarica l'app da GitHub in %LOCALAPPDATA%\OrizonCall\app (o aggiorna).
#    3. Crea un ambiente Python isolato con tutte le dipendenze.
#    4. Crea il comando `orizon-call`, i collegamenti nel menu Start e sul
#       Desktop (avvio senza finestra console).
#
#  Disinstallazione (dopo l'installazione):
#    orizon-call uninstall
#
#  Variabili opzionali:
#    $env:ORIZON_CALL_REF = 'nome-branch'   # installa da un branch diverso
#    $env:GITHUB_TOKEN = '<token>'          # solo per repository privato; in
#                                           # alternativa basta la GitHub CLI
#                                           # autenticata (gh auth login)
# ─────────────────────────────────────────────────────────────────────────────

$ErrorActionPreference = 'Stop'
# PowerShell 7.4+: senza questa riga un comando nativo (git, winget) che esce
# con codice != 0 solleva un errore terminante prima che $LASTEXITCODE venga
# letto, e il fallback interattivo (token) non parte mai. Ignorata su 5.1.
$PSNativeCommandUseErrorActionPreference = $false

$Repo = if ($env:ORIZON_CALL_REPO) { $env:ORIZON_CALL_REPO } else { 'zano97/orizon-call' }
$Ref  = if ($env:ORIZON_CALL_REF)  { $env:ORIZON_CALL_REF }  else { 'master' }

$Base = Join-Path $env:LOCALAPPDATA 'OrizonCall'
$App  = Join-Path $Base 'app'
$Venv = Join-Path $Base 'venv'
$Bin  = Join-Path $Base 'bin'

function Say($msg)  { Write-Host "* $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "! $msg" -ForegroundColor Yellow }

# ── PATH utente (registro, senza espandere le variabili altrui) ──────────────
# GetEnvironmentVariable restituisce il PATH *espanso*: riscriverlo perderebbe
# per sempre le voci %USERPROFILE%\... di altri programmi e cambierebbe il tipo
# della chiave. Si lavora quindi sul valore grezzo e si conserva ExpandString.
function Update-UserPath {
    param([string]$Add, [string]$Remove)
    $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $true)
    try {
        $raw = [string]$key.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        $parts = @($raw -split ';' | Where-Object { $_ })
        $changed = $false
        if ($Remove -and ($parts -contains $Remove)) {
            $parts = @($parts | Where-Object { $_ -ne $Remove }); $changed = $true
        }
        if ($Add -and -not ($parts -contains $Add)) {
            $parts += $Add; $changed = $true
        }
        if ($changed) {
            $key.SetValue('Path', ($parts -join ';'), [Microsoft.Win32.RegistryValueKind]::ExpandString)
            Send-EnvironmentChange
        }
        return $changed
    } finally { $key.Close() }
}

# Avvisa Explorer che l'ambiente e' cambiato (WM_SETTINGCHANGE), come fa
# [Environment]::SetEnvironmentVariable: altrimenti i terminali aperti dal
# menu Start non vedono `orizon-call` fino al prossimo accesso.
function Send-EnvironmentChange {
    try {
        if (-not ('OrizonCall.NativeMethods' -as [type])) {
            Add-Type -Namespace OrizonCall -Name NativeMethods -MemberDefinition @'
[DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Auto)]
public static extern IntPtr SendMessageTimeout(IntPtr hWnd, uint Msg, UIntPtr wParam, string lParam, uint fuFlags, uint uTimeout, out UIntPtr lpdwResult);
'@
        }
        $result = [UIntPtr]::Zero
        [void][OrizonCall.NativeMethods]::SendMessageTimeout([IntPtr]0xffff, 0x1A, [UIntPtr]::Zero, 'Environment', 2, 5000, [ref]$result)
    } catch { }
}

# ── Disinstallazione ─────────────────────────────────────────────────────────
if ($env:ORIZON_CALL_UNINSTALL -eq '1') {
    Say 'Rimuovo Orizon Call...'
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $App, $Venv, $Bin
    $startMenu = Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs\Orizon Call.lnk'
    $desktop   = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Orizon Call.lnk'
    Remove-Item -Force -ErrorAction SilentlyContinue $startMenu, $desktop
    Update-UserPath -Remove $Bin
    Say 'Fatto. Le registrazioni (Downloads) e i token/log (~\.orizon-call) NON sono stati toccati.'
    Remove-Item Env:\ORIZON_CALL_UNINSTALL -ErrorAction SilentlyContinue
    return
}

# ── 1. Python ≥ 3.10 ─────────────────────────────────────────────────────────
function Find-Python {
    foreach ($cand in @('py -3.13', 'py -3.12', 'py -3.11', 'py -3.10', 'py -3', 'python3', 'python')) {
        $parts = $cand -split ' '
        $exe = $parts[0]
        $extra = @()
        if ($parts.Count -gt 1) { $extra = $parts[1..($parts.Count-1)] }
        if (Get-Command $exe -ErrorAction SilentlyContinue) {
            try {
                $v = & $exe @extra -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>$null
                if ($v -and ([version]$v -ge [version]'3.10')) { return ,($parts) }
            } catch { }
        }
    }
    return $null
}

$Python = Find-Python
if (-not $Python) {
    Warn 'Python >= 3.10 non trovato.'
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Say 'Lo installo con winget (Python 3.12)...'
        winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
        # Aggiorna il PATH di questa sessione e riprova
        $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                    [Environment]::GetEnvironmentVariable('Path', 'User')
        $Python = Find-Python
    }
    if (-not $Python) {
        throw 'Installa Python 3 da https://www.python.org/downloads/ (spunta "Add python.exe to PATH") e rilancia questo comando.'
    }
}
$PyExe = $Python[0]
$PyArgs = @()
if ($Python.Count -gt 1) { $PyArgs = $Python[1..($Python.Count-1)] }
Say "Python trovato: $(& $PyExe @PyArgs --version)"

# ── 2. Scarica / aggiorna il codice ──────────────────────────────────────────
New-Item -ItemType Directory -Force -Path $Base | Out-Null

# Credenziali per repository privato: $env:GITHUB_TOKEN, altrimenti la
# GitHub CLI gia' autenticata (gh auth token). Mai scritte su disco.
$Token = $env:GITHUB_TOKEN
if (-not $Token -and (Get-Command gh -ErrorAction SilentlyContinue)) {
    try { $Token = (gh auth token 2>$null | Out-String).Trim() } catch { $Token = $null }
}

function Get-AuthPieces {
    $git = @(); $web = @{}
    if ($script:Token) {
        $b64 = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("x-access-token:$($script:Token)"))
        $git = @('-c', "http.https://github.com/.extraheader=AUTHORIZATION: basic $b64")
        $web = @{ Authorization = "Bearer $($script:Token)" }
    }
    return ,@($git, $web)
}

function Fetch-Repo {
    $pieces = Get-AuthPieces
    $gitAuthArgs = $pieces[0]; $webHeaders = $pieces[1]
    if ((Get-Command git -ErrorAction SilentlyContinue) -and (Test-Path (Join-Path $App '.git'))) {
        Say "Aggiorno Orizon Call ($Ref)..."
        git -C $App @gitAuthArgs fetch --depth 1 origin $Ref
        if ($LASTEXITCODE -ne 0) { return $false }
        git -C $App reset --hard FETCH_HEAD
        return ($LASTEXITCODE -eq 0)
    } elseif (Get-Command git -ErrorAction SilentlyContinue) {
        Say 'Scarico Orizon Call da GitHub (git)...'
        # Clone in una dir temporanea: un download fallito non deve mai
        # distruggere un'installazione esistente.
        $tmpApp = "$App.tmp"
        if (Test-Path $tmpApp) { Remove-Item -Recurse -Force $tmpApp }
        git @gitAuthArgs clone --depth 1 --branch $Ref "https://github.com/$Repo" $tmpApp
        if ($LASTEXITCODE -ne 0) { return $false }
        if (Test-Path $App) { Remove-Item -Recurse -Force $App }
        Move-Item $tmpApp $App
        return $true
    } else {
        Say 'Scarico Orizon Call da GitHub (zip)...'
        $zip = Join-Path $env:TEMP 'orizon-call.zip'
        try {
            Invoke-WebRequest "https://api.github.com/repos/$Repo/zipball/refs/heads/$Ref" -Headers $webHeaders -OutFile $zip
        } catch {
            try {
                Invoke-WebRequest "https://codeload.github.com/$Repo/zip/refs/heads/$Ref" -Headers $webHeaders -OutFile $zip
            } catch { return $false }
        }
        $tmp = Join-Path $env:TEMP 'orizon-call-unzip'
        if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
        Expand-Archive $zip -DestinationPath $tmp
        if (Test-Path $App) { Remove-Item -Recurse -Force $App }
        Move-Item (Get-ChildItem $tmp | Select-Object -First 1).FullName $App
        Remove-Item -Force $zip
        Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
        return $true
    }
}

if (-not (Fetch-Repo)) {
    Warn 'Download fallito: se il repository e'' privato servono credenziali GitHub.'
    Warn 'Via piu'' comoda: installa GitHub CLI (winget install GitHub.cli), esegui ''gh auth login'' e rilancia.'
    $manual = Read-Host 'In alternativa incolla ora un token GitHub in sola lettura (Invio per annullare)'
    if ($manual) {
        $script:Token = $manual.Trim()
        if (-not (Fetch-Repo)) { throw 'Impossibile scaricare il repository (token non valido o senza accesso).' }
    } else {
        throw 'Impossibile scaricare il repository. Usa ''gh auth login'' oppure $env:GITHUB_TOKEN e rilancia.'
    }
}

# ── 3. Ambiente Python isolato ───────────────────────────────────────────────
$VenvPython  = Join-Path $Venv 'Scripts\python.exe'
$VenvPythonW = Join-Path $Venv 'Scripts\pythonw.exe'
if (-not (Test-Path $VenvPython)) {
    Say 'Creo l''ambiente Python isolato...'
    & $PyExe @PyArgs -m venv $Venv
}
Say 'Installo le dipendenze (la prima volta puo'' richiedere qualche minuto)...'
& $VenvPython -m pip install --quiet --upgrade pip
& $VenvPython -m pip install --quiet -r (Join-Path $App 'requirements.txt')

# ── 4. Comando `orizon-call` + collegamenti ─────────────────────────────────
New-Item -ItemType Directory -Force -Path $Bin | Out-Null

# Avvio normale: pythonw = nessuna finestra console. I percorsi NON sono
# incorporati nel file: cmd.exe espande %LOCALAPPDATA% correttamente in
# qualunque code page, mentre un nome utente con accenti (C:\Users\Nicolò)
# scritto in ASCII diventerebbe illeggibile.
@"
@echo off
setlocal
set "OC=%LOCALAPPDATA%\OrizonCall"
rem update/uninstall usano la copia locale dello script: funziona anche
rem se il repository e' privato.
if "%~1"=="update"    powershell -NoProfile -ExecutionPolicy Bypass -Command "`$env:ORIZON_CALL_REF='$Ref'; & '%OC%\app\install.ps1'" & goto :eof
if "%~1"=="uninstall" powershell -NoProfile -ExecutionPolicy Bypass -Command "`$env:ORIZON_CALL_UNINSTALL='1'; & '%OC%\app\install.ps1'" & goto :eof
start "" "%OC%\venv\Scripts\pythonw.exe" "%OC%\app\main.py" %*
"@ | Set-Content -Encoding ASCII (Join-Path $Bin 'orizon-call.cmd')

# Variante con console visibile, utile per vedere i log dal vivo.
@"
@echo off
setlocal
set "OC=%LOCALAPPDATA%\OrizonCall"
"%OC%\venv\Scripts\python.exe" "%OC%\app\main.py" %*
"@ | Set-Content -Encoding ASCII (Join-Path $Bin 'orizon-call-debug.cmd')

# PATH utente
if (Update-UserPath -Add $Bin) {
    Say 'Aggiunto al PATH utente (apri un nuovo terminale per usare `orizon-call`).'
}

# Collegamenti menu Start + Desktop con icona
$icon = Join-Path $App 'assets\icons\orizon-call.ico'
$shell = New-Object -ComObject WScript.Shell
foreach ($where in @(
    (Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs\Orizon Call.lnk'),
    (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Orizon Call.lnk')
)) {
    $lnk = $shell.CreateShortcut($where)
    $lnk.TargetPath = $VenvPythonW
    $lnk.Arguments = "`"$App\main.py`""
    $lnk.WorkingDirectory = $App
    if (Test-Path $icon) { $lnk.IconLocation = $icon }
    $lnk.Description = 'Registratore di chiamate (microfono + audio di sistema)'
    $lnk.Save()
}

Write-Host ''
Say 'Installazione completata! Avvia "Orizon Call" dal menu Start o dal Desktop,'
Say 'oppure da terminale:  orizon-call     (log visibili: orizon-call-debug)'
Say 'Aggiorna: orizon-call update      Disinstalla: orizon-call uninstall'
