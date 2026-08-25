"""
API server tests against a real ThreadedHTTPServer on a random port,
using a Qt-free fake widget (QObject is enough for queued slots).
"""

import json
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from PyQt6.QtCore import QCoreApplication, QObject, pyqtSlot

import api_server
from api_server import RecorderAPIHandler, start_api_server


class FakeWidget(QObject):
    def __init__(self):
        super().__init__()
        self.state_name = "idle"
        self.calls = []

    def recorder_state_name(self) -> str:
        return self.state_name

    def recorder_status(self) -> dict:
        return {
            "state": self.state_name,
            "elapsed": 1.5,
            "has_system_audio": True,
            "output_path": "/tmp/recording_x.wav",
            "mic_level": 0.1,
            "sys_level": 0.2,
            "muted": False,
            "dropped_chunks": 0,
            "segments": [],
        }

    @pyqtSlot()
    def api_start(self):
        self.calls.append("start")

    @pyqtSlot()
    def api_stop(self):
        self.calls.append("stop")

    @pyqtSlot()
    def api_pause(self):
        self.calls.append("pause")

    @pyqtSlot()
    def api_resume(self):
        self.calls.append("resume")

    @pyqtSlot()
    def api_mute(self):
        self.calls.append("mute")

    @pyqtSlot()
    def api_unmute(self):
        self.calls.append("unmute")

    @pyqtSlot()
    def api_quit(self):
        self.calls.append("quit")


@pytest.fixture(scope="module")
def qapp():
    app = QCoreApplication.instance() or QCoreApplication([])
    yield app


@pytest.fixture()
def server(qapp, tmp_path):
    """Start the server through the bound-socket adoption path (the same
    one main.py uses for the single-instance check)."""
    widget = FakeWidget()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]

    srv = start_api_server(widget, port=port, output_dir=tmp_path,
                           require_auth=True, bound_socket=sock)
    assert srv is not None
    token = RecorderAPIHandler.auth_token
    yield {"port": port, "widget": widget, "token": token, "dir": tmp_path}
    srv.shutdown()
    srv.server_close()


def _request(port, path, method="GET", token=None, headers=None, body=None):
    url = f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url, method=method, data=body)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


class TestAuth:

    def test_health_is_public(self, server):
        status, data = _request(server["port"], "/health")
        assert status == 200 and data == {"ok": True}

    def test_status_requires_token(self, server):
        status, data = _request(server["port"], "/status")
        assert status == 401

    def test_wrong_token_rejected(self, server):
        status, _ = _request(server["port"], "/status", token="nope")
        assert status == 403

    def test_correct_token_accepted(self, server):
        status, data = _request(server["port"], "/status", token=server["token"])
        assert status == 200
        assert data["state"] == "idle"

    def test_non_ascii_token_is_403_not_500(self, server):
        """Raw bytes that aren't valid ASCII must yield a clean 401/403,
        never an exception inside compare_digest."""
        raw = (b"GET /status HTTP/1.1\r\n"
               b"Host: 127.0.0.1\r\n"
               b"Authorization: Bearer cos\xc3\xac-\xe2\x80\xa6\r\n"
               b"Connection: close\r\n\r\n")
        with socket.create_connection(("127.0.0.1", server["port"]), timeout=5) as s:
            s.sendall(raw)
            response = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                response += chunk
        status_line = response.split(b"\r\n", 1)[0]
        assert b" 401" in status_line or b" 403" in status_line

    def test_token_file_permissions(self, server):
        import stat as _stat
        mode = api_server._token_path().stat().st_mode
        assert not (mode & (_stat.S_IRGRP | _stat.S_IROTH))


class TestHostValidation:

    def test_dns_rebinding_host_rejected(self, server):
        status, data = _request(server["port"], "/health",
                                headers={"Host": "evil.example.com"})
        assert status == 403

    def test_localhost_host_ok(self, server):
        status, _ = _request(server["port"], "/health",
                             headers={"Host": f"localhost:{server['port']}"})
        assert status == 200


class TestStateGuards:

    def test_start_while_recording_is_409(self, server):
        server["widget"].state_name = "recording"
        try:
            status, data = _request(server["port"], "/start", method="POST",
                                    token=server["token"])
            assert status == 409
            assert data["state"] == "recording"
        finally:
            server["widget"].state_name = "idle"

    def test_start_when_idle_is_200(self, server):
        status, data = _request(server["port"], "/start", method="POST",
                                token=server["token"])
        assert status == 200 and data["status"] == "starting"

    def test_stop_when_idle_is_409(self, server):
        status, data = _request(server["port"], "/stop", method="POST",
                                token=server["token"])
        assert status == 409

    def test_stop_while_recording_returns_meta(self, server):
        server["widget"].state_name = "recording"
        try:
            status, data = _request(server["port"], "/stop", method="POST",
                                    token=server["token"])
            assert status == 200
            assert data["status"] == "stopping"
            assert "elapsed" in data and "output_path" in data
        finally:
            server["widget"].state_name = "idle"

    def test_post_with_body_is_drained(self, server):
        body = json.dumps({"some": "payload"}).encode()
        status, _ = _request(server["port"], "/start", method="POST",
                             token=server["token"], body=body,
                             headers={"Content-Type": "application/json"})
        assert status in (200, 409)


class TestEvents:

    def test_events_accepts_query_token(self, server):
        """EventSource can't send headers: ?token= must work for /events."""
        url = f"http://127.0.0.1:{server['port']}/events?token={server['token']}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            assert resp.headers["Content-Type"].startswith("text/event-stream")
            line = resp.readline().decode()
            assert line.startswith("data: ")
            payload = json.loads(line[len("data: "):])
            assert payload["state"] == "idle"

    def test_events_rejects_missing_token(self, server):
        status, _ = _request(server["port"], "/events")
        assert status == 401


class TestFiles:

    def test_files_sorted_most_recent_first_across_extensions(self, server):
        d = server["dir"]
        import os
        old = d / "recording_old.wav"
        new = d / "recording_new.mp3"
        other = d / "notes.txt"
        for f in (old, new, other):
            f.write_bytes(b"x")
        os.utime(old, (time.time() - 100, time.time() - 100))
        os.utime(new, (time.time(), time.time()))

        status, data = _request(server["port"], "/files", token=server["token"])
        assert status == 200
        names = [f["name"] for f in data["files"]]
        assert names == ["recording_new.mp3", "recording_old.wav"]
        assert "notes.txt" not in names

    def test_download_non_recording_name_is_403(self, server):
        status, _ = _request(server["port"], "/files/secrets.txt",
                             token=server["token"])
        assert status == 403

    def test_download_missing_recording_is_404(self, server):
        status, _ = _request(server["port"], "/files/recording_none.wav",
                             token=server["token"])
        assert status == 404

    def test_download_path_traversal_tilde(self, server):
        # Even though "recording_~" doesn't have slashes, it could resolve
        # outside the recordings directory.
        status, _ = _request(server["port"], "/files/recording_~",
                             token=server["token"])
        # In Linux it just resolves to recording_~ inside the directory which doesn't exist,
        # but on some path resolution contexts it could expand, so we expect 403 or 404
        assert status in (403, 404)

    def test_download_path_traversal_windows_drive(self, server):
        # Similar edge case handling for C: paths
        status, _ = _request(server["port"], "/files/recording_C:boot.ini",
                             token=server["token"])
        assert status in (400, 403, 404)

    def test_download_real_file(self, server):
        d = server["dir"]
        f = d / "recording_dl.wav"
        f.write_bytes(b"RIFFdata")
        url = f"http://127.0.0.1:{server['port']}/files/recording_dl.wav"
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {server['token']}")
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            assert resp.read() == b"RIFFdata"
