"""The phone: read the screen fast, act through adb, wait on conditions instead of sleeping.

Reads go through uiautomator2 (a jar under /data/local/tmp, ~0.2 s per snapshot instead of
~2.5 s for `uiautomator dump`). Input goes through plain `adb shell input`, which has no
server to crash. Nothing here knows about MacroFactor.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import shlex
import subprocess
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import TypeVar

from . import trace
from .text import ascii_fold
from .trace import STATE_DIR
from .ui import Node, Snapshot

T = TypeVar("T")
SLOP = 24  # px a finger travels before the content under it starts to follow


class PhoneError(RuntimeError):
    """The phone is unreachable or did not do what was asked."""


class Cancelled(Exception):
    """The operation was asked to stop and did, between two actions. Not a failure, and not caught as one."""


class Uncertain(PhoneError):
    """A saving input may have reached the phone. Reconcile, never repeat this operation."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"save outcome uncertain; check the phone before making another change: {reason}")


class UiTimeout(PhoneError):
    """A screen condition was not met in time; the message says where the evidence was saved."""

    def __init__(self, message: str, report: Path | None = None):
        super().__init__(f"{message} (evidence: {report})" if report else message)


def connected_serials() -> list[str]:
    out = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=15).stdout
    serials = [f[0] for f in (line.split() for line in out.splitlines()[1:]) if len(f) >= 2 and f[1] == "device"]
    return sorted(serials, key=lambda s: ":" in s)  # USB before Wi-Fi


def phone_serial() -> str | None:
    """The phone mf drives, if adb sees it: the one named by ANDROID_SERIAL, else the first connected."""
    serials = connected_serials()
    wanted = os.environ.get("ANDROID_SERIAL")
    return (wanted if wanted in serials else None) if wanted else next(iter(serials), None)


def phone_environment(serial: str, package: str) -> dict[str, str]:
    """What screen readers depend on: the version of `package` installed on that phone, and its screen's size in pixels."""
    command = f"dumpsys package {package} | grep -m1 versionName; wm size"
    result = subprocess.run(["adb", "-s", serial, "shell", command], capture_output=True, text=True, timeout=15)
    version, sizes = re.search(r"versionName=(\S+)", result.stdout), re.findall(r"size: (\d+x\d+)", result.stdout)
    if result.returncode != 0 or version is None or not sizes:
        raise PhoneError(f"the phone did not tell its app version and screen size: {(result.stderr or result.stdout).strip()}")
    return {"app": version[1], "screen": sizes[-1]}


class Phone:
    def __init__(self, serial: str | None = None):
        serial = serial or phone_serial()
        if not serial:
            raise PhoneError("phone not connected: plug in USB with USB debugging on")
        self.serial = serial
        self._u2 = None
        self.actions = 0  # inputs sent so far
        self.checkpoint: Callable[[], None] | None = None  # called before every command and screen read; may raise Cancelled

    # ---- adb

    def _adb(self, *args: str, timeout: float = 30) -> bytes:
        result = subprocess.run(["adb", "-s", self.serial, *args], capture_output=True, timeout=timeout)
        if result.returncode != 0:
            raise PhoneError(f"adb {' '.join(args[:3])} failed: {result.stderr.decode(errors='replace').strip()}")
        return result.stdout

    def shell(self, command: str, timeout: float = 30) -> str:
        """Run a command on the phone that changes nothing there (ls, stat) and return what it printed."""
        if self.checkpoint is not None:
            self.checkpoint()
        return self._adb("shell", command, timeout=timeout).decode(errors="replace")

    @contextlib.contextmanager
    def _sending(self, what: str) -> Iterator[None]:
        """An input (a tap, a key, text, a launch): counted, so readers know the screen may have changed, and traced."""
        if self.checkpoint is not None:
            self.checkpoint()
        self.actions += 1
        started = time.monotonic()
        try:
            yield
        finally:
            trace.emit("phone", "input", cmd=what, action=self.actions, ms=round((time.monotonic() - started) * 1000))

    def _input(self, command: str) -> None:
        with self._sending(command):
            self._adb("shell", command)

    def _ui(self, call: Callable[..., T], what: str) -> T:
        """Ask the uiautomator2 server on the phone, reconnecting when it has died (it can in many ways)."""
        last: Exception | None = None
        for attempt in range(4):
            try:
                if self._u2 is None:
                    import uiautomator2

                    self._u2 = uiautomator2.connect(self.serial)
                return call(self._u2)
            except Exception as error:  # noqa: BLE001
                last = error
                self._u2 = None
                time.sleep(0.4 * (attempt + 1))
        raise PhoneError(f"cannot {what}: {last}")

    # ---- reading

    def snapshot(self) -> Snapshot:
        def read(ui) -> Snapshot:
            started = time.monotonic()
            xml = ui.dump_hierarchy()
            trace.screen(xml, action=self.actions, ms=round((time.monotonic() - started) * 1000))
            return Snapshot.from_xml(xml)

        return self._ui(read, "read the screen")

    def picture(self):
        """The screen as drawn, in greys (a PIL image): for what the app paints and tells no reader."""
        return self._ui(lambda ui: ui.screenshot().convert("L"), "take a picture of the screen")

    def until(
        self,
        check: Callable[[Snapshot], T],
        what: str,
        timeout: float = 10.0,
        settle: bool = True,
        interval: float = 0.12,
    ) -> T:
        """Poll until `check(snapshot)` is truthy and return its value.

        With `settle`, the screen must also be identical in two consecutive snapshots, so
        coordinates taken from the result are not mid-animation.
        """
        started = time.monotonic()
        deadline = started + timeout
        previous: str | None = None
        reads, matched, ok = 1, 0, False
        try:
            snap = self.snapshot()
            while True:
                if self.checkpoint is not None:
                    self.checkpoint()
                value = check(snap)
                matched += bool(value)
                signature = _signature(snap)
                if value and (not settle or signature == previous):
                    ok = True
                    return value
                previous = signature if value else None
                if time.monotonic() > deadline:
                    reason = "condition matched, but did not settle" if matched else "condition never matched"
                    raise UiTimeout(f"timed out waiting for {what} ({reason})", self.save_evidence(what, snap))
                time.sleep(interval)
                snap = self.snapshot()
                reads += 1
        finally:
            trace.emit("phone", "wait", what=what, ms=round((time.monotonic() - started) * 1000), reads=reads, matched=matched, ok=ok)

    def settled(self, timeout: float = 4.0) -> Snapshot:
        return self.until(lambda s: s, "screen to settle", timeout)

    # ---- acting

    def tap(self, target: Node | tuple[int, int]) -> None:
        x, y = target.center if isinstance(target, Node) else target
        self._input(f"input tap {x} {y}")

    def type(self, text: str) -> None:
        """Press the keys of `text`. For figures: the phone's keyboard corrects the words it is sent this way."""
        folded = ascii_fold(text)
        if not folded:
            return
        self._input("input text " + shlex.quote(folded.replace(" ", "%s")))

    def fill(self, text: str) -> None:
        """Make `text` the content of the focused text field, in place of what it held, exactly as given.

        Seen 2026-11-21: "Fleurs en Fleurettes" sent as key presses reached the field as "Fleur's en Fleurettes".
        """
        with self._sending(f"fill {text}"):
            field = {"className": "android.widget.EditText", "focused": True}  # "focused" alone is also true of other nodes
            self._ui(lambda ui: ui(**field).set_text(text, timeout=2), "set the text of the focused field")

    def key(self, *codes: int | str) -> None:
        self._input("input keyevent " + " ".join(str(c) for c in codes))

    def back(self) -> None:
        self.key(4)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> None:
        self._input(f"input swipe {x1} {y1} {x2} {y2} {ms}")

    def drag(self, x: int, y1: int, y2: int) -> None:
        """Drag and release at rest: no fling, so content stops where the finger stops.

        The first `SLOP` pixels only arm the gesture and move nothing: the content follows the finger from there.
        """
        slop = SLOP if y2 > y1 else -SLOP
        mid = (y1 + slop + y2) // 2
        moves = "".join(f"input motionevent MOVE {x} {y}; " for y in (y1 + slop, mid, y2, y2))
        self._input(f"input motionevent DOWN {x} {y1}; {moves}input motionevent UP {x} {y2}")

    def hide_keyboard(self) -> Snapshot:
        snap = self.snapshot()
        if snap.keyboard_top is None:
            return snap
        self.back()
        return self.until(lambda s: s if s.keyboard_top is None else None, "keyboard to hide", 5)

    def wake(self) -> None:
        self._input("input keyevent KEYCODE_WAKEUP; wm dismiss-keyguard")

    def launch(self, package: str) -> None:
        self._input(f"monkey -p {package} -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1")

    def screenshot(self, path: Path) -> None:
        path.write_bytes(self._adb("exec-out", "screencap", "-p"))

    def pull(self, remote: str, local: Path) -> None:
        self._adb("pull", remote, str(local), timeout=120)

    # ---- evidence and exclusivity

    def save_evidence(self, what: str, snap: Snapshot | None = None) -> Path:
        """Keep what the screen looked like when something went wrong (private, outside the repo)."""
        folder = STATE_DIR / "failures" / datetime.now().strftime("%Y%m%d-%H%M%S")
        folder.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(Exception):
            snap = snap or self.snapshot()
            (folder / "screen.xml").write_text(snap.xml)
        with contextlib.suppress(Exception):
            (folder / "screen.txt").write_text(f"{what}\n\n{(snap or self.snapshot()).dump(pkg=None)}\n")
        with contextlib.suppress(Exception):
            self.screenshot(folder / "screen.png")
        return folder


def _signature(snap: Snapshot) -> str:
    return "\n".join(f"{n.label}|{n.text}|{n.x1},{n.y1},{n.x2},{n.y2}|{n.focused}" for n in snap.nodes if n.label or n.clickable)


@contextlib.contextmanager
def phone_lock(timeout: float = 120.0) -> Iterator[None]:
    """One process drives the phone at a time (CLI, web server and agents share this lock)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with (STATE_DIR / "phone.lock").open("w") as handle:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise PhoneError("the phone is busy with another mf operation") from None
                time.sleep(0.2)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
