"""In-app updates: installed-commit detection, GitHub comparison (API
stubbed), the detached update helper (run for real on POSIX with a fake
installer and a fake app) and the prompt flow of UpdateManager."""

import subprocess
import sys
import time
import urllib.error
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

import updater
from updater import UpdateCheckError, UpdateInfo, UpdateManager

SHA_A = "a" * 40
SHA_B = "b" * 40


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


class TestLocalCommit:

    def test_detached_head(self, tmp_path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "HEAD").write_text(SHA_A + "\n")
        assert updater.local_commit(tmp_path) == SHA_A

    def test_branch_ref_and_packed_refs(self, tmp_path):
        git = tmp_path / ".git"
        git.mkdir()
        (git / "HEAD").write_text("ref: refs/heads/master\n")
        (git / "packed-refs").write_text(f"# pack-refs\n{SHA_B} refs/heads/master\n")
        assert updater.local_commit(tmp_path) == SHA_B
        (git / "refs" / "heads").mkdir(parents=True)
        (git / "refs" / "heads" / "master").write_text(SHA_A + "\n")
        assert updater.local_commit(tmp_path) == SHA_A

    def test_archive_marker(self, tmp_path):
        (tmp_path / updater.MARKER_FILE).write_text("abc1234\n")
        assert updater.local_commit(tmp_path) == "abc1234"

    def test_unknown(self, tmp_path):
        assert updater.local_commit(tmp_path) is None

    def test_same_commit(self):
        assert updater.same_commit(SHA_A, SHA_A)
        assert updater.same_commit("aaaaaaa", SHA_A)
        assert not updater.same_commit("aaaa", SHA_A)      # too short to trust
        assert not updater.same_commit(SHA_A, SHA_B)
        assert not updater.same_commit(None, SHA_A)


class TestManagedInstall:

    def test_dev_checkout_is_not_managed(self, tmp_path):
        assert updater.is_managed_install(tmp_path) is False

    def test_installer_folder_is_managed(self, tmp_path, monkeypatch):
        app = tmp_path / "app"
        app.mkdir()
        (app / ("install.ps1" if sys.platform == "win32" else "install.sh")).write_text("")
        monkeypatch.setattr(updater, "managed_app_dir", lambda: app)
        assert updater.is_managed_install(app) is True


def _commit(msg):
    return {"commit": {"message": msg}}


class TestCheck:

    def _api(self, monkeypatch, responses):
        def fake(path, token):
            value = responses[path.split("/")[0]]
            if isinstance(value, Exception):
                raise value
            return value
        monkeypatch.setattr(updater, "_api_get", fake)

    def test_up_to_date(self, monkeypatch):
        self._api(monkeypatch, {"commits": {"sha": SHA_A}})
        info = updater.check_for_update(SHA_A, "t")
        assert info.available is False

    def test_update_with_changelog(self, monkeypatch):
        self._api(monkeypatch, {
            "commits": {"sha": SHA_B},
            "compare": {"status": "ahead", "ahead_by": 3, "commits": [
                _commit("Prima novità\n\ndettagli"),
                _commit("Merge branch x"),
                _commit("Seconda novità"),
            ]},
        })
        info = updater.check_for_update(SHA_A, "t")
        assert info.available and info.count == 3
        assert info.changes == ["Seconda novità", "Prima novità"]

    def test_installed_newer_than_master(self, monkeypatch):
        self._api(monkeypatch, {"commits": {"sha": SHA_B},
                                "compare": {"status": "behind", "ahead_by": 0}})
        assert updater.check_for_update(SHA_A, "t").available is False

    def test_compare_failure_still_proposes(self, monkeypatch):
        self._api(monkeypatch, {"commits": {"sha": SHA_B},
                                "compare": urllib.error.URLError("x")})
        info = updater.check_for_update("abc1234", "t")
        assert info.available and info.count == 0

    def test_private_repo_without_token(self, monkeypatch):
        err = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
        self._api(monkeypatch, {"commits": err})
        with pytest.raises(UpdateCheckError, match="gh auth login"):
            updater.check_for_update(SHA_A, None)

    def test_offline(self, monkeypatch):
        self._api(monkeypatch, {"commits": urllib.error.URLError("down")})
        with pytest.raises(UpdateCheckError, match="offline"):
            updater.check_for_update(SHA_A, None)


class TestStatus:

    def test_read_and_clear(self, tmp_path, monkeypatch):
        status = tmp_path / "update_status"
        monkeypatch.setattr(updater, "STATUS_FILE", status)
        assert updater.read_and_clear_status() is None
        status.write_text("0\n")
        assert updater.read_and_clear_status() == 0
        assert not status.exists()
        status.write_text("garbage")
        assert updater.read_and_clear_status() == 1


def test_windows_helper_script_shape(tmp_path):
    script = updater.build_windows_helper(4242, tmp_path / "O'app", r"C:\v\pythonw.exe", ["--verbose"])
    assert "Wait-Process -Id 4242" in script
    assert "O''app" in script                      # PowerShell quote escaping
    assert "ORIZON_CALL_NONINTERACTIVE" in script
    assert "Start-Process" in script and "--verbose" in script
    assert "GITHUB_TOKEN = " not in script         # the token never lands on disk


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX helper")
def test_posix_helper_end_to_end(tmp_path, monkeypatch):
    """Run the real helper: waits for the (already gone) app, runs the
    installer with the token from the environment, records the exit code
    and relaunches the app with the original arguments."""
    app = tmp_path / "my app"
    app.mkdir()
    (app / "install.sh").write_text(
        'echo "installer ran ref=$ORIZON_CALL_REF token=$GITHUB_TOKEN"\n'
        f'echo updated > "{tmp_path}/installed"\n')
    fake_python = tmp_path / "python"
    fake_python.write_text(f'#!/bin/sh\necho "$@" > "{tmp_path}/relaunched"\n'
                           f'echo "token=$GITHUB_TOKEN" >> "{tmp_path}/relaunched"\n')
    fake_python.chmod(0o755)
    monkeypatch.setattr(updater, "STATUS_FILE", tmp_path / "update_status")
    monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "logs" / "update.log")

    dead = subprocess.Popen(["true"])
    dead.wait()
    script = updater.build_posix_helper(dead.pid, app, str(fake_python), ["--verbose"])
    helper = tmp_path / "helper.sh"
    helper.write_text(script)
    subprocess.run(["bash", str(helper)], env={"PATH": "/usr/bin:/bin", "GITHUB_TOKEN": "s3cret"},
                   timeout=30, check=True)

    assert (tmp_path / "installed").read_text().strip() == "updated"
    assert (tmp_path / "update_status").read_text().strip() == "0"
    log = (tmp_path / "logs" / "update.log").read_text()
    assert "ref=master" in log and "token=s3cret" in log
    relaunched = (tmp_path / "relaunched").read_text().splitlines()
    assert relaunched[0] == f"{app / 'main.py'} --verbose"
    assert relaunched[1] == "token="              # not inherited by the app
    assert not helper.exists()                     # deletes itself
    assert "s3cret" not in script


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


def _press(prompt, label):
    for text, cb, _accent in prompt.actions:
        if text == label:
            return cb()
    raise AssertionError(label)


def _manager(checker, launcher=None, clock=None, managed=True):
    w = FakeWidget()
    launched = []
    m = UpdateManager(w, enabled=False, managed=managed, checker=checker,
                      launcher=launcher or launched.append,
                      token_provider=lambda: "tok", clock=clock or (lambda: 0.0))
    return w, m, launched


AVAILABLE = UpdateInfo(True, SHA_B, SHA_A, 2, ["Rilevamento call più veloce", "Fix"])


class TestManager:

    def test_prompt_and_apply(self, qapp, monkeypatch):
        monkeypatch.setattr(updater.QTimer, "singleShot", staticmethod(lambda ms, fn: fn()))
        w, m, launched = _manager(lambda sha, tok: AVAILABLE)
        m.check_now()
        assert _pump(qapp, lambda: bool(w.prompts))
        assert "2 novità" in w.prompts[0].text and "Rilevamento call" in w.prompts[0].text
        _press(w.prompts[0], "Aggiorna ora")
        assert launched == ["tok"] and w.quits == 1

    def test_never_during_recording(self, qapp, monkeypatch):
        monkeypatch.setattr(updater.QTimer, "singleShot", staticmethod(lambda ms, fn: fn()))
        w, m, launched = _manager(lambda sha, tok: AVAILABLE)
        w.state = "recording"
        m.check_now()
        assert not _pump(qapp, lambda: bool(w.prompts), timeout=0.3)
        w.state = "idle"
        w.recording_stopped.emit("/tmp/x.wav")
        assert len(w.prompts) == 1

    def test_later_snoozes(self, qapp):
        now = [0.0]
        w, m, _ = _manager(lambda sha, tok: AVAILABLE, clock=lambda: now[0])
        m.check_now()
        assert _pump(qapp, lambda: bool(w.prompts))
        _press(w.prompts[0], "Più tardi")
        now[0] = 3600
        m.check_now()
        assert not _pump(qapp, lambda: len(w.prompts) > 1, timeout=0.3)
        now[0] = updater.SNOOZE_S + 1
        m.check_now()
        assert _pump(qapp, lambda: len(w.prompts) == 2)

    def test_manual_check_reports_everything(self, qapp):
        w, m, _ = _manager(lambda sha, tok: UpdateInfo(False, SHA_A, SHA_A))
        m.check_manually()
        assert _pump(qapp, lambda: bool(w.notes))
        assert "già aggiornato" in w.notes[0][1]

        def failing(sha, tok):
            raise UpdateCheckError("Accesso a GitHub negato")
        w2, m2, _ = _manager(failing)
        m2.check_now()                      # automatic: silent
        assert not _pump(qapp, lambda: bool(w2.notes), timeout=0.3)
        m2.check_manually()
        assert _pump(qapp, lambda: bool(w2.notes))

    def test_dev_checkout_never_updates(self, qapp):
        calls = []
        w, m, _ = _manager(lambda sha, tok: calls.append(1), managed=False)
        m.set_enabled(True)
        m.check_now()
        assert not calls and m.available is False
        m.check_manually()
        assert "git pull" in w.notes[0][1]

    def test_launch_failure_keeps_app_running(self, qapp):
        def boom(token):
            raise OSError("no bash")
        w, m, _ = _manager(lambda sha, tok: AVAILABLE, launcher=boom)
        m.check_now()
        assert _pump(qapp, lambda: bool(w.prompts))
        _press(w.prompts[0], "Aggiorna ora")
        assert w.quits == 0 and w.notes[-1][0] == "error"

    def test_report_last_update(self, qapp, tmp_path, monkeypatch):
        monkeypatch.setattr(updater, "STATUS_FILE", tmp_path / "st")
        w, m, _ = _manager(lambda sha, tok: AVAILABLE)
        m.report_last_update()
        assert not w.notes
        (tmp_path / "st").write_text("0")
        m.report_last_update()
        (tmp_path / "st").write_text("1")
        m.report_last_update()
        assert [k for k, _ in w.notes] == ["info", "error"]
