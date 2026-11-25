"""Screen readers, on a hand-written hierarchy shaped like MacroFactor's accessibility tree."""

from dataclasses import replace
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from mf import screens as S
from mf.app import (
    AppError,
    Entry,
    FoodNotFound,
    Item,
    MacroFactor,
    NoSuchUnit,
    SeveralFoods,
    _new_entries,
    _Seen,
    check_recipe,
    continue_list,
    fmt_quantity,
    is_plate_of,
    match_foods,
    same_food,
    search_query,
)
from mf.device import SLOP, Cancelled, UiTimeout
from mf.ui import Node, Snapshot

PKG = "com.sbs.diet"


def node(label: str, bounds: str, cls: str = "View", children: str = "", **flags: bool) -> str:
    attrs = "".join(f' {key}="true"' for key, on in flags.items() if on)
    label = label.replace("\n", "&#10;")
    return f'<node class="android.view.{cls}" package="{PKG}" content-desc="{label}" text="" bounds="{bounds}"{attrs}>{children}</node>'


def food_log_xml() -> str:
    eight = node(
        "78\n0\nP\n0\nF\n21\nC\n08:30",
        "[0,300][720,560]",
        children=node("08:00", "[0,320][120,404]", clickable=True)
        + node("", "[124,320][208,404]", clickable=True)
        + node("Apple, Fresh\n78\n  0P  0F  21C  •  150 g", "[130,404][690,544]", clickable=True),
    )
    nine = node(
        "",
        "[0,560][720,700]",
        children=node("09:00", "[0,580][120,664]", clickable=True) + node("", "[124,580][208,664]", clickable=True),
    )
    body = (
        node("Today", "[0,0][720,280]", clickable=True)
        + node("78 / 2600\nP\n0 / 120\nF\n0 / 80\nC\n21 / 330", "[0,176][720,250]")
        + eight
        + nine
        + "".join(node("", f"[{x},1280][{x + 152},1376]", clickable=True) for x in (32, 200, 368, 536))
        + node("Food Log\nTab 2 of 5", "[144,1395][288,1501]", cls="Button", clickable=True, selected=True)
    )
    return f'<hierarchy rotation="0">{node("", "[0,0][720,1600]", children=body)}</hierarchy>'


def test_food_log_reader():
    log = S.food_log(Snapshot.from_xml(food_log_xml()))
    assert log is not None and log.title == "Today"
    assert log.totals == {"kcal": (78, 2600), "protein": (0, 120), "fat": (0, 80), "carbs": (21, 330)}
    assert [(g.hour, g.times, [r.name for r in g.rows]) for g in log.hours] == [
        (8, ["08:30"], ["Apple, Fresh"]),
        (9, [], []),
    ]
    row = log.hours[0].rows[0]
    assert (row.kcal, row.carbs, row.serving) == (78, 21, "150 g")
    assert log.hours[0].plus is not None and len(log.shortcuts) == 4
    assert log.bottom < 1280


def test_rows_of_an_hour_whose_header_scrolled_off_still_belong_to_it():
    """A long hour: the list is scrolled so that 10:00's header is gone, one row is cut by the top edge, two are whole."""
    pencil = lambda y: node("", f"[590,{y}][686,{y + 96}]", cls="Button", clickable=True)  # noqa: E731
    ten = node(
        "201\n3\nP\n2\nF\n43\nC\n10:14\n10:35",
        "[0,280][720,896]",
        children=node("Galette\n29\n  1P  0F  6C  •  7.67 g", "[130,280][690,412]", clickable=True, children=pencil(278))
        + node("Galette\n29\n  1P  0F  6C  •  7.67 g", "[130,412][690,584]", clickable=True, children=pencil(450))
        + node("Apple, Fresh\n89\n  0P  0F  24C  •  172 g", "[130,584][690,724]", clickable=True, children=pencil(606)),
    )
    eleven = node("", "[0,896][720,1392]", children=node("11:00", "[0,916][120,1000]", clickable=True))
    body = (
        node("Yesterday", "[0,0][720,280]", clickable=True)
        + ten
        + eleven
        + node("Food Log\nTab 2 of 5", "[144,1395][288,1501]", cls="Button", clickable=True, selected=True)
    )
    log = S.food_log(Snapshot.from_xml(f'<hierarchy rotation="0">{node("", "[0,0][720,1600]", children=body)}</hierarchy>'))
    assert [(g.hour, g.chip is not None, g.times, [(r.name, r.node.y1) for r in g.rows]) for g in log.hours] == [
        (10, False, ["10:14", "10:35"], [("Galette", 412), ("Apple, Fresh", 584)]),
        (11, True, [], []),
    ]
    assert log.hours[0].y1 < log.top  # treated as above the screen
    assert log.layout != S.food_log(Snapshot.from_xml(food_log_xml())).layout


def test_another_screen_is_not_a_food_log():
    xml = food_log_xml().replace(' selected="true"', "")
    assert S.food_log(Snapshot.from_xml(xml)) is None


def test_diagnostic_dump_includes_unnamed_recipe_images():
    image = node("", "[10,20][30,40]", cls="ImageView")
    assert "ImageView '' [10,20 30,40]" in Snapshot.from_xml(f"<hierarchy>{image}</hierarchy>").dump()


def test_servings_titles_and_time_chips():
    assert S.split_serving("150 g") == (150.0, "g")
    assert S.split_serving("0.5 serving (162.5 g)") == (0.5, "serving")
    assert S.split_serving('1 large - 3 1/4" diameter (223 g)') == (1.0, 'large - 3 1/4" diameter')
    today = date(2026, 11, 14)
    assert S.title_for(today, today) == "Today"
    assert S.title_for(date(2026, 11, 13), today) == "Yesterday"
    assert S.title_for(date(2026, 10, 21), today) == "Wed, Oct 21"
    assert S.title_for(date(2025, 12, 31), today) == "Wed, Dec 31"  # as seen on the phone: no year
    for day in (today, date(2026, 11, 13), date(2026, 10, 21), date(2025, 12, 31)):
        assert S.title_date(S.title_for(day, today), today) == day  # what is navigated to can be read back
    assert S.title_date("Settings", today) is None
    assert S.title_date("Sat, Jan 2", today) == date(2027, 1, 2)  # as the phone titled a day of the next year
    assert S.title_date("Wed, Dec 31", today) == date(2025, 12, 31) and S.title_date("Sat, Nov 21", today) == date(2026, 11, 21)
    assert S.parse_when("08:00", today) == (today, 8)
    assert S.parse_when("21:00\nYesterday", today) == (date(2026, 11, 13), 21)


def test_date_wheels_show_no_year():
    today = date(2026, 1, 2)
    assert S.wheel_date_label(date(2026, 1, 3), today) == "Tomorrow"
    assert S.wheel_date_label(date(2025, 12, 30), today) == "Tue Dec 30"
    assert S.wheel_date("Yesterday", today) == date(2026, 1, 1)
    assert S.wheel_date("Tue Dec 30", today) == date(2025, 12, 30)  # last year's, not eleven months ahead
    assert S.wheel_date("Sat Jan 10", today) == date(2026, 1, 10)
    assert S.wheel_date("Thu Feb 29", date(2025, 1, 10)) == date(2024, 2, 29)  # only one of the nearby years has it


def test_calendar_dates_hidden_by_the_return_to_today_arrow_are_not_tappable():
    from datetime import timedelta

    start = date(2026, 1, 1)
    cells = "".join(
        node(
            f"{day.day}\n{day:%a}"[: len(str(day.day)) + 3],
            f"[{32 + i % 7 * 96},{1080 + i // 7 * 180}][{112 + i % 7 * 96},{1200 + i // 7 * 180}]",
            clickable=True,
        )
        for i in range(14)
        for day in [start + timedelta(days=i)]
    )
    overlay = node("", "[608,1260][688,1380]", clickable=True)
    strip = node("", "[0,180][720,300]", cls="HorizontalScrollView", children=node("30\nFr", "[0,180][80,300]", clickable=True))
    snap = Snapshot.from_xml(f"<hierarchy>{strip}{node('', '[0,176][720,1392]', cls='ScrollView', children=cells)}{overlay}</hierarchy>")
    dated = S.calendar_cells(snap, start)
    assert [c.day for c in dated] == [start + timedelta(days=i) for i in range(14)]
    assert [c.day for c in dated if c.covered] == [date(2026, 1, 14)]


def test_driver_helpers():
    # adb types ASCII only and the app's search matches fragments of words: each word gives its longest plain run
    assert search_query("Galettes 4 Céréales, Bio By Carrefour") == "Galettes 4 ales Bio"
    assert search_query("Brassé Nature") == "Brass Nature" and search_query("P’tit à La Crème") == "tit La Cr"
    assert search_query("Apple, Fresh, With Skin") == "Apple Fresh With Skin"  # a comma would hide the history results
    assert search_query("Pâté") == "P" and search_query("Thé") == "Th"  # never nothing
    assert same_food("Skyr Nature 0% By Yoplait", "Skyr Nature 0%")
    assert not same_food("Banana", "Apple, Fresh")
    assert fmt_quantity(150) == "150" and fmt_quantity(0.5) == "0.5"


def test_clear_day_is_never_tapped():
    class Phone:
        taps = 0

        def tap(self, node):
            self.taps += 1

    phone = Phone()
    app = MacroFactor(phone)
    with pytest.raises(AppError, match="refusing"):
        app._tap(Node("View", "Clear Day", "", PKG, 0, 0, 100, 100, clickable=True))
    app._tap(Node("View", "Done", "", PKG, 0, 0, 100, 100, clickable=True))
    assert phone.taps == 1


def test_new_entries_are_found_even_among_identical_rows():
    from mf.app import Entry

    def entry(position: int) -> Entry:
        return Entry(date(2026, 11, 14), 8, "Apple, Fresh", "150 g", 78, 0, 0, 21, position=position)

    assert len(_new_entries([entry(0)], [entry(0), entry(1)])) == 1
    assert _new_entries([entry(0)], [entry(0)]) == []


def test_hour_scan_and_entry_selection_start_before_the_first_row_even_when_header_is_offscreen():
    """Three identical rows; the initial viewport only shows the last one and the NEXT hour's chip."""

    def chip(y):
        return Node("View", "", "", PKG, 0, y, 120, y + 84)

    def row(y):
        return S.FoodRow("Test cracker", 29, 1, 0, 6, "7 g", Node("View", "", "", PKG, 130, y, 690, y + 172))

    def screen(groups):
        return S.FoodLog("Today", {}, groups, 280, 1268, None)

    tail = screen([S.HourGroup(19, rows=[row(280)]), S.HourGroup(20, chip=chip(472))])
    full = screen([S.HourGroup(19, chip=chip(300), rows=[row(384), row(556), row(728)]), S.HourGroup(20, chip=chip(920))])

    class Phone:
        pass

    app = MacroFactor(Phone())
    current, scrolls = [tail], []
    app._log = lambda: current[0]

    def scroll(log, pixels):
        scrolls.append(pixels)
        current[0] = full if pixels < 0 else screen([S.HourGroup(20, chip=chip(300))])

    app._scroll = scroll
    seen, _ = app._scan(range(19, 20))
    assert len(seen) == 3 and scrolls[0] < 0
    current[0] = tail
    target = app._entries(date.today(), seen, {})[2]
    found = app._find(target)
    assert found.node.y1 == 728  # ordinal counted from the start, not from the viewport
    target.serving = "14 g"
    with pytest.raises(AppError, match="changed before selection"):
        app._find(target)  # same name and ordinal, different serving


def test_a_scan_never_guesses_scroll_distance_or_returns_a_partial_read():
    class Phone:
        pass

    def frame(hour):
        chip = Node("View", "", "", PKG, 0, 300, 120, 384)
        return S.FoodLog("Today", {}, [S.HourGroup(hour, chip=chip)], 280, 1268, None)

    app = MacroFactor(Phone())
    current = [frame(0)]
    app._log = lambda: current[0]
    app._scroll = lambda log, pixels: current.__setitem__(0, frame(12))
    with pytest.raises(AppError, match="overlap"):
        app._scan()  # no common anchors; the old code trusted its requested drag distance
    current[0] = frame(0)
    app._scroll = lambda log, pixels: None
    with pytest.raises(AppError, match="stopped scrolling"):
        app._scan()  # stationary is not proof that the rest of the day is empty
    current[0].at_end = True
    assert app._scan()[0] == []
    current[0] = frame(19)
    current[0].hours[0].chip = None
    with pytest.raises(AppError, match="could not reach"):
        app._scan(range(19, 20))  # repeated rows can alias after a scroll: a missing header is never a proven start
    current[0] = S.FoodLog("Today", {}, [], 280, 1268, None, at_end=True)
    with pytest.raises(AppError, match="hours are missing"):
        app._scan()  # Notes alone proves the bottom, not that earlier entries are absent


@pytest.mark.parametrize("change", ["midnight", "midnight with overlap", "title", "eaten totals"])
def test_whole_day_read_restarts_only_with_fresh_rows_and_times(change):
    day = date(2026, 11, 17)
    now = datetime(2026, 11, 17, 23, 59)
    requested = []

    def frame(title, hour, y, name, minute, end=False):
        chip = Node("View", "", "", PKG, 0, y, 120, y + 84)
        row = S.FoodRow(name, 10, 0, 0, 2, "10 g", Node("View", "", "", PKG, 130, y + 84, 690, y + 224))
        group = S.HourGroup(hour, chip=chip, rows=[row], times=[f"{hour}:{minute:02}"])
        return S.FoodLog(title, {"kcal": (10, 100)}, [group], 280, 1268, None, at_end=end)

    current = frame("Today", 0, 400, "First reading", 10)
    app = MacroFactor(SimpleNamespace(), now=lambda: now)

    def goto(target):
        nonlocal current
        requested.append(target)
        if len(requested) == 2:
            current = frame(S.title_for(day, now.date()), 0, 400, "Requested day", 20, end=True)
        return current

    def scroll(log, pixels):
        nonlocal current, now
        if change.startswith("midnight"):
            now = datetime(2026, 11, 18)
        current = frame("Yesterday" if change == "title" else "Today", 8 if change == "midnight" else 0, 310, "First reading", 30, end=True)
        if change == "eaten totals":
            current.totals["kcal"] = (11, 100)

    app.goto = goto
    app._scroll_top = lambda read: read()
    app._log = lambda: current
    app._scroll = scroll
    view = app.read_day(day)
    assert requested == [day, day]
    assert [(e.day, e.name, e.minute) for e in view.entries] == [(day, "Requested day", 20)]
    assert view.times == {0: [20]}


def test_second_day_change_fails_and_navigation_refusals_survive_midnight():
    day = date(2026, 11, 17)
    now = datetime(2026, 11, 17, 23, 59)
    messages = []
    app = MacroFactor(SimpleNamespace(), now=lambda: now, log=messages.append)
    app.goto = lambda target: S.FoodLog("Yesterday", {}, [], 280, 1268, None)
    with pytest.raises(AppError, match="expected Today, found Yesterday"):
        app.read_day(day)
    assert len(messages) == 1 and "discarding" in messages[0]

    def draft(target):
        nonlocal now
        now = datetime(2026, 11, 18)
        raise AppError("draft plate")

    app.goto = draft
    with pytest.raises(AppError, match="draft plate"):
        app.read_day(day)
    assert len(messages) == 1


def test_whole_day_read_preserves_an_overlap_failure_on_the_same_date():
    app = MacroFactor(SimpleNamespace(), now=lambda: datetime(2026, 11, 18))
    requested = []

    def fail(day):
        requested.append(day)
        raise AppError("cannot establish the overlap")

    app.goto = fail
    day = date(2026, 11, 17)
    with pytest.raises(AppError, match="cannot establish the overlap"):
        app.read_day(day)
    assert requested == [day]


def test_saving_intent_is_recorded_before_input_and_failure_to_record_prevents_the_tap():
    events = []

    class Phone:
        def tap(self, node):
            events.append("tap")

    node = Node("View", "Done", "", PKG, 0, 0, 100, 100)
    app = MacroFactor(Phone(), saving=lambda: events.append("durable intent"))
    app._commit(node)
    assert events == ["durable intent", "tap"]

    def fail():
        raise OSError("disk full")

    app = MacroFactor(Phone(), saving=fail)
    with pytest.raises(OSError):
        app._commit(node)
    assert events == ["durable intent", "tap"] and not app.committed


def test_picker_uses_controlled_movement_and_confirmation_requires_both_requested_values():
    class Phone:
        minute = 0
        drags = 0

        def snapshot(self):
            body = node("12", "[200,860][360,1260]", cls="SeekBar")
            body += node(f"{self.minute:02d}", "[360,860][520,1260]", cls="SeekBar")
            body += node("Done", "[0,1320][720,1400]", cls="Button", clickable=True)
            return Snapshot.from_xml(f"<hierarchy>{body}</hierarchy>")

        def until(self, check, *args):
            result = check(self.snapshot())
            if not result:
                raise AppError("selection has changed")
            return result

        def drag(self, x, start, end):
            self.drags += 1
            self.minute += round((start - end) / 80)

        def tap(self, position):
            self.minute += round((position[1] - 1060) / 80)

    phone = Phone()
    app = MacroFactor(phone)
    app._set_wheel(1, "45", int)
    assert phone.minute == 45 and phone.drags > 0  # no fling API is available
    assert app._picker_button("Done", ["12", "45"]).label == "Done"
    phone.minute = 46  # a previously selected value is not permission to save a different one
    with pytest.raises(AppError, match="selection has changed"):
        app._picker_button("Done", ["12", "45"])


@pytest.mark.parametrize(
    "icons,overflow,plate_text,empty",
    [
        (2, 5, "Your Plate", False),
        (3, 0, "Your Plate", False),
        (1, 0, "Your plate is empty", False),
        (0, 0, "Your Plate\nYour plate is empty", True),
        (0, 0, "", False),
        (0, 5, "Your Plate", False),
    ],
)
def test_only_explicit_empty_plate_allows_a_new_draft(icons, overflow, plate_text, empty):
    children = node("", "[16,80][112,176]", cls="Button", clickable=True)
    children += node("12:00", "[128,80][272,176]", clickable=True)
    children += "".join(node("Test food", f"[{522 + i * 58},104][{570 + i * 58},152]", cls="ImageView") for i in range(icons))
    header = node("100 / 2000" + (f"\n+{overflow}" if overflow else ""), "[0,0][720,192]", children=children, clickable=True)
    plate = node(plate_text, "[0,192][720,1000]")
    tabs = node("Search\nTab 2 of 6", "[166,220][350,332]", clickable=True, selected=True)
    logger = S.logger(Snapshot.from_xml(f"<hierarchy>{header}{plate}{tabs}</hierarchy>"))
    assert logger.plate_empty == empty


def plate_frame(count=1, offset=0, search=False, recipe=False, wrappers=1, scrollable=True):
    header = node(
        "100 / 2000",
        "[0,0][720,192]",
        clickable=True,
        children=node("", "[16,80][112,176]", cls="Button", clickable=True)
        + node("12:00", "[128,80][272,176]", clickable=True)
        + "".join(node("", f"[{520 + i * 12},104][{540 + i * 12},124]", cls="ImageView") for i in range(3 if recipe else 1)),
    )
    rows = ""
    for i in range(count):
        top, bottom = 322 + i * 144 - offset, 450 + i * 144 - offset
        if bottom <= 192 or top >= 1375:
            continue
        bounds = f"[32,{max(192, top)}][688,{min(1375, bottom)}]"
        # A stale passive quantity field is deliberately inconsistent with the row summary.
        food = node("Test food\n50\n0P 0F 12C • 100 g", bounds, clickable=True, children=node("1", bounds, cls="EditText"))
        for _ in range(wrappers):
            food = node("", bounds, children=food)
        rows += food
    end = 322 + count * 144 - offset
    body = node("Your Plate", "[0,192][720,1375]", children=rows)
    nutrition = node("Nutrition\nCalories\n50 kcal in plate", f"[0,{max(192, end)}][720,1600]")
    tabs = node("Search\nTab 2 of 6", f"[166,{220 if search else 1403}][350,{332 if search else 1515}]", clickable=True, selected=True)
    save = node("Log Foods", "[469,1251][688,1347]", clickable=True)
    area = node("", "[0,0][720,1600]", cls="ScrollView" if scrollable else "View", children=body + nutrition)
    return Snapshot.from_xml(f"<hierarchy>{header}{area}{tabs}{save}</hierarchy>")


def expected_food(**changes):
    fields = dict(title="Test food", quantity="100", unit="g", kcal=50, protein=0, fat=0, carbs=12, expanded=False, field_node=None)
    return S.Detail(**(fields | changes))


def test_exposed_plate_reads_food_summaries_not_icons_or_quantity_boxes():
    p = S.plate(plate_frame(recipe=True))
    assert p.at_start and p.at_end and len(p.rows) == 1
    assert match_foods(p.rows, [expected_food()], staged=True)
    assert S.plate(plate_frame(search=True)) is None
    assert not S.plate(plate_frame(count=9)).at_end
    assert not S.plate(plate_frame(count=9, offset=200)).at_start


@pytest.mark.parametrize("wrappers", [0, 1, 3])
@pytest.mark.parametrize("scrollable", [False, True])
def test_plate_semantics_do_not_depend_on_layout_wrappers_or_scrollability(wrappers, scrollable):
    plate = S.plate(plate_frame(wrappers=wrappers, scrollable=scrollable))
    assert plate.at_start and plate.at_end
    assert match_foods(plate.rows, [expected_food()], staged=True)


def test_log_returns_request_order_with_the_correct_macros_and_phone_positions():

    first, second = expected_food(), expected_food(title="Other", protein=1)
    a = Entry(date.today(), 12, "Test food", "100 g", 50, 0, 0, 12, position=1)
    b = Entry(date.today(), 12, "Other", "100 g", 50, 1, 0, 12, position=0)

    class App(MacroFactor):
        reads = 0

        def hour_entries(self, *args, **kwargs):
            self.reads += 1
            return [] if self.reads == 1 else [b, a]

        def _log(self):
            return SimpleNamespace(hour=lambda h: None, shortcuts=[a])

        def _day_reader(self, day, navigate):
            return self._log

        def _scan(self, hours, stop, read):  # the hour's header is looked for, and is not there: the shortcut is used
            return [], stop(read(), [])

        def _tap(self, *args):
            pass

        def _until(self, check, what, *args):
            return SimpleNamespace(plate_empty=True)

        def _set_logger_time(self, logger, *args):
            return logger

        def _add_to_plate(self, item, on_plate):
            return first if item.name == first.title else second

        def _verify_plate(self, expected):
            return SimpleNamespace(log_button=a)

    app = App(SimpleNamespace())
    result = app.log([Item("Test food"), Item("Other")], date.today(), 12)
    assert result == [a, b] and [e.position for e in result] == [1, 0]
    assert [e.protein for e in result] == [0, 1]


def test_plate_rejects_covered_or_unreadable_foods_and_the_unit_is_read_without_its_weight():
    title = node("Test food", "[0,192][720,312]", clickable=True)
    summary = node("50\nCalories\n0\nProtein\n0\nFat\n12\nCarbs", "[0,312][720,500]")
    keypad = node(
        "",
        "[0,1200][720,1500]",
        children=node("0.5", "[32,1200][300,1300]", cls="EditText")
        + node("serving • 200 g", "[300,1200][400,1300]")
        + node("Add", "[543,1400][710,1500]", clickable=True),
    )
    snap = Snapshot.from_xml(plate_frame().xml.replace("</hierarchy>", title + summary + keypad + "</hierarchy>"))
    detail = S.detail(snap)
    assert (detail.quantity, detail.unit) == ("0.5", "serving")
    assert S.plate(snap) is None  # the old plate rows are still in the hierarchy
    assert S.plate(Snapshot.from_xml(plate_frame().xml.replace("Test food&#10;50", "Unknown row"))) is None


def test_plate_scan_refuses_to_infer_a_complete_list_when_it_stalls():
    class Phone:
        def hide_keyboard(self):
            return plate_frame(9, search=True)

        def tap(self, _):
            pass

        def until(self, check, *args):
            return check(plate_frame(9))

        def drag(self, *args):
            pass

    with pytest.raises(AppError, match="no complete reading"):
        MacroFactor(Phone())._verify_plate([expected_food()] * 9)


@pytest.mark.parametrize("count", [1, 7, 12])
def test_plate_scan_keeps_every_identical_row_and_rejects_missing_food(count):
    class Phone:
        offset = 0

        def hide_keyboard(self):
            return plate_frame(count, search=True)

        def tap(self, _):
            pass

        def until(self, check, *args):
            result = check(plate_frame(count, self.offset))
            assert result
            return result

        def drag(self, x, start, end):
            self.offset += start - end - SLOP

        def save_evidence(self, _):
            return "synthetic-evidence"

    phone = Phone()
    app = MacroFactor(phone)
    assert app._verify_plate([expected_food() for _ in range(count)]).at_end
    phone.offset = 0
    with pytest.raises(AppError, match="staged foods do not match"):
        app._verify_plate([expected_food() for _ in range(count + 1)])


def test_food_matching_checks_names_amounts_nutrition_duplicates_and_reordering():
    row = S.plate(plate_frame()).rows[0]
    good = expected_food()
    for field, value in [
        ("title", "Another food"),
        ("quantity", "101"),
        ("unit", "serving"),
        ("kcal", 55),
        ("protein", 4),
        ("fat", 4),
        ("carbs", 16),
    ]:
        bad = SimpleNamespace(**(vars(good) | {field: value}))
        assert match_foods([row], [bad]) is None
    assert match_foods([row, row], [good]) is None
    assert match_foods([row], [good, good]) is None
    assert match_foods([row, row], [good, good]) == [good, good]
    other = SimpleNamespace(**(vars(good) | {"title": "Other"}))
    other_row = S.FoodRow("Other", 50, 0, 0, 12, "100 g", row.node)
    assert match_foods([other_row, row], [good, other]) == [other, good]


@pytest.mark.parametrize(
    "plate_row, quantity, unit",
    [
        ("417.86 g", "1", "serving"),  # the details said "serving • 417.9 g": two roundings of one weight
        ("", "1", "serving"),  # a recipe without a total weight
        ("", "16", "fl oz"),  # a drink counted by volume
        ("4.55 g", "0.5", "cube"),
        ("7.67 g", "7.67", "g"),
    ],
)
def test_a_staged_food_is_compared_in_the_terms_the_plate_shows(plate_row, quantity, unit):
    """Every plate row names its food and its nutrition. Its weight is there only for foods that have one, in grams."""
    row = S.FoodRow("Test food", 50, 0, 0, 12, plate_row, None)
    food = expected_food(quantity=quantity, unit=unit)
    assert match_foods([row], [food], staged=True) == [food]
    assert match_foods([row], [expected_food(quantity=quantity, unit=unit, kcal=100)], staged=True) is None  # another amount of it
    assert match_foods([row], [expected_food(quantity=quantity, unit=unit, title="Other")], staged=True) is None


def test_the_same_food_staged_in_grams_and_in_a_unit_is_matched_whatever_the_order():
    grams, unit = expected_food(quantity="100", unit="g"), expected_food(quantity="1", unit="serving")
    rows = [S.FoodRow("Test food", 50, 0, 0, 12, "100 g", None), S.FoodRow("Test food", 50, 0, 0, 12, "", None)]
    assert match_foods(rows, [unit, grams], staged=True) == [grams, unit]


def test_grams_entered_must_be_the_grams_on_the_plate_and_a_saved_row_must_show_the_selected_serving():
    food = expected_food(quantity="100", unit="g")
    for shown in ("101 g", "", "100 serving"):
        assert match_foods([S.FoodRow("Test food", 50, 0, 0, 12, shown, None)], [food], staged=True) is None
    recipe = expected_food(quantity="0.333", unit="serving")
    saved = S.FoodRow("Test food", 50, 0, 0, 12, "0.33 serving", None)  # the app displays two decimals
    assert match_foods([saved], [recipe]) == [recipe]
    for shown in ("0.5 serving", "0.33 g", "", "200 g"):  # a saved serving is never a guessed conversion
        assert match_foods([S.FoodRow("Test food", 50, 0, 0, 12, shown, None)], [recipe]) is None


def test_adding_food_cannot_hide_the_loss_of_an_existing_entry():
    old = SimpleNamespace(key=("Old", "100 g"))
    new = SimpleNamespace(key=("New", "100 g"))
    with pytest.raises(AppError, match="disappeared"):
        _new_entries([old], [new])


def test_repeated_rows_have_a_unique_measured_overlap_without_a_visible_hour_chip():
    def frame(offset):
        rows = [
            S.FoodRow(
                "Test cracker", 29, 1, 0, 6, "7 g", Node("View", "", "", PKG, 130, 320 + i * 172 - offset, 690, 492 + i * 172 - offset)
            )
            for i in range(10)
        ]
        return S.FoodLog("Today", {}, [S.HourGroup(19, rows=rows)], 280, 1268, None)

    before = frame(0)
    assert S.scan_step(before) == 86
    assert S.scroll_offset(before, frame(80), 110) == 80  # measured, not the requested 86
    assert S.scroll_offset(before, frame(172), 110) == 0  # indistinguishable: the scanner refuses apparent non-movement


def test_a_write_stops_between_actions_until_its_saving_tap_and_never_after():
    class Phone:
        checkpoint = None

        def __init__(self):
            self.taps = []

        def tap(self, node):
            if self.checkpoint:
                self.checkpoint()  # as the real phone does before every command
            self.taps.append(node.label)

    asked = []
    button = lambda label: Node("View", label, "", PKG, 0, 0, 100, 100, clickable=True)  # noqa: E731
    phone = Phone()
    app = MacroFactor(phone, cancelled=lambda: bool(asked))
    app._tap(button("Search"))
    asked.append("stop")
    with pytest.raises(Cancelled):
        app._tap(button("Add"))
    app._tap(button("Close"))  # backing out after the stop is not interrupted again
    assert phone.taps == ["Search", "Close"]

    asked.clear()
    phone = Phone()
    app = MacroFactor(phone, cancelled=lambda: bool(asked))
    app._commit(button("Log Foods"))
    asked.append("stop")
    app._tap(button("Done"))  # past the saving tap, the write runs to its end
    assert phone.taps == ["Log Foods", "Done"] and app.committed

    phone = Phone()
    app = MacroFactor(phone, cancelled=lambda: True)
    with pytest.raises(Cancelled):
        app._commit(button("Log Foods"))  # asked just before: the saving tap is not sent
    assert phone.taps == [] and not app.committed


def test_the_dialog_that_refuses_an_export_is_recognised():
    refusal = (
        node("Unusual Activity", "[64,1108][544,1172]")
        + node("Something went wrong, please try again in 24 hours.", "[64,1204][656,1296]")
        + node("OK", "[64,1328][656,1440]", clickable=True)
    )
    snap = Snapshot.from_xml(f'<hierarchy rotation="0">{node("", "[0,0][720,1600]", children=refusal)}</hierarchy>')
    assert S.export_refused(snap).center == (360, 1384)
    assert S.export_refused(Snapshot.from_xml(food_log_xml())) is None


def test_the_logger_is_recognised_when_its_header_holds_five_digits():
    """Changing 113 g to servings shows 113 servings for a moment: "34681/2666", without the spaces."""
    wide = Snapshot.from_xml(plate_frame().xml.replace("100 / 2000", "34681/2666"))
    assert S.logger(wide) is not None and S.plate(wide) is not None


def test_a_quantity_of_one_is_not_taken_for_the_one_key():
    """Every food last logged as 1 serving opens with "1" in the field: its unit chips must still be found."""

    def keypad(expanded: bool) -> Snapshot:
        keys = node("1", "[10,1093][178,1189]", clickable=True) if expanded else ""
        chips = node("", "[100,971][720,1077]", children=node("g", "[100,971][236,1077]", clickable=True)) if expanded else ""
        pad = node(
            "",
            "[0,837][720,1600]",
            children=node("1", "[30,861][690,957]", cls="EditText", clickable=True, focused=True)
            + node("small • 148 g", "[290,891][660,957]")
            + chips
            + keys
            + node("Add", "[543,1400][710,1500]", clickable=True),
        )
        title = node("Test food", "[0,192][720,312]", clickable=True)
        summary = node("50\nCalories\n0\nProtein\n0\nFat\n12\nCarbs", "[0,312][720,500]")
        return Snapshot.from_xml(f"<hierarchy>{title}{summary}{pad}</hierarchy>")

    open_pad, closed = S.detail(keypad(True)), S.detail(keypad(False))
    assert open_pad.expanded and list(open_pad.units) == ["g"] and (open_pad.quantity, open_pad.unit) == ("1", "small")
    assert not closed.expanded and not closed.units


@pytest.mark.parametrize(
    "position, stamps, minute",
    [
        (3, ["17:33", "17:36"], 36),  # after three rows of 17:33: the new, last row has the last minute
        (3, ["17:33"], 33),  # logged in the same minute as the others
        (1, ["17:33", "17:36", "17:50"], 36),  # a later entry exists, but only 17:36 is within the time the plate was open
        (1, ["17:10", "17:50"], None),  # neither the clock nor the order tells: the minute stays unknown
    ],
)
def test_a_food_logged_now_takes_the_minute_the_hour_lists_for_the_time_its_plate_was_open(position, stamps, minute):

    food = expected_food()
    old = [Entry(date.today(), 17, "Light Cake", "1 serving", 105, 6, 1, 19, position=i) for i in range(3)]
    new = Entry(date.today(), 17, "Test food", "100 g", 50, 0, 0, 12, position=position)
    after = sorted([*old, new], key=lambda e: (e.position, e is not new))
    for index, entry in enumerate(after):
        entry.position = index

    class App(MacroFactor):
        reads = 0

        def hour_entries(self, *args, **kwargs):
            self.reads += 1
            return old if self.reads == 1 else after

        def _log(self):
            return SimpleNamespace(hour=lambda h: SimpleNamespace(times=stamps, plus=None), shortcuts=[new])

        def _tap(self, *args):
            pass

        def _until(self, check, what, *args):
            return SimpleNamespace(plate_empty=True)

        def _set_logger_time(self, logger, *args):
            return logger

        def _add_to_plate(self, item, on_plate):
            return food

        def _verify_plate(self, expected):
            return SimpleNamespace(log_button=new)

    app = App(SimpleNamespace(), now=lambda: datetime(date.today().year, date.today().month, date.today().day, 17, 36))
    assert [e.minute for e in app.log([Item("Test food")])] == [minute]


def test_the_weight_beside_a_unit_is_read_or_known_to_be_absent():
    assert S._shown_weight("serving • 417.9 g") == 417.9 and S._shown_weight("small • 148 g") == 148
    assert S._shown_weight("serving") is None and S._shown_weight("fl oz") is None and S._shown_weight("g") is None


def test_the_logger_chip_of_a_day_in_another_year_is_read_as_that_day():
    today = date(2026, 11, 18)
    assert S.parse_when("12:00\nJan 2", today) == (date(2027, 1, 2), 12)  # as shown for a day 45 days ahead
    assert S.parse_when("08:00\nNov 21", today) == (date(2026, 11, 21), 8)
    assert S.parse_when("08:00\nTomorrow", today) == (date(2026, 11, 19), 8) and S.parse_when("08:00", today) == (today, 8)


def test_an_ingredients_title_is_not_a_field_of_the_recipe_form_behind_it():
    # Seen with three ingredients listed: the form behind the details had scrolled its servings field to the title's height.
    behind = node("1", "[32,232][688,328]", cls="EditText", clickable=True)
    title = node("Mustard Seed", "[0,204][720,324]", clickable=True)
    summary = node("32\nCalories\n21%\n1.6\nProtein\n64%\n2.3\nFat\n15%\n1.8\nCarbs", "[0,324][720,496]")
    pad = node(
        "",
        "[0,837][720,1600]",
        children=node("1", "[30,861][690,957]", cls="EditText", clickable=True, focused=True)
        + node("tbsp • 6.3 g", "[290,891][660,957]")
        + node("Add", "[543,1411][710,1507]", clickable=True),
    )
    found = S.detail(Snapshot.from_xml(f"<hierarchy>{behind}{title}{summary}{pad}</hierarchy>"))
    assert (found.title, found.unit, found.grams) == ("Mustard Seed", "tbsp", 6.3)


def test_a_long_recipe_form_is_known_by_its_ingredients_header_and_tells_their_weight():
    rows = node("Carrots, Raw\n135\n  3P  1F  31C  •  300 g", "[0,1070][720,1198]", clickable=True) + node(
        "Olive Oil\n133\n  0P  15F  0C  •  15 g", "[0,1200][720,1328]", clickable=True
    )
    body = (
        node("Create Recipe", "[239,100][481,148]")
        + node("Ingredients", "[32,209][205,249]")
        + node("Weight of ingredients is 3674.1 g", "[32,265][452,303]")
        + node("", "[592,208][688,304]", cls="Button", clickable=True)
        + rows
        + node("Next", "[0,1360][720,1472]", clickable=True)
    )
    form = S.recipe_form(Snapshot.from_xml(f"<hierarchy>{body}</hierarchy>"))
    assert form.name is None and form.next is not None and form.add_ingredient is not None  # the three fields are scrolled away
    assert [r.name for r in form.ingredients] == ["Carrots, Raw", "Olive Oil"] and form.ingredients_weight == 3674.1
    summary = Snapshot.from_xml(
        f"<hierarchy>{node('Stew\n1593\n  58P  71F  202C  •  1 serving\nEdit Icon', '[32,392][688,516]', clickable=True)}</hierarchy>"
    )
    total = S.food_row(summary.first(lambda n: n.clickable))
    assert (total.kcal, total.protein, total.fat, total.carbs, total.serving) == (1593, 58, 71, 202, "1 serving")


class Saved(MacroFactor):
    """The driver's own remove and edit, on a phone whose hour reads `after` once the saving tap is sent."""

    def __init__(self, after):
        super().__init__(SimpleNamespace(save_evidence=lambda what: "none"))
        self.after, self.taps = after, []

    def _before_saving(self, day, hour, before):
        import contextlib

        return contextlib.nullcontext()

    def _open_entry(self, target):
        return SimpleNamespace(actions={"Remove": "remove"}, keys={"Done": "done"}, done=None, title=target.name, quantity="2", unit="g")

    def _set_serving(self, detail, quantity, unit, read):
        return SimpleNamespace(**vars(detail), protein=1, fat=2, carbs=3)

    def _commit(self, node):
        self.taps.append(node)

    def _until(self, check, what, *args):
        return True

    def hour_entries(self, day, hour, **kwargs):
        return self.after


def hour_of(*servings: str) -> list[Entry]:
    return [Entry(date(2026, 11, 21), 8, f"Food {i}", serving, 50, 0, 0, 12, position=i) for i, serving in enumerate(servings)]


@pytest.mark.parametrize(
    "after, confirmed",
    [
        (["1 g", "3 g"], True),  # exactly the one aimed at is gone
        (["1 g", "2 g", "3 g"], False),  # the tap was sent and nothing left
        (["1 g"], False),  # another one went with it
        (["1 g", "9 g"], False),  # a neighbour is not as it was
    ],
)
def test_a_removal_is_reported_only_when_the_hour_lost_that_entry_and_nothing_else(after, confirmed):
    before = hour_of("1 g", "2 g", "3 g")
    left = [e for e in hour_of("1 g", "2 g", "3 g") if e.serving in after] if confirmed or len(after) != 2 else hour_of("1 g", "9 g")
    app = Saved(left)
    if confirmed:
        assert app.remove(before, 1) is before[1] and app.taps == ["remove"]
    else:
        with pytest.raises(AppError, match="removal not confirmed"):
            app.remove(before, 1)


@pytest.mark.parametrize(
    "after, error",
    [
        (["1 g", "2 g", "3 g"], None),
        (["1 g", "5 g", "3 g"], "edit not confirmed"),  # saved, at another amount than asked
        (["1 g", "2 g", "9 g"], "edit not confirmed"),  # a neighbour changed
        (["1 g", "2 g"], "entry count changed"),
    ],
)
def test_an_edit_is_reported_only_when_that_entry_shows_the_amount_asked_and_the_others_are_as_they_were(after, error):
    before = hour_of("1 g", "7 g", "3 g")
    app = Saved(hour_of(*after))
    if error is None:
        entry = app.edit(before, 1, 2, "g")
        assert (entry.serving, entry.protein, entry.fat, entry.carbs) == ("2 g", 1, 2, 3)  # the details' figures, to the decimal
    else:
        with pytest.raises(AppError, match=error):
            app.edit(before, 1, 2, "g")


def test_the_amount_asked_is_typed_and_the_field_must_show_it_and_a_unit_must_be_one_offered():
    """`_set_serving` on a details sheet that shows what was typed, one that never does, and one without the unit."""

    class Sheet(MacroFactor):
        def __init__(self, shows: bool):
            self.detail = SimpleNamespace(
                expanded=True, title="Test food", quantity="1", unit="serving", units={"serving": None}, unit_strip=None, backspace=None
            )
            super().__init__(SimpleNamespace(type=lambda text: setattr(self.detail, "quantity", text) if shows else None))

        def _until(self, check, what, *args):
            if not (found := check(self.detail)):
                raise UiTimeout(what)
            return found

    def read(detail):
        return detail

    sheet = Sheet(shows=True)
    assert sheet._set_serving(sheet.detail, 2.5, None, read).quantity == "2.5"
    sheet = Sheet(shows=False)
    with pytest.raises(UiTimeout):  # typed, and the field still shows the default: nothing goes on as if it were set
        sheet._set_serving(sheet.detail, 2.5, None, read)
    with pytest.raises(AppError, match="has no unit 'cup'; it offers: serving"):
        sheet._set_serving(sheet.detail, 1, "cup", read)


def test_a_recipe_is_refused_when_the_phones_totals_are_not_those_of_its_ingredients():
    def added():
        return [
            {"grams": 200.0, "grams_within": 0.0, "kcal": 230},
            {"grams": 27.4, "grams_within": 0.1, "kcal": 242},  # 2 tbsp: the details tell a spoon's weight to a tenth of a gram
        ]

    def total(kcal):
        return S.FoodRow("Soup", kcal, 10, 5, 20, "1 serving", None)

    check_recipe(added(), 227.5)  # within what the rounding allows, before the summary is shown
    check_recipe(added(), 227.5, 2, total(236))
    check_recipe([{"grams": None, "grams_within": 0.0, "kcal": 105}], None, 1, total(105))  # nothing of it weighs anything
    with pytest.raises(AppError, match="weigh 427.4 g on the phone, 227.4 g were added"):
        check_recipe(added(), 427.4)  # an ingredient went in twice
    with pytest.raises(AppError, match="shows 300 kcal a serving, its ingredients add up to 236"):
        check_recipe(added(), 227.4, 2, total(300))
    with pytest.raises(AppError, match="does not show its figures"):
        check_recipe(added(), 227.4, 2, None)


def test_a_search_list_read_in_frames_keeps_each_row_once_in_its_own_section():
    def row(name, section="", kcal=100):
        return S.FoodRow(name, kcal, 1, 1, 1, "1 serving", None, section)

    # As seen for the query "a": the next frame starts on the last rows of the one before, their header scrolled away.
    first = [row("Apple", "From History"), row("Sauce", "Custom"), row("Cookies", "Custom"), row("Apple", "Common", 116)]
    shown, new = continue_list([], first)
    assert new == first
    second = [row("Cookies"), row("Apple", kcal=116), row("Avocado"), row("Apple", "Branded", 72), row("Avocado", "Branded")]
    shown, new = continue_list(shown, second)
    assert [(r.name, r.section) for r in new] == [("Avocado", "Common"), ("Apple", "Branded"), ("Avocado", "Branded")]
    # Two results alike in every figure are two results, unless they are the overlap itself.
    shown, new = continue_list(shown, [row("Avocado"), row("Avocado"), row("Pear")])
    assert [(r.name, r.section) for r in new] == [("Avocado", "Branded"), ("Pear", "Branded")]
    shown, new = continue_list(shown, [row("Zebra")])  # no row in common: the list went on under the same header
    assert [(r.name, r.section) for r in new] == [("Zebra", "Branded")]


def test_rows_of_one_name_in_one_section_are_never_picked_among():
    """Seen: two recipes named alike, each with its own history row, the first one taken whatever was meant."""

    def row(name, section, kcal, y):
        return S.FoodRow(name, kcal, 1, 1, 1, "1 serving", Node("View", "", "", PKG, 0, y, 720, y + 100), section)

    class App(MacroFactor):
        def __init__(self, rows):
            super().__init__(SimpleNamespace(drag=lambda *where: None))
            self.panel = SimpleNamespace(
                rows=rows,
                detail=None,
                top=300,
                bottom=1500,
                keyboard=False,
                section_nodes={},
                sections=[],
                reachable=lambda r: True,
                layout=0,
            )

        def _until(self, check, what, *args):
            return self.panel

    twins = [row("Roasted Chickpeas", "From History", 967, 400), row("Roasted Chickpeas", "From History", 266, 520)]
    branded = row("Roasted Chickpeas", "Branded", 483, 700)
    for item in (Item("Roasted Chickpeas"), Item("Roasted Chickpeas", exact=True), Item("Roasted Chickpeas", library=True)):
        with pytest.raises(SeveralFoods, match="lists 2 foods named 'Roasted Chickpeas'"):
            App([*twins, branded])._reach(item, None)
        assert App([twins[1], branded])._reach(item, None).kcal == 266  # the same name in another section is not in the way
    # One food listed twice at two amounts is one food (seen: refused, a log of galettes asked from the page).
    galette = (
        row("Galettes 4 Céréales By Carrefour Bio", "From History", 29, 400),
        row("Galettes 4 Céréales By Carrefour Bio", "From History", 87, 520),
    )
    galette[0].serving, galette[1].serving = "7.67 g", "3 portion (23.01 g)"
    assert App(list(galette))._reach(Item("Galettes 4 Céréales"), None).kcal == 29
    assert App(list(galette))._reach(Item("Galettes 4 Céréales", 3, "portion"), None).kcal == 87  # the one that has the unit asked
    assert App(list(galette))._reach(Item("Galettes 4 Céréales", 20, "g"), None).kcal == 29
    assert App(list(galette))._reach(Item("Galettes 4 Céréales", 3, "portion"), None, 1).kcal == 29  # that one lacked it: the other
    # They are two foods, the database's and a copy made by hand: at 20 g the first shows 77 kcal, 2% more to the gram
    # (seen: refused again, once the first had been logged at another amount).
    galette[0].serving, galette[0].kcal = "20 g", 77
    assert App(list(galette))._reach(Item("Galettes 4 Céréales", 3, "portion"), None).kcal == 87
    galette[0].serving, galette[0].kcal = "20 g", 85  # a tenth more: another food
    with pytest.raises(SeveralFoods):
        App(list(galette))._reach(Item("Galettes 4 Céréales", 3, "portion"), None)
    galette[0].serving, galette[0].kcal = "7.67 g", 29
    with pytest.raises(FoodNotFound):
        App(list(galette))._reach(Item("Galettes 4 Céréales", 3, "portion"), None, 2)
    galette[1].serving = "3 portion"  # no weight told for one of them: not known to be the same
    with pytest.raises(SeveralFoods):
        App(list(galette))._reach(Item("Galettes 4 Céréales"), None)
    # Told which of them is meant (the Library lists each by its one serving), that one is taken, wherever it stands.
    library = [row("Roasted Chickpeas", "", 967, 400), row("Roasted Chickpeas", "", 266, 520)]
    library[0].serving = "1 serving (270 g)"
    meant = {"kcal": 266.0, "protein": 1.0, "fat": 1.0, "carbs": 1.0, "grams": None}
    richer = replace(library[1], protein=9)  # alike in energy and weight: the Library row is told by all its serving shows
    assert App([richer, library[1]])._reach(Item("Roasted Chickpeas", library=True, shown=meant), None).protein == 1
    assert App(library)._reach(Item("Roasted Chickpeas", library=True, shown=meant), None).kcal == 266
    with pytest.raises(FoodNotFound):  # edited on the phone since: nothing listed as it was known is taken in its place
        App([library[0]])._reach(Item("Roasted Chickpeas", library=True, shown=meant), None)


def test_an_amount_is_told_to_be_of_one_recipe_by_its_serving_as_listed():
    weighed = S.one_serving(S.FoodRow("Soup", 201, 5, 4, 39, "1 serving (687.5 g)", None))
    plain = S.one_serving(S.FoodRow("Soup", 315, 10, 16, 41, "1 serving", None))
    assert weighed == {"kcal": 201.0, "protein": 5.0, "fat": 4.0, "carbs": 39.0, "grams": 687.5} and plain["grams"] is None
    assert S.one_serving(S.FoodRow("Soup", 100, 2, 2, 20, "0.5 serving (343.75 g)", None)) is None
    assert S.is_amount_of(weighed, 1, "serving", 201, 687.5) and S.is_amount_of(weighed, 2.5, "serving", 503, 1718.75)
    assert S.is_amount_of(weighed, 300, "g", 88) and not S.is_amount_of(plain, 300, "g", 88)  # no weight: no grams of it
    assert not S.is_amount_of(weighed, 1, "serving", 201, None) and not S.is_amount_of(plain, 1, "serving", 315, 500.0)
    assert not S.is_amount_of(weighed, 1, "serving", 232, 687.5) and not S.is_amount_of(weighed, 1, "cup", 201)
    assert S.is_amount_of(plain, 2, "serving", 630) and not S.is_amount_of(plain, 2, "serving", 633)  # where no weight is told


STAGED_FOOD = {"title": "Test food", "quantity": "100", "unit": "g", "kcal": 50, "protein": 0, "fat": 0, "carbs": 12}


def test_a_plate_is_the_one_a_write_was_filling_when_it_holds_what_was_written_down_of_it():
    one = S.plate(plate_frame()).rows
    two = S.plate(plate_frame(count=2)).rows
    other = STAGED_FOOD | {"title": "Other food"}
    assert is_plate_of(one, [STAGED_FOOD]) and is_plate_of(two, [STAGED_FOOD, STAGED_FOOD])
    assert is_plate_of(one, [STAGED_FOOD, other])  # each food is written down before it is added: the last may not be there
    assert not is_plate_of(one, [STAGED_FOOD, other, other]) and not is_plate_of(two, [STAGED_FOOD])
    assert not is_plate_of(one, [other]) and not is_plate_of(one, [STAGED_FOOD | {"kcal": 55}])
    assert not is_plate_of([], [STAGED_FOOD]) and not is_plate_of(one, [])  # nothing written down: no plate is ours


def written(*foods, asked=("Test food",)):
    return {"asked": list(asked), "plate": list(foods)}


@pytest.mark.parametrize(
    "staged, open_food, ours",
    [
        (written(STAGED_FOOD), None, True),  # left by a write that was interrupted (seen: mf killed as it said "saving")
        (written(STAGED_FOOD), "Test food", True),  # with the next food still open: backed out, then compared
        (None, None, False),  # nothing of ours was being filled
        (written(STAGED_FOOD | {"title": "Other food"}), None, False),  # a plate was being filled, and this is not it
        (written(STAGED_FOOD), "Something else", False),  # an open food no write of ours was adding: not even backed out
    ],
)
def test_a_plate_found_on_the_phone_is_discarded_only_when_it_is_the_one_an_interrupted_write_left(staged, open_food, ours):
    class Phone:
        def __init__(self):
            self.frames, self.taps = [plate_frame(search=True)], []

        def settled(self):
            return self.frames[-1]

        def tap(self, node):
            self.taps.append(node)

    class App(MacroFactor):
        def _read_plate(self):
            return None, S.plate(plate_frame()).rows

        def _abandon_plate(self):
            self.phone.frames.append(Snapshot.from_xml(food_log_xml()))

    left, phone = [], Phone()
    app = App(phone, staging=left.append, staged=lambda: None if left else staged)
    if open_food:
        real, back = S.logger, Node("View", "back", "", PKG, 0, 0, 10, 10, clickable=True)
        seen = iter([S.Detail(open_food, 50, 0, 0, 12, "100", "g", False, None, back=back)])

        def logger_with_food_open(snap):  # the first frame shows that food's details; backed out, they are gone
            found = real(snap)
            return found and replace(found, detail=next(seen, None))

        S.logger = logger_with_food_open
    try:
        if ours:
            assert app.home() is not None and left == [None]  # forgotten once the phone is seen without the plate
            assert [node.label for node in phone.taps] == (["back"] if open_food else [])
        else:
            with pytest.raises(AppError, match="a draft plate open that mf did not create"):
                app.home()
            assert left == [] and phone.taps == [] and len(phone.frames) == 1  # not touched
    finally:
        if open_food:
            S.logger = real


def test_what_a_plate_holds_is_written_down_before_each_food_goes_on_it():
    told = []

    class App(MacroFactor):
        def _choose(self, item, read):
            return None, S.Detail(**(STAGED_FOOD | {"title": item.name}), expanded=False, field_node=None, keys={"Add": "add"})

        def _tap(self, node, snap=None):
            told.append("tap")

        def _until(self, check, what, *args):
            return True

    app = App(SimpleNamespace())
    app._add_to_plate(Item("A"), lambda food: told.append(food.title))
    assert told == ["A", "tap"]


def test_what_opens_must_be_the_recipe_that_was_meant_or_nothing_is_added():
    meant = {"kcal": 315.0, "protein": 10.0, "fat": 16.0, "carbs": 41.0, "grams": None}

    def detail(kcal, grams=None):
        return S.Detail("Soup", kcal, 10, 16, 41, "2", "serving", False, None, grams=grams)

    class App(MacroFactor):
        opened = None

        def _until(self, check, what, *args):
            return SimpleNamespace(detail=None, search_field=True, selected_tab="Library", tabs={})

        def _type_query(self, *args, **more):
            return lambda snap: None

        def _reach(self, item, read, after=0):
            return SimpleNamespace(name="Soup", node=None)

        def _tap(self, node, snap=None):
            pass

        def _set_serving(self, shown, quantity, unit, read):
            return self.opened

    app = App(SimpleNamespace())
    soup = Item("Soup", 2, "serving", library=True, shown=meant)
    app.opened = detail(630)
    assert app._choose(soup, None)[1].kcal == 630
    for other in (detail(402), detail(630, grams=687.5)):  # another recipe of the name: another energy, or a weight this one lacks
        app.opened = other
        with pytest.raises(AppError, match="not as the recipe of 315 kcal a serving"):
            app._choose(soup, None)
    assert app._choose(Item("Soup", 2, "serving", library=True), None)[1].grams == 687.5  # nothing said of which: as before
    weighed = meant | {"kcal": 201.0, "grams": 687.5}  # the details show what one serving weighs, whatever the amount
    app.opened = S.Detail("Soup", 101, 3, 2, 19, "0.5", "serving", False, None, grams=687.5)
    assert app._choose(Item("Soup", 0.5, "serving", library=True, shown=weighed), None)[1].kcal == 101


def test_the_other_row_of_a_food_is_tried_when_the_one_opened_lacks_the_unit_asked_for():
    """Seen: a galette made anew to have a "portion", beside the old one of the same name, alike to the gram."""
    opened, left = [], []

    class App(MacroFactor):
        rows = ["old", "new"]

        def _list_head(self, read):
            return SimpleNamespace(detail=None, search_field=True, selected_tab="Search", tabs={})

        def _until(self, check, what, *args):
            return SimpleNamespace(detail=None, title="Galettes")

        def _type_query(self, *args, **more):
            return lambda snap: None

        def _reach(self, item, read, after=0):
            if after >= len(self.rows):
                raise FoodNotFound(item.name, [])
            opened.append(self.rows[after])
            return SimpleNamespace(name="Galettes", node=None)

        def _tap(self, node, snap=None):
            pass

        def _leave_details(self):
            left.append(opened[-1])

        def _set_serving(self, shown, quantity, unit, read):
            if opened[-1] == "old":
                raise NoSuchUnit("Galettes has no unit 'portion'; it offers: g, oz, serving, lb")
            return S.Detail("Galettes", 87, 2, 1, 18, "3", "portion", False, None)

    app = App(SimpleNamespace())
    assert app._choose(Item("Galettes", 3, "portion"), S.logger)[1].unit == "portion" and (opened, left) == (["old", "new"], ["old"])
    del opened[:], left[:]
    App.rows = ["old"]
    with pytest.raises(NoSuchUnit, match="it offers: g, oz, serving, lb"):  # no other row of it: said as the food said it
        app._choose(Item("Galettes", 3, "portion"), S.logger)
    assert (opened, left) == (["old"], ["old"])
    del opened[:], left[:]
    App.rows = ["old", "new"]
    with pytest.raises(NoSuchUnit):  # in the recipe wizard an open food is already an ingredient: nothing else is opened
        app._choose(Item("Galettes", 3, "portion"), S.picker)
    assert (opened, left) == (["old"], [])


def test_leaving_a_food_s_details_closes_the_keypad_first_then_goes_back():
    back = Node("View", "back", "", PKG, 0, 0, 10, 10, clickable=True)
    food = S.Detail("Galettes", 29, 1, 0, 6, "7.67", "g", True, None, back=back)
    frames = [SimpleNamespace(detail=food), SimpleNamespace(detail=replace(food, expanded=False)), SimpleNamespace(detail=None)]
    did = []

    class App(MacroFactor):
        def _until(self, check, what, *args):
            return frames.pop(0)

        def _tap(self, node, snap=None):
            did.append(node.label)

    app = App(SimpleNamespace(back=lambda: did.append("system back")))
    app._leave_details()
    assert did == ["system back", "back"] and frames == []
    frames[:] = [SimpleNamespace(detail=replace(food, expanded=False))] * 6
    with pytest.raises(AppError, match="did not close"):
        app._leave_details()


def test_an_unsaved_edit_open_on_the_phone_is_left_there_and_named():
    """Seen 2026-11-25 in a recipe left half made: Back answered "Stay", fourteen times, and the Food Log was never reached."""

    class Phone:
        sent = []

        def settled(self):
            dialog = node("Exit Without Saving?", "[64,980][544,1108]") + node("Exit", "[64,1200][656,1312]", clickable=True)
            return Snapshot.from_xml(f"<hierarchy>{node('', '[0,0][720,1600]', children=dialog)}</hierarchy>")

        def back(self):
            self.sent.append("back")

    with pytest.raises(AppError, match="Exit Without Saving"):
        MacroFactor(Phone()).home()
    assert Phone.sent == []


def test_a_time_drawn_beside_a_row_is_made_out_in_a_light_and_a_dark_theme():
    """Seen 2026-11-25: MacroFactor draws "21:26" beside the first row logged then, and tells no reader which row that is."""
    from PIL import Image, ImageDraw

    for ground, ink in ((244, 30), (18, 230)):
        picture = Image.new("L", (720, 1600), ground)
        draw = ImageDraw.Draw(picture)
        draw.line((63, 0, 63, 1600), fill=(ground + ink) // 2, width=2)  # the day's line runs down the margin
        for x in range(43, 88, 9):  # five glyphs of a time, beside the row that starts at 524
            draw.rectangle((x, 565, x + 5, 583), fill=ink)
        with_time, without = Node("View", "", "", PKG, 130, 524, 690, 696), Node("View", "", "", PKG, 130, 384, 690, 524)
        assert S.stamp_beside(picture, with_time) and not S.stamp_beside(picture, without)


def test_each_row_has_the_time_drawn_above_it_when_the_rows_marked_are_as_many_as_the_times():
    def seen(hour, marks):
        rows = [S.FoodRow(f"Food {i}", 10, 0, 0, 2, "10 g", None) for i in range(len(marks))]
        return [_Seen(row, hour, 100 * i, mark) for i, (row, mark) in enumerate(zip(rows, marks, strict=True))]

    app, day = MacroFactor(SimpleNamespace()), date(2026, 11, 25)
    times = {21: ["21:00", "21:26", "21:28"]}
    minutes = lambda marks: [e.minute for e in app._entries(day, seen(21, marks), times)]  # noqa: E731
    assert minutes([None, True, False, True]) == [0, 26, 26, 28]  # as seen on the phone: the hour's first row has its first time
    assert minutes([None, False, False, True]) == [None] * 4  # a time was not made out: nothing is guessed
    assert minutes([None, True, None, True]) == [None] * 4  # a row was not looked at
    assert [e.minute for e in app._entries(day, seen(8, [None, None]), {8: ["8:05"]})] == [5, 5]  # one time: everyone's
