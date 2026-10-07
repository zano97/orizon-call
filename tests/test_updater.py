"""In-app updates from GitHub Releases: version parsing, release lookup
(API stubbed), download + checksum verification (real HTTP on localhost,
including the token-stripping redirect), the per-platform helper scripts
(executed for real where the OS allows) and UpdateManager's prompt flow."""

import hashlib
import http.server
import json
import subprocess
import sys
import threading
import time
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

import updater
from updater import PreparedUpdate, UpdateCheckError, UpdateInfo, UpdateManager
from version import parse_version

POSIX = sys.platform != "win32"


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _pump(qapp, predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


# ---------- versions, notes, checksums ----------

class TestVersions:

    def test_ordering(self):
        assert parse_version("v1.10.0") > parse_version("1.9.9")
        assert parse_version("1.2") == parse_version("1.2.0")
        assert parse_version("1.2.0") > parse_version("1.2.0-rc1")
        assert parse_version("1.2.0-rc1") > parse_version("1.1.9")
        assert parse_version("garbage") == (0,)

    def test_release_bullets(self):
        body = "Intro\n\n- Prima **novità**\n* Seconda\n\nAltro testo\n- Terza"
        assert updater.release_bullets(body) == ["Prima novità", "Seconda", "Terza"]
        assert updater.release_bullets("", limit=3) == []

    def test_parse_checksums(self):
        a, b = "a" * 64, "b" * 64
        text = f"{a}  OrizonCall-1.1.0-linux-x86_64.AppImage\n{b} *Setup.exe\nnot a line\n"
        assert updater.parse_checksums(text) == {
            "OrizonCall-1.1.0-linux-x86_64.AppImage": a, "Setup.exe": b}


class TestInstallKind:

    def test_source_checkout_is_dev(self):
        assert updater.install_kind() == "dev"

    def test_managed_script_install(self, tmp_path, monkeypatch):
        app = tmp_path / "app"
        app.mkdir()
        (app / ("install.ps1" if sys.platform == "win32" else "install.sh")).write_text("")
        monkeypatch.setattr(updater, "managed_app_dir", lambda: app)
        assert updater.is_managed_install(app)
        assert not updater.is_managed_install(tmp_path)

    @pytest.mark.parametrize("kind, suffix", [
        ("windows-installer", "-windows-x64-setup.exe"),
        ("script", None), ("dev", None),
    ])
    def test_asset_suffix(self, kind, suffix):
        assert updater.asset_suffix(kind) == suffix

    def test_asset_suffix_appimage_and_mac(self, monkeypatch):
        monkeypatch.setattr(updater.platform, "machine", lambda: "x86_64")
        assert updater.asset_suffix("appimage") == "-linux-x86_64.AppImage"
        monkeypatch.setattr(updater, "_mac_hardware_arch", lambda: "arm64")
        assert updater.asset_suffix("macos-app") == "-macos-arm64.zip"


# ---------- release lookup ----------

def _release(tag="v1.1.0", assets=None):
    return {"tag_name": tag, "html_url": "https://example/rel",
            "body": "- Novità uno\n- Novità due",
            "assets": assets if assets is not None else [
                {"name": f"OrizonCall-{tag[1:]}-windows-x64-setup.exe", "size": 10},
                {"name": f"OrizonCall-{tag[1:]}-linux-x86_64.AppImage", "size": 10},
                {"name": "SHA256SUMS.txt", "size": 1},
            ]}


class TestCheck:

    def _api(self, monkeypatch, value):
        def fake(path, token):
            assert path == "releases/latest"
            if isinstance(value, Exception):
                raise value
            return value
        monkeypatch.setattr(updater, "_api_get", fake)

    def test_newer_release_with_asset(self, monkeypatch):
        self._api(monkeypatch, _release())
        info = updater.check_for_update(None, "windows-installer", current="1.0.0")
        assert info.available and info.latest == "1.1.0" and info.current == "1.0.0"
        assert info.asset["name"].endswith("-windows-x64-setup.exe")
        assert info.checksums["name"] == "SHA256SUMS.txt"
        assert info.notes == ["Novità uno", "Novità due"]

    def test_same_or_older_is_not_an_update(self, monkeypatch):
        self._api(monkeypatch, _release("v1.0.0"))
        assert not updater.check_for_update(None, "appimage", current="1.0.0").available
        assert not updater.check_for_update(None, "appimage", current="1.2.0").available

    def test_missing_platform_asset(self, monkeypatch):
        self._api(monkeypatch, _release(assets=[{"name": "SHA256SUMS.txt"}]))
        info = updater.check_for_update(None, "windows-installer", current="1.0.0")
        assert info.available and info.asset is None

    def test_private_repo_without_token(self, monkeypatch):
        self._api(monkeypatch, urllib.error.HTTPError("u", 404, "Not Found", {}, None))
        with pytest.raises(UpdateCheckError, match="gh auth login"):
            updater.check_for_update(None, "appimage")

    def test_no_release_yet(self, monkeypatch):
        self._api(monkeypatch, urllib.error.HTTPError("u", 404, "Not Found", {}, None))
        with pytest.raises(UpdateCheckError, match="Nessuna versione"):
            updater.check_for_update("token", "appimage")

    def test_offline(self, monkeypatch):
        self._api(monkeypatch, urllib.error.URLError("down"))
        with pytest.raises(UpdateCheckError, match="offline"):
            updater.check_for_update(None, "appimage")


# ---------- download over real HTTP ----------

class _Server:
    """Tiny localhost HTTP server: serves ``files``, can redirect, and
    records the Authorization header of every request."""

    def __init__(self, files=None, redirects=None):
        self.files = files or {}
        self.redirects = redirects or {}
        self.auth_seen = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.auth_seen.append((self.path, self.headers.get("Authorization")))
                if self.path in outer.redirects:
                    self.send_response(302)
                    self.send_header("Location", outer.redirects[self.path])
                    self.end_headers()
                    return
                data = outer.files.get(self.path)
                if data is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, path, host="127.0.0.1"):
        return f"http://{host}:{self.port}{path}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture()
def no_proxy(monkeypatch):
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    import urllib.request
    monkeypatch.setattr(updater, "_opener", urllib.request.build_opener(
        urllib.request.ProxyHandler({}), updater._DropAuthOnRedirect))


def test_token_not_forwarded_to_other_host(no_proxy, tmp_path):
    storage = _Server(files={"/blob": b"payload"})
    api = _Server(redirects={"/asset": storage.url("/blob", host="localhost")})
    try:
        asset = {"name": "x", "size": 7, "url": api.url("/asset"),
                 "browser_download_url": api.url("/asset")}
        digest = updater._download(asset, tmp_path / "x", "s3cret")
        assert (tmp_path / "x").read_bytes() == b"payload"
        assert digest == hashlib.sha256(b"payload").hexdigest()
        assert api.auth_seen == [("/asset", "Bearer s3cret")]
        assert storage.auth_seen == [("/blob", None)]
    finally:
        api.close()
        storage.close()


def test_truncated_download_is_rejected(no_proxy, tmp_path):
    srv = _Server(files={"/f": b"short"})
    try:
        asset = {"name": "f", "size": 999, "browser_download_url": srv.url("/f")}
        with pytest.raises(UpdateCheckError, match="interrotto"):
            updater._download(asset, tmp_path / "f", None)
        assert not (tmp_path / "f").exists()
    finally:
        srv.close()


class TestPrepare:

    NAME = "OrizonCall-1.1.0-linux-x86_64.AppImage"

    def _setup(self, monkeypatch, tmp_path, payload=b"#!/bin/sh\necho new\n", sums=None):
        sums = sums if sums is not None else f"{hashlib.sha256(payload).hexdigest()}  {self.NAME}\n"
        srv = _Server(files={"/app": payload, "/sums": sums.encode()})
        target = tmp_path / "OrizonCall.AppImage"
        target.write_text("old")
        monkeypatch.setattr(updater, "_target_for", lambda kind: target)
        info = UpdateInfo(True, "1.0.0", "1.1.0",
                          asset={"name": self.NAME, "size": len(payload),
                                 "browser_download_url": srv.url("/app")},
                          checksums={"name": "SHA256SUMS.txt", "browser_download_url": srv.url("/sums")})
        return srv, info, target

    def test_download_verify_and_reuse(self, no_proxy, monkeypatch, tmp_path):
        srv, info, target = self._setup(monkeypatch, tmp_path)
        cache = tmp_path / "cache"
        (cache / "0.9.0").mkdir(parents=True)        # stale download: removed
        try:
            prepared = updater.prepare_update(info, None, "appimage", cache_dir=cache)
            assert prepared.payload.read_bytes().startswith(b"#!/bin/sh")
            assert prepared.target == target and prepared.version == "1.1.0"
            if POSIX:
                assert prepared.payload.stat().st_mode & 0o111
            assert not (cache / "0.9.0").exists()
            hits = len(srv.auth_seen)
            updater.prepare_update(info, None, "appimage", cache_dir=cache)
            # Already verified: only the checksum list is fetched again.
            assert [p for p, _ in srv.auth_seen[hits:]] == ["/sums"]
        finally:
            srv.close()

    def test_checksum_mismatch_rejected(self, no_proxy, monkeypatch, tmp_path):
        srv, info, _ = self._setup(monkeypatch, tmp_path, sums=f"{'0' * 64}  {self.NAME}\n")
        try:
            with pytest.raises(UpdateCheckError, match="checksum"):
                updater.prepare_update(info, None, "appimage", cache_dir=tmp_path / "c")
            assert not (tmp_path / "c" / "1.1.0" / self.NAME).exists()
        finally:
            srv.close()

    def test_asset_missing_from_checksums(self, no_proxy, monkeypatch, tmp_path):
        srv, info, _ = self._setup(monkeypatch, tmp_path, sums=f"{'0' * 64}  other.bin\n")
        try:
            with pytest.raises(UpdateCheckError, match="checksum"):
                updater.prepare_update(info, None, "appimage", cache_dir=tmp_path / "c")
        finally:
            srv.close()

    def test_release_without_checksums_refused(self, tmp_path):
        info = UpdateInfo(True, "1.0.0", "1.1.0", asset={"name": "x"}, checksums=None)
        with pytest.raises(UpdateCheckError, match="sicurezza"):
            updater.prepare_update(info, None, "appimage", cache_dir=tmp_path)

    def test_no_asset_for_this_platform(self, tmp_path):
        info = UpdateInfo(True, "1.0.0", "1.1.0", asset=None)
        with pytest.raises(UpdateCheckError, match="pacchetto"):
            updater.prepare_update(info, None, "appimage", cache_dir=tmp_path)


def test_cleanup_downloads(tmp_path):
    for v in ("0.9.0", "1.0.0", "1.1.0"):
        (tmp_path / v).mkdir()
    updater.cleanup_downloads(tmp_path, current="1.0.0")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["1.1.0"]


def test_read_and_clear_status(tmp_path, monkeypatch):
    status = tmp_path / "update_status"
    monkeypatch.setattr(updater, "STATUS_FILE", status)
    assert updater.read_and_clear_status() is None
    status.write_text("0\n")
    assert updater.read_and_clear_status() == 0 and not status.exists()
    status.write_text("garbage")
    assert updater.read_and_clear_status() == 1


# ---------- helper scripts ----------

@pytest.fixture()
def helper_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "STATUS_FILE", tmp_path / "state" / "update_status")
    monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "logs" / "update.log")
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    return SimpleNamespace(pid=dead.pid, status=tmp_path / "state" / "update_status",
                           log=tmp_path / "logs" / "update.log")


def _run_bash(script: str, tmp_path: Path, env=None):
    path = tmp_path / "helper.sh"
    path.write_text(script)
    subprocess.run(["bash", str(path)], env=env or {"PATH": "/usr/bin:/bin"}, timeout=60, check=True)
    assert not path.exists()       # the helper deletes itself


@pytest.mark.skipif(not POSIX, reason="bash helper")
def test_appimage_helper_replaces_and_relaunches(tmp_path, helper_paths):
    target = tmp_path / "Apps dir" / "OrizonCall.AppImage"
    target.parent.mkdir()
    marker = tmp_path / "relaunched"
    new = tmp_path / "new.AppImage"
    new.write_text(f'#!/bin/sh\necho "new $*" > "{marker}"\n')
    target.write_text("#!/bin/sh\necho old\n")
    script = updater.build_appimage_helper(
        helper_paths.pid, PreparedUpdate("appimage", "1.1.0", new, target), ["--verbose"])
    _run_bash(script, tmp_path)
    assert helper_paths.status.read_text().strip() == "0"
    assert marker.read_text().strip() == "new --verbose"     # relaunched from the target path
    assert not new.exists()
    assert "1.1.0" in helper_paths.log.read_text()


@pytest.mark.skipif(not POSIX, reason="bash helper")
def test_macos_helper_swaps_bundle(tmp_path, helper_paths):
    """The real swap logic; /usr/bin/open and xattr are stubbed out so it
    also runs on Linux CI."""
    apps = tmp_path / "Applications"
    current = apps / "Orizon Call.app"
    (current / "Contents").mkdir(parents=True)
    (current / "Contents" / "version").write_text("1.0.0")
    staged = tmp_path / "staged" / "Orizon Call.app"
    (staged / "Contents").mkdir(parents=True)
    (staged / "Contents" / "version").write_text("1.1.0")
    opened = tmp_path / "opened"
    script = updater.build_macos_helper(
        helper_paths.pid, PreparedUpdate("macos-app", "1.1.0", staged, current), ["--verbose"])
    script = script.replace("/usr/bin/xattr", "true").replace(
        "exec /usr/bin/open", f'echo >"{opened}"')
    _run_bash(script, tmp_path)
    assert (current / "Contents" / "version").read_text() == "1.1.0"
    assert not staged.exists() and not Path(str(current) + ".old-update").exists()
    assert helper_paths.status.read_text().strip() == "0"
    assert opened.exists()


@pytest.mark.skipif(not POSIX, reason="bash helper")
def test_macos_helper_rolls_back(tmp_path, helper_paths):
    current = tmp_path / "Orizon Call.app"
    (current / "Contents").mkdir(parents=True)
    missing = tmp_path / "nope" / "Orizon Call.app"     # staged copy vanished
    script = updater.build_macos_helper(
        helper_paths.pid, PreparedUpdate("macos-app", "1.1.0", missing, current), [])
    script = script.replace("/usr/bin/xattr", "true").replace("exec /usr/bin/open", "true")
    _run_bash(script, tmp_path)
    assert (current / "Contents").is_dir()                 # old app restored
    assert helper_paths.status.read_text().strip() == "1"


def test_windows_installer_helper_shape(tmp_path):
    prepared = PreparedUpdate("windows-installer", "1.1.0",
                              tmp_path / "OrizonCall-1.1.0-windows-x64-setup.exe",
                              Path(r"C:\Users\O'Brien\AppData\Local\Programs\Orizon Call"))
    script = updater.build_windows_installer_helper(4242, prepared, r"C:\X\Orizon Call.exe", [])
    assert "Wait-Process -Id 4242" in script
    assert "/VERYSILENT" in script and "/SUPPRESSMSGBOXES" in script
    assert "O''Brien" in script                            # PowerShell quote escaping
    assert "-ArgumentList" not in script.split("Start-Process -FilePath 'C:\\X")[1]  # no empty args
    with_args = updater.build_windows_installer_helper(1, prepared, r"C:\X\a.exe", ["--verbose"])
    assert '"--verbose"' in with_args


@pytest.mark.skipif(not POSIX, reason="bash helper")
def test_script_install_helper_end_to_end(tmp_path, helper_paths):
    app = tmp_path / "my app"
    app.mkdir()
    (app / "install.sh").write_text(
        'echo "installer ref=$ORIZON_CALL_REF token=$GITHUB_TOKEN"\n'
        f'echo updated > "{tmp_path}/installed"\n')
    fake_python = tmp_path / "python"
    fake_python.write_text(f'#!/bin/sh\necho "$@" > "{tmp_path}/relaunched"\n'
                           f'echo "token=$GITHUB_TOKEN" >> "{tmp_path}/relaunched"\n')
    fake_python.chmod(0o755)
    script = updater.build_script_posix_helper(helper_paths.pid, app, str(fake_python), ["--verbose"])
    _run_bash(script, tmp_path, env={"PATH": "/usr/bin:/bin", "GITHUB_TOKEN": "s3cret"})
    assert (tmp_path / "installed").read_text().strip() == "updated"
    assert helper_paths.status.read_text().strip() == "0"
    assert "ref=master" in helper_paths.log.read_text()
    relaunched = (tmp_path / "relaunched").read_text().splitlines()
    assert relaunched == [f"{app / 'main.py'} --verbose", "token="]
    assert "s3cret" not in script


# ---------- UpdateManager ----------

class FakeWidget(QObject):
    recording_stopped = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.state = "idle"
        self.is_busy = False
        self.prompts = []
        self.notes = []
        self.quits = 0

    def recorder_state_name(self):
        return self.state

    def show_prompt(self, text, actions, on_timeout=None, duration_ms=30000):
        p = SimpleNamespace(text=text, actions=actions, on_timeout=on_timeout)
        self.prompts.append(p)
        return p

    def notify(self, message, kind="warn", duration_ms=6000):
        self.notes.append((kind, message))

    def request_quit(self):
        self.quits += 1


def _labels(prompt):
    return [a[0] for a in prompt.actions]


def _press(prompt, label):
    for text, cb, _accent in prompt.actions:
        if text == label:
            return cb()
    raise AssertionError(f"{label!r} not in {_labels(prompt)}")


INFO = UpdateInfo(True, "1.0.0", "1.1.0", ["Rilevamento call più veloce", "Fix"],
                  "https://example/rel", asset={"name": "a"}, checksums={"name": "s"})
PREPARED = PreparedUpdate("appimage", "1.1.0", Path("/tmp/new"), Path("/tmp/old"))


def _manager(kind="appimage", checker=None, preparer=None, launcher=None, clock=None):
    w = FakeWidget()
    calls = SimpleNamespace(launched=[], opened=[])
    m = UpdateManager(
        w, enabled=False, kind=kind,
        checker=checker or (lambda token, kind: INFO),
        preparer=preparer or (lambda info, token, kind: PREPARED),
        launcher=launcher or (lambda kind, prepared, token: calls.launched.append((kind, prepared, token))),
        token_provider=lambda: "tok", opener=calls.opened.append,
        clock=clock or (lambda: 0.0))
    return w, m, calls


@pytest.fixture()
def instant_timers(monkeypatch):
    monkeypatch.setattr(updater.QTimer, "singleShot", staticmethod(lambda ms, fn: fn()))


class TestManager:

    def test_ready_update_prompt_and_apply(self, qapp, instant_timers):
        w, m, calls = _manager()
        m.check_now()
        assert _pump(qapp, lambda: bool(w.prompts))
        p = w.prompts[0]
        assert "Orizon Call 1.1.0" in p.text and "hai la 1.0.0" in p.text
        assert "Rilevamento call più veloce" in p.text
        assert _labels(p) == ["Aggiorna e riavvia", "Più tardi"]
        _press(p, "Aggiorna e riavvia")
        assert calls.launched == [("appimage", PREPARED, None)]   # no token for packaged apps
        assert w.quits == 1

    def test_preparation_failure_offers_manual_download(self, qapp):
        def failing(info, token, kind):
            raise UpdateCheckError("Sposta Orizon Call nella cartella Applicazioni")
        w, m, calls = _manager(kind="macos-app", preparer=failing)
        m.check_now()
        assert _pump(qapp, lambda: bool(w.prompts))
        assert "Applicazioni" in w.prompts[0].text
        _press(w.prompts[0], "Scarica")
        assert calls.opened == ["https://example/rel"] and not calls.launched

    def test_script_install_runs_installer_with_token(self, qapp, instant_timers):
        prepared_calls = []
        w, m, calls = _manager(kind="script",
                               preparer=lambda *a: prepared_calls.append(a))
        m.check_now()
        assert _pump(qapp, lambda: bool(w.prompts))
        assert not prepared_calls                       # nothing to download
        _press(w.prompts[0], "Aggiorna e riavvia")
        assert calls.launched == [("script", None, "tok")]

    def test_never_during_recording(self, qapp, instant_timers):
        w, m, _ = _manager()
        w.state = "recording"
        m.check_now()
        assert not _pump(qapp, lambda: bool(w.prompts), timeout=0.3)
        w.state = "idle"
        w.recording_stopped.emit("/tmp/x.wav")
        assert len(w.prompts) == 1

    def test_later_snoozes_a_day(self, qapp):
        now = [0.0]
        w, m, _ = _manager(clock=lambda: now[0])
        m.check_now()
        assert _pump(qapp, lambda: bool(w.prompts))
        _press(w.prompts[0], "Più tardi")
        now[0] = 3600
        m.check_now()
        assert not _pump(qapp, lambda: len(w.prompts) > 1, timeout=0.3)
        now[0] = updater.SNOOZE_S + 1
        m.check_now()
        assert _pump(qapp, lambda: len(w.prompts) == 2)

    def test_manual_check_always_answers(self, qapp):
        w, m, _ = _manager(checker=lambda token, kind: UpdateInfo(False, "1.0.0", "1.0.0"))
        m.check_manually()
        assert _pump(qapp, lambda: bool(w.notes))
        assert "aggiornato" in w.notes[0][1]

        def failing(token, kind):
            raise UpdateCheckError("Impossibile contattare GitHub")
        w2, m2, _ = _manager(checker=failing)
        m2.check_now()                      # automatic: silent
        assert not _pump(qapp, lambda: bool(w2.notes), timeout=0.3)
        m2.check_manually()
        assert _pump(qapp, lambda: bool(w2.notes))

    @pytest.mark.parametrize("kind", ["dev", "frozen-other"])
    def test_copies_that_do_not_self_update(self, qapp, kind):
        checks = []
        w, m, _ = _manager(kind=kind, checker=lambda *a: checks.append(a))
        assert m.available is False
        m.set_enabled(True)
        m.check_now()
        assert not checks
        m.check_manually()
        assert "release" in w.notes[0][1]

    def test_launch_failure_keeps_app_running(self, qapp):
        def boom(kind, prepared, token):
            raise OSError("no bash")
        w, m, _ = _manager(launcher=boom)
        m.check_now()
        assert _pump(qapp, lambda: bool(w.prompts))
        _press(w.prompts[0], "Aggiorna e riavvia")
        assert w.quits == 0 and w.notes[-1][0] == "error"

    def test_report_last_update(self, qapp, tmp_path, monkeypatch):
        monkeypatch.setattr(updater, "STATUS_FILE", tmp_path / "st")
        monkeypatch.setattr(updater, "UPDATES_DIR", tmp_path / "updates")
        w, m, _ = _manager()
        m.report_last_update()
        assert not w.notes
        (tmp_path / "st").write_text("0")
        m.report_last_update()
        (tmp_path / "st").write_text("1")
        m.report_last_update()
        assert [k for k, _ in w.notes] == ["info", "error"]
        assert "versione" in w.notes[0][1]


def test_release_json_fixture_is_valid():
    # Guard for the shape the workflow publishes (see release.yml).
    assert json.loads(json.dumps(_release()))["assets"][-1]["name"] == "SHA256SUMS.txt"
