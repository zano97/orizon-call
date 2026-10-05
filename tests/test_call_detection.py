"""Call detection: app naming, per-platform parsing of "who is using the
mic", the debouncing state machine and the assistant's prompt/auto flow
(fake widget, fake clock — no audio, no OS APIs)."""

from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

import call_detection as cd
from call_detection import CallAssistant, CallDetector, MicUsage


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _no_leftover_toasts():
    """Dismiss toasts still fading at the end of a test: the module's
    QApplication is destroyed with them, and a deleted toast left in
    Toast._active would break the next module's widget tests."""
    yield
    from floating_widget import Toast
    for t in list(Toast._active):
        t._dismiss()


class TestFriendlyNames:

    @pytest.mark.parametrize("raw, expected", [
        ("C:#Program Files#Zoom#bin#Zoom.exe", "Zoom"),
        ("MicrosoftTeams_8wekyb3d8bbwe", "Microsoft Teams"),
        ("MSTeams_8wekyb3d8bbwe", "Microsoft Teams"),
        ("C:#Program Files#Google#Chrome#Application#chrome.exe", "Google Chrome"),
        ("com.google.Chrome.helper", "Google Chrome"),
        ("us.zoom.xos", "Zoom"),
        ("com.microsoft.edgemac", "Microsoft Edge"),
        ("Firefox", "Firefox"),
        ("C:#Tools#Recorder#rec.exe", "rec"),
        ("SomeVendor.App_abc123", "SomeVendor.App"),
    ])
    def test_names(self, raw, expected):
        assert cd.friendly_app_name(raw) == expected


class TestWindowsConsent:

    def test_live_entries_only(self):
        entries = [
            ("C:#Program Files#Zoom#bin#Zoom.exe", 133000000000, 0),      # live
            ("C:#Apps#old.exe", 133000000000, 133000000500),            # ended
            ("MicrosoftTeams_8wekyb3d8bbwe", 0, 0),                     # never used
        ]
        assert cd.active_from_consent(entries, []) == ["Zoom"]

    def test_own_interpreter_excluded(self):
        own = "C:#Python313#python.exe"
        entries = [(own.upper(), 1, 0), ("C:#x#Zoom.exe", 1, 0)]
        assert cd.active_from_consent(entries, [own.lower()]) == ["Zoom"]

    def test_duplicates_collapsed(self):
        entries = [("C:#a#chrome.exe", 1, 0), ("C:#b#chrome.exe", 1, 0)]
        assert cd.active_from_consent(entries, []) == ["Google Chrome"]


def _so(app, pid=1234, source=1, corked=False, binary=""):
    return SimpleNamespace(
        proplist={"application.name": app, "application.process.id": str(pid),
                  "application.process.binary": binary},
        corked=corked, source=source, name="stream")


class TestPulse:

    SOURCES = {
        1: SimpleNamespace(name="alsa_input.usb-mic", monitor_of_sink=0xFFFFFFFF),
        2: SimpleNamespace(name="alsa_output.speakers.monitor", monitor_of_sink=3),
    }

    def test_other_app_on_mic(self):
        assert cd.active_from_pulse([_so("Google Chrome input")], self.SOURCES, 99) == ["Google Chrome"]

    def test_filters(self):
        outputs = [
            _so("Python", pid=99),                    # this process
            _so("Zoom", corked=True),                 # paused stream
            _so("Discord", source=2),                 # monitor = not the mic
            _so("PulseAudio Volume Control"),         # meter
            _so("easyeffects", binary="easyeffects"),
        ]
        assert cd.active_from_pulse(outputs, self.SOURCES, 99) == []


class TestDetector:

    def test_short_grab_is_not_a_call(self):
        d = CallDetector(start_delay=3, end_delay=10)
        assert d.update(0, ["Zoom"]) is None
        assert d.update(2, ["Zoom"]) is None
        assert d.update(2.5, []) is None
        assert d.update(5, ["Zoom"]) is None   # debounce restarted
        assert d.active_app is None

    def test_start_and_end(self):
        d = CallDetector(start_delay=3, end_delay=10)
        d.update(0, ["Zoom"])
        ev = d.update(3, ["Zoom"])
        assert ev.kind == "started" and ev.app == "Zoom"
        assert d.update(4, ["Zoom"]) is None     # no repeat while on
        assert d.update(5, []) is None
        assert d.update(9, ["Zoom"]) is None     # brief gap mid-call
        assert d.update(10, []) is None
        ev = d.update(20, [])
        assert ev.kind == "ended" and ev.app == "Zoom"
        assert d.active_app is None

    def test_unknown_holds_state(self):
        d = CallDetector(start_delay=3, end_delay=10)
        d.update(0, ["Zoom"])
        d.update(3, ["Zoom"])
        for t in range(4, 60):
            assert d.update(t, None) is None
        assert d.active_app == "Zoom"

    def test_ignored_apps(self):
        d = CallDetector(start_delay=0, end_delay=0, ignored=["zoom"])
        assert d.update(0, ["Zoom"]) is None
        assert d.update(1, ["Zoom", "Slack"]).app == "Slack"


class FakeWidget(QObject):
    recording_started = pyqtSignal(str)
    recording_stopped = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.state = "idle"
        self.is_busy = False
        self.capturing = False
        self.prompts = []
        self.starts = []
        self.stops = []
        self.notes = []

    def recorder_state_name(self):
        return self.state

    def is_capturing_mic(self):
        return self.capturing or self.state != "idle"

    def show_prompt(self, text, actions, on_timeout=None):
        prompt = SimpleNamespace(text=text, actions=actions, on_timeout=on_timeout,
                                 closed=False)
        prompt.close_silently = lambda: setattr(prompt, "closed", True)
        self.prompts.append(prompt)
        return prompt

    def start_recording_for_call(self, app, automatic):
        self.starts.append((app, automatic))
        self.state = "recording"
        self.recording_started.emit("/tmp/rec.wav")

    def stop_recording_for_call(self, app, automatic):
        self.stops.append((app, automatic))
        self.state = "idle"
        self.recording_stopped.emit("/tmp/rec.wav")

    def notify(self, message, kind="warn", duration_ms=6000):
        self.notes.append(message)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _assistant(mode="propose", ignored=(), on_ignored_changed=None):
    w = FakeWidget()
    clock = Clock()
    a = CallAssistant(w, mode="off", ignored=ignored, probe=lambda: MicUsage([]),
                      clock=clock, on_ignored_changed=on_ignored_changed)
    a._mode = mode   # drive feed() by hand: no probe thread in tests
    return w, a, clock


def _run(a, clock, apps, seconds, step=1.0, includes_self=False):
    end = clock.t + seconds
    while clock.t <= end:
        a.feed(MicUsage(list(apps), includes_self=includes_self))
        clock.t += step


def _press(prompt, label):
    for text, cb, _accent in prompt.actions:
        if text == label:
            return cb()
    raise AssertionError(f"no button {label!r} in {[x[0] for x in prompt.actions]}")


class TestAssistant:

    def test_propose_then_record_then_stop(self, qapp):
        w, a, clock = _assistant()
        _run(a, clock, ["Zoom"], 4)
        assert len(w.prompts) == 1 and "Zoom" in w.prompts[0].text
        _press(w.prompts[0], "Registra")
        assert w.starts == [("Zoom", False)]
        # Our own recording must not keep the call "alive" forever: the
        # probe excludes us, so the mic released by Zoom ends the call.
        _run(a, clock, [], 15)
        assert len(w.prompts) == 2 and "terminata" in w.prompts[1].text
        _press(w.prompts[1], "Stop e salva")
        assert w.stops == [("Zoom", False)]

    def test_not_now_does_not_nag(self, qapp):
        w, a, clock = _assistant()
        _run(a, clock, ["Zoom"], 4)
        _press(w.prompts[0], "Non ora")
        _run(a, clock, ["Zoom"], 60)
        assert len(w.prompts) == 1 and not w.starts
        # The call ends: no stop prompt (nothing was recorded) …
        _run(a, clock, [], 15)
        assert len(w.prompts) == 1
        # … and the next call is proposed again.
        _run(a, clock, ["Zoom"], 4)
        assert len(w.prompts) == 2

    def test_prompt_withdrawn_when_call_ends_unanswered(self, qapp):
        w, a, clock = _assistant()
        _run(a, clock, ["Zoom"], 4)
        _run(a, clock, [], 15)
        assert w.prompts[0].closed is True

    def test_prompt_withdrawn_when_user_starts_by_hand(self, qapp):
        w, a, clock = _assistant()
        _run(a, clock, ["Zoom"], 4)
        w.state = "recording"
        w.recording_started.emit("/tmp/x.wav")
        assert w.prompts[0].closed is True

    def test_ignore_app_persists(self, qapp):
        saved = []
        w, a, clock = _assistant(on_ignored_changed=saved.append)
        _run(a, clock, ["Zoom"], 4)
        _press(w.prompts[0], "Mai per quest'app")
        assert saved == [["Zoom"]]
        _run(a, clock, [], 15)
        _run(a, clock, ["Zoom"], 10)
        assert len(w.prompts) == 1

    def test_auto_mode_starts_and_stops(self, qapp):
        w, a, clock = _assistant(mode="auto")
        _run(a, clock, ["Teams"], 4)
        assert w.starts == [("Teams", True)] and not w.prompts
        _run(a, clock, [], 15)
        assert w.stops == [("Teams", True)]

    def test_auto_mode_asks_before_stopping_a_manual_recording(self, qapp):
        w, a, clock = _assistant(mode="auto")
        w.state = "recording"   # user was already recording
        _run(a, clock, ["Teams"], 4)
        assert not w.starts
        _run(a, clock, [], 15)
        assert not w.stops and "terminata" in w.prompts[-1].text

    def test_no_prompt_while_already_recording_unrelated(self, qapp):
        w, a, clock = _assistant()
        w.state = "recording"
        _run(a, clock, ["Zoom"], 4)
        assert not w.prompts

    def test_device_level_probe_ignored_while_we_capture(self, qapp):
        """macOS < 14.2: the probe cannot exclude our own capture."""
        w, a, clock = _assistant()
        w.capturing = True   # e.g. pre-roll streams open while idle
        _run(a, clock, ["un'altra app"], 30, includes_self=True)
        assert not w.prompts
        w.capturing = False
        _run(a, clock, ["un'altra app"], 4, includes_self=True)
        assert len(w.prompts) == 1

    def test_set_mode_off_withdraws_prompt(self, qapp):
        w, a, clock = _assistant()
        _run(a, clock, ["Zoom"], 4)
        a.set_mode("off")
        assert w.prompts[0].closed is True
        assert a.mode == "off"

    def test_probe_thread_delivers_on_gui_thread(self, qapp):
        import time
        w = FakeWidget()
        calls = []
        a = CallAssistant(w, mode="propose", probe=lambda: calls.append(1) or MicUsage(["Zoom"]),
                          interval=0.01, clock=lambda: len(calls) * 10.0)
        try:
            deadline = time.monotonic() + 5
            while not w.prompts and time.monotonic() < deadline:
                qapp.processEvents()
                time.sleep(0.01)
        finally:
            a.shutdown()
        assert w.prompts, "the probe thread never produced a prompt"


class TestWidgetIntegration:
    """The real FloatingRecorderWidget behind a CallAssistant: the prompt
    buttons start/stop the recording (recorder start/stop stubbed)."""

    @pytest.fixture()
    def widget(self, qapp, tmp_path, monkeypatch):
        from audio_recorder import AudioRecorder, RecordingState
        from floating_widget import FloatingRecorderWidget
        r = AudioRecorder()
        r.set_output_directory(tmp_path)
        path = tmp_path / "recording_call.wav"

        def fake_start():
            r._state = RecordingState.RECORDING
            return path

        def fake_stop():
            r._state = RecordingState.IDLE
            return path
        monkeypatch.setattr(r, "start", fake_start)
        monkeypatch.setattr(r, "stop", fake_stop)
        w = FloatingRecorderWidget(r)
        yield w, r
        w.shutdown()
        w.close()

    def _pump(self, qapp, predicate, timeout=3.0):
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            qapp.processEvents()
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def test_prompt_buttons_drive_recording(self, qapp, widget):
        from audio_recorder import RecordingState
        from floating_widget import PromptToast
        w, r = widget
        clock = Clock()
        a = CallAssistant(w, mode="off", probe=lambda: MicUsage([]), clock=clock)
        a._mode = "propose"
        w.set_call_assistant(a)

        _run(a, clock, ["Zoom"], 4)
        prompt = a._prompt
        assert isinstance(prompt, PromptToast) and prompt.isVisible()
        assert [b.text() for b in prompt.buttons] == ["Registra", "Non ora", "Mai per quest'app"]
        prompt.buttons[0].click()
        assert self._pump(qapp, lambda: r.state == RecordingState.RECORDING and not w.is_busy)
        assert w.is_capturing_mic()

        _run(a, clock, [], 15)
        stop_prompt = a._prompt
        assert isinstance(stop_prompt, PromptToast)
        stop_prompt.buttons[0].click()
        assert self._pump(qapp, lambda: r.state == RecordingState.IDLE and not w.is_busy)
        assert a._prompt is None

    def test_apply_ui_settings(self, qapp, widget):
        w, _r = widget
        a = CallAssistant(w, mode="off", probe=lambda: MicUsage([]))
        toggles = []
        guard = SimpleNamespace(set_enabled=toggles.append)
        w.set_privacy_guard(guard)
        w.set_call_assistant(a)
        w.apply_ui_settings({"hide_from_screen_share": False, "call_detection": "propose",
                             "call_detection_ignored": ["Slack"]})
        try:
            assert toggles == [False]
            assert a.mode == "propose" and a.ignored == ["Slack"]
        finally:
            a.shutdown()

    def test_prompt_timeout_counts_as_not_now(self, qapp):
        from floating_widget import PromptToast
        timeouts = []
        p = PromptToast("x", [("A", lambda: None, True)], on_timeout=lambda: timeouts.append(1),
                        duration_ms=10)
        anchor = _anchor_widget()
        p.show_above(anchor)
        assert self._pump(qapp, lambda: bool(timeouts))
        # Withdrawn prompts never call back.
        q = PromptToast("y", [("A", lambda: None, True)], on_timeout=lambda: timeouts.append(2),
                        duration_ms=50)
        q.show_above(anchor)
        q.close_silently()
        self._pump(qapp, lambda: False, timeout=0.3)
        assert timeouts == [1]


def _anchor_widget():
    from PyQt6.QtWidgets import QWidget
    w = QWidget()
    w.resize(60, 60)
    w.show()
    return w
