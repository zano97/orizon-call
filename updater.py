"""
In-app updates.

The installers put the app in a fixed place (``~/.orizon-call/app`` on
macOS/Linux, ``%LOCALAPPDATA%\\OrizonCall\\app`` on Windows) tracking the
``master`` branch of the GitHub repository. This module:

1. reads the installed commit (``.git/HEAD`` of the shallow clone, or the
   ``.installed_commit`` marker the installers write for zip/tarball
   installs) — no git executable needed;
2. asks the GitHub API whether ``master`` moved past it, authenticating
   like the installers do (``GITHUB_TOKEN`` / ``GH_TOKEN``, else the
   GitHub CLI's ``gh auth token``: the repository is private);
3. on request, quits the app and hands over to a small detached helper
   that waits for this process to exit, runs the regular installer
   (the very same path as ``orizon-call update``) and starts the app
   again. Updating after exit matters on Windows, where the running
   interpreter keeps the venv's DLLs locked.

The outcome of the installer is written to ``~/.orizon-call/update_status``
and reported by the next start (success toast, or failure + log path).
The token is only ever passed through the helper's environment, never
written to disk.

Development checkouts (anything outside the installer's folder) are never
touched: there ``git pull`` is the user's call.
"""

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from PyQt6.QtCore import QObject, QTimer, pyqtSignal, pyqtSlot

from orizon_logging import get_logger

log = get_logger("updater")

REPO = os.environ.get("ORIZON_CALL_REPO", "zano97/orizon-call")
REF = os.environ.get("ORIZON_CALL_REF", "master")

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path.home() / ".orizon-call"
STATUS_FILE = DATA_DIR / "update_status"
UPDATE_LOG = DATA_DIR / "logs" / "update.log"
MARKER_FILE = ".installed_commit"

FIRST_CHECK_DELAY_MS = 20_000
CHECK_INTERVAL_MS = 6 * 3600 * 1000
SNOOZE_S = 24 * 3600
HTTP_TIMEOUT_S = 10


# ---------- Installation ----------

def managed_app_dir() -> Path:
    """Where the installers put the app on this platform."""
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
    """Installed by install.sh / install.ps1 (and so safe to update in place)."""
    installer = "install.ps1" if sys.platform == "win32" else "install.sh"
    return _same_path(app_dir, managed_app_dir()) and (app_dir / installer).exists()


def _read_ref(git_dir: Path, ref: str) -> Optional[str]:
    path = git_dir / ref
    if path.is_file():
        return path.read_text(encoding="utf-8").strip() or None
    packed = git_dir / "packed-refs"
    if packed.is_file():
        for line in packed.read_text(encoding="utf-8").splitlines():
            parts = line.strip().split(" ", 1)
            if len(parts) == 2 and parts[1] == ref:
                return parts[0]
    return None


def local_commit(app_dir: Path = APP_DIR) -> Optional[str]:
    """Installed commit: full SHA from .git, or the (possibly short) SHA of
    the marker written by the installers for archive downloads."""
    try:
        head = app_dir / ".git" / "HEAD"
        if head.is_file():
            content = head.read_text(encoding="utf-8").strip()
            if content.startswith("ref:"):
                return _read_ref(app_dir / ".git", content[4:].strip())
            return content or None
        marker = app_dir / MARKER_FILE
        if marker.is_file():
            return marker.read_text(encoding="utf-8").strip() or None
    except OSError:
        log.debug("Cannot read the installed commit", exc_info=True)
    return None


def same_commit(a: Optional[str], b: Optional[str]) -> bool:
    """Full vs short SHA compare (archive installs only know 7 chars)."""
    if not a or not b:
        return False
    a, b = a.lower(), b.lower()
    n = min(len(a), len(b))
    return n >= 7 and a[:n] == b[:n]


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
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def github_token() -> Optional[str]:
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(var):
            return os.environ[var].strip()
    gh = find_gh()
    if not gh:
        return None
    try:
        out = subprocess.run([gh, "auth", "token"], capture_output=True, text=True,
                             timeout=10, creationflags=_no_window_flags())
    except (OSError, subprocess.SubprocessError):
        return None
    token = out.stdout.strip()
    return token if out.returncode == 0 and token else None


def _api_get(path: str, token: Optional[str]) -> dict:
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/{path}",
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": "orizon-call-updater",
                 "X-GitHub-Api-Version": "2022-11-28"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


@dataclass
class UpdateInfo:
    available: bool
    remote_sha: str = ""
    local_sha: str = ""
    count: int = 0                                  # commits behind, 0 = unknown
    changes: List[str] = field(default_factory=list)  # first lines, newest first


class UpdateCheckError(Exception):
    """Human-readable reason (Italian): shown on a manual check."""


def summarize_changes(commits: list, limit: int = 5) -> List[str]:
    """First line of each commit message, newest first, merges skipped
    (they repeat the branch's own commits)."""
    lines = []
    for c in reversed(commits):
        msg = (c.get("commit", {}).get("message") or "").strip().splitlines()
        if not msg or msg[0].lower().startswith("merge "):
            continue
        lines.append(msg[0][:90])
        if len(lines) >= limit:
            break
    return lines


def check_for_update(local_sha: Optional[str], token: Optional[str]) -> UpdateInfo:
    """Compare the installed commit with the branch head on GitHub."""
    try:
        head = _api_get(f"commits/{REF}", token)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            raise UpdateCheckError(
                "Accesso a GitHub negato: il repository è privato. Esegui 'gh auth login' "
                "(GitHub CLI) una volta, poi riprova.") from e
        raise UpdateCheckError(f"GitHub ha risposto con errore {e.code}.") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise UpdateCheckError("Impossibile contattare GitHub (sei offline?).") from e
    remote = head.get("sha", "")
    if not remote:
        raise UpdateCheckError("Risposta inattesa da GitHub.")
    if not local_sha:
        raise UpdateCheckError("Versione installata sconosciuta: aggiorna una volta con "
                               "'orizon-call update'.")
    if same_commit(local_sha, remote):
        return UpdateInfo(False, remote, local_sha)
    info = UpdateInfo(True, remote, local_sha)
    try:
        cmp = _api_get(f"compare/{local_sha}...{remote}", token)
        status = cmp.get("status")
        if status in ("identical", "behind"):
            # The installed commit is newer than master (e.g. a branch
            # install): nothing to propose.
            return UpdateInfo(False, remote, local_sha)
        info.count = int(cmp.get("ahead_by") or 0)
        info.changes = summarize_changes(cmp.get("commits") or [])
    except (urllib.error.URLError, OSError, ValueError):
        # Unknown local commit (force-push, archive SHA): master differs,
        # that is enough to propose the update.
        log.debug("Compare failed; proposing the update anyway", exc_info=True)
    return info


# ---------- Applying ----------

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


def _relaunch_python() -> str:
    exe = sys.executable
    if sys.platform == "win32" and os.path.basename(exe).lower() == "python.exe":
        pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.isfile(pyw):
            exe = pyw   # no console window for the restarted app
    return exe


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def build_posix_helper(pid: int, app_dir: Path, python: str, args: List[str]) -> str:
    q = shlex.quote
    relaunch = " ".join(q(a) for a in [python, str(app_dir / "main.py"), *args])
    return f"""#!/usr/bin/env bash
# Orizon Call in-app update helper (temporary file, deletes itself).
rm -f "$0"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
while kill -0 {pid} 2>/dev/null; do sleep 0.5; done
mkdir -p {q(str(UPDATE_LOG.parent))}
ORIZON_CALL_REF={q(REF)} bash {q(str(app_dir / "install.sh"))} > {q(str(UPDATE_LOG))} 2>&1 < /dev/null
echo $? > {q(str(STATUS_FILE))}
unset GITHUB_TOKEN
exec {relaunch}
"""


def build_windows_helper(pid: int, app_dir: Path, python: str, args: List[str]) -> str:
    arglist = " ".join(f'"{a}"' for a in [str(app_dir / "main.py"), *args])
    return f"""# Orizon Call in-app update helper (temporary file, deletes itself).
Remove-Item -LiteralPath $MyInvocation.MyCommand.Path -Force -ErrorAction SilentlyContinue
Wait-Process -Id {pid} -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path {_ps_quote(str(UPDATE_LOG.parent))} | Out-Null
$env:ORIZON_CALL_REF = {_ps_quote(REF)}
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


def launch_update_helper(token: Optional[str], args: Optional[List[str]] = None,
                         app_dir: Path = APP_DIR) -> None:
    """Start the detached helper; the caller must quit the app right after.
    Raises OSError if the helper cannot be started."""
    args = list(sys.argv[1:] if args is None else args)
    pid = os.getpid()
    python = _relaunch_python()
    env = dict(os.environ)
    if token:
        env["GITHUB_TOKEN"] = token
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        STATUS_FILE.unlink()
    except OSError:
        pass
    if sys.platform == "win32":
        script = build_windows_helper(pid, app_dir, python, args)
        fd, path = tempfile.mkstemp(prefix="orizon-update-", suffix=".ps1")
        with os.fdopen(fd, "w", encoding="utf-8-sig") as f:
            f.write(script)
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                 | getattr(subprocess, "CREATE_NO_WINDOW", 0))
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-WindowStyle", "Hidden", "-File", path],
            env=env, creationflags=flags, close_fds=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        script = build_posix_helper(pid, app_dir, python, args)
        fd, path = tempfile.mkstemp(prefix="orizon-update-", suffix=".sh")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(script)
        os.chmod(path, 0o700)
        # New session: no controlling terminal, so the installer never
        # waits on a question, and closing the terminal does not kill it.
        subprocess.Popen(
            ["bash", path], env=env, start_new_session=True, close_fds=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log.info("Update helper started (%s); quitting to let it update and restart.", path)


# ---------- Qt glue ----------

class UpdateManager(QObject):
    """
    Periodic check (first one shortly after start, then every 6 h) and
    the prompt. Never interrupts a recording: an update found meanwhile
    is proposed when the recording ends. "Più tardi" snoozes for a day.

    The widget is duck-typed: ``recorder_state_name``, ``is_busy``,
    ``show_prompt``, ``notify``, ``request_quit``, ``recording_stopped``.
    """

    _checked = pyqtSignal(object, object, bool)   # UpdateInfo | None, error | None, manual

    def __init__(self, widget, enabled: bool = True, managed: Optional[bool] = None,
                 checker=None, launcher=None, token_provider=None,
                 clock=time.monotonic, first_delay_ms: int = FIRST_CHECK_DELAY_MS):
        super().__init__(widget if isinstance(widget, QObject) else None)
        self._widget = widget
        self._managed = is_managed_install() if managed is None else managed
        self._checker = checker or check_for_update
        self._launcher = launcher or launch_update_helper
        self._token_provider = token_provider or github_token
        self._clock = clock
        self._enabled = False
        self._checking = False
        self._pending: Optional[UpdateInfo] = None
        self._snoozed_until = 0.0
        self._prompt = None
        self._timer = QTimer(self)
        self._timer.setInterval(CHECK_INTERVAL_MS)
        self._timer.timeout.connect(self.check_now)
        self._first_delay_ms = first_delay_ms
        self._checked.connect(self._on_checked)
        widget.recording_stopped.connect(self._on_recording_stopped)
        self.set_enabled(enabled)

    @property
    def available(self) -> bool:
        """In-app updates possible for this copy of the app."""
        return self._managed

    def set_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled) and self._managed
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
        """Called once at start: tell how the previous in-app update went."""
        code = read_and_clear_status()
        if code is None:
            return
        if code == 0:
            self._widget.notify("Orizon Call è stato aggiornato all'ultima versione.",
                                kind="info", duration_ms=6000)
        else:
            self._widget.notify(f"Aggiornamento non riuscito: resta la versione precedente. "
                                f"Dettagli in {UPDATE_LOG}", kind="error", duration_ms=10000)

    # -- checking --

    @pyqtSlot()
    def check_now(self, manual: bool = False) -> None:
        if self._checking:
            return
        if not self._managed:
            if manual:
                self._widget.notify(
                    "Questa copia non è stata installata con l'installer (cartella di "
                    "sviluppo): aggiornala con 'git pull'.", kind="info", duration_ms=7000)
            return
        self._checking = True

        def worker():
            info, error = None, None
            try:
                info = self._checker(local_commit(), self._token_provider())
            except UpdateCheckError as e:
                error = str(e)
            except Exception as e:   # never let a check kill anything
                log.debug("Update check crashed", exc_info=True)
                error = f"Controllo aggiornamenti non riuscito: {e}"
            self._checked.emit(info, error, manual)

        threading.Thread(target=worker, daemon=True, name="update-check").start()

    def check_manually(self) -> None:
        self._snoozed_until = 0.0
        self.check_now(manual=True)

    @pyqtSlot(object, object, bool)
    def _on_checked(self, info, error, manual: bool) -> None:
        self._checking = False
        if error:
            log.info("Update check: %s", error)
            if manual:
                self._widget.notify(error, kind="warn", duration_ms=8000)
            return
        if not info.available:
            log.info("Update check: up to date (%s).", info.remote_sha[:7])
            if manual:
                self._widget.notify("Orizon Call è già aggiornato.", kind="info", duration_ms=4000)
            return
        log.info("Update available: %s → %s", info.local_sha[:7], info.remote_sha[:7])
        self._pending = info
        if manual or self._clock() >= self._snoozed_until:
            self._maybe_prompt()

    # -- prompting --

    def _idle(self) -> bool:
        return self._widget.recorder_state_name() == "idle" and not self._widget.is_busy

    def _maybe_prompt(self) -> None:
        info = self._pending
        if info is None or self._prompt is not None:
            return
        if not self._idle():
            return   # proposed when the recording ends
        what = (f"{info.count} novità" if info.count > 1
                else "una novità" if info.count == 1 else "novità")
        text = f"È disponibile un aggiornamento di Orizon Call ({what})."
        if info.changes:
            text += "\n" + "\n".join(f"• {c}" for c in info.changes[:3])
        text += "\nL'app si chiude, si aggiorna e si riapre da sola."
        self._prompt = self._widget.show_prompt(
            text,
            [("Aggiorna ora", self._apply, True),
             ("Più tardi", self._snooze, False)],
            on_timeout=self._snooze, duration_ms=60000)

    def _snooze(self) -> None:
        self._prompt = None
        self._snoozed_until = self._clock() + SNOOZE_S

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
        try:
            self._launcher(self._token_provider())
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
