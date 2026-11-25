"""Local store: import of exports, day views, food catalog, journal."""

import csv
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from threading import Barrier

import pytest
from conftest import HEADER, TODAY
from openpyxl import Workbook

from mf.store import DatabaseFormatError, ExportFormatError, Store


def test_import_builds_days_and_catalog(store):
    day = store.day(TODAY - timedelta(days=5))
    assert [(e["time"], e["name"]) for e in day["entries"]] == [
        ("08:00", "Apple, Fresh"),
        ("12:30", "Skyr Nature 0%"),
        ("16:00", "Galettes 4 Céréales"),
    ]
    assert day["totals"]["kcal"] == 250
    assert day["entries"][2]["grams"] == 15
    apple = store.food("Apple, Fresh")
    assert apple.count == 20
    assert apple.servings[0]["quantity"] == 150 and apple.servings[0]["unit"] == "g"
    assert round(apple.per_unit["g"]["kcal"], 2) == 0.52


def _export(path, rows):
    """Write an export holding `rows` ([date, time, name, unit, quantity]) and return its path."""
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle, quoting=csv.QUOTE_ALL)
        writer.writerow(HEADER)
        writer.writerows([*row, "1.0", "50", "1", "10", "1"] for row in rows)
    return path


def test_a_sync_keeps_the_id_of_every_entry_that_did_not_change(store, export_csv, tmp_path):
    ids = {(e["date"], e["time"], e["name"]): e["id"] for d in range(8) for e in store.day(TODAY - timedelta(days=d))["entries"]}
    store.import_food_log(export_csv)
    again = {(e["date"], e["time"], e["name"]): e["id"] for d in range(8) for e in store.day(TODAY - timedelta(days=d))["entries"]}
    assert ids and again == ids

    day = TODAY.isoformat()
    kept = store.add_entry(day=TODAY, time="21:??", name="Skyr", unit="g", quantity=200, kcal=114, protein=19, fat=0, carbs=8)
    twin = store.add_entry(day=TODAY, time="21:??", name="Skyr", unit="g", quantity=200, kcal=114, protein=19, fat=0, carbs=8)
    edited = store.add_entry(day=TODAY, time="21:10", name="Apple, Fresh", unit="g", quantity=170, kcal=88, protein=0, fat=0, carbs=23)
    removed = store.add_entry(day=TODAY, time="22:00", name="Corn Starch", unit="g", quantity=2, kcal=8, protein=0, fat=0, carbs=2)
    rows = [[day, "21:05", "Skyr", "g", "200.0"], [day, "21:07", "Skyr", "g", "200.0"], [day, "21:10", "Apple, Fresh", "g", "186.0"]]
    store.import_food_log(_export(tmp_path / "later.csv", rows), period=(TODAY, TODAY))
    after = {e["id"]: (e["time"], e["name"], e["quantity"]) for e in store.day(TODAY)["entries"]}
    assert after[kept] == ("21:05", "Skyr", 200.0) and after[twin] == ("21:07", "Skyr", 200.0)  # the same entries, now with their minute
    assert edited not in after and removed not in after  # changed or removed on the phone: those entries are no more
    assert sorted(after.values()) == [("21:05", "Skyr", 200.0), ("21:07", "Skyr", 200.0), ("21:10", "Apple, Fresh", 186.0)]


def test_search_ignores_accents_and_ranks_by_use(store):
    assert [f.name for f in store.search("cereales")] == ["Galettes 4 Céréales"]
    assert [f.name for f in store.search("galette")][0] == "Galettes 4 Céréales"
    assert store.search("")[0].name == "Apple, Fresh"


def test_entries_added_locally_show_until_removed(store):
    entry_id = store.add_entry(day=TODAY, time="09:10", name="Apple, Fresh", unit="g", quantity=120, kcal=62, protein=0, fat=0, carbs=17)
    assert [e["id"] for e in store.day(TODAY)["entries"]] == [entry_id]
    store.update_entry(entry_id, time="10:00")
    assert store.entry(entry_id)["time"] == "10:00"
    store.delete_entry(entry_id)
    assert store.day(TODAY)["entries"] == []


def test_aliases_and_journal(store):
    store.set_alias("Pomme", "Apple, Fresh")
    assert store.alias("pomme") == "Apple, Fresh"
    store.set_alias("pomme", None)
    assert store.alias("pomme") is None
    op = store.record("log", {"text": "apple"}, "done", {"entries": []})
    assert store.ops(1)[0]["id"] == op and store.ops(1)[0]["status"] == "done"
    store.mark_op(op, "undone")
    assert store.ops(1)[0]["status"] == "undone"


def test_recipe_never_logged_is_still_loggable(store):
    store.save_recipe("Winter Soup", {"Serving Qty": 4, "Calories (kcal)": 800, "Protein (g)": 40})
    found = store.food("winter soup")
    assert found.kind == "recipe" and found.count == 0 and found.per_unit["serving"]["kcal"] == 200
    assert [f.name for f in store.search("soup")] == ["Winter Soup"]
    assert store.search("")[0].name == "Apple, Fresh"  # never-logged recipes rank after everything eaten
    store.drop_recipe("Winter Soup")
    assert store.food("winter soup") is None


def test_catalog_follows_writes_made_by_another_process(store):
    """The web server reads through one connection while the phone worker and the CLI write through others."""
    assert store.food("Apple, Fresh").count == 20  # computed and cached
    other = Store(store.path)
    other.add_entry(day=TODAY, time="09:10", name="Apple, Fresh", unit="g", quantity=120, kcal=62, protein=0, fat=0, carbs=17)
    assert store.food("Apple, Fresh").count == 21
    assert store.food("Apple, Fresh").servings[0]["quantity"] == 120  # the amount last used comes first


def test_concurrent_reads_on_one_store(store):
    """Overlapping web requests must not lose rows from COUNT(*) or PRAGMA data_version."""
    barrier = Barrier(8)
    expected = store.db.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    main_connection = store.db

    def read(_):
        connection = store.db
        barrier.wait(timeout=10)
        assert connection is not main_connection
        for _ in range(100):
            assert store.db is connection
            assert store.db.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == expected
            assert store.food("Apple, Fresh").count == 20
            assert store.day(TODAY)["entries"] == []
        return connection

    with ThreadPoolExecutor(max_workers=8) as pool:
        connections = list(pool.map(read, range(8)))
    assert len({id(connection) for connection in connections}) == 8


def test_catalog_follows_writes_between_threads(store):
    """Each thread caches its own catalog and notices commits from the other threads."""
    assert store.food("Apple, Fresh").count == 20
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(lambda: store.food("Apple, Fresh").count).result(timeout=10) == 20
        entry_id = pool.submit(
            store.add_entry, day=TODAY, time="09:10", name="Apple, Fresh", unit="g", quantity=120, kcal=62, protein=0, fat=0, carbs=17
        ).result(timeout=10)
        assert pool.submit(lambda: store.food("Apple, Fresh").count).result(timeout=10) == 21
        assert store.food("Apple, Fresh").count == 21
        store.delete_entry(entry_id)
        assert store.food("Apple, Fresh").count == 20
        assert pool.submit(lambda: store.food("Apple, Fresh").count).result(timeout=10) == 20


def test_a_blank_line_in_an_export_is_not_an_entry(store, export_csv):
    """MacroFactor writes one for a day that had entries and has none any more."""
    before = store.day(TODAY - timedelta(days=5))
    count = store.db.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    with export_csv.open("a", encoding="utf-8") as handle:
        handle.write(",,,,,,,,,\n")
    result = store.import_food_log(export_csv)
    assert result["rows"] == count and result["from"] == (TODAY - timedelta(days=20)).isoformat()
    assert store.db.execute("SELECT COUNT(*) FROM entries WHERE date = '' OR name = ''").fetchone()[0] == 0
    assert store.day(TODAY - timedelta(days=5))["totals"] == before["totals"]


@pytest.mark.parametrize("content", ["", "Time,Weight\n08:00,75\n", ",".join(HEADER) + "\n,08:00,Apple,g,100,1,50,0,12,0\n"])
def test_unrecognized_food_log_cannot_replace_entries_or_totals(store, tmp_path, content):
    store.add_entry(day=TODAY, time="08:00", name="Apple", unit="g", quantity=100, kcal=50, protein=0, fat=0, carbs=12)
    store.set_phone_totals(TODAY, {"kcal": 50, "protein": 0, "fat": 0, "carbs": 12})
    before = store.day(TODAY), store.catalog(), store.get("last_sync")
    path = tmp_path / "other.csv"
    path.write_text(content)
    with pytest.raises(ExportFormatError):
        store.import_food_log(path)
    assert (store.day(TODAY), store.catalog(), store.get("last_sync")) == before


def test_empty_food_log_only_replaces_a_known_period(store, tmp_path):
    since = TODAY - timedelta(days=2)
    path = _export(tmp_path / "empty.csv", [])
    before = store.day(since)
    assert before["entries"]
    with pytest.raises(ExportFormatError, match="period"):
        store.import_food_log(path)
    assert store.day(since) == before
    assert store.import_food_log(path, (since, TODAY))["rows"] == 0
    assert store.day(since)["entries"] == []
    assert store.day(since - timedelta(days=1))["entries"]


def test_workbook_distinguishes_absent_empty_and_invalid_sheets(store, tmp_path):
    store.save_recipe("Soup", {})
    path = tmp_path / "library.xlsx"
    book = Workbook()
    book.active.title = "History"
    book.active.append(["Food Name"])
    book.save(path)
    assert store.import_library(path) == {"history": 0}
    assert store.recipes() == [{"name": "Soup"}]

    recipes = book.create_sheet("Recipes")
    recipes.append(["Recipe Name"])
    book.active.append(["Example food"])
    book.save(path)
    assert store.import_library(path) == {"recipe": 0, "history": 1}
    assert store.recipes() == []

    recipes.append(["New soup"])
    book.active["A1"] = "Description"
    book.save(path)
    catalog = store.catalog()
    with pytest.raises(ExportFormatError, match="History"):
        store.import_library(path)
    assert store.recipes() == [] and store.catalog() == catalog
    assert store.db.execute("SELECT name FROM library WHERE kind='history'").fetchone()[0] == "Example food"


def test_a_food_knows_when_it_was_last_logged_and_at_which_hours(store):
    apple, galette = store.food("Apple, Fresh"), store.food("Galettes De Riz")
    assert apple.last_at == f"{TODAY - timedelta(days=1)} 08:00"
    assert apple.hours[8] == 20 and sum(apple.hours) == 20  # every morning of the last 30 days
    assert galette.last_at.endswith("19:15") and galette.hours[19] == 1


def test_the_phones_own_totals_are_kept_as_a_difference_from_the_entries(store, export_csv):
    day = TODAY - timedelta(days=1)  # an apple and a skyr: 78 + 114 kcal as exported
    assert store.day(day)["phone"] is None
    kept = store.set_phone_totals(day, {"kcal": 193, "protein": 20, "fat": 1, "carbs": 29})
    assert kept == {"offset": {"kcal": 1.0, "protein": -0.4, "fat": 0.3, "carbs": 0.3}, "apart": False}
    assert store.day(day)["phone"]["totals"]["kcal"] == 193
    assert store.set_phone_totals(day, {"kcal": 400, "protein": 20, "fat": 1, "carbs": 29})["apart"]  # more than rounding: other entries
    store.import_food_log(export_csv)  # the day is read anew: what was measured against it before is gone
    assert store.day(day)["phone"] is None


def test_nutrition_of_a_unit_is_pooled_over_the_entries_that_agree(store):
    """One entry says 78 kcal for 150 g; twenty say the same, and one more of 300 g says 157: 0.5215 kcal/g, not 0.52."""
    store.add_entry(day=TODAY, time="09:00", name="Apple, Fresh", unit="g", quantity=300, kcal=157, protein=1, fat=1, carbs=41)
    per = store.food("Apple, Fresh").per_unit["g"]
    assert abs(per["kcal"] - (20 * 78 + 157) / (20 * 150 + 300)) < 1e-9
    store.add_entry(day=TODAY, time="10:00", name="Apple, Fresh", unit="g", quantity=100, kcal=90, protein=0, fat=0, carbs=20)
    assert store.food("Apple, Fresh").per_unit["g"]["kcal"] == 0.9  # another food under the same name now: not averaged with the old one


def test_the_catalog_counts_its_changes_and_the_last_amount_is_the_last_logged(store):
    before, foods = store.catalog(), store.foods()
    assert store.foods() is foods  # nothing changed: the same catalog
    store.set("exports", "[]")  # not a change to the catalog
    assert store.catalog() == before and store.foods() is foods
    name = foods[0].name
    for quantity, time in ((150, "08:00"), (150, "09:00"), (80, "10:00")):
        store.add_entry(day=TODAY + timedelta(days=1), time=time, name=name, unit="g", quantity=quantity, kcal=1, protein=0, fat=0, carbs=0)
    assert store.catalog() == before + 3
    assert next(f for f in store.foods() if f.name == name).servings[0]["quantity"] == 80  # the later one, not the more usual of that day
    store.set_alias("zz", name)
    assert store.catalog() == before + 4
    assert Store(store.path).catalog() == before + 4  # as another process sees it


@pytest.mark.parametrize("marker", [0, 123])
def test_unsupported_database_is_left_untouched(tmp_path, marker):
    path = tmp_path / "other.db"
    with sqlite3.connect(path) as db:
        db.execute(f"PRAGMA application_id={marker}")
        db.execute("CREATE TABLE example (value)")
        db.execute("INSERT INTO example VALUES (123)")
    original = path.read_bytes()
    with pytest.raises(DatabaseFormatError, match="unsupported database format"):
        Store(path)
    assert path.read_bytes() == original


def test_import_keeps_identifiable_twins_and_respects_file_and_requested_bounds(store, tmp_path):
    day = TODAY.isoformat()
    path = tmp_path / "period.csv"

    def food(time):
        return [day, time, "Apple", "g", "120"]

    store.import_food_log(_export(path, [food("08:05"), food("08:25"), food("08:25")]))
    early, *twins = store.day(TODAY)["entries"]
    tomorrow = TODAY + timedelta(days=1)
    future = store.add_entry(day=tomorrow, time="09:00", name="Other", unit="g", quantity=10, kcal=2, protein=0, fat=0, carbs=0)
    store.import_food_log(_export(path, [food("08:05"), food("08:25")]))
    a, b = store.day(TODAY)["entries"]
    assert a["id"] == early["id"] and b["id"] not in [e["id"] for e in twins]
    assert store.entry(future)
    store.import_food_log(_export(path, []), (TODAY, TODAY))
    assert not store.day(TODAY)["entries"] and store.entry(future)
    store.import_food_log(path, (date.min, date.max))
    assert not store.entry(future)


def test_simultaneous_first_openers_share_one_database(tmp_path):
    path = tmp_path / "new.db"
    start = Barrier(4)

    def open_store(i):
        start.wait()
        store = Store(path)
        store.set(f"worker-{i}", str(i))

    with ThreadPoolExecutor(4) as pool:
        list(pool.map(open_store, range(4)))
    store = Store(path)
    assert [store.get(f"worker-{i}") for i in range(4)] == list("0123")


def test_known_and_unknown_minutes_can_both_explain_one_survivor(store, tmp_path):
    path = tmp_path / "twins.csv"

    def add(time):
        return store.add_entry(day=TODAY, time=time, name="Apple", unit="g", quantity=120, kcal=50, protein=1, fat=1, carbs=10)

    old = {add("08:00"), add("08:??")}
    store.import_food_log(_export(path, [[TODAY.isoformat(), "08:00", "Apple", "g", "120"]]))
    (survivor,) = store.day(TODAY)["entries"]
    assert survivor["id"] not in old


def test_catalog_read_inside_a_rolled_back_import_cannot_outlive_it(store):
    def add(name):
        store.add_entry(day=TODAY, time="08:00", name=name, unit="g", quantity=10, kcal=5, protein=0, fat=0, carbs=1)

    with pytest.raises(RuntimeError), store.atomic():
        add("Discarded food")
        assert store.food("Discarded food")
        raise RuntimeError("local import failed")
    add("Committed food")  # the committed catalog now has the same version as the rolled-back one
    assert store.food("Committed food") and not store.food("Discarded food")


def test_calories_entered_by_hand_are_entries_of_their_day_but_not_a_food_to_log_again(store):
    """MacroFactor exports a Quick Add under that name; its search has no such food."""
    store.add_entry(day=date.today(), time="12:00", name="Quick Add", unit="serving", quantity=1, kcal=700, protein=0, fat=0, carbs=0)
    assert [e["name"] for e in store.day(date.today())["entries"]] == ["Quick Add"]
    assert store.food("Quick Add") is None and not store.search("quick")


def test_what_recipes_are_made_of_is_found_by_name_most_used_first(store):
    store.save_recipe("Dal", {"Ingredients": "Lentils Red Raw (1 cup)\nCumin, Seed (1 tbsp)\nOlive Oil (1 tbsp)"})
    store.save_recipe("Curry", {"Ingredients": "Cumin, Seed (6 g)\nCumin, Ground (1 tbsp)\nSquash (1 cup, cubes)"})
    assert store.ingredients("cumin") == [("Cumin, Seed", 2), ("Cumin, Ground", 1)]
    assert store.ingredients("squash") == [("Squash", 1)] and store.ingredients("nothing") == []


def test_recipes_of_one_name_are_named_by_the_least_that_tells_them_apart():
    from mf.store import _labels

    def recipe(kcal, grams, protein=5.0):
        return {"kcal": kcal, "protein": protein, "fat": 4.0, "carbs": 39.0, "grams": grams}

    assert _labels("Soup ", [recipe(201.0, 687.5), recipe(315.0, None)]) == ["Soup (201 kcal)", "Soup (315 kcal)"]
    assert _labels("Soup", [recipe(201.0, 687.5), recipe(201.0, None), recipe(315.0, 500.0)]) == [
        "Soup (201 kcal 687.5 g)",
        "Soup (201 kcal no weight)",
        "Soup (315 kcal)",
    ]
    assert _labels("Soup", [recipe(201.0, 500.0), recipe(201.0, 500.0, 9.0)]) == [
        "Soup (201 kcal 500 g 5P 4F 39C)",
        "Soup (201 kcal 500 g 9P 4F 39C)",
    ]
    assert not any("," in label for label in _labels("Soup", [recipe(201.0, 500.0), recipe(201.0, 500.0, 9.0)]))  # commas separate foods
