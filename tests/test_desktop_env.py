"""Desktop integration of the packaged app: clean child environment,
AppImage menu entry, macOS location checks and the move-to-Applications
helper (executed for real with the macOS-only tools stubbed)."""

import subprocess
import sys
from pathlib import Path

import pytest

import desktop_env


class TestCleanEnv:

    def test_source_run_is_untouched(self):
        env = {"QT_PLUGIN_PATH": "/x", "_PYI_ARCHIVE_FILE": "y", "HOME": "/h"}
        assert desktop_env.clean_child_env(env) == env

    def test_frozen_strips_bundle_variables(self, monkeypatch, tmp_path):
        monkeypatch.setattr(desktop_env, "FROZEN", True)
        monkeypatch.setattr(desktop_env, "bundle_dir", lambda: tmp_path)
        env = {
            "_PYI_APPLICATION_HOME_DIR": str(tmp_path),
            "_MEIPASS2": str(tmp_path),
            "QT_PLUGIN_PATH": str(tmp_path / "PyQt6" / "plugins"),
            "PYTHONPATH": "/user/own/path",          # not ours: kept
            "HOME": "/home/u",
        }
        out = desktop_env.clean_child_env(env)
        assert out == {"PYTHONPATH": "/user/own/path", "HOME": "/home/u",
                       "PYINSTALLER_RESET_ENVIRONMENT": "1"}


class TestAppImageEntry:

    def test_created_then_idempotent_then_follows_moves(self, tmp_path):
        image = tmp_path / "Scaricati" / "Orizon Call.AppImage"
        image.parent.mkdir()
        image.write_text("")
        data = tmp_path / "share"
        assert desktop_env.ensure_linux_desktop_entry(image, data) is True
        entry = data / "applications" / "orizon-call.desktop"
        text = entry.read_text()
        assert f'Exec="{image}"' in text                # spec quoting: path has a space
        assert "Icon=orizon-call" in text
        assert (data / "icons" / "hicolor" / "256x256" / "apps" / "orizon-call.png").is_file()
        assert desktop_env.ensure_linux_desktop_entry(image, data) is False
        moved = tmp_path / "Apps" / "OrizonCall.AppImage"
        moved.parent.mkdir()
        moved.write_text("")
        assert desktop_env.ensure_linux_desktop_entry(moved, data) is True
        assert str(moved) in entry.read_text()

    @pytest.mark.parametrize("arg, quoted", [
        ("/home/u/OrizonCall.AppImage", "/home/u/OrizonCall.AppImage"),
        ("/home/u/My Apps/O.AppImage", '"/home/u/My Apps/O.AppImage"'),
        ('/x/a"b$c', '"/x/a\\\\"b\\\\$c"'),
    ])
    def test_exec_quoting(self, arg, quoted):
        assert desktop_env.desktop_exec_quote(arg) == quoted

    def test_not_an_appimage(self, monkeypatch):
        monkeypatch.delenv("APPIMAGE", raising=False)
        assert desktop_env.appimage_path() is None
        assert desktop_env.ensure_linux_desktop_entry() is False


class TestMacLocation:

    @pytest.mark.parametrize("path, problem", [
        ("/private/var/folders/x/T/AppTranslocation/ABC/d/Orizon Call.app", "translocated"),
        ("/Volumes/Orizon Call/Orizon Call.app", "dmg"),
    ])
    def test_problems(self, path, problem):
        assert desktop_env.macos_location_problem(Path(path)) == problem

    def test_fine_and_none(self, tmp_path):
        app = tmp_path / "Orizon Call.app"
        app.mkdir()
        assert desktop_env.macos_location_problem(app) is None
        assert desktop_env.macos_location_problem(None) is None

    def test_not_a_bundle_when_running_from_source(self):
        assert desktop_env.macos_bundle_path() is None


@pytest.mark.skipif(sys.platform == "win32", reason="bash helper")
def test_move_helper_copies_and_opens(tmp_path):
    src = tmp_path / "Volumes" / "Orizon Call.app"
    (src / "Contents").mkdir(parents=True)
    (src / "Contents" / "Info.plist").write_text("new")
    dest = tmp_path / "Applications" / "Orizon Call.app"
    (dest / "Contents").mkdir(parents=True)
    (dest / "Contents" / "Info.plist").write_text("old copy")
    opened = tmp_path / "opened"
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    script = desktop_env.build_move_helper(dead.pid, src, dest)
    script = (script.replace("/usr/bin/ditto", "cp -R")
                    .replace("/usr/bin/xattr", "true")
                    .replace("/usr/bin/open", f'echo >>"{opened}"'))
    helper = tmp_path / "move.sh"
    helper.write_text(script)
    subprocess.run(["bash", str(helper)], check=True, timeout=30, env={"PATH": "/usr/bin:/bin"})
    assert (dest / "Contents" / "Info.plist").read_text() == "new"
    assert opened.exists() and not helper.exists()
