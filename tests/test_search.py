"""Picking a food from MacroFactor's search results, on a pretend sheet that behaves as the real one was seen to:
five history rows then "See N More", a search bar that floats over the end of the list and takes the taps that land
on or just above it, a keyboard that covers more, sections whose headers scroll away with their rows."""

from types import SimpleNamespace

import pytest

from mf import screens as S
from mf.app import FoodNotFound, Item, MacroFactor
from mf.device import SLOP, Phone
from mf.ui import Snapshot

APP, KEYBOARD, SYSTEM = "com.sbs.diet", "com.google.android.inputmethod.latin", "com.android.systemui"
TOP, ROW, HEADER = 332, 122, 56


def xml(label: str, box: tuple[int, int, int, int], cls: str = "View", pkg: str = APP, inside: str = "", **flags: bool) -> str:
    attrs = "".join(f' {key}="true"' for key, on in flags.items() if on)
    bounds = f"[{box[0]},{box[1]}][{box[2]},{box[3]}]"
    label = label.replace("\n", "&#10;")
    return f'<node class="android.view.{cls}" package="{pkg}" content-desc="{label}" text="" bounds="{bounds}"{attrs}>{inside}</node>'


class Sheet(Phone):
    """The search sheet after a query was typed, keyboard still up."""

    def __init__(self, history: list[str], common: list[str] = ()):
        self.history, self.common = list(history), list(common)
        self.keyboard, self.expanded, self.offset = True, False, 0
        self.stale = False  # the keyboard has gone and the tree still has the search bar above where it was
        self.opened: str | None = None
        self.actions, self.checkpoint = 0, None

    def _list(self) -> list[tuple[str, int, int]]:
        """(label, top, bottom) of headers and rows, in list coordinates."""
        shown = self.history if self.expanded else self.history[:5]
        hidden = len(self.history) - 5
        link = "\nSee Less" if self.expanded else f"\nSee {hidden} More" if hidden > 0 else ""
        parts = [(f"From History{link}", HEADER)] + [(f"{name}\n50\n  1P  1F  1C  •  100 g", ROW) for name in shown]
        if self.common:
            parts += [("Common\nSee 15 More", HEADER)] + [(f"{name}\n60\n  1P  1F  1C  •  100 g", ROW) for name in self.common]
        out, y = [], TOP + 14
        for label, height in parts:
            out.append((label, y, y + height))
            y += height
        return out

    @property
    def bar(self) -> int | None:
        """Top of the floating search bar; gone while the list is scrolled."""
        return None if self.offset else 877 if self.keyboard or self.stale else 1367

    def snapshot(self) -> Snapshot:
        rows = ""
        for label, top, bottom in self._list():
            top, bottom = max(TOP, top - self.offset), min(1600, bottom - self.offset)
            if bottom <= top:
                continue
            plus = xml("", (620, top, 716, bottom), cls="Button", clickable=True) if "\n50\n" in label or "\n60\n" in label else ""
            rows += xml(label, (0, top, 720, bottom), inside=plus, clickable=True)
        bar = ""
        if self.bar is not None:
            bar = xml("", (32, self.bar, 453, self.bar + 96), cls="EditText", clickable=True, focused=True)
            bar += xml("Log Foods", (469, self.bar, 688, self.bar + 96), clickable=True)
        tabs = xml("", (0, 192, 720, 332), inside=xml("Search\nTab 2 of 6", (166, 220, 350, 332), clickable=True, selected=True))
        body = xml("", (0, TOP, 720, 1600), inside=xml("", (0, TOP, 720, 1600), cls="ScrollView", inside=rows) + bar)
        app = xml("", (0, 0, 720, 1600), cls="FrameLayout", inside=tabs + body)
        keys = xml(
            "", (0, 994, 720, 1600), cls="FrameLayout", pkg=KEYBOARD, inside=xml("q", (0, 994, 72, 1094), pkg=KEYBOARD, clickable=True)
        )
        nav = xml("", (0, 1504, 720, 1600), cls="FrameLayout", pkg=SYSTEM)
        return Snapshot.from_xml(f"<hierarchy>{keys if self.keyboard else ''}{nav}{app}</hierarchy>")

    def tap(self, target) -> None:
        x, y = target.center if hasattr(target, "center") else target
        self.actions += 1
        assert y < 1504, "a tap on Android's navigation bar leaves the app"
        assert not (self.keyboard and y >= 994), "a tap on the keyboard types"
        if self.bar is not None and y >= self.bar - 16:
            self.keyboard = False  # the bar takes the tap: nothing opens
            return
        for label, top, bottom in self._list():
            if top - self.offset <= y < bottom - self.offset and y >= TOP:
                if label.startswith("From History\nSee") and x > 500:
                    self.expanded = not self.expanded
                elif "\n" in label and not label.startswith(("From History", "Common")):
                    self.opened = label.split("\n")[0]

    def back(self) -> None:
        self.actions += 1
        self.keyboard = False

    def drag(self, x: int, y1: int, y2: int) -> None:
        self.actions += 1
        end = self._list()[-1][2]
        self.offset = max(0, min(self.offset + (y1 - y2) - SLOP, max(0, end - 1504)))


def choose(sheet: Sheet, name: str, **item) -> str | None:
    app = MacroFactor(sheet)
    row = app._reach(Item(name, **item), S.picker)
    panel = S.picker(sheet.snapshot())
    assert panel.reachable(next(r for r in panel.rows if r.node.y1 == row.node.y1))
    sheet.tap(row.node)
    return sheet.opened


FOODS = [f"Food {i}" for i in range(1, 31)]


@pytest.mark.parametrize("position", [1, 4, 5, 6, 12, 13, 29, 30])
def test_a_history_result_is_opened_wherever_it_is_listed(position):
    """First row, under the search bar with the keyboard up, behind See More, and far down the expanded list."""
    sheet = Sheet(FOODS, common=["Food 3", "Food 99"])
    assert choose(sheet, f"Food {position}") == f"Food {position}"


def test_a_food_only_listed_outside_history_is_not_taken_for_the_one_in_history():
    sheet = Sheet(FOODS[:7], common=["Food 99", "Food 100"])
    with pytest.raises(FoodNotFound):
        choose(sheet, "Food 99")
    assert sheet.opened is None and sheet.expanded  # looked through all of history first, and opened nothing
    with pytest.raises(FoodNotFound):
        choose(Sheet(FOODS[:3], common=["Food 99"]), "Food 99")


def test_a_brand_shown_after_the_name_is_the_same_food_and_a_longer_name_is_not():
    sheet = Sheet(["Skyr Nature 0% By Yoplait", "Skyr By Arla", "Skyr Nature"])
    assert choose(sheet, "Skyr") == "Skyr By Arla"


def test_the_list_reader_knows_what_covers_the_list_and_which_section_a_cut_row_is_in():
    sheet = Sheet(FOODS, common=["Other"])
    panel = S.picker(sheet.snapshot())
    assert panel.keyboard and panel.bottom == 877 - S.CLEARANCE  # the bar floats above the keyboard
    assert [panel.reachable(r) for r in panel.rows[:5]] == [True, True, True, False, False]
    sheet.keyboard = False
    assert S.picker(sheet.snapshot()).bottom == 1367 - S.CLEARANCE
    sheet.expanded, sheet.offset = True, 300
    panel = S.picker(sheet.snapshot())
    assert panel.search_field is None and panel.bottom == 1504  # the bar is gone; Android's navigation bar is not
    assert panel.sections == [] and {r.section for r in panel.rows} == {""}  # the cut first row is not a header
    assert not panel.reachable(panel.rows[0]) and panel.reachable(panel.rows[1])


def test_a_list_found_already_expanded_is_still_scanned():
    sheet = Sheet(FOODS)
    sheet.expanded = True
    assert choose(sheet, "Food 20") == "Food 20"


def test_a_search_field_the_tree_left_where_the_keyboard_was_is_not_one_to_tap():
    """Seen after the time picker: no keyboard, the field still drawn over the fifth row. A tap there opened that food."""
    sheet = Sheet(FOODS)
    sheet.keyboard, sheet.stale = False, True
    assert S.picker(sheet.snapshot()).search_field is None
    sheet.stale = False
    assert S.picker(sheet.snapshot()).search_field is not None


def test_a_library_search_is_shown_once_every_row_listed_holds_the_words_typed():
    """The Library filters a moment after the text shows. The recipe meant may be far down the filtered list: it is
    not what is waited for (seen: five seconds lost on a "Soup" five screens down)."""

    def panel(text, *names):
        rows = [SimpleNamespace(name=name) for name in names]
        return SimpleNamespace(search_field=SimpleNamespace(text=text, focused=True), rows=rows, sections=[])

    app = MacroFactor(SimpleNamespace(fill=lambda text: None))
    shown = app._type_query(panel(""), "Cr me Soup", lambda snap: snap, filters=True)
    assert not shown(panel("Cr me Soup", "Lentil Stew", "Crème De Soup"))  # not filtered yet
    assert not shown(panel("Cr me", "Crème De Soup"))  # the text is still being typed
    assert not shown(panel("Cr me Soup"))  # nothing listed: not an answer yet
    assert shown(panel("Cr me Soup", "Crème De Soup", "Soup Of Crème Fraîche"))
