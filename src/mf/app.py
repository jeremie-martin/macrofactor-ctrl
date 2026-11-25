"""MacroFactor operations on the phone. Every step acts, then waits for the screen it expects.

Rules that keep real data safe:
  * a write is only reported as done after the saved Food Log shows it;
  * nothing is tapped by coordinate unless it was just read from the screen;
  * a draft plate that this code did not create is never touched (its own, left by a write that was interrupted,
    is known by what was written down of it as it was filled, and discarded);
  * "Clear Day" is never tapped.

Stopping a write. Each write has exactly one tap that saves (`_commit`). Until that tap, a cancel request
is honoured between any two actions: the app is backed out to the Food Log and the hour is read again to
establish that nothing changed, then `Cancelled` is raised. From that tap on, the write runs to the end
and is verified as usual; taking it back is then the caller's business (an undo).
"""

from __future__ import annotations

import contextlib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path

from . import PACKAGE
from . import screens as S
from .device import SLOP, Cancelled, Phone, PhoneError, UiTimeout
from .text import ascii_fold, norm
from .ui import Node, Snapshot

ROW_TOLERANCE = 24  # px: two sightings of a row closer than this in list coordinates are the same row
DOWNLOADS = "/sdcard/Download"


class AppError(PhoneError):
    """MacroFactor did not behave as expected. The service determines whether saving had begun."""


FOREIGN_DRAFT = "MacroFactor has a draft plate open that mf did not create; log or discard it on the phone"
STAGED = ("title", "quantity", "unit", "kcal", "protein", "fat", "carbs")  # of a food's details: what tells it on a plate


class DayChanged(AppError):
    """The settled Food Log changed date or contents during a whole-day read."""


class ExportRefused(AppError):
    """MacroFactor would not export: it gives only so many in a day, then refuses for 24 hours."""


class SeveralFoods(AppError):
    """MacroFactor lets foods and recipes share a name; mf names a food by its name, and never picks among them."""

    def __init__(self, rows: list[S.FoodRow]):
        shown = "; ".join(f"{r.serving}, {r.kcal} kcal" for r in rows)
        super().__init__(f"MacroFactor lists {len(rows)} foods named {rows[0].name!r} ({shown}): choose the one meant on the phone")


class NoSuchUnit(AppError):
    """The food that was opened does not offer the unit asked for."""


class FoodNotFound(AppError):
    def __init__(self, name: str, candidates: list[str]):
        super().__init__(f"no food named {name!r} in MacroFactor search; closest: {', '.join(candidates[:5]) or 'nothing'}")
        self.candidates = candidates


@dataclass
class Item:
    """A food to log. Without quantity/unit, MacroFactor's last-used serving is kept."""

    name: str
    quantity: float | None = None
    unit: str | None = None
    library: bool = False  # a recipe never logged before: picked in the Library tab, search history lacks it
    exact: bool = False  # named as the phone's search lists it, and taken from the first section that lists it
    shown: dict | None = None  # one of several recipes of this name: the one whose serving the Library lists so (`S.one_serving`)


def stamp(hour: int, minute: int | None) -> str:
    """A time as entries carry it: "08:27", or "08:??" where the phone showed the hour alone."""
    return f"{hour:02d}:{minute:02d}" if minute is not None else f"{hour:02d}:??"


@dataclass
class Entry:
    """One row of the saved Food Log."""

    day: date
    hour: int
    name: str
    serving: str
    kcal: int
    protein: float
    fat: float
    carbs: float
    minute: int | None = None  # known when the hour shows a single timestamp, or when we set it
    position: int = 0  # index among the rows of that hour, top to bottom
    weighed: bool | None = None  # when it was just logged: whether MacroFactor showed a weight for its amount (None: not seen)

    @property
    def key(self) -> tuple[str, str]:
        return (self.name, self.serving)

    def as_dict(self) -> dict:
        split = S.split_serving(self.serving)
        return {
            "date": self.day.isoformat(),
            "time": stamp(self.hour, self.minute),
            "hour": self.hour,
            "name": self.name,
            "serving": self.serving,
            "quantity": split[0] if split else None,
            "unit": split[1] if split else None,
            "kcal": self.kcal,
            "protein": self.protein,
            "fat": self.fat,
            "carbs": self.carbs,
            "position": self.position,
        }


@dataclass
class _Seen:
    row: S.FoodRow
    hour: int
    y: int  # list coordinate (screen y + how far the list is scrolled)
    stamped: bool | None = None  # a time is drawn beside it: the first row of that minute. None: not looked at


@dataclass
class DayView:
    totals: dict[str, tuple[int, int]]
    entries: list[Entry] = field(default_factory=list)  # top to bottom, which is in time order
    times: dict[int, list[int]] = field(default_factory=dict)  # the distinct minutes each hour shows, in order


def same_food(shown: str, wanted: str) -> bool:
    """Search rows show "Name By Brand"; exports and people usually say just "Name"."""
    a, b = norm(shown), norm(wanted)
    return a == b or a.startswith(b + " by ")


def search_query(name: str) -> str:
    """Text to type in MacroFactor's search to find `name` in history.

    The search matches fragments of words but neither a letter for its accented
    form nor across punctuation ("Apple, Fresh" finds nothing in history; "Brass Nature" finds "Brassé Nature").
    So the query is every word of the name, each cut down to its longest plain run: specific enough to bring the
    food to the top of the results, where the row is then matched exactly.
    """
    name = re.sub(r"\s+By\s+.*$", "", name)
    runs = [(max(re.findall(r"[A-Za-z0-9%.']+", word), key=len, default=""), word) for word in name.split()]
    words = [run for run, word in runs if len(run) > 1 or run == word]  # a stray letter matches too much
    return " ".join(words) or max((run for run, _ in runs), key=len, default="") or ascii_fold(name)  # "Pâté": better than nothing


def fmt_quantity(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


class MacroFactor:
    def __init__(
        self,
        phone: Phone,
        now: Callable[[], datetime] = datetime.now,
        log: Callable[[str], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
        saving: Callable[[], None] | None = None,
        staging: Callable[[list[dict] | None], None] | None = None,
        staged: Callable[[], list[dict] | None] | None = None,
    ):
        self.phone = phone
        self.now = now
        self.say = log or (lambda message: None)
        self.cancelled = cancelled  # asked between actions: should this write stop?
        self.saving = saving or (lambda: None)
        # A plate being filled, written down: the foods "asked" for it, and what its "plate" holds, before each food
        # goes on it. Forgotten (None) once the phone is seen without a plate. A plate found on the phone that holds
        # just that is ours, left by a write that was interrupted (a cable pulled, mf killed). Without them, no
        # plate found is ours.
        self.staging = staging or (lambda plate: None)
        self.staged = staged or (lambda: None)
        self.committed = False  # the tap that saves has been sent: no stopping any more
        self._stopping = False  # a cancel is being carried out: backing out must not be interrupted either
        self._seen_log: tuple[int, S.FoodLog] | None = None
        self._last_log: S.FoodLog | None = None  # the Food Log frame before the current one
        phone.checkpoint = self._checkpoint

    # ------------------------------------------------------------ primitives

    def _tap(self, node: Node, snap: Snapshot | None = None) -> None:
        if node.label in S.NEVER_TAP:
            raise AppError(f"refusing to tap {node.label!r}")
        if snap is not None and snap.keyboard_top is not None and node.center[1] >= snap.keyboard_top:
            raise AppError(f"{node.label!r} is under the keyboard")
        self.phone.tap(node)

    def _checkpoint(self) -> None:
        if self.cancelled is not None and not self.committed and not self._stopping and self.cancelled():
            self._stopping = True
            raise Cancelled()

    def _commit(self, node: Node, snap: Snapshot | None = None) -> None:
        """The one tap of a write that saves. The last moment it can be stopped is just before."""
        self._checkpoint()
        self.saving()  # durable intent BEFORE the input: even a lost adb answer may have saved
        self.committed = True
        self._tap(node, snap)

    def abort(self) -> None:
        """Leave whatever is open without saving and return to the Food Log."""
        self._stopping = True
        self._abandon_plate()
        self.home()

    @contextlib.contextmanager
    def _before_saving(self, day: date, hour: int, before: list[Entry]):
        """The part of a write that changes nothing yet. Left by a cancel, the app is backed out and the hour read
        again: `Cancelled` then states a fact. Left by a failure, the app is backed out as far as it will go."""
        try:
            yield
        except Cancelled:
            self.say("cancelling")
            self.abort()
            if [e.key for e in self.hour_entries(day, hour)] != [e.key for e in before]:
                evidence = self.phone.save_evidence("cancel verification")
                raise AppError(f"stopped, but {hour:02d}:00 on {day} is not as it was before (evidence: {evidence})") from None
            raise
        except Exception:
            if not self.committed:
                with contextlib.suppress(PhoneError):
                    self.abort()
            raise

    def _no_plate(self) -> None:
        """The phone is seen without a plate: whatever was written down of one is of no plate any more."""
        if self.staged() is not None:
            self.staging(None)

    def _until(self, check: Callable[[Snapshot], object], what: str, timeout: float = 10.0, settle: bool = True):
        return self.phone.until(check, what, timeout, settle)

    def _button(self, label: str, what: str | None = None, timeout: float = 8.0) -> Node:
        return self._until(lambda s: S.button(s, label), what or f"'{label}' button", timeout)

    # ------------------------------------------------------------ navigation

    def home(self) -> S.FoodLog:
        """Reach the Food Log tab with nothing open, from wherever the app is."""
        for _attempt in range(14):
            snap = self.phone.settled()
            if snap.foreground != PACKAGE:
                if snap.foreground in ("com.google.android.permissioncontroller",):
                    self.phone.back()
                else:
                    self.say("opening MacroFactor")
                    self.phone.launch(PACKAGE)
                    time.sleep(1.5)
                continue
            log = S.food_log(snap)
            if log is not None:
                if log.calendar_open and log.calendar_button is not None:
                    self._tap(log.calendar_button)
                    continue
                if not log.in_selection:
                    self._no_plate()
                    return log
                if log.selecting is not None:
                    self._tap(log.selecting)  # leave selection / move / copy mode
                else:
                    bar = next(iter(log.actions.values()))
                    self.phone.swipe(200, bar.center[1], 640, bar.center[1], 250)  # bring "Select" back
                continue
            logger = S.logger(snap)
            if logger is not None:
                staged = self.staged()
                if logger.plate_empty:
                    self._no_plate()
                elif staged is None:
                    raise AppError(FOREIGN_DRAFT)
                if snap.keyboard_top is not None:
                    self.phone.back()  # changes nothing of a plate
                    continue
                if logger.detail is not None and logger.detail.back is not None:
                    if not logger.plate_empty and not any(same_food(logger.detail.title, name) for name in staged["asked"]):
                        raise AppError(FOREIGN_DRAFT)  # an open food that no write of ours was adding: left as it is
                    self._tap(logger.detail.back)  # drops the provisional food
                    continue
                if not logger.plate_empty:  # a write of ours was interrupted while filling a plate: is this that plate?
                    _, rows = self._read_plate()
                    if not is_plate_of(rows, staged["plate"]):
                        raise AppError(FOREIGN_DRAFT)
                    self.say("discarding the plate an interrupted write left")
                    self._abandon_plate()  # forgotten once the phone is seen without it, and not before
                    continue
                self._tap(logger.close)
                continue
            nav = snap.first(lambda n: n.label.startswith("Food Log\nTab ") and n.clickable)
            if nav is not None and not snap.has("Scrim") and not nav.selected:
                self._tap(nav)
                continue
            if not any(n.label for n in snap.app()):  # between two screens: nothing to go back from, and Back would leave the app
                time.sleep(0.3)
                continue
            if snap.has("Exit Without Saving?"):  # Back only answers "Stay", and what is being edited is not mf's to drop
                raise AppError("MacroFactor is editing something unsaved (it asks 'Exit Without Saving?'); save or exit it on the phone")
            self.phone.back()
            time.sleep(0.3)
        raise AppError(f"cannot reach the Food Log (evidence: {self.phone.save_evidence('home')})")

    def goto(self, day: date) -> S.FoodLog:
        """Show `day` in the Food Log."""
        today = self.now().date()
        wanted = S.title_for(day, today)

        def check_date():
            if self.now().date() != today:
                raise DayChanged(f"local date changed while navigating to {day}")

        log = self.home()
        check_date()
        shown = S.title_date(log.title, today)
        if shown == day:
            return log
        if shown is None:
            raise AppError(f"unreadable Food Log title {log.title!r}")
        self.say(f"going to {wanted}")

        def arrived(snap: Snapshot):
            check_date()
            found = S.food_log(snap)
            return found if found is not None and S.title_date(found.title, today) == day else None

        near = shown  # cells say "31 / Su": the same run of days recurs months apart, so each frame is dated from the last

        def calendar(snap: Snapshot):
            check_date()
            return S.calendar_cells(snap, near)

        # The calendar's accessibility tree is only correct when the day list is scrolled to the top.
        log = self._scroll_top()
        if log.calendar_button is None:
            raise AppError("calendar button not found")
        self._tap(log.calendar_button)
        cells = self._until(calendar, "calendar")
        for _ in range(200):
            near = cells[len(cells) // 2].day
            target = next((c for c in cells if c.day == day), None)
            cell = target if target and not target.covered and target.node.y1 > 170 and target.node.y2 < 1390 else None
            if cell is not None:
                self._tap(cell.node)
                return self._until(arrived, f"Food Log on {wanted}")
            first, last = cells[0].day, cells[-1].day
            before = [c.day for c in cells]
            if day < first:
                self.phone.swipe(360, 500, 360, 1200, 350)  # earlier days are above
            elif day > last:
                self.phone.swipe(360, 1200, 360, 500, 350)
            else:
                # In range but clipped or covered: bring the date towards the middle, without a fling.
                self.phone.drag(360, 700, 940 if target and target.node.center[1] < 700 else 460)
            cells = self._until(calendar, "calendar")
            if [c.day for c in cells] == before and not (first <= day <= last):
                raise AppError(f"the calendar does not reach {day}")
        raise AppError(f"could not find {day} in the calendar")

    # ------------------------------------------------------------ reading a day

    def _log(self) -> S.FoodLog:
        """The settled Food Log. Re-reading is skipped while nothing has been sent to the phone since."""
        if self._seen_log is not None and self._seen_log[0] == self.phone.actions:
            return self._seen_log[1]
        log = self._until(lambda s: S.food_log(s), "Food Log")
        if log.hours and log.hours[0].hour == S.UNKNOWN_HOUR:
            # Seen while rows are selected: the header scrolled off and the times went with it. The rows were on
            # screen under their header one frame earlier (frames overlap), and that frame says which hour they are.
            shown = {(r.name, r.serving, r.kcal) for r in log.hours[0].rows}
            earlier = self._last_log.hours if self._last_log is not None and self._last_log.title == log.title else []
            hours = {g.hour for g in earlier if g.hour != S.UNKNOWN_HOUR and shown & {(r.name, r.serving, r.kcal) for r in g.rows}}
            if len(hours) != 1:
                raise AppError("cannot tell which hour the rows at the top of the Food Log belong to")
            log.hours[0].hour = hours.pop()
        self._seen_log = (self.phone.actions, log)
        self._last_log = log
        return log

    def _scroll(self, log: S.FoodLog | S.Plate | S.Picker, pixels: int) -> None:
        """Drag the visible list by about `pixels` (positive: later rows come up), without fling."""
        span = log.bottom - log.top - 60
        pixels = max(-span, min(span, pixels))
        y1 = log.bottom - 20 if pixels > 0 else log.top + 30
        self.phone.drag(420, y1, y1 - pixels - (SLOP if pixels > 0 else -SLOP))

    def _scroll_top(self, read: Callable[[], S.FoodLog] | None = None) -> S.FoodLog:
        read = read or self._log
        log = read()
        for _ in range(8):
            signature = log.layout
            self.phone.swipe(420, log.top + 60, 420, log.bottom - 20, 120)
            log = read()
            if log.layout == signature:
                break
        return log

    def _scan(
        self,
        hours: range | None = None,
        stop: Callable[[S.FoodLog, list[_Seen]], object] | None = None,
        *,
        read: Callable[[], S.FoodLog] | None = None,
    ):
        """Walk down the day list, giving every row a stable list coordinate.

        Returns (rows seen in order, value returned by `stop` or None). Rows of the same food
        and serving are distinct entries, so identity is position, measured from the overlapping
        hour chips and rows between snapshots. Ambiguous overlap is an error.
        """
        read = read or self._log
        log = read()
        first_hour = hours.start if hours else 0
        # Bring the first wanted hour into view.
        for _ in range(40):
            visible = [g.hour for g in log.hours if log.top <= g.y1 and g.y2 <= log.bottom]
            if not log.hours:
                raise AppError("Food Log hours are missing; no complete reading")
            target = next((g for g in log.hours if g.hour >= first_hour and g.y1 >= log.top - 4), None)
            before = log.layout
            starts_above = log.hours[0].hour > first_hour or (log.hours[0].hour == first_hour and log.hours[0].y1 < log.top)
            if not starts_above and visible and target is not None and target.y2 <= log.bottom:
                if target.y1 - log.top > 160:
                    self._scroll(log, target.y1 - log.top - 30)
                    log = read()
                break
            if starts_above:
                self._scroll(log, -(log.bottom - log.top))
            else:
                self._scroll(log, log.bottom - log.top)
            log = read()
            if log.layout == before:
                if starts_above and log.hours[0].y1 >= log.top:
                    break  # top reached: earlier hours really are absent
                raise AppError("could not reach the requested hour; the list did not move")
        else:
            raise AppError("could not reach the start of the requested hour")
        seen: list[_Seen] = []
        offset = 0  # how far the list has been scrolled since the first snapshot
        for _ in range(60):
            for group in log.hours:
                if hours is not None and group.hour not in hours:
                    continue
                for row in group.rows:
                    y = row.node.y1 + offset
                    known = next(
                        (
                            s
                            for s in seen
                            if s.hour == group.hour
                            and s.row.name == row.name
                            and s.row.serving == row.serving
                            and abs(s.y - y) <= ROW_TOLERANCE
                        ),
                        None,
                    )
                    if known is None:
                        seen.append(_Seen(row, group.hour, y))
                    else:
                        known.row = row  # keep the node that is on screen now, so callers can tap it
            if stop is not None:
                value = stop(log, seen)
                if value:
                    return seen, value
            if hours is not None and any(g.hour >= hours.stop and g.y1 < log.bottom for g in log.hours):
                break
            if log.at_end:
                break
            step = S.scan_step(log)
            self._scroll(log, step)
            new_log = read()
            moved = S.scroll_offset(log, new_log, step + SLOP)
            if moved is None:
                raise AppError("cannot establish the overlap between Food Log screens; no complete reading")
            if moved <= 2:
                raise AppError("Food Log stopped scrolling before the end; no complete reading")
            offset += moved
            log = new_log
        else:
            raise AppError("Food Log scan did not reach the end")
        seen.sort(key=lambda s: (s.hour, s.y))
        return seen, None

    def _entries(self, day: date, seen: list[_Seen], times: dict[int, list[str]]) -> list[Entry]:
        """The rows seen as entries. An hour lists its distinct timestamps (`times`), not whose they are. Where it
        lists one, every row of it has that minute. Where it lists several, each is drawn beside the first row of
        its minute (`stamped`): when the rows so marked are as many as the times, each row has the time above it."""
        entries: list[Entry] = []
        position: dict[int, int] = {}
        minutes: dict[int, list[int]] = {}  # hour -> the minute of each of its rows, where the screen settles it
        for hour, stamps in times.items():
            rows = [s for s in seen if s.hour == hour]
            starts = [True, *(s.stamped for s in rows[1:])]  # the hour's first row has its first time
            if len(stamps) == 1 or (None not in starts and sum(starts) == len(stamps)):
                at = [sum(starts[: i + 1]) - 1 if len(stamps) > 1 else 0 for i in range(len(rows))]
                minutes[hour] = [int(stamps[i].split(":")[1]) for i in at]
        for s in seen:
            index = position.get(s.hour, 0)
            position[s.hour] = index + 1
            entries.append(
                Entry(
                    day,
                    s.hour,
                    s.row.name,
                    s.row.serving,
                    s.row.kcal,
                    s.row.protein,
                    s.row.fat,
                    s.row.carbs,
                    minute=minutes[s.hour][index] if s.hour in minutes else None,
                    position=index,
                )
            )
        return entries

    def read_day(self, day: date) -> DayView:
        try:
            return self._read_day(day)
        except DayChanged as error:
            self.say(f"{error}; discarding the scan and reading {day} again")
            self._seen_log = None
        return self._read_day(day)

    def _day_reader(self, day: date, navigate: bool, *, whole: bool = False) -> Callable[[], S.FoodLog]:
        """Bind settled scan frames to the requested day. Only a whole-day scan restarts on change."""
        started = self.now().date()
        self._seen_log = None  # another scan needs a new observation even if mf sent no input
        arrived = self.goto(day) if navigate else self._log()
        if whole and navigate and not arrived.totals and S.title_date(arrived.title, started) == day:
            # Seen once (2026-11-18): the totals drawn on screen were absent from what the app exposes, read after
            # read, until another day had been shown. Nothing is written here, so show a neighbour and come back.
            self.say("the day's totals are not readable; showing the day before and coming back")
            self.goto(day - timedelta(days=1))
            arrived = self.goto(day)
        wanted = S.title_for(day, started)

        def check(log: S.FoodLog) -> S.FoodLog:
            if self.now().date() != started:
                raise DayChanged(f"local date changed while reading {day}")
            if S.title_date(log.title, started) != day:
                raise DayChanged(f"expected {wanted}, found {log.title}")
            if whole:
                if not log.totals or not arrived.totals:
                    raise AppError("Food Log totals are missing; no complete reading")
                if any(log.totals[m][0] != pair[0] for m, pair in arrived.totals.items()):
                    raise DayChanged(f"eaten totals changed while reading {day}")
            return log

        check(arrived)
        return lambda: check(self._log())

    def _read_day(self, day: date) -> DayView:
        read = self._day_reader(day, navigate=True, whole=True)
        self._scroll_top(read)
        times: dict[int, list[str]] = {}

        def note_times(current: S.FoodLog, seen: list[_Seen]) -> None:
            picture = None
            for group in current.hours:
                if group.times:
                    times[group.hour] = group.times
                if len(group.times) < 2:
                    continue
                for s in seen:  # whose time is whose is only drawn: beside the first row of each
                    whole = s.row.node.y1 + s.row.node.x1 <= current.bottom
                    if s.stamped is None and whole and any(s.row is row for row in group.rows):
                        picture = picture or self.phone.picture()
                        s.stamped = S.stamp_beside(picture, s.row.node)

        seen, _ = self._scan(stop=note_times, read=read)
        minutes = {h: sorted(int(s.split(":")[1]) for s in stamps) for h, stamps in times.items()}
        return DayView(read().totals, self._entries(day, seen, times), minutes)

    def hour_entries(self, day: date, hour: int, navigate: bool = True) -> list[Entry]:
        """Rows of one hour. `navigate=False` when the Food Log is known to already show `day`."""
        read = self._day_reader(day, navigate)
        seen, _ = self._scan(range(hour, hour + 1), read=read)
        return self._entries(day, seen, {hour: (read().hour(hour) or S.HourGroup(hour)).times})

    def log(self, items: list[Item], day: date | None = None, hour: int | None = None, minute: int | None = None) -> list[Entry]:
        """Log foods and return confirmed entries in request order, retaining their actual phone positions."""
        if not items:
            return []
        now = self.now()
        day = day or now.date()
        is_now = hour is None
        hour = now.hour if hour is None else hour
        before = self.hour_entries(day, hour)
        log = self._log()
        group = log.hour(hour)

        def plus_in_view() -> bool:
            return group is not None and group.plus is not None and log.top <= group.plus.y1 and group.plus.y2 <= log.bottom

        if not is_now and not plus_in_view():
            # Reading a long hour leaves its header above the screen. Its own "+" opens the logger on that day and
            # hour; the search shortcut opens it on now, and the time picker then needed was seen to leave the search
            # field reported where the keyboard had been, for good. So the header is brought back.
            _, log = self._scan(range(hour, hour + 1), stop=lambda shown, seen: shown, read=self._day_reader(day, navigate=False))
            group = log.hour(hour)
        if is_now and day == now.date() and log.shortcuts:
            self._tap(log.shortcuts[0])
        elif plus_in_view():
            self._tap(group.plus)
        elif log.shortcuts:
            self._tap(log.shortcuts[0])
        else:
            raise AppError("no way to open the food search from this screen")
        opened = self.now() - timedelta(seconds=2)  # the tap above; the phone's clock may be a little ahead
        logger = self._until(lambda s: S.logger(s), "food search")
        if not logger.plate_empty:  # checked before anything of ours is on it, and before anything may be backed out
            raise AppError(FOREIGN_DRAFT)
        added: list[S.Detail] = []
        with self._before_saving(day, hour, before):

            def write_down(next_food: S.Detail | None = None) -> None:
                foods = [*added, *([next_food] if next_food else [])]
                plate = [{key: getattr(food, key) for key in STAGED} for food in foods]
                self.staging({"asked": [item.name for item in items], "plate": plate})

            write_down()
            logger = self._set_logger_time(logger, day, hour)
            for item in items:
                added.append(self._add_to_plate(item, write_down))
            plate = self._verify_plate(added)
            self.say("saving")
            self._commit(plate.log_button)
        self._until(lambda s: S.food_log(s), "Food Log after saving", 15)
        self._no_plate()  # saved
        after = self.hour_entries(day, hour, navigate=False)
        new = _new_entries(before, after)
        matched = match_foods(new, added)
        if matched is None:
            raise AppError(
                f"saved foods at {hour:02d}:00 do not match the requested foods and amounts: "
                f"expected {[(d.title, d.quantity, d.unit) for d in added]}, found {[(e.name, e.serving) for e in new]}; "
                "the Food Log was not changed further "
                f"(evidence: {self.phone.save_evidence('log verification')})"
            )
        if not is_now and minute:
            new = self.retime(after, [e.position for e in new], hour, minute)
            matched = match_foods(new, added)
            if matched is None:
                raise AppError("retimed foods no longer match the saved plate")
        elif not is_now:
            for entry in new:
                entry.minute = 0
        else:
            # The hour lists the minutes it holds and its rows in time order, not which row has which. MacroFactor
            # stamps a plate somewhere between its opening and its saving (measured: either side of a minute that
            # turned meanwhile), so the entry's minute is the one the hour lists within that span; failing that,
            # rows that come last in the hour have its last minute.
            listed = sorted({int(stamp.split(":")[1]) for stamp in self._hour_times(hour)})
            span = {(opened + timedelta(seconds=step)).minute for step in range(0, int((self.now() - opened).total_seconds()) + 3)}
            ours = [m for m in listed if m in span and opened.hour == hour]
            last = {e.position for e in new} == set(range(len(before), len(after)))
            for entry in new if len(ours) == 1 or (last and listed) else []:
                entry.minute = ours[0] if len(ours) == 1 else listed[-1]
        # Otherwise keep the readback's minute (possibly unknown), never the clock from before staging began.
        # Both front ends associate each result with its request. The phone is allowed to reorder the plate.
        ordered = sorted(zip(new, matched, strict=True), key=lambda pair: added.index(pair[1]))
        for entry, detail in ordered:
            entry.weighed = detail.weight is not None
            entry.protein, entry.fat, entry.carbs = detail.protein, detail.fat, detail.carbs
        return [entry for entry, _ in ordered]

    def _set_logger_time(self, logger: S.Logger, day: date, hour: int) -> S.Logger:
        today = self.now().date()
        shown_day, shown_hour = S.parse_when(logger.when, today)
        if (shown_day, shown_hour) == (day, hour):
            return logger
        self.say(f"setting time to {S.wheel_date_label(day, today)} {hour:02d}:00")
        self._tap(logger.time_chip)
        self._until(lambda s: len(S.wheels(s)) == 2 and S.button(s, "Select Date and Time"), "date and time picker")
        self._set_wheel(0, S.wheel_date_label(day, today), lambda label: S.wheel_date(label, today).toordinal())
        self._set_wheel(1, f"{hour}:00", lambda label: int(label.split(":")[0]))
        self._tap(self._picker_button("Select Date and Time", [S.wheel_date_label(day, today), f"{hour}:00"]))
        logger = self._until(lambda s: S.logger(s), "food search")
        if S.parse_when(logger.when, today) != (day, hour):
            raise AppError(f"time picker did not apply: header shows {logger.when!r}")
        return logger

    def _set_wheel(self, index: int, target: str, order: Callable[[str], int]) -> None:
        """Turn a picker wheel to `target`. `order` maps a label to its position on the wheel.

        Read after every move. Release drags at rest so a transient label during a fling cannot
        be mistaken for the final selection. Nearby rows are selected with a tap.
        """
        goal = order(target)
        previous = None
        for _ in range(300):
            wheel = self._until(lambda s: (w := S.wheels(s)) and len(w) > index and w[index], "picker wheel", 8)
            diff = goal - order(wheel.value)
            if diff == 0:
                return
            if wheel.value == previous:
                raise AppError(f"picker did not move towards {target!r}")
            previous = wheel.value
            x, cy = wheel.node.center
            row = wheel.row_height
            sign = 1 if diff > 0 else -1
            size = abs(diff)
            if size <= 2:
                self.phone.tap((x, round(cy + sign * size * row)))
            else:
                self.phone.drag(x, round(cy + sign * 2 * row), round(cy - sign * 2 * row))
        raise AppError(f"could not set picker to {target!r}")

    def _picker_button(self, label: str, values: list[str]) -> Node:
        """The confirmation button is usable only while ALL wheels still show the requested selection."""
        return self._until(
            lambda s: [wheel.value for wheel in S.wheels(s)] == values and S.button(s, label),
            f"picker set to {', '.join(values)}",
        )

    def _add_to_plate(self, item: Item, write_down: Callable[[S.Detail], None]) -> S.Detail:
        _, detail = self._choose(item, S.logger)
        self.say(f"adding {detail.quantity} {detail.unit} {detail.title} ({detail.kcal:g} kcal)")
        add = detail.keys.get("Add")
        if add is None:
            raise AppError("Add button not found")
        write_down(detail)  # before it is there
        self._tap(add)
        self._until(lambda s: (lg := S.logger(s)) and lg.detail is None, "the list after adding food")
        return detail

    def _verify_plate(self, expected: list[S.Detail]) -> S.Plate:
        """Read our exposed draft completely, then verify identities, amounts and multiplicity before saving."""
        self.say("checking plate")
        plate, rows = self._read_plate()
        if match_foods(rows, expected, staged=True) is None:
            raise AppError(
                f"staged foods do not match the request: expected {[(d.title, d.quantity, d.unit) for d in expected]}, "
                f"found {[(r.name, r.serving) for r in rows]} "
                f"(evidence: {self.phone.save_evidence('plate verification')})"
            )
        return plate

    def _read_plate(self) -> tuple[S.Plate, list[S.FoodRow]]:
        """Expose the plate and read every food on it, top to bottom; the plate is left showing its end."""
        snap = self.phone.hide_keyboard()
        logger = S.logger(snap)
        if logger is None or logger.detail is not None:
            raise AppError("food search is not ready to show the plate")
        self._tap(logger.header, snap)
        plate = self._until(S.plate, "exposed plate")
        for _ in range(40):
            if plate.at_start:
                break
            previous = plate.layout
            self._scroll(plate, -(plate.bottom - plate.top) // 2)
            plate = self._until(S.plate, "plate start")
            if plate.layout == previous:
                raise AppError("plate stopped scrolling before its heading; no complete reading")
        else:
            raise AppError("could not reach the start of the plate")
        seen: list[tuple[int, S.FoodRow]] = []
        offset = 0
        for _ in range(120):
            for row in plate.rows:
                y = row.node.y1 + offset
                if not any(abs(y - old_y) <= ROW_TOLERANCE for old_y, _ in seen):
                    seen.append((y, row))
            if plate.at_end:
                break
            step = S.scan_step(plate)
            self._scroll(plate, step)
            new = self._until(S.plate, "plate after scrolling")
            moved = S.scroll_offset(plate, new, step + SLOP)
            if moved is None or moved <= 2:
                raise AppError("cannot establish the overlap between plate screens; no complete reading")
            offset += moved
            plate = new
        else:
            raise AppError("plate scan did not reach Nutrition; no complete reading")
        return plate, [row for _, row in sorted(seen, key=lambda item: item[0])]

    def _choose(self, item: Item, read: Callable[[Snapshot], S.Logger | S.Picker | None]) -> tuple[S.FoodRow, S.Detail]:
        """Search a food in the list `read` sees, open it and set its serving. Returns its search row and its details."""
        panel = self._list_head(read)
        tab = "Library" if item.library else "Search"
        if panel.selected_tab != tab:
            for _ in range(3):  # six tabs, four or so in view: Search is at the left of the strip, Library at its right
                if tab in panel.tabs or not panel.tabs:
                    break
                y = next(iter(panel.tabs.values())).center[1]
                self.phone.swipe(*((200, y, 620, y) if tab == "Search" else (620, y, 200, y)), 300)
                panel = self._list(read, "the tabs")
            if tab not in panel.tabs:
                raise AppError(f"{tab} tab not found")
            self._tap(panel.tabs[tab])
            panel = self._until(lambda s: (p := read(s)) and p.selected_tab == tab and p.search_field and p, f"{tab} tab")
        query = search_query(item.name)
        self.say(f"searching {query!r}")
        results = self._type_query(panel, query, read, filters=item.library)

        try:
            panel = self._until(results, f"search results for {query!r}", 5 if item.library else 12)
        except UiTimeout:
            snap = self.phone.snapshot()
            chip = snap.first(lambda n: n.label == "Recipes" and n.clickable)
            if item.library and chip is not None and snap.first(lambda n: "No foods match" in n.label):
                self._tap(chip)  # the library was showing custom foods, not recipes
                with contextlib.suppress(UiTimeout):
                    self._until(results, "recipes in the library", 6)
            panel = self._until(lambda s: read(s), "food search")

        missing: NoSuchUnit | None = None
        for opened in range(4):
            try:
                row = self._reach(item, read, opened)
            except FoodNotFound as error:
                raise missing or error from None
            self._tap(row.node)
            detail = self._until(
                lambda s, row=row: (p := read(s)) and p.detail and same_food(p.detail.title, row.name) and p.detail,
                f"{row.name} details",
            )
            try:
                detail = self._set_serving(detail, item.quantity, item.unit, lambda s: (p := read(s)) and p.detail)
                break
            except NoSuchUnit as error:
                # Two foods of one name, alike to the gram, need not have the same units (seen: a galette made anew to
                # have a "portion"). In the logger an open food leaves the plate with its details: the other is tried.
                if read is not S.logger:
                    raise
                missing = error
                self._leave_details()
        else:
            raise missing
        if item.shown is not None and not S.is_amount_of(item.shown, float(detail.quantity), detail.unit, detail.kcal, detail.weight):
            raise AppError(  # the row was chosen by what it showed; what it opened as decides
                f"{detail.title} opened as {detail.kcal:g} kcal for {detail.quantity} {detail.unit}, "
                f"not as the recipe of {item.shown['kcal']:g} kcal a serving that was meant"
            )
        return row, detail

    def _leave_details(self) -> None:
        """From a food open in the logger back to its list, the food taken off the plate again."""
        for _ in range(6):
            logger = self._until(S.logger, "food details")
            if logger.detail is None:
                return
            if logger.detail.expanded or logger.detail.back is None:
                self.phone.back()  # the keypad first
            else:
                self._tap(logger.detail.back)
        raise AppError("the food's details did not close")

    def _list(self, read: Callable[[Snapshot], S.Logger | S.Picker | None], what: str) -> S.Logger | S.Picker:
        """The food list, no food's details open over it."""
        return self._until(lambda s: (p := read(s)) and p.detail is None and p, what)

    def _list_head(self, read: Callable[[Snapshot], S.Logger | S.Picker | None]) -> S.Logger | S.Picker:
        """The food list with its search field in view. The Library's field heads its list and leaves with it, and
        the wizard's Done with the field (seen after adding a recipe found eight rows down)."""
        for _ in range(40):
            panel = self._list(read, "food search list")
            if panel.search_field:
                return panel
            self.phone.drag(360, panel.top + 60, panel.bottom - 20)
        raise AppError("the search field did not come back into view")

    def _type_query(self, panel: S.Picker, query: str, read: Callable[[Snapshot], S.Picker | None], filters: bool = False):
        """Type `query` in the search field. Returns the check that its results are shown. `filters`: the Library, whose
        list has no sections and is cut down to the rows holding every word typed, a moment after the text shows."""
        if not panel.search_field.focused:
            self._tap(panel.search_field)
        self.phone.fill(query)

        def results(snap: Snapshot):
            found = read(snap)
            if found is None or found.search_field is None or norm(found.search_field.text) != norm(query):
                return None
            if filters:
                words = norm(query).split()
                return found if found.rows and all(word in norm(r.name) for r in found.rows for word in words) else None
            return found if found.sections and "Picks" not in " ".join(found.sections) else None

        return results

    def search(self, query: str) -> tuple[list[S.FoodRow], dict[str, int]]:
        """What MacroFactor's food search lists for `query`, section after section (history, the common foods and the
        branded ones of its database), and how many more each section holds behind its "See N More". Nothing is
        opened or added."""
        log = self.home()
        if not log.shortcuts:
            raise AppError("no way to open the food search from this screen")
        self._tap(log.shortcuts[0])
        logger = self._until(lambda s: (lg := S.logger(s)) and lg.detail is None and lg.search_field and lg, "food search")
        if not logger.plate_empty:
            raise AppError(FOREIGN_DRAFT)
        try:
            if logger.selected_tab != "Search":
                self._tap(logger.tabs["Search"])
                logger = self._until(lambda s: (lg := S.logger(s)) and lg.selected_tab == "Search" and lg.search_field and lg, "Search tab")
            results = self._type_query(logger, ascii_fold(query).strip(), S.logger)
            with contextlib.suppress(UiTimeout):  # nothing found is an answer too: the list then stays empty
                self._until(results, "search results", 10)
            self.phone.hide_keyboard()
            rows: list[S.FoodRow] = []
            more: dict[str, int] = {}
            shown: list[S.FoodRow] = []  # the frame before, each row with its section
            for _ in range(12):  # the list is longer than the screen: its sections follow one another down it
                found = self._until(lambda s: (lg := S.logger(s)) and not lg.keyboard and lg, "search results")
                for name, header in found.section_nodes.items():
                    if hidden := re.search(r"See (\d+) More", header.label):
                        more[name] = int(hidden[1])
                shown, new = continue_list(shown, found.rows)
                rows += [row for row in new if row.section and "Picks" not in row.section]
                before = found.layout
                self.phone.drag(360, found.bottom - 20, found.top + 60)
                if self._until(lambda s: (lg := S.logger(s)) and lg, "search results").layout == before:
                    break  # the end of the list
            return rows, more
        finally:
            self.home()

    def _reach(self, item: Item, read: Callable[[Snapshot], S.Logger | S.Picker | None], after: int = 0) -> S.FoodRow:
        """The result row of `item`, whole and uncovered, wherever it is in the list. `after`: how many rows of that
        food were already opened and found without the unit asked for (it is then the next one; none left: not found).
        The rows of one food are counted in one frame: one of them out of view is not tried, and the unit is refused.

        The keyboard and the floating search bar cover the end of the list. History results show five rows, then
        "See N More"; expanded, they are followed by the other sections, whose headers scroll with the rows.
        """

        scrolled = after > 0  # a second look finds the list where the first left it, perhaps past its header
        panel = self._list(read, "search results")
        for _ in range(60):
            half = (panel.bottom - panel.top) // 2
            each = rows_meant(panel.rows, item, ("From History", "") if scrolled else ("From History",))  # scrolled: no header above
            if each and after >= len(each):
                raise FoodNotFound(item.name, [r.name for r in each])
            row = each[after] if each else None
            if row is not None and panel.reachable(row):
                return row
            if panel.keyboard:
                self.phone.hide_keyboard()
                panel = self._list(read, "search results")
                continue
            more = panel.section_nodes.get("From History")
            hidden = more is not None and "More" in more.label  # history shows five rows until it is expanded
            if hidden and (row is None or (item.exact and row.section != "From History")):
                self.phone.tap((more.x2 - 105, more.center[1]))  # the link is the right end of the header
                self._until(
                    lambda s: (p := read(s)) and "Less" in getattr(p.section_nodes.get("From History"), "label", ""), "all of history"
                )
                panel = self._list(read, "search results")
                continue
            expanded = scrolled or (more is not None and "Less" in more.label)  # what the screen says, not what was tapped
            if (
                row is None
                and not item.library
                and not item.exact
                and (not expanded or any(name != "From History" for name in panel.sections))
            ):
                break  # every history result has been seen
            before = panel.layout
            if row is not None and row.node.y1 <= panel.top:  # cut by the top edge: bring it back down
                self._scroll(panel, -half)
            else:
                self._scroll(panel, half if row is None else min(half, row.node.y2 - panel.bottom + 40))
            scrolled = True
            panel = self._list(read, "search results")
            if panel.layout == before:
                break  # the end of the list
        raise FoodNotFound(item.name, [r.name for r in panel.rows if item.exact or r.section in ("From History", "")])

    def _set_serving(self, detail: S.Detail, quantity: float | None, unit: str | None, read: Callable[[Snapshot], object]) -> S.Detail:
        """Set unit then quantity on an open food and confirm what the field shows."""
        if quantity is None and unit is None:
            return detail
        if not detail.expanded:
            self._tap(detail.field_node)
            detail = self._until(lambda s: (d := read(s)) and d.expanded and d, "quantity keypad")
        if unit is not None and norm(detail.unit) != norm(unit):

            def chip_of(shown: S.Detail) -> Node | None:
                found = next((node for label, node in shown.units.items() if norm(label) == norm(unit)), None)
                return found if found is not None and found.x1 >= 96 and found.x2 <= 724 else None

            with contextlib.suppress(UiTimeout):  # the chips load a moment after the keypad
                detail = self._until(lambda s: (d := read(s)) and d.expanded and chip_of(d) and d, "unit chips", 2.5)
            offered = list(detail.units)  # the strip scrolls: every unit it showed on the way
            for _ in range(6):
                if chip_of(detail) is not None or detail.unit_strip is None:
                    break
                y = detail.unit_strip.center[1]
                before = [(label, node.x1) for label, node in detail.units.items()]
                self.phone.swipe(620, y, 260, y, 350)
                detail = self._until(lambda s: (d := read(s)) and d.expanded and d, "unit chips")
                offered += [label for label in detail.units if label not in offered]
                if [(label, node.x1) for label, node in detail.units.items()] == before:
                    break
            chip = chip_of(detail)
            if chip is None:
                raise NoSuchUnit(f"{detail.title} has no unit {unit!r}; it offers: {', '.join(offered)}")
            self._tap(chip)
            detail = self._until(lambda s: (d := read(s)) and norm(d.unit) == norm(unit) and d, f"unit {unit}")
        if quantity is not None:
            wanted = fmt_quantity(quantity)
            if detail.quantity != wanted:
                self.phone.type(wanted)  # the field's content is selected when the keypad opens
                try:
                    detail = self._until(lambda s: (d := read(s)) and d.quantity == wanted and d, f"quantity {wanted}", 4)
                except UiTimeout:
                    detail = self._until(lambda s: read(s), "quantity keypad")
                    if detail.backspace is None:
                        raise
                    for _ in range(len(detail.quantity) + 1):
                        self.phone.tap(detail.backspace)
                    self.phone.type(wanted)
                    detail = self._until(lambda s: (d := read(s)) and d.quantity == wanted and d, f"quantity {wanted}", 6)
        return detail

    def _abandon_plate(self) -> None:
        """After a failure while filling the plate: close the logger without saving anything."""
        try:
            for _ in range(10):
                snap = self.phone.settled()
                if S.food_log(snap) is not None:
                    return
                if snap.has("Exit the Plate?"):
                    self._tap(self._button("Exit"))
                    continue
                logger = S.logger(snap)
                if logger is None or snap.keyboard_top is not None:
                    self.phone.back()
                elif logger.detail is not None and logger.detail.back is not None:
                    self._tap(logger.detail.back)
                else:
                    self._tap(logger.close)
        except PhoneError:
            pass

    # ------------------------------------------------------------ changing saved entries

    def _find(self, target: Entry) -> S.FoodRow:
        """Bring the `position`-th row of `hour` fully on screen and return it."""
        day, hour, position = target.day, target.hour, target.position
        read = self._day_reader(day, navigate=False)  # going home would cancel a selection in progress

        def on_screen(log: S.FoodLog, seen: list[_Seen]):
            rows = sorted((s for s in seen if s.hour == hour), key=lambda s: s.y)
            if len(rows) <= position:
                return None
            node = rows[position].row.node
            visible = any(r.node is node for g in log.hours for r in g.rows)
            return rows[position].row if visible and node.y1 >= log.top and node.y2 <= log.bottom else None

        _, found = self._scan(range(hour, hour + 1), stop=on_screen, read=read)
        if not found:
            raise AppError(f"no entry #{position + 1} at {hour:02d}:00 on {day}")
        if (found.name, found.serving) != target.key:
            raise AppError(f"entry #{position + 1} at {hour:02d}:00 on {day} changed before selection")
        return found

    def _open_entry(self, target: Entry) -> S.Detail:
        row = self._find(target)
        if row.button is None:
            raise AppError("edit button not found")
        self._tap(row.button)
        return self._until(lambda s: s.has("Scrim") and (d := S.detail(s)) and same_food(d.title, row.name) and d, f"{row.name} editor")

    def remove(self, before: list[Entry], position: int) -> Entry:
        target = before[position]
        day, hour = target.day, target.hour
        with self._before_saving(day, hour, before):
            detail = self._open_entry(target)
            remove = detail.actions.get("Remove")
            if remove is None:
                raise AppError("Remove button not found")
            self.say(f"removing {target.name} {target.serving}")
            self._commit(remove)
        self._until(lambda s: S.food_log(s), "Food Log after removing", 12)
        after = self.hour_entries(day, hour)
        expected = [e.key for i, e in enumerate(before) if i != position]
        if [e.key for e in after] != expected:
            raise AppError(f"removal not confirmed (evidence: {self.phone.save_evidence('remove verification')})")
        return target

    def edit(self, before: list[Entry], position: int, quantity: float, unit: str | None = None) -> Entry:
        target = before[position]
        day, hour = target.day, target.hour
        with self._before_saving(day, hour, before):
            detail = self._open_entry(target)
            detail = self._set_serving(detail, quantity, unit, lambda s: s.has("Scrim") and S.detail(s))
            done = detail.keys.get("Done") or detail.done
            if done is None:
                raise AppError("Done button not found")
            self.say(f"setting {detail.title} to {detail.quantity} {detail.unit}")
            self._commit(done)
        self._until(lambda s: S.food_log(s), "Food Log after editing", 12)
        after = self.hour_entries(day, hour)
        if len(after) != len(before):
            raise AppError("entry count changed while editing")
        entry = after[position]
        unchanged = all(e.key == before[i].key for i, e in enumerate(after) if i != position)
        if not unchanged or entry.name != before[position].name or not S.same_serving(entry.serving, quantity, detail.unit):
            raise AppError(f"edit not confirmed (evidence: {self.phone.save_evidence('edit verification')})")
        entry.protein, entry.fat, entry.carbs = detail.protein, detail.fat, detail.carbs
        return entry

    def _select(self, before: list[Entry], positions: list[int]) -> S.FoodLog:
        """Select these rows of one observed hour; returns the Food Log with its selection bar."""
        for count, position in enumerate(sorted(positions), start=1):
            row = self._find(before[position])
            self.phone.tap((row.node.x1 + 180, row.node.center[1]))  # row body (not the pencil) selects it
            self._until(
                lambda s, count=count: (f := S.food_log(s)) and f.selecting and f"\n{count} Food" in f.selecting.label,
                f"{count} selected",
            )
        return self._until(lambda s: (f := S.food_log(s)) and f.in_selection and f, "selection bar")

    def move(self, before: list[Entry], positions: list[int], to_day: date, new_hour: int) -> tuple[list[Entry], S.FoodLog]:
        """Move the selected rows of one observed hour to `new_hour` of another day with MacroFactor's own Move.

        The entries themselves are moved: they keep what was logged, whatever their recipe has become since.
        Returns them as read at the destination, with their minute where the screen tells it, and the Food Log
        of the day they left as it stood after the move (for its totals); the app is left on the destination day.
        """
        targets = [before[p] for p in positions]
        day, hour = targets[0].day, targets[0].hour
        today = self.now().date()
        if move_steps(day, to_day) != [to_day]:
            raise AppError(f"MacroFactor's Move does not reach {to_day} from {day} in one go")
        arriving = self.hour_entries(to_day, new_hour)
        listed = set(self._hour_times(new_hour))
        if [_whole(e) for e in self.hour_entries(day, hour)] != [_whole(e) for e in before]:
            raise AppError(f"{hour:02d}:00 on {day} changed before the move")
        date_label, hour_label = S.wheel_date_label(to_day, today), f"{new_hour}:00"
        with self._before_saving(day, hour, before):
            log = self._select(before, positions)
            if "Move" not in log.actions:
                raise AppError("Move not found in the selection bar")
            self._tap(log.actions["Move"])
            log = self._until(lambda s: (f := S.food_log(s)) and "To Date & Time" in f.actions and f, "move targets")
            self._tap(log.actions["To Date & Time"])
            self._until(lambda s: len(S.wheels(s)) == 2 and S.button(s, "Move to Date and Time"), "date and time picker")
            self.say(f"moving to {date_label} {new_hour:02d}:00")
            self._set_wheel(0, date_label, lambda label: S.wheel_date(label, today).toordinal())
            self._set_wheel(1, hour_label, lambda label: int(label.split(":")[0]))
            self._commit(self._picker_button("Move to Date and Time", [date_label, hour_label]))
        self._until(lambda s: (f := S.food_log(s)) and not f.in_selection and not S.wheels(s), "Food Log after moving", 12)
        remaining = self.hour_entries(day, hour, navigate=False)
        if [_whole(e) for e in remaining] != [_whole(e) for e in before if e.position not in positions]:
            raise AppError(f"the hour left did not lose exactly the moved entries (evidence: {self.phone.save_evidence('move source')})")
        left = self._log()  # still the day they left, its totals now without them
        arrived = _new_entries(arriving, self.hour_entries(to_day, new_hour))
        if sorted(_whole(e) for e in arrived) != sorted(_whole(e) for e in targets):
            raise AppError(
                f"the moved entries are not at {new_hour:02d}:00 on {to_day} as they were (evidence: {self.phone.save_evidence('move')})"
            )
        # MacroFactor was seen to put a moved entry at the full hour, and also to keep its minute: read, not assumed.
        # The hour lists its minutes, not whose they are; one minute it did not list before is the one that arrived.
        appeared = set(self._hour_times(new_hour)) - listed
        result, pool = [], list(arrived)
        for target in targets:
            match = next(e for e in pool if _whole(e) == _whole(target))
            pool.remove(match)
            if match.minute is None and len(appeared) == 1:
                match.minute = int(next(iter(appeared)).split(":")[1])
            result.append(match)
        return result, left

    def _hour_times(self, hour: int) -> list[str]:
        """The timestamps the Food Log on screen lists for an hour just read."""
        return (self._log().hour(hour) or S.HourGroup(hour)).times

    def retime(self, before: list[Entry], positions: list[int], new_hour: int, new_minute: int) -> list[Entry]:
        """Give the selected rows of one observed hour a new timestamp."""
        targets = [before[p] for p in positions]
        day, hour = targets[0].day, targets[0].hour
        destination_before = before if new_hour == hour else self.hour_entries(day, new_hour, navigate=False)
        with self._before_saving(day, hour, before):
            log = self._select(before, positions)
            for _ in range(4):
                if "Modify Timestamp" in log.actions and log.actions["Modify Timestamp"].x2 <= 720:
                    break
                bar = log.actions[next(iter(log.actions))]
                self.phone.swipe(620, bar.center[1], 240, bar.center[1], 300)
                log = self._until(lambda s: (f := S.food_log(s)) and f.in_selection and f, "selection bar")
            else:
                raise AppError("Modify Timestamp not found")
            self._tap(log.actions["Modify Timestamp"])
            self._until(lambda s: len(S.wheels(s)) == 2 and S.button(s, "Done"), "time picker")
            self.say(f"setting time to {new_hour:02d}:{new_minute:02d}")
            self._set_wheel(0, str(new_hour), int)
            self._set_wheel(1, f"{new_minute:02d}", int)
            self._commit(self._picker_button("Done", [str(new_hour), f"{new_minute:02d}"]))
        self._until(lambda s: (f := S.food_log(s)) and not f.in_selection, "Food Log after changing time", 12)
        after = self.hour_entries(day, new_hour, navigate=False)
        if new_hour == hour:
            if _new_entries(before, after):
                raise AppError("other entries changed during retiming")
            moved = after
        else:
            moved = _new_entries(destination_before, after)
            if sorted(e.key for e in moved) != sorted(e.key for e in targets):
                raise AppError("destination hour did not gain exactly the selected entries")
        stamps = self._hour_times(new_hour)
        result: list[Entry] = []
        pool = list(moved)
        for target in targets:
            match = next((e for e in pool if e.key == target.key), None)
            if match is None or f"{new_hour}:{new_minute:02d}" not in stamps:
                raise AppError(f"time change not confirmed for {target.name} (evidence: {self.phone.save_evidence('retime verification')})")
            pool.remove(match)
            match.minute = new_minute
            result.append(match)
        if new_hour != hour:
            remaining = self.hour_entries(day, hour, navigate=False)
            if [e.key for e in remaining] != [e.key for e in before if e.position not in positions]:
                raise AppError("source hour did not lose exactly the selected entries")
        return result

    # ------------------------------------------------------------ recipes

    def _open_library(self) -> S.Logger:
        log = self.home()
        if len(log.shortcuts) < 4:  # just launched: the row of shortcuts is drawn a moment after the log
            with contextlib.suppress(UiTimeout):
                log = self._until(lambda s: (shown := S.food_log(s)) and len(shown.shortcuts) >= 4 and shown, "Food Log shortcuts", 6)
        if len(log.shortcuts) < 4:
            raise AppError("recipes shortcut not found on the Food Log")
        self._tap(log.shortcuts[3])
        logger = self._until(lambda s: (lg := S.logger(s)) and lg.selected_tab == "Library" and lg.search_field and lg, "library")
        if not logger.plate_empty:
            raise AppError(FOREIGN_DRAFT)
        return logger

    def read_recipes(self, known: Callable[[S.FoodRow], bool] | None = None) -> tuple[list[S.FoodRow], bool]:
        """The Library's recipes as listed, most recently modified first. Reads nothing but the list; changes nothing
        but its order, which is put back.

        With `known`, reading stops once a full screen of rows was all known already: a recipe created or edited
        since comes before them in that order. Returns the rows read and whether the list was read to its end.
        """
        self._open_library()
        snap = self.phone.hide_keyboard()
        control = S.library_sort(snap) or self._until(S.library_sort, "the library's sort control")
        if not snap.has("Recipes"):
            raise AppError("the Library is not listing recipes")
        was = control.label
        try:
            self._library_order("Modified")
            self.say("reading recipes")
            rows: list[S.FoodRow] = []
            for _ in range(200):
                panel = self._until(lambda s: (found := S.picker(s)) and found.rows and found, "recipes")
                fresh = [r for r in panel.rows if not any(_whole(r) == _whole(seen) for seen in rows)]  # read, not tapped: any row counts
                rows += fresh
                if known is not None and len(fresh) >= 3 and all(known(r) for r in fresh):
                    return rows, False
                before = panel.layout
                self.phone.drag(360, panel.bottom - 20, panel.top + 60)
                if self._until(S.picker, "recipes").layout == before:
                    return rows, True
            raise AppError("the recipe list did not end")
        finally:
            try:
                self._library_order(was)
            except PhoneError as error:
                self.say(f"the Library's order could not be put back to {was}: {error}")
            self.abort()

    def _library_order(self, order: str) -> None:
        """Set the order the Library lists recipes in. Its control scrolls away with the list: the Library is
        opened again (at its top) when it is not on screen."""
        if S.library_sort(self.phone.settled()) is None:
            self.abort()
            self._open_library()
            self.phone.hide_keyboard()
        control = self._until(S.library_sort, "the library's sort control")
        if control.label == order:
            return
        self._tap(control)
        self._tap(self._until(lambda s: S.library_sort_choice(s, order), "sort choices"))
        self._until(lambda s: (c := S.library_sort(s)) and c.label == order and not s.has("Scrim"), f"recipes ordered by {order}")

    def create_recipe(
        self, name: str, ingredients: list[Item], servings: float = 1, total_weight: float | None = None, rehearse: bool = False
    ) -> dict:
        """Create a recipe. Returns what MacroFactor shows for it, and for each ingredient as it was added.

        `rehearse`: everything is done up to the summary, whose figures are read, and the wizard is left without creating.
        """
        if not ingredients:
            raise AppError("a recipe needs at least one ingredient")
        typed = ascii_fold(name).strip()
        logger = self._open_library()
        if logger.plus is None:
            raise AppError("create button not found in the library")
        self._tap(logger.plus)
        self._tap(
            self._until(
                lambda s: s.has("Create Recipe") and s.has("Build from scratch") and S.button(s, "Next"),
                "recipe wizard",
            )
        )
        form = self._until(lambda s: S.recipe_form(s), "recipe form")
        self.say(f"creating recipe {typed!r}")
        try:
            self._tap(form.name)
            self.phone.fill(typed)
            self._until(lambda s: (f := S.recipe_form(s)) and norm(f.name.text) == norm(typed), "recipe name", 6, settle=False)
            self.phone.hide_keyboard()
            for field_name, value in (("servings", servings), ("weight", total_weight)):
                form = self._until(lambda s: S.recipe_form(s), "recipe form")
                node = getattr(form, field_name)
                wanted = fmt_quantity(value) if value is not None else ""
                if value is None or node.text == wanted:
                    continue
                self._tap(node)
                self.phone.key("KEYCODE_MOVE_END", *([67] * (len(node.text) + 1)))
                self.phone.type(wanted)
                self._until(
                    lambda s, field_name=field_name, wanted=wanted: (f := S.recipe_form(s)) and getattr(f, field_name).text == wanted,
                    f"recipe {field_name}",
                    6,
                    settle=False,
                )
                self.phone.hide_keyboard()
            form = self._until(lambda s: (f := S.recipe_form(s)) and f.add_ingredient and f, "recipe form")
            self._tap(form.add_ingredient)
            added: list[dict] = []
            for item in ingredients:
                row, detail = self._choose(item, S.picker)
                self.say(f"ingredient {detail.quantity} {detail.unit} {detail.title} ({detail.kcal:g} kcal)")
                added.append(
                    {
                        "name": row.name,
                        "from": "library" if item.library else row.section.removeprefix("From ").lower() or "history",
                        "quantity": float(detail.quantity),
                        "unit": detail.unit,
                        "grams": detail.weight and round(detail.weight, 2),
                        "grams_within": 0.0 if norm(detail.unit) == "g" or not detail.grams else 0.05 * float(detail.quantity),
                        **{m: getattr(detail, m) for m in ("kcal", "protein", "fat", "carbs")},
                    }
                )
                add = detail.keys.get("Add")
                if add is None:
                    raise AppError("Add button not found")
                self._tap(add)
                self._until(lambda s: (p := S.picker(s)) and p.detail is None, "ingredient added")
            self._list_head(S.picker)
            snap = self.phone.hide_keyboard()
            panel = S.picker(snap)
            if panel is None or panel.confirm is None or panel.confirm.label != "Done":
                raise AppError("Done button not found after adding ingredients")
            self._tap(panel.confirm, snap)
            form = self._until(lambda s: (f := S.recipe_form(s)) and f.next and f.ingredients and f, "ingredients listed")
            check_recipe(added, form.ingredients_weight)
            self._tap(form.next)
            create = self._until(lambda s: s.has("Create & Add") and S.button(s, "Create"), "recipe summary")
            summary = self.phone.snapshot().first(lambda n: n.clickable and norm(n.lines[0]) == norm(typed))
            total = S.food_row(summary) if summary is not None else None
            check_recipe(added, form.ingredients_weight, servings, total)
            for ingredient in added:
                del ingredient["grams_within"]
            shown = {"name": typed, **{m: getattr(total, m) for m in ("kcal", "protein", "fat", "carbs")}, "serving": total.serving}
            if rehearse:
                self._leave_wizard()
                self.home()
                return {**shown, "ingredients": added, "ingredients_weight": form.ingredients_weight}
            self._commit(create)
            logger = self._until(
                lambda s: (lg := S.logger(s)) and lg.selected_tab == "Library" and lg.rows and lg,
                "library after creating",
                15,
            )
        except Exception:
            self._leave_wizard()
            raise
        row = next((r for r in logger.rows if norm(r.name) == norm(typed)), None)
        if row is None:
            raise AppError(f"recipe {typed!r} not found in the library after creating it")
        self.home()
        return {
            "name": row.name,
            **{m: getattr(row, m) for m in ("kcal", "protein", "fat", "carbs")},
            "serving": row.serving,
            "ingredients": added,
            "ingredients_weight": form.ingredients_weight,
        }

    def delete_recipe(self, name: str) -> None:
        """Delete a recipe by exact name. MacroFactor cannot undo this."""
        logger = self._open_library()
        self._tap(logger.search_field)
        self.phone.fill(search_query(name))
        logger = self._until(lambda s: (lg := S.logger(s)) and lg.search_field and lg.search_field.text and lg, "filtered recipes", 8)
        row = next((r for r in logger.rows if norm(r.name) == norm(name)), None)
        if row is None:
            self._abandon_plate()
            raise AppError(f"no recipe named {name!r}; closest: {', '.join(r.name for r in logger.rows[:5]) or 'nothing'}")
        self.phone.hide_keyboard()
        logger = self._until(lambda s: (lg := S.logger(s)) and lg.rows and lg, "filtered recipes")
        named = [r for r in logger.rows if norm(r.name) == norm(name)]
        if len(named) != 1:  # a deletion cannot be undone: never one of several
            self._abandon_plate()
            raise SeveralFoods(named) if named else AppError(f"no recipe named {name!r}")
        row = named[0]
        self._tap(row.node)
        detail = self._until(
            lambda s: (lg := S.logger(s)) and lg.detail and norm(lg.detail.title) == norm(name) and lg.detail,
            "recipe details",
        )
        delete = detail.actions.get("Delete")
        if delete is None:
            self._abandon_plate()
            raise AppError(f"{name} is not a recipe that can be deleted here")
        self.say(f"deleting recipe {row.name}")
        self._tap(delete)
        confirm = self._until(
            lambda s: s.first(lambda n: n.label.startswith(f"Are you sure you want to delete {row.name}?")) and S.button(s, "Delete"),
            "delete confirmation",
        )
        self._commit(confirm)
        self._until(
            lambda s: (lg := S.logger(s)) and lg.detail is None and not any(norm(r.name) == norm(name) for r in lg.rows),
            "recipe gone",
            12,
        )
        self._abandon_plate()

    def _leave_wizard(self) -> None:
        """Back out of Create Recipe without saving."""
        with contextlib.suppress(PhoneError):
            for _ in range(10):
                snap = self.phone.settled()
                if S.food_log(snap) is not None:
                    return
                leave = S.button(snap, "Discard") or S.button(snap, "Exit") or S.button(snap, "Leave")
                if leave is not None:
                    self._tap(leave)
                elif S.logger(snap) is not None and snap.keyboard_top is None:
                    self._abandon_plate()
                    return
                else:
                    self.phone.back()

    # ------------------------------------------------------------ export

    def export_food_log(self, destination: Path, period: str = "All Time") -> Path:
        """Granular Export > Food Log as CSV, pulled into `destination`."""
        self._open_export()
        self._until(lambda s: S.button(s, "Granular Export\nTab 2 of 2"), "Data Export")
        tab = self._button("Granular Export\nTab 2 of 2")
        if not tab.selected:
            self._tap(tab)
        export = None
        for _ in range(14):
            snap = self.phone.settled()
            export = S.button(snap, "Export Food Log")
            if export is not None and export.y2 < 1220:
                break
            self.phone.swipe(360, 1050, 360, 450, 300)
        else:
            raise AppError("Export Food Log not found")
        snap = self.phone.settled()
        card = snap.first(lambda n: n.label.startswith("Food Log\n"))
        chooser = next((c for c in (card.children if card else []) if c.clickable and c.label != "Export Food Log"), None)
        if chooser is None:
            raise AppError("Food Log period chooser not found")
        if chooser.label != period:
            self._tap(chooser)
            self._tap(self._until(lambda s: s.has("Scrim") and S.button(s, period), f"period {period}"))
            self._until(lambda s: not s.has("Scrim") and S.button(s, period), "period applied")
        return self._save_export(self._button("Export Food Log"), destination, ".csv")

    def export_library(self, destination: Path) -> Path:
        """Granular Export of every Metrics, Library and Other category as one workbook."""
        self._open_export()
        tab = self._button("Granular Export\nTab 2 of 2")
        if not tab.selected:
            self._tap(tab)
        for _ in range(6):
            self.phone.swipe(360, 500, 360, 1150, 150)
        seen: dict[str, bool] = {}
        for _ in range(16):
            snap = self.phone.settled()
            select = next((n for n in snap.labelled("Select All", clickable=True) if 300 < n.y1 < 1200), None)
            if select is not None:
                self._tap(select)
                continue
            for box in snap.all(lambda n: n.cls == "CheckBox" and 300 < n.center[1] < 1200):
                if not box.checked:
                    self._tap(box)
                    time.sleep(0.3)
                    break
                seen[box.label] = True
            else:
                if snap.first(lambda n: n.label == "Logs"):
                    break
                self.phone.swipe(360, 1000, 360, 560, 500)
        if len(seen) < 20:
            raise AppError(f"only {len(seen)} export categories could be selected")
        return self._save_export(self._button("Export"), destination, ".xlsx")

    def _open_export(self) -> None:
        self.home()
        self._tap(self._button("More\nTab 5 of 5"))
        for _ in range(10):
            snap = self.phone.settled()
            if S.button(snap, "Granular Export\nTab 2 of 2"):
                return
            entry = S.button(snap, "Data Export")
            if entry is not None and entry.center[1] < 1380:
                self._tap(entry)
                self._until(lambda s: S.button(s, "Granular Export\nTab 2 of 2"), "Data Export")
                return
            self.phone.swipe(360, 1200, 360, 500, 300)
        raise AppError("Data Export not found in More")

    def _save_export(self, trigger: Node, destination: Path, suffix: str) -> Path:
        pattern = f"{DOWNLOADS}/MacroFactor-*{suffix}"
        existing = set(self.phone.shell(f"ls {pattern} 2>/dev/null").split())
        self.say("exporting")
        self._tap(trigger)

        def picker(snap: Snapshot):
            if snap.foreground != "com.google.android.documentsui":
                return None
            save = snap.first(lambda n: n.label == "SAVE" and n.clickable, pkg="com.google.android.documentsui")
            name = snap.first(
                lambda n: n.cls == "EditText" and n.text.startswith("MacroFactor-"),
                pkg="com.google.android.documentsui",
            )
            place = snap.first(lambda n: n.text == "Downloads" or n.label == "Downloads", pkg="com.google.android.documentsui")
            return (save, name.text) if save and name and place else None

        answer = self._until(lambda s: S.export_refused(s) or picker(s), "Android save dialog", 180)
        if isinstance(answer, Node):  # the OK of its refusal
            self._tap(answer)
            raise ExportRefused("MacroFactor refuses to export for now: it says to try again in 24 hours")
        save, name = answer
        if not re.fullmatch(rf"MacroFactor-\d+{re.escape(suffix)}", name):
            raise AppError(f"unexpected export file name {name!r}")
        self.phone.tap(save)
        remote = f"{DOWNLOADS}/{name}"
        deadline = time.monotonic() + 120
        size = -1
        while time.monotonic() < deadline:
            listing = self.phone.shell(f"stat -c %s {remote} 2>/dev/null").strip()
            if listing.isdigit() and int(listing) > 0 and int(listing) == size:
                break
            size = int(listing) if listing.isdigit() else -1
            time.sleep(0.7)
        else:
            raise AppError("export file did not appear in Downloads")
        if remote in existing:
            raise AppError("export would overwrite an existing file")
        destination.mkdir(parents=True, exist_ok=True)
        local = destination / name
        self.phone.pull(remote, local)
        if local.stat().st_size != size:
            raise AppError("export was truncated while copying")
        self._until(lambda s: s.foreground == PACKAGE, "MacroFactor after export", 20)
        return local


MOVE_REACH = (-29, 7)  # days from the entry's own day that the date wheel of MacroFactor's Move goes to (seen from two days)


NOT_SHOWN = object()


def is_plate_of(rows: list[S.FoodRow], staged: list[dict]) -> bool:
    """Is a plate found holding `rows` the one a write was filling when it wrote down `staged`? Each food is written
    down before it is added: the plate holds them all, or all but the last."""
    foods = [S.Detail(**food, expanded=False, field_node=None) for food in staged]
    return bool(rows) and any(match_foods(rows, part, staged=True) is not None for part in (foods, foods[:-1]))


def rows_meant(rows: list[S.FoodRow], item: Item, sections: tuple[str, ...]) -> list[S.FoodRow]:
    """Which of the rows on screen are the food `item` names: none (not in view), or the rows of that one food, the
    likeliest to offer the unit asked for first. This is the whole of how a row is chosen:

    - by name: a recipe in the Library by its exact name and, among recipes sharing it, by the one serving listed
      (`item.shown`); a food named exactly, in the first section that lists it (the user's own come first); any other
      in the history (`sections`), by `same_food`;
    - rows of that name in that section that differ in what a gram or a serving is are different foods, and are
      never picked among (`SeveralFoods`); alike, they are one food to mf, though the phone may hold two with other
      units (seen: a galette made anew to have a "portion"): the row last used in the unit asked for comes first.
    """

    def meant(row: S.FoodRow) -> bool:
        return item.shown is None or S.one_serving(row) == item.shown

    def in_unit(row: S.FoodRow) -> bool:
        amount = S.split_serving(row.serving)
        return bool(amount and item.unit and norm(amount[1]) == norm(item.unit))

    if item.library:
        first = next((r for r in rows if norm(r.name) == norm(item.name) and meant(r)), None)
    elif item.exact:
        first = min((r for r in rows if norm(r.name) == norm(item.name)), key=lambda r: r.node.y1, default=None)
    else:
        first = next((r for r in rows if r.section in sections and same_food(r.name, item.name)), None)
    if first is None:
        return []
    same = [r for r in rows if norm(r.name) == norm(first.name) and r.section == first.section and meant(r)]
    if not same_figures(same):
        raise SeveralFoods(same)
    return sorted(same, key=lambda r: not in_unit(r))


# A food copied by hand to give it a unit is entered from its label, the database's from someone else's reading of it
# (seen: 378 and 385 kcal for 100 g of one galette). Recipes of one name differ by far more (seen: 266 and 967).
COPY = 0.05


def same_figures(rows: list[S.FoodRow]) -> bool:
    """Do these rows of one name show one food? History can list a food twice, at two amounts (seen: a galette at
    "7.67 g" and at "3 portion (23.01 g)"), or hold a copy of it (`COPY`); two recipes of a name differ in what a gram
    or a serving of them is, or in one having a weight and the other none. Energy is shown in whole kcal."""

    def measure(row: S.FoodRow) -> tuple[str, float] | None:
        amount, weight = S.split_serving(row.serving), S.SERVING_GRAMS.search(row.serving)
        if amount is None:
            return None
        if weight or norm(amount[1]) == "g":
            return "g", float(weight[1]) if weight else amount[0]
        return norm(amount[1]), amount[0]

    first, size = rows[0], measure(rows[0])
    for row in rows[1:]:
        other = measure(row)
        if size is None or other is None or other[0] != size[0]:
            return False
        ours, theirs = first.kcal * other[1], row.kcal * size[1]  # each one's energy for the other's amount
        if abs(ours - theirs) > (size[1] + other[1]) / 2 + COPY * max(ours, theirs):
            return False
    return True


def continue_list(shown: list[S.FoodRow], frame: list[S.FoodRow]) -> tuple[list[S.FoodRow], list[S.FoodRow]]:
    """A search list read frame after frame as it scrolls: `frame`'s rows with their sections, and those of them new.

    The frames overlap: the end of the one before (`shown`) is the start of this one, and those rows are not new,
    however alike two results may be elsewhere in the list. A row under a header that has scrolled away is still
    in that header's section: the one the row before it was in.
    """

    def same_rows(n: int) -> bool:
        return all(replace(new, section=old.section) == old for old, new in zip(shown[len(shown) - n :], frame, strict=False))

    same = next(n for n in range(min(len(shown), len(frame)), -1, -1) if same_rows(n))
    section = shown[-1].section if shown else ""
    labelled = shown[len(shown) - same :]
    for row in frame[same:]:
        section = row.section or section
        labelled.append(replace(row, section=section))
    return labelled, labelled[same:]


def check_recipe(added: list[dict], weight: float | None, servings: float = 1, total: S.FoodRow | None = NOT_SHOWN) -> None:
    """Refuse a recipe whose totals on the phone are not those of its ingredients as each showed when it was added.

    A long list is not on one screen, so the ingredients are not counted there. What tells that every one is in,
    once, at its amount: the weight MacroFactor adds up for them (when each has one), and, once the summary is
    shown (`total`), the energy of a serving.
    """
    weights = [i["grams"] for i in added]
    if weight is not None and all(w is not None for w in weights):
        slack = 0.15 + sum(i["grams_within"] for i in added)  # the details tell what one unit weighs to a tenth of a gram
        if abs(weight - sum(weights)) > slack:
            raise AppError(f"the recipe's ingredients weigh {weight:g} g on the phone, {sum(weights):g} g were added")
    if total is NOT_SHOWN:
        return
    if total is None:
        raise AppError("the recipe's summary does not show its figures")
    energy = sum(i["kcal"] for i in added) / servings
    if abs(total.kcal - energy) > 1 + len(added) / 2 / servings:  # each ingredient showed a whole number
        raise AppError(f"the recipe's summary shows {total.kcal} kcal a serving, its ingredients add up to {energy:.0f}")


def move_steps(day: date, to_day: date) -> list[date]:
    """The days an entry of `day` is moved to, one after the other, to reach `to_day` within the wheel's reach."""
    back, ahead = MOVE_REACH
    steps: list[date] = []
    while day != to_day:
        day += timedelta(days=max(back, min(ahead, (to_day - day).days)))
        steps.append(day)
    return steps


def _whole(entry: Entry) -> tuple:
    """Everything a Food Log row says about an entry besides where it is."""
    return (entry.name, entry.serving, entry.kcal, entry.protein, entry.fat, entry.carbs)


def _new_entries(before: list[Entry], after: list[Entry]) -> list[Entry]:
    """Entries of `after` not accounted for by `before` (multiset difference, keeping order)."""
    pool = [e.key for e in before]
    new: list[Entry] = []
    for entry in after:
        if entry.key in pool:
            pool.remove(entry.key)
        else:
            new.append(entry)
    if pool:
        raise AppError(f"previously saved entries disappeared while adding food: {pool}")
    return new


def match_foods(actual: list[Entry] | list[S.FoodRow], expected: list[S.Detail], *, staged: bool = False) -> list[S.Detail] | None:
    """Match the complete multiset, returning details in actual row order. Counts alone never establish a match.

    Each side is compared in the terms both screens show. Every row, on the plate or in the Food Log, names its food
    and gives its energy and macros: those must agree with the details the food was staged from. A saved row also
    shows the serving in the unit that was selected. A plate row shows a weight in grams, and only for foods
    MacroFactor has a weight for (not a drink counted in fl oz, not a recipe without a total weight): it is compared
    when grams are what was entered. Converting a serving through the grams per unit of the details is not a
    comparison: both figures are rounded, differently (417.9 g a serving beside a plate row of 417.86 g).
    """
    if len(actual) != len(expected):
        return None
    remaining = list(expected)
    matched = []

    def agrees(row: Entry | S.FoodRow, detail: S.Detail) -> bool:
        if not same_food(row.name, detail.title):
            return False
        if any(abs(getattr(row, key) - getattr(detail, key)) > 0.51 for key in ("kcal", "protein", "fat", "carbs")):
            return False
        if staged and norm(detail.unit) != "g":
            return True
        return S.same_serving(row.serving, float(detail.quantity), detail.unit)

    remaining.sort(key=lambda detail: norm(detail.unit) != "g")  # a row showing grams goes to the food entered in grams first
    for row in actual:
        index = next((i for i, detail in enumerate(remaining) if agrees(row, detail)), None)
        if index is None:
            return None
        matched.append(remaining.pop(index))
    return matched
