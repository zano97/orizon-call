# ─────────────────────────────────────────────────────────────────────────────
#  Orizon Call — installer per Windows 10/11
#
#  Installazione con un solo comando (PowerShell):
#
#    irm https://raw.githubusercontent.com/zano97/orizon-call/master/install.ps1 | iex
#
#  Cosa fa:
#    1. Controlla Python ≥ 3.10 (se manca prova a installarlo con winget).
#    2. Scarica l'app da GitHub in %LOCALAPPDATA%\OrizonCall\app (o aggiorna).
#    3. Crea un ambiente Python isolato con tutte le dipendenze.
#    4. Crea il comando `orizon-call`, i collegamenti nel menu Start e sul
#       Desktop (avvio senza finestra console).
#
#  Disinstallazione:
#    $env:ORIZON_CALL_UNINSTALL='1'; irm https://raw.githubusercontent.com/zano97/orizon-call/master/install.ps1 | iex
#
#  Variabili opzionali:
#    $env:ORIZON_CALL_REF = 'nome-branch'   # installa da un branch diverso
#    $env:GITHUB_TOKEN = '<token>'          # necessario solo finché il
#                                           # repository è privato (lettura)
# ─────────────────────────────────────────────────────────────────────────────

$ErrorActionPreference = 'Stop'

$Repo = if ($env:ORIZON_CALL_REPO) { $env:ORIZON_CALL_REPO } else { 'zano97/orizon-call' }
$Ref  = if ($env:ORIZON_CALL_REF)  { $env:ORIZON_CALL_REF }  else { 'master' }

$Base = Join-Path $env:LOCALAPPDATA 'OrizonCall'
$App  = Join-Path $Base 'app'
$Venv = Join-Path $Base 'venv'
$Bin  = Join-Path $Base 'bin'

function Say($msg)  { Write-Host "* $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "! $msg" -ForegroundColor Yellow }

# ── Disinstallazione ─────────────────────────────────────────────────────────
if ($env:ORIZON_CALL_UNINSTALL -eq '1') {
    Say 'Rimuovo Orizon Call...'
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $App, $Venv, $Bin
    $startMenu = Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs\Orizon Call.lnk'
    $desktop   = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Orizon Call.lnk'
    Remove-Item -Force -ErrorAction SilentlyContinue $startMenu, $desktop
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if ($userPath -like "*$Bin*") {
        $newPath = ($userPath -split ';' | Where-Object { $_ -and $_ -ne $Bin }) -join ';'
        [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
    }
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

# Repository privato: usa $env:GITHUB_TOKEN al volo (mai scritto su disco).
$gitAuthArgs = @()
$webHeaders = @{}
if ($env:GITHUB_TOKEN) {
    $b64 = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("x-access-token:$($env:GITHUB_TOKEN)"))
    $gitAuthArgs = @('-c', "http.https://github.com/.extraheader=AUTHORIZATION: basic $b64")
    $webHeaders = @{ Authorization = "Bearer $($env:GITHUB_TOKEN)" }
}

if ((Get-Command git -ErrorAction SilentlyContinue) -and (Test-Path (Join-Path $App '.git'))) {
    Say "Aggiorno Orizon Call ($Ref)..."
    git -C $App @gitAuthArgs fetch --depth 1 origin $Ref
    git -C $App reset --hard FETCH_HEAD
} elseif (Get-Command git -ErrorAction SilentlyContinue) {
    Say 'Scarico Orizon Call da GitHub (git)...'
    if (Test-Path $App) { Remove-Item -Recurse -Force $App }
    git @gitAuthArgs clone --depth 1 --branch $Ref "https://github.com/$Repo" $App
} else {
    Say 'Scarico Orizon Call da GitHub (zip)...'
    $zip = Join-Path $env:TEMP 'orizon-call.zip'
    try {
        Invoke-WebRequest "https://api.github.com/repos/$Repo/zipball/refs/heads/$Ref" -Headers $webHeaders -OutFile $zip
    } catch {
        Invoke-WebRequest "https://codeload.github.com/$Repo/zip/refs/heads/$Ref" -Headers $webHeaders -OutFile $zip
    }
    $tmp = Join-Path $env:TEMP 'orizon-call-unzip'
    if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
    Expand-Archive $zip -DestinationPath $tmp
    if (Test-Path $App) { Remove-Item -Recurse -Force $App }
    Move-Item (Get-ChildItem $tmp | Select-Object -First 1).FullName $App
    Remove-Item -Force $zip
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
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

# Avvio normale: pythonw = nessuna finestra console.
@"
@echo off
if "%1"=="update"    powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/$Repo/$Ref/install.ps1 | iex" & goto :eof
if "%1"=="uninstall" powershell -NoProfile -ExecutionPolicy Bypass -Command "`$env:ORIZON_CALL_UNINSTALL='1'; irm https://raw.githubusercontent.com/$Repo/$Ref/install.ps1 | iex" & goto :eof
start "" "$VenvPythonW" "$App\main.py" %*
"@ | Set-Content -Encoding ASCII (Join-Path $Bin 'orizon-call.cmd')

# Variante con console visibile, utile per vedere i log dal vivo.
@"
@echo off
"$VenvPython" "$App\main.py" %*
"@ | Set-Content -Encoding ASCII (Join-Path $Bin 'orizon-call-debug.cmd')

# PATH utente
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if ($userPath -notlike "*$Bin*") {
    [Environment]::SetEnvironmentVariable('Path', "$userPath;$Bin", 'User')
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
