"""
Regression tests for the reliability / cross-platform review:

* output folder created on demand, safe fallbacks
* PortAudio input-overflow accounting from the callback status flags
* two-pass (measured, linear) loudness normalization and its fallbacks
* MP3 conversion carries the metadata sidecar over
* signal handling delegates to the GUI callback
* API: private-network preflight, robust /files, guarded handlers
* logging under pythonw (no stderr) and thread exception hook
* device detection through the official PyAudioWPatch / pulsectl APIs
* widget: window-manager close never kills a recording
"""

import json
import logging
import os
import sys
import threading
import time
import types
import urllib.request
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

import audio_recorder
from audio_recorder import (
    AudioRecorder,
    SAMPLE_RATE,
    _ffmpeg_codec_args,
    _loudnorm_filter,
    _parse_loudnorm_stats,
)
from tests import fake_ffmpeg


# ---------- helpers ----------

def _make_wav(path: Path, seconds: float = 0.2, level: float = 0.3) -> None:
    data = np.full((int(SAMPLE_RATE * seconds), 2), level, dtype=np.float32)
    sf.write(str(path), data, SAMPLE_RATE, subtype="PCM_16")


_LOUDNORM_JSON = """[Parsed_loudnorm_0 @ 0x1]
{
\t"input_i" : "-23.50",
\t"input_tp" : "-5.00",
\t"input_lra" : "6.10",
\t"input_thresh" : "-33.70",
\t"output_i" : "-16.00",
\t"output_tp" : "-1.50",
\t"output_lra" : "6.10",
\t"output_thresh" : "-26.20",
\t"normalization_type" : "linear",
\t"target_offset" : "0.30"
}
"""

_SILENT_JSON = _LOUDNORM_JSON.replace('"-23.50"', '"-inf"').replace('"-5.00"', '"-inf"') \
                             .replace('"0.30"', '"inf"')


def _two_pass_fake(tmp_path: Path, monkeypatch, measurement_json: str) -> Path:
    """Fake ffmpeg: pass 1 (output '-') prints loudnorm JSON on stderr,
    pass 2 writes the output file. Every invocation's args are logged."""
    args_log = tmp_path / "args.txt"
    fake_ffmpeg.install(tmp_path, monkeypatch, rc=0, measure_json=measurement_json, log=args_log)
    return args_log


@pytest.fixture(scope="module")
def qapp():
    """One QApplication for the whole module (offscreen). The reference is
    held here on purpose: a QApplication that goes out of scope is
    destroyed, and creating a QWidget afterwards aborts the process."""
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


# ---------- output folder ----------

class TestOutputFolder:

    def test_configured_folder_is_created_on_demand(self, tmp_path):
        r = AudioRecorder()
        target = tmp_path / "Registrazioni" / "2026"
        r.set_output_directory(target)
        assert r.output_directory == target
        assert target.is_dir()
        assert r._generate_output_path().parent == target

    def test_unusable_folder_falls_back(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("x")
        r = AudioRecorder()
        r.set_output_directory(blocker / "sub")   # mkdir must fail: parent is a file
        chosen = r.output_directory
        assert chosen == tmp_path / "Downloads"
        assert chosen.is_dir()
        # Disk-space check and file generation agree on the folder.
        assert r._generate_output_path().parent == chosen
        assert r._check_disk_space() is True

    def test_expanduser(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        r = AudioRecorder()
        r.set_output_directory(Path("~/rec"))
        assert r.output_directory == Path(os.path.expanduser("~/rec"))


# ---------- overflow accounting ----------

class TestCallbackStatus:

    def test_input_overflow_is_counted(self):
        r = AudioRecorder()
        status = types.SimpleNamespace(input_overflow=True)
        for _ in range(3):
            r._mic_callback(np.zeros((4, 1), dtype=np.float32), 4, None, status)
        assert r.input_overflows == 3
        clean = types.SimpleNamespace(input_overflow=False)
        r._sys_callback(np.zeros((4, 2), dtype=np.float32), 4, None, clean)
        assert r.input_overflows == 3

    def test_none_status_is_fine(self):
        r = AudioRecorder()
        r._sys_callback(np.zeros((4, 2), dtype=np.float32), 4, None, None)
        assert r.input_overflows == 0

    def test_sidecar_reports_counters(self, tmp_path, monkeypatch):
        r = AudioRecorder()
        r._mic_device = 0
        r._mic_samplerate = SAMPLE_RATE
        r.set_output_directory(tmp_path)
        monkeypatch.setattr(r, "_open_streams", lambda: setattr(r, "_streams_open", True))
        monkeypatch.setattr(r, "_close_streams", lambda: setattr(r, "_streams_open", False))
        path = r.start()
        r._note_callback_status("Microphone", types.SimpleNamespace(input_overflow=True))
        r.stop()
        meta = json.loads(Path(str(path) + ".json").read_text())
        assert meta["input_overflows"] == 1
        assert meta["dropped_chunks"] == 0
        assert meta["format"] == "wav"
        assert "recorded_at" in meta and "finished_at" in meta


# ---------- loudness normalization ----------

class TestLoudnormHelpers:

    def test_parse_stats(self):
        stats = _parse_loudnorm_stats("noise\n" + _LOUDNORM_JSON)
        assert stats == {"input_i": -23.5, "input_lra": 6.1, "input_tp": -5.0,
                         "input_thresh": -33.7, "target_offset": 0.3}

    def test_parse_stats_garbage(self):
        assert _parse_loudnorm_stats("no json here") is None
        assert _parse_loudnorm_stats('{"input_i": "x"}') is None
        assert _parse_loudnorm_stats('{"input_i": "-1"}') is None  # missing keys

    def test_filter_two_pass_vs_single(self):
        measured = _parse_loudnorm_stats(_LOUDNORM_JSON)
        two = _loudnorm_filter(-16.0, measured)
        assert "measured_I=-23.50" in two and "linear=true" in two and "offset=0.30" in two
        single = _loudnorm_filter(-16.0, None)
        assert "measured_I" not in single and "linear" not in single

    def test_codec_per_container(self):
        assert _ffmpeg_codec_args(Path("a.wav")) == ["-c:a", "pcm_s16le"]
        assert _ffmpeg_codec_args(Path("a.FLAC"))[:2] == ["-c:a", "flac"]


class TestLoudnormTwoPass:

    def test_measured_values_feed_second_pass(self, tmp_path, monkeypatch):
        wav = tmp_path / "recording_n.wav"
        _make_wav(wav)
        args_log = _two_pass_fake(tmp_path, monkeypatch, _LOUDNORM_JSON)

        r = AudioRecorder()
        r._segment_paths = [wav]
        r._loudness_normalize_segments(-16.0)

        calls = args_log.read_text().strip().splitlines()
        assert len(calls) == 2, calls
        assert "print_format=json" in calls[0] and calls[0].endswith("-f null -")
        assert "-nostdin" in calls[0]
        assert "measured_I=-23.50" in calls[1] and "linear=true" in calls[1]
        assert "pcm_s16le" in calls[1]
        assert calls[1].endswith(".wav")
        assert wav.exists()
        assert list(tmp_path.glob("*.norm.*")) == []

    def test_flac_segment_uses_flac_codec(self, tmp_path, monkeypatch):
        flac = tmp_path / "recording_f.flac"
        data = np.full((SAMPLE_RATE // 10, 2), 0.3, dtype=np.float32)
        sf.write(str(flac), data, SAMPLE_RATE, format="FLAC", subtype="PCM_16")
        args_log = _two_pass_fake(tmp_path, monkeypatch, _LOUDNORM_JSON)
        r = AudioRecorder()
        r._segment_paths = [flac]
        r._loudness_normalize_segments(-16.0)
        second = args_log.read_text().strip().splitlines()[1]
        assert "-c:a flac" in second and "pcm_s16le" not in second

    def test_silent_file_is_left_alone(self, tmp_path, monkeypatch):
        wav = tmp_path / "recording_s.wav"
        _make_wav(wav, level=0.0)
        original = wav.read_bytes()
        args_log = _two_pass_fake(tmp_path, monkeypatch, _SILENT_JSON)
        r = AudioRecorder()
        r._segment_paths = [wav]
        r._loudness_normalize_segments(-16.0)
        assert len(args_log.read_text().strip().splitlines()) == 1  # no second pass
        assert wav.read_bytes() == original

    def test_measurement_failure_degrades_to_single_pass(self, tmp_path, monkeypatch):
        wav = tmp_path / "recording_d.wav"
        _make_wav(wav)
        args_log = tmp_path / "args.txt"
        fake_ffmpeg.install(tmp_path, monkeypatch, rc=0, measure_rc=1, log=args_log)
        r = AudioRecorder()
        r._segment_paths = [wav]
        r._loudness_normalize_segments(-16.0)
        calls = args_log.read_text().strip().splitlines()
        assert len(calls) == 2
        assert "measured_I" not in calls[1] and "loudnorm=I=-16.0" in calls[1]
        assert wav.exists()


@pytest.mark.skipif(audio_recorder._find_ffmpeg() is None, reason="no ffmpeg available")
class TestLoudnormRealFfmpeg:

    def test_two_pass_hits_target(self, tmp_path):
        """End to end with the bundled/system ffmpeg: a -20 LUFS-ish noise
        file lands within 1 LU of the -16 LUFS target."""
        wav = tmp_path / "recording_real.wav"
        rng = np.random.default_rng(3)
        sf.write(str(wav), (rng.standard_normal((SAMPLE_RATE * 2, 2)) * 0.05).astype(np.float32),
                 SAMPLE_RATE, subtype="PCM_16")
        r = AudioRecorder()
        r._segment_paths = [wav]
        r._loudness_normalize_segments(-16.0)
        ffmpeg = audio_recorder._find_ffmpeg()
        measured = audio_recorder._measure_loudness(ffmpeg, wav, -16.0, timeout=120)
        assert measured is not None
        assert abs(measured["input_i"] - (-16.0)) < 1.0, measured


# ---------- MP3 sidecar ----------

class TestMp3Sidecar:

    def test_sidecar_follows_the_mp3(self, tmp_path, monkeypatch):
        wav = tmp_path / "recording_z.wav"
        _make_wav(wav)
        sidecar = Path(str(wav) + ".json")
        sidecar.write_text(json.dumps({"software": "Orizon Call", "format": "wav",
                                       "segments": [str(wav)], "duration_seconds": 0.2}))
        fake_ffmpeg.install(tmp_path, monkeypatch, rc=0)

        r = AudioRecorder()
        r._segment_paths = [wav]
        r._output_path = wav
        r._convert_to_mp3()

        mp3 = wav.with_suffix(".mp3")
        assert mp3.exists() and not wav.exists() and not sidecar.exists()
        meta = json.loads(Path(str(mp3) + ".json").read_text())
        assert meta["format"] == "mp3"
        assert meta["segments"] == [str(mp3)]
        assert meta["duration_seconds"] == 0.2

    def test_id3v2_3_requested(self, tmp_path, monkeypatch):
        wav = tmp_path / "recording_i.wav"
        _make_wav(wav)
        args_log = tmp_path / "args.txt"
        fake_ffmpeg.install(tmp_path, monkeypatch, rc=0, log=args_log)
        r = AudioRecorder()
        r._segment_paths = [wav]
        r._output_path = wav
        r._convert_to_mp3()
        assert "-id3v2_version 3" in args_log.read_text()


# ---------- signals ----------

class TestSignals:

    def test_callback_takes_over(self):
        r = AudioRecorder()
        seen = []
        r.set_signal_callback(seen.append)
        r._signal_handler(2, None)   # must not raise SystemExit
        assert seen == [2]

    def test_without_callback_exits_after_emergency_save(self):
        r = AudioRecorder()
        r.set_signal_callback(None)
        with pytest.raises(SystemExit):
            r._signal_handler(15, None)

    def test_failing_callback_falls_back(self, monkeypatch):
        r = AudioRecorder()

        def boom(_sig):
            raise RuntimeError("gui gone")
        r.set_signal_callback(boom)
        with pytest.raises(SystemExit):
            r._signal_handler(2, None)

    def test_public_emergency_save_is_noop_when_idle(self):
        r = AudioRecorder()
        r.emergency_save()
        assert r.state.name == "IDLE"


class TestGracefulShutdownWiring:

    def test_first_signal_quits_second_hard_exits(self, qapp, monkeypatch):
        app = qapp
        import main as main_mod

        calls = []

        class FakeSignal:
            def connect(self, fn):
                calls.append(("connect", fn))

        class FakeApp:
            aboutToQuit = FakeSignal()
            commitDataRequest = FakeSignal()

        class FakeWidget:
            def request_quit(self):
                calls.append("request_quit")

            def shutdown(self):
                calls.append("shutdown")

        exits = []
        monkeypatch.setattr(os, "_exit", lambda code: exits.append(code))
        recorder = AudioRecorder()
        widget = FakeWidget()
        main_mod._install_graceful_shutdown(FakeApp(), widget, recorder)

        recorder._signal_handler(2, None)          # first: graceful
        deadline = time.monotonic() + 2
        while "request_quit" not in calls and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert "request_quit" in calls
        assert exits == []

        recorder._signal_handler(2, None)          # second: hard exit
        assert exits == [130]


# ---------- API server ----------

class TestApiHardening:

    @pytest.fixture()
    def server(self, qapp, tmp_path):
        import socket
        from tests.test_api_server import FakeWidget
        from api_server import RecorderAPIHandler, start_api_server, stop_api_server
        widget = FakeWidget()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        srv = start_api_server(widget, port=port, output_dir=tmp_path / "missing",
                               require_auth=True, bound_socket=sock)
        yield {"port": port, "widget": widget, "token": RecorderAPIHandler.auth_token,
               "dir": tmp_path / "missing"}
        stop_api_server(srv)

    def _options(self, port, headers):
        req = urllib.request.Request(f"http://127.0.0.1:{port}/start", method="OPTIONS")
        for k, v in headers.items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, dict(resp.headers)

    def test_private_network_preflight_for_allowed_origin(self, server):
        status, headers = self._options(server["port"], {
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Private-Network": "true",
        })
        assert status == 204
        assert headers.get("Access-Control-Allow-Private-Network") == "true"
        assert headers.get("Access-Control-Allow-Origin") == "http://localhost:3000"
        assert headers.get("Access-Control-Max-Age") == "600"

    def test_private_network_not_granted_to_foreign_origin(self, server):
        status, headers = self._options(server["port"], {
            "Origin": "https://evil.example",
            "Access-Control-Request-Private-Network": "true",
        })
        assert status == 204
        assert "Access-Control-Allow-Private-Network" not in headers
        assert "Access-Control-Allow-Origin" not in headers

    def test_files_missing_folder_is_empty_list(self, server):
        assert not server["dir"].exists()
        req = urllib.request.Request(f"http://127.0.0.1:{server['port']}/files")
        req.add_header("Authorization", f"Bearer {server['token']}")
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            assert json.loads(resp.read()) == {"files": []}

    def test_handler_exception_becomes_json_500(self, server, monkeypatch):
        from api_server import RecorderAPIHandler

        def explode(self):
            raise RuntimeError("boom")
        monkeypatch.setattr(RecorderAPIHandler, "_handle_status", explode)
        req = urllib.request.Request(f"http://127.0.0.1:{server['port']}/status")
        req.add_header("Authorization", f"Bearer {server['token']}")
        try:
            urllib.request.urlopen(req, timeout=5)
            assert False, "expected HTTP 500"
        except urllib.error.HTTPError as e:
            assert e.code == 500
            assert json.loads(e.read()) == {"error": "internal error"}

    def test_stop_api_server_accepts_none(self):
        from api_server import stop_api_server
        stop_api_server(None)


# ---------- logging ----------

class TestLoggingHardening:

    @pytest.fixture()
    def reset(self):
        import orizon_logging
        orizon_logging._CONFIGURED = False
        logger = logging.getLogger(orizon_logging._ROOT_NAME)
        saved = list(logger.handlers)
        for h in saved:
            if type(h).__name__ != "LogCaptureHandler":
                logger.removeHandler(h)
        yield
        orizon_logging._CONFIGURED = False
        for h in list(logger.handlers):
            if type(h).__name__ != "LogCaptureHandler":
                logger.removeHandler(h)
                h.close()
        logger.propagate = True

    def test_no_console_handler_without_stderr(self, reset, monkeypatch, tmp_path):
        import orizon_logging
        monkeypatch.setattr(sys, "stderr", None)          # pythonw.exe
        monkeypatch.setattr(orizon_logging, "_log_dir", lambda: tmp_path)
        logger = orizon_logging.setup_logging()
        stream_handlers = [h for h in logger.handlers
                           if type(h) is logging.StreamHandler]
        assert stream_handlers == []
        assert any(isinstance(h, logging.handlers.RotatingFileHandler)
                   for h in logger.handlers)
        logger.info("must not raise")

    def test_thread_excepthook_logs(self, monkeypatch):
        import orizon_logging
        monkeypatch.setattr(threading, "excepthook", threading.__excepthook__)
        records = []

        class Collect(logging.Handler):
            def emit(self, record):
                records.append(record)

        logger = logging.getLogger("orizon.threads")
        handler = Collect()
        logger.addHandler(handler)
        try:
            orizon_logging.install_thread_excepthook()

            def worker():
                raise ValueError("thread boom")
            t = threading.Thread(target=worker, name="boom-thread")
            t.start()
            t.join(2)
        finally:
            logger.removeHandler(handler)
        assert any("boom-thread" in r.getMessage() for r in records)
        assert any(r.exc_info and r.exc_info[0] is ValueError for r in records)


# ---------- device detection ----------

class TestWindowsLoopbackSelection:

    def _dev(self, name, idx=1, ch=2, loop=True):
        return {"index": idx, "name": name, "maxInputChannels": ch,
                "defaultSampleRate": 48000.0, "isLoopbackDevice": loop}

    def test_prefers_official_default_helper(self):
        from platform_audio import _windows_pick_loopback
        chosen = self._dev("Speakers [Loopback]")

        class P:
            def get_default_wasapi_loopback(self):
                return chosen

            def get_loopback_device_info_generator(self):
                raise AssertionError("must not be needed")
        assert _windows_pick_loopback(P(), pyaudio=None) is chosen

    def test_generator_when_default_has_no_loopback(self):
        from platform_audio import _windows_pick_loopback
        other = self._dev("Headset [Loopback]", idx=7)

        class P:
            def get_default_wasapi_loopback(self):
                raise LookupError("none")

            def get_loopback_device_info_generator(self):
                yield self_dev_zero
                yield other
        self_dev_zero = self._dev("Zero", ch=0)
        assert _windows_pick_loopback(P(), pyaudio=None) is other

    def test_manual_scan_for_old_releases(self):
        from platform_audio import _windows_pick_loopback
        devices = [
            {"index": 0, "name": "Speakers (Realtek)", "maxInputChannels": 0,
             "defaultSampleRate": 48000.0, "isLoopbackDevice": False},
            self._dev("Other [Loopback]", idx=1),
            self._dev("Speakers (Realtek) [Loopback]", idx=2),
        ]

        class P:  # no helper methods at all
            def get_host_api_info_by_type(self, kind):
                return {"defaultOutputDevice": 0}

            def get_device_info_by_index(self, i):
                return devices[i]

            def get_device_count(self):
                return len(devices)
        pyaudio = types.SimpleNamespace(paWASAPI=13)
        assert _windows_pick_loopback(P(), pyaudio)["index"] == 2


class TestLinuxMonitorSelection:

    def _install_fake_pulsectl(self, monkeypatch, sources, sinks, default_sink):
        class FakePulse:
            def __init__(self, name):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def server_info(self):
                return types.SimpleNamespace(default_sink_name=default_sink)

            def sink_list(self):
                return sinks

            def source_list(self):
                return sources
        monkeypatch.setitem(sys.modules, "pulsectl", types.SimpleNamespace(Pulse=FakePulse))

    def test_monitor_of_default_sink_wins_regardless_of_name(self, monkeypatch):
        from platform_audio import _detect_linux_pulsectl
        sinks = [types.SimpleNamespace(index=3, name="alsa_output.usb"),
                 types.SimpleNamespace(index=9, name="alsa_output.hdmi")]
        sources = [
            types.SimpleNamespace(index=1, name="alsa_input.mic", description="Mic",
                                  monitor_of_sink=0xFFFFFFFF, monitor_of_sink_name=None),
            types.SimpleNamespace(index=2, name="hdmi-monitor", description="Monitor of HDMI",
                                  monitor_of_sink=9, monitor_of_sink_name="alsa_output.hdmi"),
            types.SimpleNamespace(index=4, name="usb-monitor", description="Monitor of USB",
                                  monitor_of_sink=3, monitor_of_sink_name="alsa_output.usb"),
        ]
        self._install_fake_pulsectl(monkeypatch, sources, sinks, "alsa_output.usb")
        assert _detect_linux_pulsectl() == ["Monitor of USB", "usb-monitor"]

    def test_falls_back_to_any_monitor(self, monkeypatch):
        from platform_audio import _detect_linux_pulsectl
        sources = [
            types.SimpleNamespace(index=1, name="alsa_input.mic", description="Mic",
                                  monitor_of_sink=0xFFFFFFFF, monitor_of_sink_name=None),
            types.SimpleNamespace(index=2, name="x.monitor", description="Monitor of X",
                                  monitor_of_sink=0xFFFFFFFF, monitor_of_sink_name=None),
        ]
        self._install_fake_pulsectl(monkeypatch, sources, [], "nope")
        assert _detect_linux_pulsectl() == ["Monitor of X", "x.monitor"]

    def test_mic_never_picked(self, monkeypatch):
        from platform_audio import _detect_linux_pulsectl
        sources = [types.SimpleNamespace(index=1, name="alsa_input.mic", description="Mic",
                                         monitor_of_sink=0xFFFFFFFF, monitor_of_sink_name=None)]
        self._install_fake_pulsectl(monkeypatch, sources, [], "nope")
        assert _detect_linux_pulsectl() == []


class TestMicValidation:

    def test_unopenable_default_mic_is_skipped(self):
        from unittest.mock import patch
        from platform_audio import detect_mic_device
        with patch("platform_audio.sd") as mock_sd:
            mock_sd.default.device = [0, 1]
            devices = [
                {"name": "Broken Mic", "max_input_channels": 1, "default_samplerate": 48000.0},
                {"name": "Good Mic", "max_input_channels": 2, "default_samplerate": 44100.0},
            ]
            mock_sd.query_devices.side_effect = lambda *a: devices[a[0]] if a else devices

            def check(device=None, **kw):
                if device == 0:
                    raise Exception("Invalid device")
            mock_sd.check_input_settings.side_effect = check
            assert detect_mic_device() == (1, 2, 44100.0)


# ---------- widget ----------

class TestWidgetClose:

    @pytest.fixture()
    def widget(self, qapp):
        from floating_widget import FloatingRecorderWidget
        r = AudioRecorder()
        w = FloatingRecorderWidget(r)
        yield w, r
        w.shutdown()
        w.close()

    def test_wm_close_is_refused_and_routed_to_quit(self, widget, monkeypatch):
        from PyQt6.QtGui import QCloseEvent
        w, _ = widget
        routed = []
        monkeypatch.setattr(w, "_quit_app", lambda confirm=False: routed.append(confirm))
        ev = QCloseEvent()
        w.closeEvent(ev)
        assert ev.isAccepted() is False
        assert routed == [True]

    def test_close_allowed_after_shutdown(self, widget):
        from PyQt6.QtGui import QCloseEvent
        w, _ = widget
        w.shutdown()
        ev = QCloseEvent()
        w.closeEvent(ev)
        assert ev.isAccepted() is True

    def test_status_exposes_overflows_and_public_api(self, widget):
        w, r = widget
        assert w.recorder_status()["input_overflows"] == 0
        assert w.is_busy is False
        assert w.recordings_dir() == r.output_directory
        # Tray is a graceful no-op offscreen; its API must still be callable.
        w._tray.set_state("recording", False)
        w._tray.notify("t", "m")
        w.bring_to_front()
