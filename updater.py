"""
In-app updates from GitHub Releases.

Every release (built by .github/workflows/release.yml) carries one asset
per platform plus SHA256SUMS.txt:

  OrizonCall-<v>-macos-arm64.zip / -macos-x86_64.zip    (.app, for updates)
  OrizonCall-<v>-macos-arm64.dmg / -macos-x86_64.dmg    (first install)
  OrizonCall-<v>-windows-x64-setup.exe                  (install + update)
  OrizonCall-<v>-linux-x86_64.AppImage                  (install + update)

How this copy updates depends on how it was installed (``install_kind``):

  macos-app          swap the .app bundle (verified signature, same team)
  windows-installer  run the new installer silently over the old one
  appimage           replace the AppImage file
  script             legacy install.sh / install.ps1: re-run the installer
  dev / frozen-other never touched: the user updates by hand

The flow is the same everywhere: a background check (shortly after start,
then every 6 h) compares ``version.__version__`` with the latest release;
a newer one is downloaded and verified in the background, then proposed
("1.0.0 → 1.1.0" with the release notes). Accepting quits the app; a tiny
detached helper waits for it to exit, installs, records the outcome in
``~/.orizon-call/update_status`` and starts the new version, which reports
how it went. Never during a recording.

Authentication: none needed when RELEASES_REPO is public. For a private
repository the GitHub token comes from GITHUB_TOKEN/GH_TOKEN or the
GitHub CLI (``gh auth token``); it is passed only through the helper's
environment, never written to disk.
"""

import hashlib
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from PyQt6.QtCore import QObject, QTimer, pyqtSignal, pyqtSlot

import desktop_env
from orizon_logging import get_logger
from version import RELEASES_REPO, __version__, parse_version

log = get_logger("updater")

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path.home() / ".orizon-call"
STATUS_FILE = DATA_DIR / "update_status"
UPDATE_LOG = DATA_DIR / "logs" / "update.log"
UPDATES_DIR = DATA_DIR / "updates"
CHECKSUMS_ASSET = "SHA256SUMS.txt"
# GitHub API by default; any server answering the same two calls
# (GET /repos/<repo>/releases/latest + the asset downloads) works too.
API_BASE = os.environ.get("ORIZON_CALL_UPDATE_API", "https://api.github.com").rstrip("/")

FIRST_CHECK_DELAY_MS = 20_000
CHECK_INTERVAL_MS = 6 * 3600 * 1000
SNOOZE_S = 24 * 3600
HTTP_TIMEOUT_S = 15

SELF_UPDATING_KINDS = ("macos-app", "windows-installer", "appimage")


# ---------- How was this copy installed? ----------

def managed_app_dir() -> Path:
    """Where install.sh / install.ps1 put the source checkout."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "OrizonCall" / "app"
    return DATA_DIR / "app"


def _same_path(a: Path, b: Path) -> bool:
    try:
        a, b = a.resolve(), b.resolve()
    except OSError:
        pass
    if sys.platform == "win32":
        return str(a).lower() == str(b).lower()
    return a == b


def is_managed_install(app_dir: Path = APP_DIR) -> bool:
    installer = "install.ps1" if sys.platform == "win32" else "install.sh"
    return _same_path(app_dir, managed_app_dir()) and (app_dir / installer).exists()


def install_kind() -> str:
    if getattr(sys, "frozen", False):
        if sys.platform == "darwin":
            return "macos-app" if desktop_env.macos_bundle_path() else "frozen-other"
        if sys.platform == "win32":
            exe_dir = Path(sys.executable).resolve().parent
            return "windows-installer" if any(exe_dir.glob("unins*.exe")) else "frozen-other"
        return "appimage" if desktop_env.appimage_path() else "frozen-other"
    return "script" if is_managed_install() else "dev"


def _mac_hardware_arch() -> str:
    """arm64 even when an x86_64 build runs under Rosetta: the update then
    moves the user to the native build."""
    try:
        out = subprocess.run(["/usr/sbin/sysctl", "-n", "hw.optional.arm64"],
                             capture_output=True, text=True, timeout=3)
        if out.stdout.strip() == "1":
            return "arm64"
    except (OSError, subprocess.SubprocessError):
        pass
    return "arm64" if platform.machine() == "arm64" else "x86_64"


def asset_suffix(kind: str) -> Optional[str]:
    if kind == "macos-app":
        return f"-macos-{_mac_hardware_arch()}.zip"
    if kind == "windows-installer":
        return "-windows-x64-setup.exe"
    if kind == "appimage":
        machine = platform.machine().lower()
        arch = "aarch64" if machine in ("aarch64", "arm64") else "x86_64"
        return f"-linux-{arch}.AppImage"
    return None


# ---------- GitHub ----------

def _no_window_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def find_gh() -> Optional[str]:
    """The GitHub CLI, also when started from a .app bundle or the Start
    menu, whose PATH does not include Homebrew or Program Files."""
    found = shutil.which("gh")
    if found:
        return found
    candidates = ["/opt/homebrew/bin/gh", "/usr/local/bin/gh", "/usr/bin/gh",
                  "/home/linuxbrew/.linuxbrew/bin/gh"]
    if sys.platform == "win32":
        for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if base:
                candidates.append(os.path.join(base, "GitHub CLI", "gh.exe"))
    return next((c for c in candidates if os.path.isfile(c)), None)


def github_token() -> Optional[str]:
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(var):
            return os.environ[var].strip()
    gh = find_gh()
    if not gh:
        return None
    try:
        out = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, timeout=10,
                             creationflags=_no_window_flags(), env=desktop_env.clean_child_env())
    except (OSError, subprocess.SubprocessError):
        return None
    token = out.stdout.strip()
    return token if out.returncode == 0 and token else None


class _DropAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """Asset downloads redirect to a signed storage URL: forwarding the
    GitHub token there is both a leak and an error (two auth methods)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and (urllib.parse.urlparse(newurl).netloc
                                != urllib.parse.urlparse(req.full_url).netloc):
            new.remove_header("Authorization")
        return new


_opener = urllib.request.build_opener(_DropAuthOnRedirect)


def _request(url: str, token: Optional[str], accept: str) -> urllib.request.Request:
    req = urllib.request.Request(url, headers={
        "Accept": accept, "User-Agent": f"orizon-call/{__version__}",
        "X-GitHub-Api-Version": "2022-11-28"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    return req


def _api_get(path: str, token: Optional[str]) -> dict:
    req = _request(f"{API_BASE}/repos/{RELEASES_REPO}/{path}", token,
                   "application/vnd.github+json")
    with _opener.open(req, timeout=HTTP_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


class UpdateCheckError(Exception):
    """Human-readable reason (Italian), shown on a manual check."""


def release_bullets(body: str, limit: int = 5) -> List[str]:
    """The user-facing bullet points of a release description."""
    out = []
    for line in (body or "").splitlines():
        line = line.strip()
        if line[:2] in ("- ", "* ", "• "):
            text = line[2:].strip().replace("**", "")
            if text:
                out.append(text[:110])
        if len(out) >= limit:
            break
    return out


@dataclass
class UpdateInfo:
    available: bool
    current: str
    latest: str = ""
    notes: List[str] = field(default_factory=list)
    html_url: str = ""
    asset: Optional[dict] = None       # this platform's file (None: not published)
    checksums: Optional[dict] = None   # SHA256SUMS.txt asset


def check_for_update(token: Optional[str], kind: str, current: str = __version__) -> UpdateInfo:
    try:
        rel = _api_get("releases/latest", token)
    except urllib.error.HTTPError as e:
        if e.code == 404 and token:
            raise UpdateCheckError("Nessuna versione pubblicata per ora.") from e
        if e.code in (401, 403, 404):
            raise UpdateCheckError(
                "Accesso agli aggiornamenti negato: il repository delle versioni è privato. "
                "Esegui 'gh auth login' (GitHub CLI) una volta, poi riprova.") from e
        raise UpdateCheckError(f"GitHub ha risposto con errore {e.code}.") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise UpdateCheckError("Impossibile contattare GitHub (sei offline?).") from e
    latest = (rel.get("tag_name") or "").lstrip("vV")
    if not latest or parse_version(latest) == (0,):
        raise UpdateCheckError("Risposta inattesa da GitHub.")
    info = UpdateInfo(parse_version(latest) > parse_version(current), current, latest,
                      release_bullets(rel.get("body") or ""), rel.get("html_url") or "")
    suffix = asset_suffix(kind)
    for asset in rel.get("assets") or []:
        name = asset.get("name", "")
        if suffix and name.endswith(suffix):
            info.asset = asset
        elif name == CHECKSUMS_ASSET:
            info.checksums = asset
    return info


def _download(asset: dict, dest: Path, token: Optional[str],
              progress: Optional[Callable[[int, int], None]] = None) -> str:
    """Stream an asset to ``dest``; returns its SHA-256. With a token the
    API URL is used (private repositories), otherwise the public one."""
    url = asset.get("url") if token else asset.get("browser_download_url")
    url = url or asset.get("browser_download_url") or asset.get("url")
    req = _request(url, token, "application/octet-stream")
    sha = hashlib.sha256()
    part = dest.with_name(dest.name + ".part")
    total = int(asset.get("size") or 0)
    done = 0
    with _opener.open(req, timeout=HTTP_TIMEOUT_S) as resp, open(part, "wb") as f:
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
            sha.update(chunk)
            done += len(chunk)
            if progress is not None:
                progress(done, total)
    if total and done != total:
        part.unlink(missing_ok=True)
        raise UpdateCheckError("Download interrotto: riprovo più tardi.")
    os.replace(part, dest)
    return sha.hexdigest()


def parse_checksums(text: str) -> dict:
    """'<sha256>  <file>' lines (sha256sum format) → {file: sha256}."""
    out = {}
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and len(parts[0]) == 64:
            out[parts[-1].lstrip("*")] = parts[0].lower()
    return out


def _sha256_file(path: Path) -> str:
    sha = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            sha.update(chunk)
    return sha.hexdigest()


# ---------- Preparing (background, before asking) ----------

@dataclass
class PreparedUpdate:
    kind: str
    version: str
    payload: Path       # new .app / setup.exe / AppImage, ready to install
    target: Path        # what gets replaced (.app, install dir, AppImage)


def _codesign_team(path: Path) -> Optional[str]:
    try:
        out = subprocess.run(["/usr/bin/codesign", "-dv", "--verbose=2", str(path)],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.stderr.splitlines():
        if line.startswith("TeamIdentifier="):
            team = line.split("=", 1)[1].strip()
            return None if team in ("", "not set") else team
    return None


def _target_for(kind: str) -> Path:
    if kind == "macos-app":
        bundle = desktop_env.macos_bundle_path()
        problem = desktop_env.macos_location_problem(bundle)
        if problem:
            raise UpdateCheckError("Sposta Orizon Call nella cartella Applicazioni per poterlo "
                                   "aggiornare automaticamente.")
        return bundle
    if kind == "windows-installer":
        return Path(sys.executable).resolve().parent
    if kind == "appimage":
        image = desktop_env.appimage_path()
        if image is None or not os.access(image.parent, os.W_OK):
            raise UpdateCheckError(f"La cartella {image.parent if image else '?'} non è scrivibile: "
                                   "scarica la nuova versione a mano.")
        return image
    raise UpdateCheckError("Questa copia non si aggiorna da sola.")


def prepare_update(info: UpdateInfo, token: Optional[str], kind: str,
                   cache_dir: Optional[Path] = None) -> PreparedUpdate:
    """Download + verify (+ unpack) the new version. Idempotent: a file
    already downloaded and verified is reused."""
    if info.asset is None:
        raise UpdateCheckError(f"La versione {info.latest} non ha ancora il pacchetto per "
                               "questo sistema: scaricala dalla pagina delle versioni.")
    if info.checksums is None:
        raise UpdateCheckError("Versione pubblicata senza checksum: aggiornamento automatico "
                               "rifiutato per sicurezza.")
    target = _target_for(kind)
    cache_dir = Path(cache_dir or UPDATES_DIR)
    work = cache_dir / info.latest
    if cache_dir.is_dir():
        for old in cache_dir.iterdir():
            if old.name == info.latest:
                continue
            if old.is_dir():
                shutil.rmtree(old, ignore_errors=True)
            else:
                old.unlink(missing_ok=True)
    work.mkdir(parents=True, exist_ok=True)

    name = info.asset["name"]
    sums_path = work / CHECKSUMS_ASSET
    _download(info.checksums, sums_path, token)
    expected = parse_checksums(sums_path.read_text(encoding="utf-8", errors="replace")).get(name)
    if not expected:
        raise UpdateCheckError(f"{name} non compare nei checksum della versione.")
    payload = work / name
    if not (payload.is_file() and _sha256_file(payload) == expected):
        got = _download(info.asset, payload, token)
        if got != expected:
            payload.unlink(missing_ok=True)
            raise UpdateCheckError("Il file scaricato non corrisponde al checksum: scartato.")

    if kind == "macos-app":
        staged = work / "staged"
        shutil.rmtree(staged, ignore_errors=True)
        staged.mkdir()
        res = subprocess.run(["/usr/bin/ditto", "-x", "-k", str(payload), str(staged)],
                             capture_output=True, text=True, timeout=300)
        apps = list(staged.glob("*.app"))
        if res.returncode != 0 or len(apps) != 1:
            raise UpdateCheckError("Pacchetto macOS non valido.")
        new_app = apps[0]
        check = subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(new_app)],
                               capture_output=True, text=True, timeout=120)
        if check.returncode != 0:
            raise UpdateCheckError("La firma della nuova versione non è valida: aggiornamento rifiutato.")
        team = _codesign_team(target)
        if team and _codesign_team(new_app) != team:
            raise UpdateCheckError("La nuova versione è firmata da uno sviluppatore diverso: "
                                   "aggiornamento rifiutato.")
        return PreparedUpdate(kind, info.latest, new_app, target)
    if kind == "appimage":
        os.chmod(payload, 0o755)
    return PreparedUpdate(kind, info.latest, payload, target)


def cleanup_downloads(cache_dir: Optional[Path] = None, current: str = __version__) -> None:
    """Remove downloaded versions that are not newer than the running one
    (installed already, or superseded)."""
    cache_dir = Path(cache_dir or UPDATES_DIR)
    if not cache_dir.is_dir():
        return
    for entry in cache_dir.iterdir():
        if parse_version(entry.name) <= parse_version(current):
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)


# ---------- Installing (detached helper, after we quit) ----------

def read_and_clear_status() -> Optional[int]:
    """Exit code of the last in-app update (None if there was none)."""
    try:
        text = STATUS_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        STATUS_FILE.unlink()
    except OSError:
        pass
    try:
        return int(text.split()[0])
    except (ValueError, IndexError):
        return 1


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _ps_args(args: List[str]) -> str:
    """Windows command-line string for Start-Process -ArgumentList."""
    return " ".join('"' + a.replace('"', '\\"') + '"' for a in args)


def _posix_prologue(pid: int) -> str:
    q = shlex.quote
    return f"""#!/bin/bash
# Orizon Call update helper (temporary file, deletes itself).
rm -f "$0"
while kill -0 {pid} 2>/dev/null; do sleep 0.3; done
mkdir -p {q(str(UPDATE_LOG.parent))} {q(str(STATUS_FILE.parent))}
"""


def build_macos_helper(pid: int, prepared: PreparedUpdate, args: List[str]) -> str:
    q = shlex.quote
    app, new = q(str(prepared.target)), q(str(prepared.payload))
    old = q(str(prepared.target) + ".old-update")
    open_args = f" --args {' '.join(q(a) for a in args)}" if args else ""
    return _posix_prologue(pid) + f"""{{
echo "== $(date) → {prepared.version}"
rm -rf {old}
if mv {app} {old} && mv {new} {app}; then
    rm -rf {old}
    /usr/bin/xattr -dr com.apple.quarantine {app} 2>/dev/null
    echo 0 > {q(str(STATUS_FILE))}
else
    [ -d {app} ] || mv {old} {app}
    echo 1 > {q(str(STATUS_FILE))}
fi
}} >> {q(str(UPDATE_LOG))} 2>&1
exec /usr/bin/open {app}{open_args}
"""


def build_appimage_helper(pid: int, prepared: PreparedUpdate, args: List[str]) -> str:
    q = shlex.quote
    image, new = q(str(prepared.target)), q(str(prepared.payload))
    tmp = q(str(prepared.target) + ".new")
    relaunch = " ".join(q(a) for a in [str(prepared.target), *args])
    return _posix_prologue(pid) + f"""{{
echo "== $(date) → {prepared.version}"
if cp -f {new} {tmp} && chmod 755 {tmp} && mv -f {tmp} {image}; then
    rm -f {new}
    echo 0 > {q(str(STATUS_FILE))}
else
    rm -f {tmp}
    echo 1 > {q(str(STATUS_FILE))}
fi
}} >> {q(str(UPDATE_LOG))} 2>&1
exec {relaunch}
"""


def build_windows_installer_helper(pid: int, prepared: PreparedUpdate, app_exe: str,
                                   args: List[str]) -> str:
    setup_args = ('/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP- /NOCANCEL '
                  f'/DIR="{prepared.target}" /LOG="{UPDATE_LOG}"')
    launch = f"Start-Process -FilePath {_ps_quote(app_exe)}"
    if args:
        launch += f" -ArgumentList {_ps_quote(_ps_args(args))}"
    return f"""# Orizon Call update helper (temporary file, deletes itself).
Remove-Item -LiteralPath $MyInvocation.MyCommand.Path -Force -ErrorAction SilentlyContinue
Wait-Process -Id {pid} -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path {_ps_quote(str(UPDATE_LOG.parent))} | Out-Null
$code = 1
try {{
    $p = Start-Process -FilePath {_ps_quote(str(prepared.payload))} -ArgumentList {_ps_quote(setup_args)} -Wait -PassThru
    $code = $p.ExitCode
}} catch {{
    $_ | Out-File -Append -FilePath {_ps_quote(str(UPDATE_LOG))}
}}
Set-Content -Path {_ps_quote(str(STATUS_FILE))} -Value $code
if ($code -eq 0) {{ Remove-Item -LiteralPath {_ps_quote(str(prepared.payload))} -Force -ErrorAction SilentlyContinue }}
{launch}
"""


def build_script_posix_helper(pid: int, app_dir: Path, python: str, args: List[str]) -> str:
    """Legacy install.sh installs (source checkout + venv)."""
    q = shlex.quote
    relaunch = " ".join(q(a) for a in [python, str(app_dir / "main.py"), *args])
    return _posix_prologue(pid) + f"""export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
ORIZON_CALL_REF=master bash {q(str(app_dir / "install.sh"))} > {q(str(UPDATE_LOG))} 2>&1 < /dev/null
echo $? > {q(str(STATUS_FILE))}
unset GITHUB_TOKEN
exec {relaunch}
"""


def build_script_windows_helper(pid: int, app_dir: Path, python: str, args: List[str]) -> str:
    """Legacy install.ps1 installs (source checkout + venv)."""
    arglist = _ps_args([str(app_dir / "main.py"), *args])
    return f"""# Orizon Call update helper (temporary file, deletes itself).
Remove-Item -LiteralPath $MyInvocation.MyCommand.Path -Force -ErrorAction SilentlyContinue
Wait-Process -Id {pid} -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path {_ps_quote(str(UPDATE_LOG.parent))} | Out-Null
$env:ORIZON_CALL_REF = 'master'
$env:ORIZON_CALL_NONINTERACTIVE = '1'
$code = 0
try {{
    & {_ps_quote(str(app_dir / "install.ps1"))} *> {_ps_quote(str(UPDATE_LOG))}
    if (-not $?) {{ $code = 1 }}
}} catch {{
    $_ | Out-File -Append {_ps_quote(str(UPDATE_LOG))}
    $code = 1
}}
Set-Content -Path {_ps_quote(str(STATUS_FILE))} -Value $code
Remove-Item Env:GITHUB_TOKEN -ErrorAction SilentlyContinue
Start-Process -FilePath {_ps_quote(python)} -ArgumentList {_ps_quote(arglist)} -WorkingDirectory {_ps_quote(str(app_dir))}
"""


def _script_python() -> str:
    exe = sys.executable
    if sys.platform == "win32" and os.path.basename(exe).lower() == "python.exe":
        pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.isfile(pyw):
            exe = pyw
    return exe


def build_helper(kind: str, prepared: Optional[PreparedUpdate], args: List[str],
                 pid: Optional[int] = None) -> str:
    pid = os.getpid() if pid is None else pid
    if kind == "macos-app":
        return build_macos_helper(pid, prepared, args)
    if kind == "appimage":
        return build_appimage_helper(pid, prepared, args)
    if kind == "windows-installer":
        return build_windows_installer_helper(pid, prepared, sys.executable, args)
    if kind == "script":
        if sys.platform == "win32":
            return build_script_windows_helper(pid, APP_DIR, _script_python(), args)
        return build_script_posix_helper(pid, APP_DIR, _script_python(), args)
    raise ValueError(kind)


def launch_update_helper(kind: str, prepared: Optional[PreparedUpdate], token: Optional[str],
                         args: Optional[List[str]] = None) -> None:
    """Start the detached helper; the caller quits right after. Raises
    OSError if it cannot be started."""
    args = list(sys.argv[1:] if args is None else args)
    script = build_helper(kind, prepared, args)
    env = desktop_env.clean_child_env()
    if token and kind == "script":
        env["GITHUB_TOKEN"] = token
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.unlink(missing_ok=True)
    if sys.platform == "win32":
        fd, path = tempfile.mkstemp(prefix="orizon-update-", suffix=".ps1")
        with os.fdopen(fd, "w", encoding="utf-8-sig") as f:
            f.write(script)
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                 | getattr(subprocess, "CREATE_NO_WINDOW", 0))
        subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                          "-WindowStyle", "Hidden", "-File", path],
                         env=env, creationflags=flags, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    else:
        fd, path = tempfile.mkstemp(prefix="orizon-update-", suffix=".sh")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(script)
        os.chmod(path, 0o700)
        # New session: no controlling terminal (the installer never waits
        # on a question) and closing a terminal does not kill the update.
        subprocess.Popen(["/bin/bash", path], env=env, start_new_session=True, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    log.info("Update helper started (%s, %s); quitting.", kind, path)


# ---------- Qt glue ----------

class UpdateManager(QObject):
    """
    Periodic check (first one shortly after start, then every 6 h). A
    newer release is downloaded and verified in the background, then
    proposed; it never interrupts a recording (proposed when it ends).
    "Più tardi" snoozes for a day; "Controlla aggiornamenti" always
    answers.

    The widget is duck-typed: ``recorder_state_name``, ``is_busy``,
    ``show_prompt``, ``notify``, ``request_quit``, ``recording_stopped``.
    """

    _checked = pyqtSignal(object, object, object, bool)  # info, prepared, error, manual

    def __init__(self, widget, enabled: bool = True, kind: Optional[str] = None,
                 checker=None, preparer=None, launcher=None, token_provider=None,
                 opener=None, clock=time.monotonic,
                 first_delay_ms: int = FIRST_CHECK_DELAY_MS):
        super().__init__(widget if isinstance(widget, QObject) else None)
        self._widget = widget
        self._kind = install_kind() if kind is None else kind
        self._checker = checker or check_for_update
        self._preparer = preparer or prepare_update
        self._launcher = launcher or launch_update_helper
        self._token_provider = token_provider or github_token
        self._opener = opener or desktop_env.open_url
        self._clock = clock
        self._enabled = False
        self._checking = False
        self._pending: "Optional[tuple]" = None   # (info, prepared, error)
        self._snoozed_until = 0.0
        self._prompt = None
        self._token: Optional[str] = None
        self._timer = QTimer(self)
        self._timer.setInterval(CHECK_INTERVAL_MS)
        self._timer.timeout.connect(self.check_now)
        self._first_delay_ms = first_delay_ms
        self._checked.connect(self._on_checked)
        widget.recording_stopped.connect(self._on_recording_stopped)
        self.set_enabled(enabled)

    @property
    def kind(self) -> str:
        return self._kind

    @property
    def available(self) -> bool:
        """This copy can look for (and normally install) updates."""
        return self._kind in SELF_UPDATING_KINDS or self._kind == "script"

    def set_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled) and self.available
        if enabled == self._enabled:
            return
        self._enabled = enabled
        if enabled:
            QTimer.singleShot(self._first_delay_ms, self._first_check)
            self._timer.start()
        else:
            self._timer.stop()

    def _first_check(self) -> None:
        if self._enabled:
            self.check_now()

    def report_last_update(self) -> None:
        """Called once at start: tell how the previous in-app update went
        and drop downloads that are no longer needed."""
        cleanup_downloads()
        code = read_and_clear_status()
        if code is None:
            return
        log.info("Previous in-app update finished with code %s (now %s).", code, __version__)
        if code == 0:
            self._widget.notify(f"Orizon Call è stato aggiornato alla versione {__version__}.",
                                kind="info", duration_ms=6000)
        else:
            self._widget.notify("Aggiornamento non riuscito: resta la versione precedente. "
                                f"Dettagli in {UPDATE_LOG}", kind="error", duration_ms=10000)

    # -- checking --

    @pyqtSlot()
    def check_now(self, manual: bool = False) -> None:
        if not self.available:
            if manual:
                self._widget.notify(
                    "Questa copia di Orizon Call non si aggiorna da sola (cartella di sviluppo "
                    "o copia portatile): scarica l'ultima versione dalla pagina delle release.",
                    kind="info", duration_ms=7000)
            return
        if self._checking:
            if manual:
                self._widget.notify("Sto già controllando gli aggiornamenti…", kind="info",
                                    duration_ms=3000)
            return
        self._checking = True
        kind = self._kind

        def worker():
            info = prepared = error = None
            try:
                token = self._token_provider()
                self._token = token     # reused by _apply: no blocking call on click
                info = self._checker(token, kind)
                if info.available and kind in SELF_UPDATING_KINDS:
                    try:
                        prepared = self._preparer(info, token, kind)
                    except UpdateCheckError as e:
                        error = str(e)   # still proposed, as a manual download
                    except Exception as e:
                        log.warning("Preparing the update failed", exc_info=True)
                        error = f"Download dell'aggiornamento non riuscito ({e})."
            except UpdateCheckError as e:
                info, error = None, str(e)
            except Exception as e:   # never let a check kill anything
                log.debug("Update check crashed", exc_info=True)
                info, error = None, f"Controllo aggiornamenti non riuscito: {e}"
            self._checked.emit(info, prepared, error, manual)

        threading.Thread(target=worker, daemon=True, name="update-check").start()

    def check_manually(self) -> None:
        self._snoozed_until = 0.0
        self.check_now(manual=True)

    @pyqtSlot(object, object, object, bool)
    def _on_checked(self, info, prepared, error, manual: bool) -> None:
        self._checking = False
        if info is None:
            log.info("Update check: %s", error)
            if manual:
                self._widget.notify(error, kind="warn", duration_ms=8000)
            return
        if not info.available:
            log.info("Update check: %s is the latest version.", info.current)
            if manual:
                self._widget.notify(f"Orizon Call è aggiornato (versione {info.current}).",
                                    kind="info", duration_ms=4000)
            return
        log.info("Update available: %s → %s%s", info.current, info.latest,
                 f" (manual: {error})" if error else "")
        self._pending = (info, prepared, error)
        if manual or self._clock() >= self._snoozed_until:
            self._maybe_prompt()

    # -- prompting --

    def _idle(self) -> bool:
        return self._widget.recorder_state_name() == "idle" and not self._widget.is_busy

    def _maybe_prompt(self) -> None:
        if self._pending is None or self._prompt is not None or not self._idle():
            return
        info, prepared, error = self._pending
        text = f"È disponibile Orizon Call {info.latest} (hai la {info.current})."
        if info.notes:
            text += "\n" + "\n".join(f"• {n}" for n in info.notes[:3])
        ready = prepared is not None or (self._kind == "script" and not error)
        if ready:
            text += "\nL'app si chiude e si riapre aggiornata in pochi secondi."
            actions = [("Aggiorna e riavvia", self._apply, True),
                       ("Più tardi", self._snooze, False)]
        else:
            text += f"\n{error}" if error else ""
            actions = [("Scarica", self._open_release_page, True),
                       ("Più tardi", self._snooze, False)]
        self._prompt = self._widget.show_prompt(text, actions, on_timeout=self._snooze,
                                                duration_ms=60000)

    def _snooze(self) -> None:
        self._prompt = None
        self._snoozed_until = self._clock() + SNOOZE_S

    def _open_release_page(self) -> None:
        self._prompt = None
        info = self._pending[0] if self._pending else None
        url = (info.html_url if info and info.html_url
               else f"https://github.com/{RELEASES_REPO}/releases/latest")
        self._opener(url)

    @pyqtSlot(str)
    def _on_recording_stopped(self, _path: str) -> None:
        if self._pending is not None and self._clock() >= self._snoozed_until:
            # Give the "saved" toast a moment before asking.
            QTimer.singleShot(4000, self._maybe_prompt)

    def _apply(self) -> None:
        self._prompt = None
        if not self._idle():
            self._widget.notify("Aggiornamento rimandato: c'è una registrazione in corso.",
                                kind="warn", duration_ms=5000)
            return
        _info, prepared, _error = self._pending
        try:
            token = self._token if self._kind == "script" else None
            self._launcher(self._kind, prepared, token)
        except Exception as e:
            log.exception("Cannot start the update")
            self._widget.notify(f"Impossibile avviare l'aggiornamento: {e}",
                                kind="error", duration_ms=8000)
            return
        self._widget.notify("Aggiornamento in corso: Orizon Call si riaprirà tra poco.",
                            kind="info", duration_ms=3000)
        QTimer.singleShot(800, self._widget.request_quit)

    def shutdown(self) -> None:
        self._enabled = False
        self._timer.stop()
