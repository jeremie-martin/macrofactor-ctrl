"""Pure readers for MacroFactor's screens: Snapshot in, plain data out. No phone access.

Everything observed here was seen on the app version and screen size in `VERIFIED` (a realme Note 70T, Android 16). When the app
changes, this is the file to update; `mf ui` shows what the app currently exposes.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .text import norm
from .ui import Node, Snapshot

VERIFIED = {"app": "5.8.11", "screen": "720x1600"}  # as `device.phone_environment` tells them
HOUR_CHIP = re.compile(r"^(\d\d):00$")
CLOCK = re.compile(r"^\d{1,2}:\d\d$")
# "  0P  0F  21C  •  150 g"  /  "8P  1F  31C  •  1 serving (325 g)"  /  "0P  0F  21C" (plate rows)
MACROS = re.compile(r"^\s*(\d+)P\s+(\d+)F\s+(\d+)C(?:\s+•\s+(.*))?$")
TOTALS = re.compile(r"(\d+) ?/ ?(\d+)\nP\n(\d+) ?/ ?(\d+)\nF\n(\d+) ?/ ?(\d+)\nC\n(\d+) ?/ ?(\d+)")  # five digits drop the spaces
SAME_QUANTITY = 0.006  # the app displays a quantity rounded to two decimals: closer than this, it is the same serving
NEVER_TAP = {"Clear Day"}  # destroys a whole day of data; no operation here ever needs it
RELATIVE_DAYS = {"Today": 0, "Yesterday": -1, "Tomorrow": 1}  # how the app names the days around today

# ----------------------------------------------------------------- food rows


@dataclass
class FoodRow:
    """A food as MacroFactor draws it in the log, search results, plate and library."""

    name: str  # as displayed, e.g. "Skyr Nature 0% By Yoplait"
    kcal: int
    protein: int
    fat: int
    carbs: int
    serving: str  # "150 g", "1 serving (325 g)"; empty for plate rows
    node: Node = field(repr=False, compare=False)
    section: str = ""  # search lists only: "From History", "Common", "Branded", "Latest"...

    @property
    def button(self) -> Node | None:
        """The round button at the right of the row (pencil in the log, + in search)."""
        return next((c for c in self.node.children if c.cls == "Button"), None)


def food_row(node: Node) -> FoodRow | None:
    lines = node.lines
    if lines[-1:] == ["Edit Icon"]:  # a new recipe's summary: the same row, with a pencil
        lines = lines[:-1]
    if len(lines) != 3 or not lines[1].isdigit():
        return None
    match = MACROS.match(lines[2])
    if not match:
        return None
    return FoodRow(
        name=lines[0],
        kcal=int(lines[1]),
        protein=int(match[1]),
        fat=int(match[2]),
        carbs=int(match[3]),
        serving=(match[4] or "").strip(),
        node=node,
    )


def split_serving(serving: str) -> tuple[float, str] | None:
    """ "150 g" -> (150.0, "g"); "1 serving (325 g)" -> (1.0, "serving"); "1 large - 3 1/4" diameter (223 g)"."""
    match = re.match(r"^([\d.]+)\s+(.*?)(?:\s+\([\d.]+ g\))?$", serving)
    if not match:
        return None
    return float(match[1]), match[2]


def same_serving(shown: str, quantity: float, unit: str) -> bool:
    """Does a row's serving text ("7.67 g", "1 serving (325 g)") show this quantity and unit?"""
    parsed = split_serving(shown)
    return parsed is not None and norm(parsed[1]) == norm(unit) and abs(parsed[0] - quantity) < SAME_QUANTITY


# ----------------------------------------------------------------- food log (a day)


UNKNOWN_HOUR = -1  # rows at the top of the list whose header is scrolled off and whose group shows no time


@dataclass
class HourGroup:
    """One hour of the day. Its header (`chip`) scrolls off before its rows do: then only the rows remain."""

    hour: int
    chip: Node | None = field(repr=False, default=None)
    plus: Node | None = field(repr=False, default=None)
    rows: list[FoodRow] = field(default_factory=list)
    times: list[str] = field(default_factory=list)  # distinct "H:MM" stamps shown in this hour

    @property
    def y1(self) -> int:
        """Top of the header on screen; -1 when it is scrolled off above."""
        return self.chip.y1 if self.chip else -1

    @property
    def y2(self) -> int:
        return self.chip.y2 if self.chip else -1


@dataclass
class FoodLog:
    title: str  # "Today", "Yesterday", "Tomorrow" or "Wed, Oct 21"
    totals: dict[str, tuple[int, int]]  # kcal/protein/fat/carbs -> (eaten, target)
    hours: list[HourGroup]
    top: int  # list area: rows are only safely tappable between top and bottom
    bottom: int
    selecting: Node | None  # the "Select / 1 Food" control; scrolls out of view with the action bar
    calendar_open: bool = False
    shortcuts: list[Node] = field(default_factory=list, repr=False)  # floating search, barcode, AI, recipes
    actions: dict[str, Node] = field(default_factory=dict, repr=False)  # selection bar: Edit, Copy, Move, Remove...
    header_buttons: list[Node] = field(default_factory=list, repr=False)
    at_end: bool = False  # Notes is below the last hour; a stationary viewport alone is not proof of the end

    @property
    def calendar_button(self) -> Node | None:
        # Today shows [calendar, settings, more]; other days show [calendar, more].
        return self.header_buttons[0] if self.header_buttons else None

    @property
    def in_selection(self) -> bool:
        """Rows are being selected (or a move/copy target is being chosen): taps do not open foods."""
        return bool(self.actions)

    def hour(self, hour: int) -> HourGroup | None:
        return next((g for g in self.hours if g.hour == hour), None)

    @property
    def layout(self) -> list[tuple]:
        """Where everything in the list sits: equal before and after a scroll means the list did not move."""
        return [(g.hour, g.y1, [(r.name, r.serving, r.node.y1) for r in g.rows]) for g in self.hours]


def _landmarks(log: FoodLog | Plate) -> list[tuple[tuple, Node]]:
    """Fully visible row/header bounds; clipped bounds cannot measure movement."""
    nodes = []
    if isinstance(log, Plate):
        nodes = [((row.name, row.serving, row.kcal), row.node) for row in log.rows]
    else:
        for group in log.hours:
            if group.chip:
                nodes.append(((group.hour,), group.chip))
            nodes.extend(((group.hour, row.name, row.serving, row.kcal), row.node) for row in group.rows)
    return [(key, node) for key, node in nodes if log.top < node.y1 and node.y2 <= log.bottom]


def scan_step(log: FoodLog | Plate) -> int:
    """Retain overlap, and move less than half a repeat's spacing so identical rows cannot trade identities."""
    nodes = _landmarks(log)
    gaps = [b.y1 - a.y1 for i, (key, a) in enumerate(nodes) for other, b in nodes[i + 1 :] if key == other and b.y1 > a.y1]
    return min([(log.bottom - log.top) // 2, *(gap // 2 for gap in gaps)])


def scroll_offset(before: FoodLog | Plate, after: FoodLog | Plate, maximum: int, tolerance: int = 4) -> int | None:
    """The unique measured displacement consistent with EVERY landmark in the overlapping area.

    A drag released at rest cannot move farther than the finger. Its distance bounds the candidates;
    it is never substituted for a measurement. Repeated rows are matched by geometry as well as content.
    """
    old, new = _landmarks(before), _landmarks(after)
    candidates = {a.y1 - b.y1 for key, a in old for other, b in new if key == other and 0 <= a.y1 - b.y1 <= maximum}

    def agrees(source, target, top, bottom, shift):
        return all(
            any(key == other and abs(a.y1 - shift - b.y1) <= tolerance for other, b in target)
            for key, a in source
            if top + tolerance < a.y1 - shift and a.y2 - shift <= bottom - tolerance
        )

    valid = [s for s in candidates if agrees(old, new, after.top, after.bottom, s) and agrees(new, old, before.top, before.bottom, -s)]
    return round(sum(valid) / len(valid)) if valid and max(valid) - min(valid) <= tolerance else None


def _nav(snap: Snapshot) -> Node | None:
    return snap.first(lambda n: n.label.startswith("Food Log\nTab "))


def stamp_beside(picture, row: Node) -> bool:
    """Is a time drawn in the margin beside the top of this Food Log row? MacroFactor writes each of an hour's times
    there, beside the first row logged at it, and tells no reader which row that is (seen 2026-11-25: "21:26" beside
    the second of four rows, in a group that only lists 21:00, 21:26, 21:28).

    `picture`: the screen in greys. The margin is as wide as the row is far from the edge, and the time sits within
    that much of the row's top. Ink is what differs from the margin's own colour, in a light or a dark theme; the
    day's vertical line crosses a few columns, a time some forty.
    """
    width = row.x1
    margin = picture.crop((0, row.y1, width, row.y1 + width))
    shades = margin.histogram()
    ground = shades.index(max(shades))
    pixels = margin.tobytes()
    inked = sum(any(abs(shade - ground) > 60 for shade in pixels[x::width]) for x in range(width))
    return inked >= width // 8


def food_log(snap: Snapshot) -> FoodLog | None:
    """The Food Log tab with no sheet over it, else None."""
    nav = _nav(snap)
    if nav is None or not nav.selected or snap.has("Scrim") or snap.foreground != nav.pkg:
        return None
    title = snap.first(lambda n: n.y1 == 0 and n.x1 == 0 and n.label and "\n" not in n.label and n.cls == "View")
    if title is None or _logger_header(snap) is not None:
        return None
    totals: dict[str, tuple[int, int]] = {}
    for node in snap.app():
        match = TOTALS.fullmatch(node.label)
        if match:
            v = [int(x) for x in match.groups()]
            totals = {"kcal": (v[0], v[1]), "protein": (v[2], v[3]), "fat": (v[4], v[5]), "carbs": (v[6], v[7])}
            break
    selecting = snap.first(lambda n: re.fullmatch(r"(Select|Move|Copy)\n\d+ Foods?", n.label) is not None)
    actions = snap.first(lambda n: n.cls == "HorizontalScrollView" and n.y1 > 1200)
    floating = [n for n in snap.app() if n.clickable and not n.label and n.y1 >= 1270 and n.y2 <= 1380 and n.width > 100]
    bottom = min([n.y1 for n in floating] + ([actions.y1] if actions else []) + [nav.y1]) - 12
    groups: list[HourGroup] = []
    for node in snap.app():
        chip = HOUR_CHIP.match(node.label)
        if chip and node.clickable and node.x1 == 0:
            group = HourGroup(hour=int(chip[1]), chip=node)
            siblings = node.parent.children if node.parent else []
            index = siblings.index(node) if node in siblings else -1
            if 0 <= index < len(siblings) - 1 and siblings[index + 1].clickable and not siblings[index + 1].label:
                group.plus = siblings[index + 1]
            if node.parent is not None:
                group.times = [line for line in node.parent.lines if CLOCK.match(line)]
            groups.append(group)
        elif node.clickable and node.x1 > 100 and (row := food_row(node)):
            if node.y1 <= title.y2 + 1 and (row.button is None or abs(row.button.center[1] - node.center[1]) > 4):
                continue  # cut by the top edge of the list: where it starts cannot be told, and it was whole one screen earlier
            if not groups:  # the header of this hour is scrolled off; its group still lists the times of its rows
                stamps = next((found for a in node.ancestors() if (found := [line for line in a.lines if CLOCK.match(line)])), None)
                # While rows are being selected the group lists no times: the frame alone does not say which hour this is.
                groups.append(HourGroup(hour=int(stamps[0].split(":")[0]) if stamps else UNKNOWN_HOUR, times=stamps or []))
            groups[-1].rows.append(row)
    buttons = sorted(snap.all(lambda n: n.cls == "Button" and not n.label and n.y1 < 100 and n.y2 < 200), key=lambda n: n.x1)
    return FoodLog(
        title=title.label,
        totals=totals,
        hours=groups,
        top=title.y2,
        bottom=bottom,
        selecting=selecting,
        calendar_open=len({n.y1 for n in _calendar_nodes(snap)}) >= 2,
        shortcuts=sorted(floating, key=lambda n: n.x1),
        actions={n.lines[0]: n for n in actions.walk() if n.clickable and n.label} if actions else {},
        header_buttons=buttons,
        at_end=any(n.label == "Notes" and title.y2 < n.y1 < bottom for n in snap.app()),
    )


def title_for(day: date, today: date | None = None) -> str:
    """The Food Log title MacroFactor shows for a date."""
    today = today or date.today()
    return _relative_name(day, today) or f"{day:%a}, {day:%b} {day.day}"  # no year, also for a day of another year


def title_date(title: str, today: date) -> date | None:
    """The date a Food Log title stands for; None when it cannot be read.

    A day of another year was seen without its year ("Sat, Jan 2" for 2027, read in November 2026): the year is
    then the one in which that date falls on that weekday, the closest to today.
    """
    if title in RELATIVE_DAYS:
        return today + timedelta(days=RELATIVE_DAYS[title])
    with contextlib.suppress(ValueError):
        return datetime.strptime(title, "%a, %b %d, %Y").date()
    options = []
    for year in sorted(range(today.year - 3, today.year + 4), key=lambda y: abs(y - today.year)):
        with contextlib.suppress(ValueError):
            day = datetime.strptime(f"{title}, {year}", "%a, %b %d, %Y").date()
            if title.startswith(f"{day:%a}"):
                options.append(day)
    return min(options, key=lambda d: abs((d - today).days), default=None)


def wheel_date_label(day: date, today: date | None = None) -> str:
    """The label of a date in MacroFactor's date wheels ("Today", "Sat Nov 14")."""
    today = today or date.today()
    return _relative_name(day, today) or f"{day:%a} {day:%b} {day.day}"


def wheel_date(label: str, today: date) -> date:
    """The date a wheel label stands for. Wheels show no year: the one that puts the date closest to today."""
    day = _day_of(label, today, "%a %b %d %Y")
    if day is None:
        raise ValueError(f"unreadable wheel date {label!r}")
    return day


def _day_of(label: str, today: date, *formats: str) -> date | None:
    """The date a label without its year stands for ("Today", "Sat Nov 14", "Jan 2" for a day of next year): in the
    year that puts it closest to today. None: not a date in any of `formats` (each ending in " %Y")."""
    if label in RELATIVE_DAYS:
        return today + timedelta(days=RELATIVE_DAYS[label])
    options = []
    for year in (today.year - 1, today.year, today.year + 1):
        for fmt in formats:
            with contextlib.suppress(ValueError):  # Feb 29 exists in one of them at most
                options.append(datetime.strptime(f"{label} {year}", fmt).date())
    return min(options, key=lambda d: abs((d - today).days), default=None)


def _relative_name(day: date, today: date) -> str | None:
    return next((name for name, offset in RELATIVE_DAYS.items() if (day - today).days == offset), None)


# ----------------------------------------------------------------- calendar


@dataclass
class CalendarCell:
    day: date
    node: Node = field(repr=False)
    covered: bool = False


def _day_nodes(snap: Snapshot) -> list[Node]:
    return snap.all(lambda n: n.clickable and re.fullmatch(r"\d{1,2}\n[A-Z][a-z]", n.label) is not None)


def _dated(nodes: list[Node], near: date) -> list[CalendarCell]:
    """Date a run of consecutive day cells ("14 / Sa", ...) by the closest start date that fits them all."""
    if not nodes:
        return []
    for distance in range(0, 900):
        for offset in {distance, -distance}:
            start = near + timedelta(days=offset)
            if all(_cell_matches(node, start + timedelta(days=i)) for i, node in enumerate(nodes)):
                return [CalendarCell(start + timedelta(days=i), node) for i, node in enumerate(nodes)]
    return []


def _calendar_nodes(snap: Snapshot) -> list[Node]:
    """Only the vertical calendar grid; the horizontal strip can remain underneath it in the tree."""
    return [
        n
        for n in _day_nodes(snap)
        if 40 < n.width < 90
        and next((a.cls for a in n.ancestors() if a.cls in ("ScrollView", "HorizontalScrollView")), None) == "ScrollView"
    ]


def calendar_cells(snap: Snapshot, near: date) -> list[CalendarCell]:
    """Dated cells of the open calendar grid.

    The grid lists consecutive days, seven per row; cells only say "14 / Sa". Its first row
    ends on the selected date when it opens, and scrolling keeps the sequence. `near` is a date the grid is
    known to be close to (the selected day when it opens, then what it last showed): the same days of the
    week recur months apart (August 2025 and May 2026 read alike), so the closest fit to a far date is wrong.
    """
    nodes = _calendar_nodes(snap)
    if len({n.y1 for n in nodes}) < 2:
        return []
    cells = _dated(sorted(nodes, key=lambda n: (n.y1, n.x1)), near)
    # The floating return-to-today arrow replaces a cell visually, but leaves that date in the tree.
    covers = [n for n in snap.app() if n.clickable and not n.label and 40 < n.width < 90]
    for cell in cells:
        x, y = cell.node.center
        cell.covered = any(n.x1 <= x < n.x2 and n.y1 <= y < n.y2 for n in covers)
    return cells


def _cell_matches(node: Node, day: date) -> bool:
    number, weekday = node.lines
    return int(number) == day.day and weekday == f"{day:%a}"[:2]


# ----------------------------------------------------------------- logger (search / plate)


@dataclass
class Detail:
    """One food opened with the quantity keypad (in the logger or when editing a logged entry)."""

    title: str
    kcal: float
    protein: float
    fat: float
    carbs: float
    quantity: str
    unit: str
    expanded: bool  # keypad and unit chips visible; Back collapses it without leaving the food
    field_node: Node = field(repr=False)
    units: dict[str, Node] = field(repr=False, default_factory=dict)
    unit_strip: Node | None = field(repr=False, default=None)
    grams: float | None = None  # what one unit weighs, as shown beside it ("serving • 325 g"); None: no weight shown
    keys: dict[str, Node] = field(repr=False, default_factory=dict)  # "Add", "Log Foods", "Done", digits
    backspace: Node | None = field(repr=False, default=None)
    actions: dict[str, Node] = field(repr=False, default_factory=dict)  # Remove, Edit, Delete, Favorite...
    back: Node | None = field(repr=False, default=None)  # arrow (logger) or X (editing) in the title bar
    done: Node | None = field(repr=False, default=None)

    @property
    def weight(self) -> float | None:
        """What the amount weighs: itself when it is in grams, else that many times what one unit is shown to weigh."""
        amount = float(self.quantity)
        if norm(self.unit) == "g":
            return amount
        return None if self.grams is None else self.grams * amount


def _logger_header(snap: Snapshot) -> Node | None:
    """The kcal pill ("78 / 2636", plus "+1" when the plate overflows) that tops the logger. Five digits are drawn
    without the spaces ("34681/2666"), as when a unit is changed while the field still holds an amount in grams.

    Its children are the close button and the time chip.
    """
    return snap.first(lambda n: n.y1 == 0 and n.clickable and re.fullmatch(r"\d+ ?/ ?\d+", n.lines[0]) is not None and len(n.children) >= 2)


def _keypad(snap: Snapshot) -> Node | None:
    """Container of the quantity field: its parent also holds Add/Done and, when expanded, the digit keys."""
    fields = snap.all(lambda n: n.cls == "EditText")
    for field_node in sorted(fields, key=lambda n: not n.focused):
        parent = field_node.parent
        if parent and any(c.clickable and c.label in ("Add", "Done", "0") for c in parent.children):
            return parent
    return None


def detail(snap: Snapshot) -> Detail | None:
    pad = _keypad(snap)
    if pad is None:
        return None
    kids = pad.children
    field_node = next(c for c in kids if c.cls == "EditText")
    index = kids.index(field_node)
    unit_node = kids[index + 1] if index + 1 < len(kids) else None
    one = next((c for c in kids if c.label == "1" and c.clickable and c is not field_node), None)  # the key, not a field holding 1
    chips: list[Node] = []
    if one is not None:
        chips = [n for n in pad.walk() if n.clickable and n.label and n.cls == "View" and n.y1 >= field_node.y2 and n.y2 <= one.y1]
    keys = {c.label: c for c in kids if c.clickable and c.label and c.cls != "EditText"}
    unlabelled = [c for c in kids if c.clickable and not c.label and c.cls == "View" and c.x1 > 500]
    summary = snap.first(lambda n: re.fullmatch(r"[\d.]+\nCalories\n.*Protein\n.*Fat\n.*Carbs", n.label, re.S) is not None)
    if summary is None:
        return None
    numbers = [line for line in summary.lines if re.fullmatch(r"[\d.]+", line)]
    if len(numbers) < 4:
        return None
    # The title bar sits just above the summary: a clickable row whose label is the food name. Not a text field: the
    # recipe form stays in the tree behind an ingredient's details, and scrolls its own fields to that height.
    title = snap.first(
        lambda n: (
            n.clickable
            and n.cls != "EditText"
            and n.label
            and "\n" not in n.label
            and n.y2 <= summary.y1 + 4
            and n.y1 >= summary.y1 - 140
            and n.width > 600
        )
    )
    actions = {
        n.label: n
        for n in snap.all(lambda n: n.clickable and n.label in ("Remove", "Edit", "Delete", "Favorite", "To Custom", "Prepare", "Share"))
        if abs(n.y1 - summary.y2) < 40
    }
    scrim = snap.has("Scrim")
    done = snap.first(lambda n: n.clickable and n.label == "Done" and n.y2 < summary.y1) if scrim else None
    return Detail(
        title=title.label if title else "",
        kcal=float(numbers[0]),
        protein=float(numbers[1]),
        fat=float(numbers[2]),
        carbs=float(numbers[3]),
        quantity=field_node.text or field_node.label,
        unit=unit_node.label.split(" • ")[0] if unit_node is not None and not unit_node.clickable else "",  # "serving • 325 g"
        grams=_shown_weight(unit_node.label) if unit_node is not None and not unit_node.clickable else None,
        expanded=one is not None,
        field_node=field_node,
        units={n.label: n for n in chips},
        unit_strip=chips[0].parent if chips else None,
        keys=keys,
        backspace=unlabelled[-1] if unlabelled and one is not None else None,
        actions=actions,
        back=next((c for c in title.children if c.clickable), None) if title else None,
        done=done,
    )


HOUR_OR_CLOCK = re.compile(r"\d{1,2}:\d\d")


CLEARANCE = 24  # px: the floating search bar takes the taps that land this close above it


@dataclass
class Picker:
    """The food list with its search field: inside the logger, and when adding recipe ingredients.

    The search field floats over the bottom of the list (above the keyboard when that is up) and is gone while
    the list is scrolled down. Rows are only reachable between `top` and `bottom`.
    """

    tabs: dict[str, Node] = field(repr=False, default_factory=dict)
    selected_tab: str = ""
    search_field: Node | None = field(repr=False, default=None)
    confirm: Node | None = field(repr=False, default=None)  # "Log Foods" or "Done" beside the search field
    rows: list[FoodRow] = field(default_factory=list)
    sections: list[str] = field(default_factory=list)
    section_nodes: dict[str, Node] = field(repr=False, default_factory=dict)  # headers offering "See N More"
    plus: Node | None = field(repr=False, default=None)  # library tab: create a recipe or food
    detail: Detail | None = None
    top: int = 0
    bottom: int = 0
    keyboard: bool = False

    def reachable(self, row: FoodRow) -> bool:
        """Whole and uncovered: a row cut by the top edge starts at it, one under the search bar or the keyboard ends too low."""
        return self.top < row.node.y1 and row.node.y2 <= self.bottom

    @property
    def layout(self) -> list[tuple]:
        return [(row.name, row.node.y1) for row in self.rows]


@dataclass(kw_only=True)
class Logger(Picker):
    """The add-food sheet: the food list (or one food's details) under a header, over the plate."""

    close: Node = field(repr=False)
    time_chip: Node = field(repr=False)
    when: str  # "08:00" or "08:00\nYesterday"
    header: Node = field(repr=False)
    plate_empty: bool = False  # only an explicit empty plate is permission to start a draft


def _sheet(tabs: dict[str, Node]) -> Node | None:
    """The body of the search sheet: what starts where the strip of tabs ends. Found without the search field, which
    hides while the list is scrolled."""
    for strip in next(iter(tabs.values())).ancestors():
        for other in strip.parent.children if strip.parent else []:
            if other is not strip and (other.x1, other.y1, other.x2) == (strip.x1, strip.y2, strip.x2):
                return other
    return None


def picker(snap: Snapshot) -> Picker | None:
    result = Picker()
    for node in snap.all(lambda n: n.clickable and re.search(r"\nTab \d of 6$", n.label) is not None):
        name = node.lines[0]
        result.tabs[name] = node
        if node.selected:
            result.selected_tab = name
    if not result.tabs:
        return None
    result.detail = detail(snap)
    if result.detail is not None:
        return result  # the list underneath is stale while a food is open
    field_node = snap.first(lambda n: n.cls == "EditText" and (n.hint.startswith("Search") or n.hint.startswith("Filter")))
    if field_node is None:
        # The hint disappears once text is typed: the search field is the EditText beside "Log Foods"/"Done".
        field_node = snap.first(
            lambda n: (
                n.cls == "EditText"
                and n.parent is not None
                and any(c.label in ("Log Foods", "Done") and c is not n for c in n.parent.children)
                and not any(c.label == "0" for c in n.parent.children)
            )
        )
    area = field_node.parent if field_node is not None else _sheet(result.tabs)
    if area is None:
        return result
    # The field sits just above the keyboard, or above Android's navigation bar when there is none. After the
    # keyboard has gone (closing the time picker does that) the tree can keep the field at its old place, over
    # rows that a tap would open: it is not the search field until it is where the screen can have it.
    floor = snap.keyboard_top or min((n.y1 for n in snap.roots if n.pkg == "com.android.systemui" and n.y1 > area.y1), default=area.y2)
    if field_node is not None and 0 <= floor - field_node.y2 <= 80:
        result.search_field = field_node
    result.confirm = next((c for c in area.children if c.label in ("Log Foods", "Done") and c.clickable), None)
    for node in area.walk():
        row = food_row(node) if node.clickable else None
        if row and row.button is not None:
            result.rows.append(row)
    headers = sorted(  # a row cut by the top edge is as low as a header, but still reads as a food
        (n for n in area.walk() if n.label and n.x1 == 0 and n.height <= 70 and n.cls == "View" and food_row(n) is None),
        key=lambda n: n.y1,
    )
    result.sections = [n.lines[0] for n in headers]
    result.section_nodes = {n.lines[0]: n for n in headers if n.clickable}
    for row in result.rows:
        above = [n for n in headers if n.y1 <= row.node.y1]
        row.section = above[-1].lines[0] if above else ""  # none above: the list is scrolled into the section it is cut in
    if result.selected_tab == "Library":
        result.plus = next((n for n in area.walk() if n.cls == "Button" and not n.label and n.x1 > 560 and n.y2 < 470), None)
    # What can cover the end of the list: the floating search bar, the keyboard, Android's navigation bar.
    covers = [n.y1 for n in snap.roots if n.pkg == "com.android.systemui" and n.y1 > area.y1]
    covers += [result.search_field.y1 - CLEARANCE] if result.search_field is not None else []
    covers += [snap.keyboard_top] if snap.keyboard_top is not None else []
    result.top, result.bottom, result.keyboard = area.y1, min([area.y2, *covers]), snap.keyboard_top is not None
    return result


def logger(snap: Snapshot) -> Logger | None:
    header = _logger_header(snap)
    if header is None or snap.has("Scrim"):
        return None
    close = next((c for c in header.children if c.cls == "Button"), None)
    chip = next((c for c in header.children if c.clickable and HOUR_OR_CLOCK.match(c.label)), None)
    found = picker(snap)
    if close is None or chip is None or found is None:
        return None
    plate = snap.first(lambda n: n.label.startswith("Your Plate"))
    icons = [n for n in header.children if n.cls == "ImageView"]
    empty = bool(
        plate is not None
        and "Your plate is empty" in plate.label
        and not icons
        and not any(re.fullmatch(r"\+\d+", line) for line in header.lines)
        and not any(food_row(n) for n in plate.walk())
    )
    return Logger(**vars(found), close=close, time_chip=chip, when=chip.label, header=header, plate_empty=empty)


@dataclass
class Plate:
    """The exposed plate, with only fully visible food summaries. Quantity boxes can be stale."""

    rows: list[FoodRow]
    top: int
    bottom: int
    at_start: bool
    at_end: bool
    log_button: Node = field(repr=False)

    @property
    def layout(self) -> list[tuple]:
        return [(r.name, r.serving, r.node.y1, r.node.y2) for r in self.rows]


def plate(snap: Snapshot) -> Plate | None:
    """Read only an exposed plate: search, details and the keyboard must all be out of the way."""
    logger_view = logger(snap)
    if logger_view is None or logger_view.detail is not None or snap.keyboard_top is not None:
        return None
    body = snap.first(lambda n: n.label == "Your Plate")
    if body is None or body.parent is None:
        return None
    # The search sheet is docked at the bottom. In search mode it starts immediately below the header.
    tab_top = min(n.y1 for n in logger_view.tabs.values())
    save = snap.first(lambda n: n.label == "Log Foods" and n.clickable and logger_view.header.y2 < n.y1 < tab_top)
    if save is None:
        return None
    top, bottom = logger_view.header.y2, save.y1
    rows = []

    def read(node: Node) -> bool:
        row = food_row(node) if node.clickable else None
        if row is not None:
            rows.append(row)
            return True  # its descendants are decoration and the passive quantity controls
        if node.label:
            return node.label == "Expand Ingredients"  # a control, not another food
        return all(read(child) for child in node.children)  # transparent layout containers have no semantic role

    if not all(read(child) for child in body.children):
        return None  # an unrecognized food is not permission to omit it
    # The heading and its padding precede the first row by 130 px; between rows there is only a 16 px gap.
    at_start = bool(rows and rows[0].node.y1 - top >= 100)
    end = next((n for n in body.parent.children if n.label.startswith("Nutrition\n")), None)
    return Plate(
        [r for r in rows if top < r.node.y1 and r.node.y2 <= bottom],
        top,
        bottom,
        at_start,
        end is not None and top < end.y1 < bottom,
        save,
    )


@dataclass
class RecipeForm:
    """Step two of Create Recipe: name, servings, total weight and ingredients.

    With the keyboard up only the text fields are visible; the buttons are None until it is hidden. A long list of
    ingredients scrolls the three fields away (they are then None); the "Ingredients" header stays, with the weight
    MacroFactor adds up for them.
    """

    name: Node | None = field(repr=False, default=None)
    servings: Node | None = field(repr=False, default=None)
    weight: Node | None = field(repr=False, default=None)
    add_ingredient: Node | None = field(repr=False, default=None)
    next: Node | None = field(repr=False, default=None)
    ingredients: list[FoodRow] = field(default_factory=list)  # those on screen
    ingredients_weight: float | None = None  # "Weight of ingredients is 3674.1 g"; None: not shown


INGREDIENTS_WEIGHT = re.compile(r"Weight of ingredients is ([\d.]+) g")


def recipe_form(snap: Snapshot) -> RecipeForm | None:
    label = snap.first(lambda n: n.label == "Ingredients")
    if not snap.has("Create Recipe") or (label is None and not snap.first(lambda n: n.label.startswith("Build your recipe"))):
        return None
    if picker(snap) is not None:
        return None  # the ingredient search is open on top of the form
    fields = sorted(snap.all(lambda n: n.cls == "EditText" and n.x1 < 100 and n.width > 500), key=lambda n: n.y1)
    form = RecipeForm(*fields[:3]) if len(fields) >= 3 else RecipeForm()
    if label is not None and snap.keyboard_top is None:
        form.add_ingredient = snap.first(
            lambda n: n.cls == "Button" and not n.label and n.x1 > 560 and abs(n.center[1] - label.center[1]) < 60
        )
        form.next = button(snap, "Next")
        form.ingredients = [row for n in snap.app() if n.clickable and n.y1 > label.y2 and (row := food_row(n))]
        weight = snap.first(lambda n: INGREDIENTS_WEIGHT.fullmatch(n.label) is not None)
        form.ingredients_weight = float(INGREDIENTS_WEIGHT.fullmatch(weight.label)[1]) if weight else None
    return form


# ----------------------------------------------------------------- pickers and dialogs


@dataclass
class Wheel:
    value: str
    node: Node = field(repr=False)

    @property
    def row_height(self) -> float:
        return self.node.height / 5  # five rows are visible, the selected one in the middle


def wheels(snap: Snapshot) -> list[Wheel]:
    return [Wheel(n.label, n) for n in snap.all(lambda n: n.cls == "SeekBar")]


def one_serving(row: FoodRow) -> dict | None:
    """A recipe as its row in the Library lists it, "1 serving (380 g)": kcal, protein, fat, carbs and `grams` (None
    when the recipe has no weight). None for a row that shows another amount."""
    if not row.serving.startswith("1 serving"):
        return None
    weight = SERVING_GRAMS.search(row.serving)
    return {m: float(getattr(row, m)) for m in ("kcal", "protein", "fat", "carbs")} | {"grams": float(weight[1]) if weight else None}


def weighs_as(recipe: dict, servings: float, weight: float | None) -> bool:
    """Is `weight` what the screen shows `servings` of the recipe to weigh (None: it shows none)? To a tenth of a gram."""
    if weight is None or recipe["grams"] is None:
        return weight is None and recipe["grams"] is None
    return abs(weight - recipe["grams"] * servings) <= 0.1 + weight / 200


def is_amount_of(recipe: dict, quantity: float, unit: str, kcal: float, weight: float | None | type(...) = ...) -> bool:
    """Is this what MacroFactor shows for `quantity` `unit` of the recipe listed as `recipe` (`one_serving`)?

    Several recipes can share a name; what one serving is tells them apart. `weight` is what the amount weighs where
    the screen says (None: it shows none); left out where it does not say. Energy is shown in whole kcal, for the
    amount and for the serving: one kcal either way, and half of one for every serving in the amount.
    """
    if norm(unit) == "serving":
        servings = quantity
        if weight is not ... and not weighs_as(recipe, quantity, weight):
            return False
    elif norm(unit) == "g" and recipe["grams"]:
        servings = quantity / recipe["grams"]
    else:
        return False
    return abs(kcal - recipe["kcal"] * servings) <= 1 + servings / 2


LIBRARY_SORTS = ("A-Z", "Z-A", "Created", "Modified")  # the orders MacroFactor's Library lists recipes in
SERVING_GRAMS = re.compile(r"\((\d+(?:\.\d+)?) g\)$")  # "1 serving (380 g)"; a recipe without a weight shows "1 serving"


def library_sort(snap: Snapshot) -> Node | None:
    """The control at the right of the Library's list header; its label is the order in use."""
    return snap.first(lambda n: n.label in LIBRARY_SORTS and n.clickable and n.x1 > 360)


def library_sort_choice(snap: Snapshot, order: str) -> Node | None:
    """`order` in the Sort sheet ("Modified / Starts with most recently modified."), when that sheet is open."""
    if not snap.has("Scrim") or not snap.has("Sort"):
        return None
    return snap.first(lambda n: n.clickable and n.lines and n.lines[0] == order and len(n.lines) > 1)


def _shown_weight(label: str) -> float | None:
    """The weight in a detail's unit label: "serving • 417.9 g" -> 417.9; "serving" or "fl oz" -> None."""
    found = re.search(r" • ([\d.]+) g$", label)
    return float(found[1]) if found else None


def export_refused(snap: Snapshot) -> Node | None:
    """The OK of the dialog with which MacroFactor refuses an export ("Unusual Activity … try again in 24 hours"):
    it limits how many it gives in a day. None when that dialog is not showing."""
    return button(snap, "OK") if snap.has("Unusual Activity") else None


def button(snap: Snapshot, label: str) -> Node | None:
    """A clickable control with this exact label; the lowest one when a title repeats the text."""
    found = sorted(snap.labelled(label, clickable=True), key=lambda n: n.y1)
    return found[-1] if found else None


def parse_when(label: str, today: date | None = None) -> tuple[date | None, int]:
    """Logger time chip -> (date or None when it cannot be read, hour). "08:00" means today."""
    today = today or date.today()
    lines = label.split("\n")
    hour = int(lines[0].split(":")[0])
    if len(lines) == 1:
        return today, hour
    return _day_of(lines[1], today, "%a %b %d %Y", "%a, %b %d %Y", "%b %d %Y"), hour
