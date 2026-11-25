"""Phone observation plumbing; the transport is synthetic and never reaches adb."""

import pytest

from mf import device, trace
from mf.device import Phone, UiTimeout
from mf.ui import Snapshot


def test_recording_a_snapshot_never_adds_phone_reads_or_retries_when_disk_is_unavailable(tmp_path, monkeypatch):
    class Hierarchy:
        reads = 0

        def dump_hierarchy(self):
            self.reads += 1
            return "<hierarchy/>"

    phone = Phone("synthetic")
    phone._u2 = Hierarchy()
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("occupied")
    monkeypatch.setattr(trace, "TRACE_DIR", blocked)
    assert phone.snapshot().xml == "<hierarchy/>"
    assert phone._u2.reads == 1


def test_inputs_keep_the_full_command_and_link_to_the_following_observation(monkeypatch):
    phone = Phone("synthetic")
    calls = []
    monkeypatch.setattr(phone, "_adb", lambda *args: calls.append(args))
    command = "input motionevent MOVE 10 20; " * 10
    phone._input(command)
    trace.screen("<hierarchy/>", action=phone.actions, ms=1)
    sent, seen = trace.read()
    assert calls == [("shell", command)]
    assert sent["cmd"] == command and sent["action"] == seen["action"] == 1


def test_timeout_distinguishes_an_unstable_match_from_never_matching(monkeypatch):
    phone = Phone("synthetic")
    frames = iter([Snapshot.from_xml("<hierarchy/>"), Snapshot.from_xml("<hierarchy rotation='0'/>")])
    ticks = iter([0, 0.1, 2, 2])
    monkeypatch.setattr(phone, "snapshot", lambda: next(frames))
    monkeypatch.setattr(phone, "save_evidence", lambda *args: None)
    monkeypatch.setattr(device.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(device.time, "sleep", lambda _: None)
    with pytest.raises(UiTimeout, match="condition matched, but did not settle"):
        phone.until(lambda s: "rotation" not in s.xml, "synthetic condition", timeout=1)
    event = trace.read()[-1]
    assert event["matched"] == 1 and event["reads"] == 2 and not event["ok"]


def test_the_phone_named_by_the_environment_is_the_one_driven_and_the_one_reported(monkeypatch):
    from mf import device

    monkeypatch.setattr(device, "connected_serials", lambda: ["watch", "phone"])
    monkeypatch.delenv("ANDROID_SERIAL", raising=False)
    assert device.phone_serial() == "watch"  # no choice stated: the first one adb lists
    monkeypatch.setenv("ANDROID_SERIAL", "phone")
    assert device.phone_serial() == "phone" == device.Phone().serial
    monkeypatch.setenv("ANDROID_SERIAL", "unplugged")
    assert device.phone_serial() is None  # another device being there does not make the phone connected


def test_a_text_field_is_filled_through_the_screen_reader_and_counts_as_an_input(monkeypatch):
    """Not as key presses: the keyboard corrected those ("Fleurs" arrived as "Fleur's")."""

    class Ui:
        filled = []

        def __call__(self, **selector):
            assert selector == {"className": "android.widget.EditText", "focused": True}
            return self

        def set_text(self, text, timeout):
            self.filled.append(text)

    phone = Phone("synthetic")
    phone._u2 = Ui()
    monkeypatch.setattr(phone, "_adb", lambda *args: pytest.fail("no key press is sent"))
    phone.fill("Fleurs en Fleurettes")
    assert Ui.filled == ["Fleurs en Fleurettes"] and phone.actions == 1
    assert trace.read()[-1]["cmd"] == "fill Fleurs en Fleurettes"
