"""Service against a fake MacroFactor: the store must mirror exactly what the phone confirmed."""

from dataclasses import replace
from datetime import date, timedelta
from shutil import copyfile

import pytest
from conftest import TODAY
from fakes import fake_service
from openpyxl import Workbook

from mf.app import AppError, Entry, ExportRefused, Item
from mf.device import Cancelled, Uncertain
from mf.parse import SpecError
from mf.service import EXPORTS_A_DAY, _minutes
from mf.store import ExportFormatError


@pytest.fixture
def service(store):
    return fake_service(store)


def today(service):
    return [(e["time"], e["name"], e["quantity"], e["unit"]) for e in service.day(TODAY)["entries"]]


def test_log_mirrors_what_the_phone_saved(service):
    result = service.log("apple 120g, 2x skyr 100g @8:30")
    assert len(result["entries"]) == 3
    assert today(service) == [
        ("08:30", "Apple, Fresh", 120.0, "g"),
        ("08:30", "Skyr Nature 0%", 100.0, "g"),
        ("08:30", "Skyr Nature 0%", 100.0, "g"),
    ]
    assert len(service.app.rows) == 3


def test_failed_write_is_journaled_and_leaves_the_store_alone(service):
    service.app.fail_next = True
    with pytest.raises(AppError):
        service.log("apple")
    assert today(service) == []
    assert service.store.ops(1)[0]["status"] == "failed"
    assert service.store.ops(1)[0]["request"]["text"] == "apple"  # what was asked is kept with the failure
    with pytest.raises(SpecError, match="nothing to undo"):
        service.undo()


def test_cancelling_initial_navigation_does_not_abandon_a_preexisting_draft(service, monkeypatch):
    from types import SimpleNamespace

    from mf.app import MacroFactor
    from mf.service import Service

    service._phone = SimpleNamespace(checkpoint=None)

    def abandon_foreign_draft(self):
        pytest.fail("a cancellation before starting our write tried to abandon the existing draft")

    monkeypatch.setattr(MacroFactor, "abort", abandon_foreign_draft)
    with pytest.raises(Cancelled), Service.phone(service):
        raise Cancelled()


def test_edit_retime_remove_by_day_number(service):
    service.log("apple 120g, skyr 100g @8")
    service.edit(2, 80, day=TODAY)
    service.retime(1, "10:15", TODAY)
    assert today(service) == [("08:00", "Skyr Nature 0%", 80.0, "g"), ("10:15", "Apple, Fresh", 120.0, "g")]
    assert [(r.hour, r.minute, r.serving) for r in service.app.rows] == [(10, 15, "120 g"), (8, 0, "80 g")]
    service.remove(1, TODAY)
    assert [r.name for r in service.app.rows] == ["Apple, Fresh"]
    with pytest.raises(SpecError, match="no #5"):
        service.remove(5, TODAY)


def test_identical_twins_are_told_apart_by_position(service):
    service.log("2x apple 100g @8")
    first, second = service.day(TODAY)["entries"]
    service.remove(second)
    assert [e["id"] for e in service.day(TODAY)["entries"]] == [first["id"]]
    assert len(service.app.rows) == 1


def test_undo_reverses_each_kind_of_change(service):
    service.log("apple 120g @8")
    service.edit(1, 200, day=TODAY)
    service.undo()
    assert today(service) == [("08:00", "Apple, Fresh", 120.0, "g")]
    service.retime(1, "11:00", TODAY)
    service.undo()
    assert today(service)[0][0] == "08:00"
    service.remove(1, TODAY)
    assert today(service) == []
    service.undo()
    assert [(r.hour, r.name) for r in service.app.rows] == [(8, "Apple, Fresh")]
    assert len(today(service)) == 1


def test_undo_walks_back_through_history_and_never_undoes_itself(service):
    service.log("apple 120g @8")
    service.log("skyr 100g @9")
    service.undo()
    assert today(service) == [("08:00", "Apple, Fresh", 120.0, "g")]
    service.undo()  # the first log, not the removal the previous undo made
    assert today(service) == [] and service.app.rows == []
    with pytest.raises(SpecError, match="nothing to undo"):
        service.undo()


def test_a_move_is_one_change_to_undo(service):
    service.log("apple 120g @8")
    yesterday = date.fromordinal(TODAY.toordinal() - 1)
    service.move(1, yesterday, "21:00", TODAY)
    assert [op["kind"] for op in service.store.ops(2)] == ["move", "log"]
    service.undo()
    assert today(service) == [("08:00", "Apple, Fresh", 120.0, "g")]
    assert [(r.day, r.hour) for r in service.app.rows] == [(TODAY, 8)]


def test_a_move_to_another_day_takes_the_entry_itself_whatever_its_recipe_has_become(service):
    service.log("apple 120g @8:20")
    logged = service.store.day(TODAY)["entries"][0]
    yesterday = date.fromordinal(TODAY.toordinal() - 1)
    before = len(service.store.day(yesterday)["entries"])
    service.app.calls.clear()
    service.app.price = lambda item: (999, 9, 9, 9)  # what logging it again would give now
    moved = service.move(1, yesterday, "21:00", TODAY)["entry"]
    assert service.app.calls == ["move"]  # MacroFactor's own Move; it arrives at the full hour, as asked
    assert today(service) == []
    assert len(service.store.day(yesterday)["entries"]) == before + 1
    assert [(r.day, r.hour, r.minute, r.kcal) for r in service.app.rows] == [(yesterday, 21, 0, logged["kcal"])]
    assert (moved["id"], moved["kcal"], moved["time"]) == (logged["id"], logged["kcal"], "21:00")
    assert service.store.phone_totals(TODAY)["totals"]["kcal"] == 0


def test_a_move_sets_the_minute_only_when_the_phone_left_another(service):
    service.log("apple 120g @8:20")
    tomorrow = TODAY + timedelta(days=1)
    service.app.calls.clear()
    service.move(1, tomorrow, "08:20", TODAY)  # same hour: the phone keeps the minute
    service.move(1, TODAY, "10:45", tomorrow)  # another hour: the phone gives :00
    assert service.app.calls == ["move", "move", "retime"]
    assert today(service) == [("10:45", "Apple, Fresh", 120.0, "g")]


def test_a_sync_of_either_day_of_an_unconfirmed_move_reads_both_and_settles_it(service):
    service.log("apple 120g @8")
    yesterday = date.fromordinal(TODAY.toordinal() - 1)
    before = len(service.store.day(yesterday)["entries"])
    service.app.fail_retime = True
    with pytest.raises(Uncertain, match="Modify Timestamp not found"):
        service.move(1, yesterday, "21:30", TODAY)
    assert len(today(service)) == 1 and len(service.store.day(yesterday)["entries"]) == before
    assert [(r.day, r.hour, r.minute) for r in service.app.rows] == [(yesterday, 21, 0)]  # moved on the phone, at the full hour
    service.app.fail_retime = False
    pending = service.store.ops(1)[0]
    assert pending["kind"] == "move" and pending["status"] == "uncertain"
    assert service.store.uncertain_days() == {TODAY.isoformat(), yesterday.isoformat()}
    service.sync_days(TODAY)  # what `mf sync` and the page's Sync ask: one day. The move's other day is read with it
    assert service.store.op(pending["id"])["status"] == "reconciled" and not service.store.uncertain_days()
    assert today(service) == [] and [e["time"] for e in service.store.day(yesterday)["entries"]] == ["21:00"]  # as the phone has it
    service.log("skyr 100g @9")  # and journaling still works after the failure
    assert service.store.ops(1)[0]["kind"] == "log" and service.store.ops(1)[0]["status"] == "done"


def test_an_undo_that_failed_halfway_must_be_reconciled_before_further_changes(service):
    service.log("apple 120g, skyr 100g @8")
    service.app.fail_remove_of = "Apple, Fresh"
    with pytest.raises(Uncertain):
        service.undo()  # skyr (last in) is removed, then the apple fails
    assert today(service) == [("08:00", "Apple, Fresh", 120.0, "g")]
    service.app.fail_remove_of = None
    with pytest.raises(SpecError, match="nothing to undo"):
        service.undo()
    service.sync_days(TODAY)
    assert today(service) == [("08:00", "Apple, Fresh", 120.0, "g")]
    assert not any(op["status"] == "uncertain" for op in service.store.ops())


def test_undo_invalidates_the_original_before_sending_any_saving_input(service):
    logged = service.log("apple 120g @8")
    yesterday = date.fromordinal(TODAY.toordinal() - 1)
    moved = service.move(1, yesterday, "21:00", TODAY)
    observed = []

    def sending():
        observed.append(service.store.op(moved["op"])["status"])
        pending = service.store.ops(1)[0]
        assert pending["kind"] == "undo" and pending["request"]["op"] == moved["op"]
        assert pending["status"] == "uncertain"

    # Runs after the durable marker, at the point that would send the saving input.
    original = service.app.saving
    service.app.saving = lambda: (original(), sending())
    service.undo(moved["op"])
    assert observed == ["uncertain"]  # moving it back is one saving input
    assert service.store.op(moved["op"])["status"] == "undone"
    assert service.store.op(logged["op"])["status"] == "done"


def test_failed_durable_marker_does_not_invalidate_an_undo_that_never_started(service):
    logged = service.log("apple 120g @8")

    def unavailable(op):
        raise OSError("job marker unavailable")

    service.saving = unavailable
    with pytest.raises(OSError, match="marker unavailable"):
        service.undo(logged["op"])
    assert len(service.app.rows) == 1
    assert service.store.op(logged["op"])["status"] == "done"


def test_deleted_entry_replaced_by_an_identical_one_cannot_be_edited_or_undone(service):
    original = service.log("apple 120g, skyr 100g @8")
    apple, skyr = original["entries"]
    service.remove(apple)
    replacement = service.log("apple 120g @8")["entries"][0]
    assert replacement["id"] > max(apple["id"], skyr["id"])
    service.app.calls.clear()
    with pytest.raises(SpecError, match="recorded state"):
        service.undo(original["op"])
    with pytest.raises(SpecError, match="recorded state"):
        service.remove(apple)
    assert not service.app.calls  # every target checked before even the first removal
    assert service.store.op(original["op"])["status"] == "done"
    assert not service.can_undo(service.store.op(original["op"]))
    assert len(service.app.rows) == len(today(service)) == 2


def test_move_sync_and_two_undos_preserve_the_identity_of_the_selected_twin(service):
    first = service.log("apple 120g @8")["entries"][0]
    logged = service.log("apple 120g @8")
    target = TODAY + timedelta(days=1)
    entry_id = logged["entries"][0]["id"]
    moved = service.move(entry_id, target, "09:35")
    assert moved["entry"]["id"] == entry_id
    service.sync_days(target, TODAY)
    assert service.store.entry(entry_id)["date"] == target.isoformat()
    service.undo(moved["op"])
    assert service.store.entry(entry_id)["date"] == TODAY.isoformat()
    service.undo(logged["op"])
    assert service.store.entry(entry_id) is None and service.store.entry(first["id"])
    assert len(service.app.rows) == 1


def test_an_entry_with_an_unknown_minute_can_be_put_back(service):
    entry_id = service.store.add_entry(
        day=TODAY, time="08:??", name="Apple, Fresh", unit="g", quantity=150, kcal=78, protein=0, fat=0, carbs=21
    )
    service.app.log([Item("Apple, Fresh", 150, "g")], TODAY, 8)
    service.remove(entry_id)
    service.undo()
    assert today(service) == [("08:00", "Apple, Fresh", 150.0, "g")]


def test_a_stopped_write_leaves_store_and_history_as_they_were(service):
    service.log("apple 120g @8")
    service.app.stop_next = True
    with pytest.raises(Cancelled):
        service.log("skyr 100g @9")
    assert today(service) == [("08:00", "Apple, Fresh", 120.0, "g")]
    assert [(op["kind"], op["status"]) for op in service.store.ops(2)] == [("log", "cancelled"), ("log", "done")]
    service.undo()  # the stopped write is not something to undo: this reverses the apple
    assert today(service) == []


def test_taking_back_reverses_that_write_and_no_other(service):
    apple = service.log("apple 120g @8")
    skyr = service.log("skyr 100g @9")
    assert service.take_back(apple) is True  # not the most recent one
    assert today(service) == [("09:00", "Skyr Nature 0%", 100.0, "g")]
    edited = service.edit(1, 150, day=TODAY)
    assert service.take_back(edited) is True
    assert today(service) == [("09:00", "Skyr Nature 0%", 100.0, "g")]
    assert service.take_back({"period": "Last 7 Days"}) is False  # a sync changes nothing on the phone
    with pytest.raises(SpecError, match="nothing to undo"):
        service.undo(apple["op"])  # already taken back
    assert service.take_back(skyr) is True and today(service) == []


def test_a_write_measures_the_entries_against_what_the_phone_showed(service, store):
    import contextlib

    seen, goal = {"kcal": 12, "protein": 1, "fat": 1, "carbs": 1}, {"kcal": 2666, "protein": 127, "fat": 88, "carbs": 339}
    showing = True

    @contextlib.contextmanager
    def phone():
        yield service.app
        if showing:
            service._seen[TODAY] = (seen, goal)  # read from the Food Log as the phone was left

    service.phone = phone
    service.log("apple 150g")
    assert store.day(TODAY)["phone"]["offset"]["kcal"] == 2.0  # the fake app saves 10 kcal an entry
    assert store.day(TODAY)["targets"] == goal  # the phone's own targets, rather than the exported program's
    assert service._seen == {}
    showing = False  # the header's figures are not always readable: what was read before this change says nothing after it
    service.remove(1, TODAY)
    assert store.day(TODAY)["phone"] is None


def exporting(service, export_csv):
    """Give the fake phone an export: returns the list of exports asked of it."""
    asked = []
    service.app.export_food_log = lambda folder, label: asked.append(label) or export_csv
    service.app.home = lambda: None
    return asked


def test_exports_are_counted_and_the_days_allowance_is_not_overspent(service, export_csv):
    asked = exporting(service, export_csv)
    for _ in range(EXPORTS_A_DAY):
        service.sync_export()
    assert service.exports()["left"] == 0
    with pytest.raises(SpecError, match="0 of 6 are left"):
        service.sync_export()
    assert len(asked) == EXPORTS_A_DAY  # refused before the phone was asked
    service.sync_export(force=True)  # a person's decision
    assert len(asked) == EXPORTS_A_DAY + 1
    with pytest.raises(SpecError, match="needs 2 exports"):
        service.backup()


def test_backup_keeps_downloads_and_export_count_when_one_file_cannot_be_imported(service, export_csv, tmp_path, monkeypatch):
    monkeypatch.setattr("mf.service.DATA_DIR", tmp_path)
    service.log("apple 120g @8")
    before = today(service), service.store.catalog()

    def foods(folder, _label):
        folder.mkdir(parents=True)
        return copyfile(export_csv, folder / "foods.csv")

    def invalid_library(folder):
        path = folder / "library.xlsx"
        Workbook().save(path)
        return path

    service.app.export_food_log = foods
    service.app.export_library = invalid_library
    service.app.home = lambda: None
    with pytest.raises(ExportFormatError):
        service.backup()
    assert (today(service), service.store.catalog()) == before
    assert service.exports()["left"] == EXPORTS_A_DAY - 2
    (folder,) = (tmp_path / "snapshots").iterdir()
    assert {p.name for p in folder.iterdir()} == {"foods.csv", "library.xlsx", "SHA256SUMS"}


def test_a_refusal_by_macrofactor_is_remembered_for_a_day(service, export_csv):
    asked = exporting(service, export_csv)

    def refuse(folder, label):
        raise ExportRefused("MacroFactor refuses to export for now")

    service.app.export_food_log = refuse
    with pytest.raises(ExportRefused):
        service.sync_export()
    service.app.export_food_log = lambda folder, label: asked.append(label) or export_csv
    for force in (False, True):  # nothing overrides MacroFactor's own refusal
        with pytest.raises(SpecError, match="refusing exports until"):
            service.sync_export(force=force)
    assert asked == [] and service.exports()["refused_until"] > 0


def test_a_day_is_brought_in_line_with_the_phones_screen_without_an_export(service, export_csv):
    asked = exporting(service, export_csv)
    service.log("apple 120g, skyr 100g @8")
    apple, skyr = service.day(TODAY)["entries"]
    service.store.update_entry(apple["id"], time="08:??")  # as when the screen could not tell the minute
    phone = service.app
    phone.rows = [r for r in phone.rows if r.name != "Skyr Nature 0%"]  # removed on the phone
    phone.rows.append(Entry(TODAY, 13, "Galettes De Riz", "30 g", 115, 2, 1, 24, 5))  # logged on the phone
    phone.rows.append(Entry(TODAY, 14, "Something New By Brand", "1 serving", 200, 5, 5, 30, None))  # a food never seen here
    result = service.sync_days(TODAY)
    assert (result["seen"], result["added"], result["changed"], result["removed"]) == (3, 2, 0, 1) and asked == []
    after = service.day(TODAY)
    assert [(e["time"], e["name"], e["quantity"], e["unit"]) for e in after["entries"]] == [
        ("08:00", "Apple, Fresh", 120.0, "g"),
        ("13:05", "Galettes De Riz", 30.0, "g"),
        ("14:??", "Something New By Brand", 1.0, "serving"),
    ]
    assert after["entries"][0]["id"] == apple["id"]  # the same entry, now with its minute
    assert after["phone"]["totals"]["kcal"] == 10 + 115 + 200 and not after["phone"]["apart"]
    assert service.sync_days(TODAY)["added"] == service.sync_days(TODAY)["removed"] == 0  # read again, nothing moves


def test_a_day_that_was_not_read_whole_changes_nothing(service):
    service.log("apple 120g, skyr 100g @8")
    whole = service.app.read_day

    def half(day):  # the screen gave the total, and only one of the two entries
        view = whole(day)
        view.entries = view.entries[:1]
        return view

    service.app.read_day = half
    with pytest.raises(AppError, match="could not be read whole"):
        service.sync_days(TODAY)
    assert len(service.day(TODAY)["entries"]) == 2


def test_an_amount_changed_on_the_phone_changes_the_same_entry(service):
    service.log("apple 120g @8:20")
    (apple,) = service.day(TODAY)["entries"]
    (row,) = service.app.rows
    row.serving, row.kcal = "200 g", 104  # edited on the phone
    result = service.sync_days(TODAY)
    (after,) = service.day(TODAY)["entries"]
    assert (result["added"], result["changed"], result["removed"]) == (0, 1, 0)
    assert (after["id"], after["time"], after["quantity"], after["kcal"]) == (apple["id"], "08:20", 200.0, 104)


def test_minutes_come_from_the_hours_timestamps_wherever_they_are_certain(service):
    service.log("apple 120g @8:00")
    service.log("skyr 100g @8:30")
    phone = service.app
    phone.rows.append(Entry(TODAY, 8, "Galettes De Riz", "30 g", 115, 2, 1, 24, 10))  # logged on the phone, between the two
    phone.rows.append(Entry(TODAY, 9, "Galettes De Riz", "30 g", 115, 2, 1, 24, 15))  # two more, in an hour the copy knows nothing of
    phone.rows.append(Entry(TODAY, 9, "Apple, Fresh", "100 g", 52, 0, 0, 14, 40))
    service.sync_days(TODAY)
    assert [e["time"] for e in service.day(TODAY)["entries"]] == ["08:00", "08:10", "08:30", "09:15", "09:40"]
    phone.rows.append(Entry(TODAY, 9, "Skyr Nature 0%", "50 g", 30, 5, 0, 2, 15))  # between a 09:15 and a 09:40: it could be either
    service.sync_days(TODAY)
    assert sorted(e["time"] for e in service.day(TODAY)["entries"])[3:] == ["09:15", "09:40", "09:??"]
    phone.times_drawn = True  # the driver made out beside which rows the hour's times are drawn: nothing is left to deduce
    service.sync_days(TODAY)
    assert sorted(e["time"] for e in service.day(TODAY)["entries"])[3:] == ["09:15", "09:15", "09:40"]


def test_the_minutes_of_an_hour():
    assert _minutes([5], [None, None]) == [5, 5]  # one stamp: every row has it
    assert _minutes([0, 10, 30], [0, None, 30]) == [0, 10, 30]  # the stamp nobody has is the new row's
    assert _minutes([0, 30], [0, None, 30]) == [0, None, 30]  # either: not told
    assert _minutes([0, 30], [0, None, 0, 30]) == [0, 0, 0, 30]  # between two rows of the same minute
    assert _minutes([16, 41, 52], [None, None, None]) == [16, 41, 52]  # as many rows as stamps: in order
    assert _minutes([16, 41], [None, None, None]) == [None, None, None]
    assert _minutes([10, 20], [20, 10]) == [10, 20]  # the copy contradicts the screen's order: retimed on the phone
    assert _minutes([10, 20], [10, 45]) == [10, 20]  # or holds a minute the hour no longer shows
    assert _minutes([], [None]) == [None]


def test_a_commit_failure_after_saving_preserves_the_durable_uncertain_journal(service, monkeypatch):
    import contextlib

    real = service.store.atomic

    @contextlib.contextmanager
    def rollback(write=True):
        with real(write):
            yield
            if service.app.rows:  # allow the pre-save marker; fail the local commit after the phone saved
                raise OSError("commit refused")

    monkeypatch.setattr(service.store, "atomic", rollback)
    with pytest.raises(Uncertain, match="commit refused"):
        service.log("apple 120g @8")
    assert len(service.app.rows) == 1 and today(service) == []
    (op,) = service.store.ops()
    assert op["status"] == "uncertain" and op["request"]["date"] == TODAY.isoformat()
    monkeypatch.setattr(service.store, "atomic", real)
    service.sync_days(TODAY)
    assert len(today(service)) == 1 and service.store.op(op["id"])["status"] == "reconciled"


def test_recovery_of_multiple_days_is_all_or_nothing(service):
    service.app.fail_after_save = True
    with pytest.raises(Uncertain):
        service.log("apple 120g @8")
    tomorrow = date.fromordinal(TODAY.toordinal() + 1)
    read = service.app.read_day

    def broken(day):
        if day == tomorrow:
            raise AppError("cannot read tomorrow")
        return read(day)

    service.app.read_day = broken
    with pytest.raises(AppError):
        service.sync_days(TODAY, tomorrow)
    assert today(service) == []
    assert service.store.ops(2)[1]["status"] == "uncertain"


def test_the_phone_lease_lasts_through_the_local_commit_even_for_a_shared_service(service, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    mirroring, release, second_started = threading.Event(), threading.Event(), threading.Event()
    add = service.store.add_entry

    def hold(**fields):
        if fields["name"] == "Apple, Fresh":
            mirroring.set()
            assert release.wait(5)
        return add(**fields)

    def second():
        second_started.set()
        return service.log("skyr 100g @9")

    monkeypatch.setattr(service.store, "add_entry", hold)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(service.log, "apple 120g @8")
        assert mirroring.wait(5)
        next_write = pool.submit(second)
        assert second_started.wait(5)
        try:
            assert not next_write.done() and len(service.app.rows) == 1
        finally:
            release.set()
        assert first.result(timeout=5)["op"] != next_write.result(timeout=5)["op"]
    assert len(today(service)) == 2
    assert {op["request"]["text"] for op in service.store.ops(2)} == {"apple 120g @8", "skyr 100g @9"}


def test_an_extra_identical_entry_on_the_phone_refuses_a_positional_write(service):
    service.log("2x apple 120g @8")
    service.app.rows.insert(0, Entry(TODAY, 8, "Apple, Fresh", "120 g", 10, 1, 1, 1, 0))
    with pytest.raises(AppError, match="sync"):
        service.remove(2, TODAY)
    assert len(service.app.rows) == 3


def test_a_twin_logged_on_the_phone_leaves_the_entries_that_were_there_as_they_are(service):
    original = service.log("apple 120g @8:15")["entries"][0]
    other = service.log("skyr 100g @8:15")["entries"][0]
    service.app.rows.append(Entry(TODAY, 8, "Apple, Fresh", "120 g", 10, 1, 1, 1, 40))  # the same again, on the phone itself
    count = service.sync_days(TODAY)
    assert (count["added"], count["removed"]) == (1, 0)
    apples = [e for e in service.day(TODAY)["entries"] if e["name"] == "Apple, Fresh"]
    assert [e["id"] == original["id"] for e in apples] == [True, False]  # the one that was there is still that entry
    assert service.store.entry(other["id"])["time"] == "08:15"


def test_an_unconfirmed_write_is_read_with_every_day_it_touched(service):
    store, days = service.store, [(TODAY + timedelta(days=n)).isoformat() for n in range(4)]
    store.record("move", {"entry": {"date": days[0]}, "date": days[1]}, "uncertain")
    store.record("move", {"entry": {"date": days[1]}, "date": days[2]}, "uncertain")
    store.record("log", {"date": days[3]}, "done")
    assert store.unconfirmed_with({days[0]}) == set(days[:3])  # through the day two of them share
    assert store.unconfirmed_with({days[3]}) == {days[3]}


def test_reconciling_an_ambiguous_survivor_retires_both_old_ids(service):
    originals = service.log("2x apple 120g @8")
    service.app.rows.pop()
    service.sync_days(TODAY)
    (only,) = service.day(TODAY)["entries"]
    assert only["id"] not in {e["id"] for e in originals["entries"]}
    assert not service.can_undo(service.store.op(originals["op"]))
    with pytest.raises(SpecError, match="recorded state"):
        service.undo(originals["op"])
    assert len(service.app.rows) == 1


def test_later_reads_do_not_hide_the_last_write_from_cli_undo(service):
    service.log("apple 120g @8")
    for _ in range(55):
        service.store.record("sync", {}, "done")
    service.undo()
    assert not today(service) and not service.app.rows


def test_relative_export_that_crosses_midnight_preserves_data_and_cost(service, export_csv, monkeypatch):
    class Clock(date):
        current = TODAY

        @classmethod
        def today(cls):
            return cls.current

    monkeypatch.setattr("mf.service.date", Clock)

    def exported(folder, label):
        Clock.current += timedelta(days=1)
        return export_csv

    service.app.export_food_log = exported
    service.app.home = lambda: None
    before = service.store.catalog()
    with pytest.raises(AppError, match="date changed"):
        service.sync_export("week")
    assert service.store.catalog() == before and service.exports()["left"] == EXPORTS_A_DAY - 1


def test_live_day_stores_the_totals_from_its_own_reading(service):
    reading = service.day(TODAY, live=True)
    stored = service.store.phone_totals(TODAY)
    assert (stored["totals"], stored["targets"]) == (reading["totals"], reading["targets"])


@pytest.mark.parametrize("amount", [0, -1, float("nan"), float("inf")])
def test_invalid_portions_are_rejected_during_planning(service, amount):
    with pytest.raises(SpecError, match="quantity"):
        service.plan([{"name": "Apple, Fresh", "quantity": amount, "unit": "g"}])
    assert service.app.calls == []


def test_a_move_further_than_the_phone_reaches_goes_in_steps_and_can_be_undone(service):
    from mf.app import move_steps

    assert move_steps(TODAY, TODAY + timedelta(days=7)) == [TODAY + timedelta(days=7)]
    assert move_steps(TODAY, TODAY - timedelta(days=29)) == [TODAY - timedelta(days=29)]
    assert move_steps(TODAY, TODAY + timedelta(days=16)) == [TODAY + timedelta(days=d) for d in (7, 14, 16)]
    assert move_steps(TODAY, TODAY - timedelta(days=40)) == [TODAY - timedelta(days=d) for d in (29, 40)]
    service.log("apple 120g @8:20")
    far = TODAY - timedelta(days=20)
    service.app.calls.clear()
    moved = service.move(1, far, "08:20", TODAY)  # back: within reach
    assert service.app.calls == ["move"] and "days" not in service.store.op(moved["op"])["request"]
    service.undo()  # forward again: three steps of at most a week
    assert service.app.calls == ["move", "move", "move", "move"]
    assert today(service) == [("08:20", "Apple, Fresh", 120.0, "g")]
    assert [(r.day, r.hour, r.minute) for r in service.app.rows] == [(TODAY, 8, 20)]


def test_recipes_are_read_from_the_phones_list_new_edited_and_weightless(service):
    old = [(f"Old {i}", "1 serving (300 g)", 200 + i, 10, 5, 20) for i in range(20)]
    service.app.recipes = [("Light Cake", "1 serving", 105, 6, 1, 19), ("Bean Soup", "1 serving (450 g)", 364, 17, 6, 59), *old]
    first = service.read_recipes()
    assert (first["seen"], first["new"], first["complete"]) == (22, 22, True)  # nothing known yet: the whole list
    cake = service.store.food("Light Cake")  # never logged, not in any export: loggable, at one serving, with no weight
    assert (cake.kind, cake.count, cake.per_unit) == (
        "recipe",
        0,
        {"serving": {"kcal": 105.0, "protein": 6.0, "fat": 1.0, "carbs": 19.0, "grams": None}},
    )
    soup = service.store.food("Bean Soup").per_unit
    assert soup["serving"]["grams"] == 450.0 and round(soup["g"]["kcal"] * 450) == 364

    # The soup is cooked again with other ingredients, a recipe is created: both come first in the phone's order.
    service.app.recipes = [
        ("New Stew", "1 serving", 500, 30, 10, 60),
        ("Bean Soup", "1 serving (400 g)", 396, 19, 9, 55),
        *service.app.recipes[:1],
        *old,
    ]
    second = service.read_recipes()
    assert (second["new"], second["changed"], second["complete"]) == (1, 1, False) and second["seen"] < 22  # it stopped at known recipes
    assert service.store.food("Bean Soup").per_unit["serving"] == {
        "kcal": 396.0,
        "protein": 19.0,
        "fat": 9.0,
        "carbs": 55.0,
        "grams": 400.0,
    }
    assert service.store.food("New Stew").kind == "recipe"

    service.app.recipes = service.app.recipes[:5]  # the others were deleted on the phone: only a whole reading tells
    assert service.read_recipes()["gone"] == 0 and service.read_recipes(everything=True)["gone"] == 18
    assert service.store.food("Old 7") is None


def test_a_logged_recipe_shows_as_it_is_now_not_as_it_was_last_eaten(service):
    service.app.recipes = [("Bean Soup", "1 serving", 396, 19, 9, 55)]
    service.store.add_entry(
        day=TODAY, time="12:00", name="Bean Soup", unit="serving", quantity=1, kcal=364, protein=17, fat=6, carbs=59, unit_grams=100.0
    )
    assert service.store.food("Bean Soup").per_unit["serving"]["grams"] == 100.0  # what an export made up for it
    service.read_recipes()
    assert service.store.food("Bean Soup").per_unit["serving"] == {"kcal": 396.0, "protein": 19.0, "fat": 9.0, "carbs": 55.0, "grams": None}
    eaten = service.store.day(TODAY)["entries"][0]
    assert today(service) == [("12:00", "Bean Soup", 1.0, "serving")] and eaten["kcal"] == 364  # what was eaten stays
    assert eaten["grams"] is None  # and is no longer shown with the weight the export made up


def test_a_request_that_was_never_formed_is_not_journaled(service):
    for ask in (lambda: service.log("no such food 100g"), lambda: service.remove(99, TODAY), service.undo):
        with pytest.raises(SpecError):
            ask()
    assert service.store.ops() == []
    service.app.fail_next = True
    with pytest.raises(AppError):
        service.log("apple 120g @8")
    assert [(op["kind"], op["status"]) for op in service.store.ops()] == [("log", "failed")]  # this one reached the phone


def test_a_move_is_saved_whether_or_not_the_day_it_left_shows_its_totals(service):
    service.log("apple 120g, skyr 100g @8")
    service.sync_days(TODAY)
    assert service.store.phone_totals(TODAY) is not None
    service.app.hide_totals = True  # seen once on the phone: the header's figures absent from what the app exposes
    moved = service.move(1, TODAY + timedelta(days=1), "09:00", TODAY)
    assert moved["entry"]["date"] == (TODAY + timedelta(days=1)).isoformat() and len(today(service)) == 1
    assert service.store.phone_totals(TODAY) is None  # what was read before the move says nothing of the day now
    assert service.store.ops(1)[0]["status"] == "done"


def test_a_food_the_phone_shows_without_a_weight_has_no_grams_here_either(service):
    service.store.add_entry(
        day=TODAY, time="07:00", name="Energy Drink", unit="fl oz", quantity=16, kcal=10, protein=0, fat=0, carbs=6, unit_grams=6.25
    )
    drink = service.store.food("Energy Drink")
    assert drink.per_unit["fl oz"]["grams"] == 6.25 and not drink.weightless  # what the export made up, until the phone is seen
    original = service.app.log

    def log(items, *args, weighed=False, **kwargs):
        saved = original(items, *args, **kwargs)
        for entry in saved:
            entry.weighed = weighed
        return saved

    service.app.log = log
    service.log([{"name": "Energy Drink", "quantity": 16, "unit": "fl oz"}], TODAY, "9:00")
    drink = service.store.food("Energy Drink")
    assert drink.weightless and list(drink.per_unit) == ["fl oz"] and drink.per_unit["fl oz"]["grams"] is None
    assert [e["grams"] for e in service.store.day(TODAY)["entries"]] == [None, None]  # the earlier entry too
    service.app.log = lambda items, *args, **kwargs: log(items, *args, weighed=True, **kwargs)
    service.log([{"name": "Energy Drink", "quantity": 16, "unit": "fl oz"}], TODAY, "10:00")
    assert not service.store.food("Energy Drink").weightless  # it has a weight now: the phone showed one


def test_undoing_a_removal_says_when_the_food_came_back_with_other_figures(service):
    service.log("apple 120g @8")
    service.remove(1, TODAY)
    assert service.undo()["note"] is None  # logged again with the same figures
    service.remove(1, TODAY)
    service.app.price = lambda item: (999, 9, 9, 9)  # its recipe was edited in between
    note = service.undo()["note"]
    assert "999 kcal" in note and "10 kcal" in note


def test_a_recipe_takes_foods_of_the_history_and_foods_named_as_the_phone_lists_them(service):
    spec = ["apple 120g, skyr 100g", "=Cumin, Seed = 6 g", "=15% Creme Legere Epaisse By Auchan = 0.2 kg"]
    tried = service.rehearse_recipe("Stew", spec, servings=2)["rehearsed"]
    assert [(i["name"], i["quantity"], i["unit"], i["from"]) for i in tried["ingredients"]] == [
        ("Apple, Fresh", 120.0, "g", "history"),
        ("Skyr Nature 0%", 100.0, "g", "history"),
        ("Cumin, Seed", 6.0, "g", "common"),
        ("15% Creme Legere Epaisse By Auchan", 200.0, "g", "common"),
    ]
    assert tried["kcal"] == 20 and tried["ingredients_weight"] == 426
    # A rehearsal changes nothing: no recipe here, nothing in the journal.
    assert service.app.calls == ["rehearse"] and service.store.food("Stew") is None and not service.store.ops()

    made = service.create_recipe("Stew", spec, servings=2)
    assert made["recipe"]["name"] == "Stew" and service.store.food("Stew").kind == "recipe"
    assert [op["kind"] for op in service.store.ops()] == ["recipe"]
    assert ("Cumin, Seed", 1) in service.store.ingredients("cumin")  # what it is made of can be found again, by its exact name


def test_the_phones_search_is_told_as_it_lists_it(service):
    told = service.search("cumin seed")
    assert told["found"] == [
        {"name": "Cumin, Seed", "from": "common", "serving": "1 tbsp (6 g)", "kcal": 23, "protein": 1, "fat": 1, "carbs": 3}
    ]
    assert told["more"] == {"history": 7}  # behind the phone's "See 7 More": said, not passed over


def test_a_whole_reading_of_the_phones_recipes_says_which_exist(service):
    store = service.store
    store.save_recipe("Old Stew", {"Serving Qty": 4, "Calories (kcal)": 800})  # as a workbook told, months ago
    store.save_recipe("Bean soup", {"Serving Qty": 2, "Calories (kcal)": 728, "Ingredients": "Beans (1 cup)"})
    service.app.recipes = [("Bean Soup", "1 serving (450 g)", 364, 17, 6, 59)]  # the phone's list: the stew was deleted there
    assert service.read_recipes(everything=True)["gone"] == 1
    assert store.food("Old Stew") is None and store.food("Bean Soup").kind == "recipe"
    assert [r["name"] for r in store.recipes()] == ["Bean soup"]  # known by another spelling: what it is made of is kept


def test_grams_are_refused_here_as_on_the_page_for_a_food_the_phone_shows_without_a_weight(service):
    service.app.recipes = [("Light Cake", "1 serving", 105, 6, 1, 19)]
    service.read_recipes()
    with pytest.raises(SpecError, match="no weight"):
        service.plan("Light Cake 50g")
    assert service.plan("Light Cake 0.5 serving").items[0].unit == "serving"


SOUPS = [("Soup", "1 serving (687.5 g)", 201, 5, 4, 39), ("Dal", "1 serving", 300, 20, 5, 40), ("Soup", "1 serving", 315, 10, 16, 41)]


def soups(service):
    """As seen on the phone: seventeen names held two to four recipes each ("Soup" four), and a name's figures here
    were one's, at random, while the driver took the first row of that name."""
    store = service.store
    for quantity, kcal in ((1, 201), (2, 630), (1, 999)):  # one of each recipe, and one of a Soup that is no more
        store.add_entry(day=TODAY, time="12:00", name="Soup", unit="serving", quantity=quantity, kcal=kcal, protein=5, fat=4, carbs=39)
    service.app.recipes = list(SOUPS)
    assert service.read_recipes(everything=True)["seen"] == 2
    asked = []
    log = service.app.log
    service.app.log = lambda items, *where: (asked.extend(items), log(items, *where))[1]
    return store, asked


def test_each_recipe_of_a_shared_name_is_a_food_of_its_own_named_for_what_tells_it_apart(service):
    store, _ = soups(service)
    assert {name: len(listed) for name, listed in store.listed_recipes().items()} == {"Soup": 2, "Dal": 1}
    assert list(store.shown_recipes()) == ["Dal"] and list(store.shared_recipes()["Soup"]) == ["Soup (201 kcal)", "Soup (315 kcal)"]
    thick, thin = store.food("Soup (201 kcal)"), store.food("Soup (315 kcal)")
    assert store.food("Soup") is None  # the name alone means none of them
    assert (thick.phone_name, thick.kind, thick.count, thick.shared["serving"]["grams"]) == ("Soup", "recipe", 1, 687.5)
    assert set(thick.per_unit) == {"serving", "g"} and (thin.weightless, set(thin.per_unit)) == (True, {"serving"})
    assert (thin.count, thin.servings[0]["quantity"]) == (1, 2)  # each has the entries that are amounts of it alone
    entries = {e["kcal"]: (e["food"], e["grams"]) for e in store.day(TODAY)["entries"]}
    assert entries == {201: ("Soup (201 kcal)", 687.5), 630: ("Soup (315 kcal)", None), 999: (None, None)}
    for text in ("Soup 1 serving", "soup 201", "soup"):
        with pytest.raises(SpecError, match="MacroFactor has 2 recipes named 'Soup'; say which: Soup .201 kcal.; Soup .315 kcal."):
            service.plan(text)
    with pytest.raises(SpecError, match="say which"):
        service.plan([{"name": "Soup", "quantity": 1, "unit": "serving"}])
    with pytest.raises(SpecError, match="no weight"):
        service.plan("Soup (315 kcal) 300g")
    assert service.plan("soup (201 kcal) 300g").items[0].food is thick


def test_the_recipe_meant_is_the_one_asked_of_the_phone_when_logged_copied_or_put_back(service):
    store, asked = soups(service)
    logged = service.log("Soup (315 kcal) 2 serving @13:00")["entries"][0]
    thin = {"kcal": 315.0, "protein": 10.0, "fat": 16.0, "carbs": 41.0, "grams": None}
    assert (asked[-1].name, asked[-1].library, asked[-1].shown) == ("Soup", True, thin)
    assert (logged["name"], logged["kcal"]) == ("Soup", 630) and store.entry(logged["id"])["name"] == "Soup"
    service.copy(logged["id"], time="14:00")
    assert (asked[-1].name, asked[-1].shown) == ("Soup", thin)
    service.remove(logged["id"])
    service.undo()
    assert (asked[-1].name, asked[-1].library, asked[-1].shown) == ("Soup", True, thin)
    old = next(e for e in store.day(TODAY)["entries"] if e["kcal"] == 999)
    with pytest.raises(SpecError, match="of none of the recipes MacroFactor now lists"):  # never asked by the bare name
        service.copy(old["id"], time="15:00")
    assert len(asked) == 3


def test_a_shared_name_is_not_deleted_nor_given_to_another_recipe_and_readings_keep_the_newest_first(service):
    store, _ = soups(service)
    for name in ("Soup", "soup (201 kcal)"):
        with pytest.raises(SpecError, match="lists 2 recipes named 'Soup'"):
            service.delete_recipe(name)
    with pytest.raises(SpecError, match="already named 'soup'"):
        service.create_recipe("soup", "apple 100g")
    assert "recipe-delete" not in service.app.calls and "recipe" not in service.app.calls
    # Read again short of the whole list: an edited recipe replaces itself, one more of a shared name comes before the others.
    dal, soup = (
        {"kcal": 310.0, "protein": 20.0, "fat": 5.0, "carbs": 40.0, "grams": None},
        {"kcal": 400.0, "protein": 9.0, "fat": 9.0, "carbs": 40.0, "grams": None},
    )
    store.save_shown({"Dal": [dal], "Soup": [soup]}, False)
    assert store.shown_recipes()["Dal"]["kcal"] == 310 and [r["kcal"] for r in store.listed_recipes()["Soup"]] == [400, 201, 315]
    store.save_shown({"Twice": [dal, dal]}, True)  # two recipes alike in every figure: one food, which the phone will not let mf pick
    assert store.shared_recipes() == {} and store.food("Twice").shared is None and len(store.listed_recipes()["Twice"]) == 2


def test_a_bare_shared_name_is_never_resolved_to_one_of_its_recipes_by_a_number_or_a_fragment(service):
    soups(service)
    for text in ("2 soup", "3 soup", "5 soup", "sou", "2x soup", "200g soup", "soup 315"):
        with pytest.raises(SpecError, match="say which|ambiguous"):
            service.plan(text)
    assert service.plan("2 Soup (315 kcal)").items[0].food.name == "Soup (315 kcal)"  # twice its usual amount


def test_two_recipes_of_a_name_in_one_hour_are_two_entries_and_each_is_found_again_as_itself(service):
    soups(service)
    thick = service.log("Soup (201 kcal) 1 serving @13:00")["entries"][0]
    thin = service.log("Soup (315 kcal) 1 serving @13:00")["entries"][0]
    service.remove(thin["id"])  # same name, same hour, same serving: told from the other by what it holds
    assert [(r.name, r.kcal) for r in service.app.rows if r.hour == 13] == [("Soup", 201)]
    assert service.store.entry(thick["id"]) and service.store.entry(thin["id"]) is None


def test_recipes_alike_in_energy_are_told_apart_by_the_weight_of_a_serving(service):
    store = service.store
    service.app.recipes = [("Soup", "1 serving (687.5 g)", 201, 5, 4, 39), ("Soup", "1 serving", 201, 5, 4, 39)]
    service.read_recipes(everything=True)
    weighed, plain = "Soup (201 kcal 687.5 g)", "Soup (201 kcal no weight)"
    assert list(store.shared_recipes()["Soup"]) == [weighed, plain]
    service.log(f"{weighed} 1 serving, {plain} 2 serving @12:00")
    told = [(e["food"], e["grams"]) for e in store.day(TODAY)["entries"]]
    # The weight recorded says which; none recorded says nothing (an edit forgets it, an export invents one for a recipe without).
    assert told == [(weighed, 687.5), (None, None)] and (store.food(weighed).count, store.food(plain).count) == (1, 0)


def test_what_the_phone_shows_of_a_weight_says_nothing_of_a_name_several_recipes_share(service):
    store, _ = soups(service)
    log = service.app.log
    service.app.log = lambda *asked: [replace(entry, weighed=False) for entry in log(*asked)]
    service.log("Soup (315 kcal) 1 serving, apple 100g")
    assert "Soup" not in store.weightless() and "Apple, Fresh" in store.weightless()
    assert store.food("Soup (201 kcal)").weightless is False


def test_the_phones_name_for_a_recipe_is_the_one_known_here_whatever_tells_it_apart(service):
    store, _ = soups(service)
    assert service._known("Soup") == "Soup"
    service.app.recipes = [
        ("Soup", "1 serving (687.5 g)", 201, 5, 4, 39),
        ("Soup", "1 serving", 315, 10, 16, 41),
        ("soup", "1 serving", 400, 9, 9, 9),
    ]
    store.db.execute("UPDATE library SET name='Soup ' WHERE kind='shown' AND name='Soup'")
    store.db.execute("UPDATE entries SET name='Soup '")
    store._changed()
    service.read_recipes(everything=True)  # the phone says "soup": still the name the entries here have
    assert list(store.listed_recipes()) == ["Soup "] and len(store.shared_recipes()["Soup "]) == 3


def test_a_recipe_used_as_an_ingredient_is_asked_for_as_itself(service):
    soups(service)
    items, described = service._ingredients(["Soup (315 kcal) 1 serving, apple 100g"])
    assert (items[0].name, items[0].library, items[0].shown["kcal"]) == ("Soup", True, 315)
    assert (items[1].name, items[1].library, items[1].shown) == ("Apple, Fresh", False, None)


def test_status_states_the_phone_s_app_and_screen_beside_those_mf_was_written_on(service, monkeypatch):
    """Stated, not judged: on anyone else's phone they differ, and that alone says nothing is wrong."""
    monkeypatch.setattr("mf.service.phone_environment", lambda serial, package: {"app": "5.9.0", "screen": "1080x2400"})
    status = service.status()
    assert (status["written_on"], status["this_phone"]) == ("MacroFactor 5.8.11, screen 720x1600", "MacroFactor 5.9.0, screen 1080x2400")
    service.app.fail_next = True
    with pytest.raises(AppError, match=r"^search results never appeared$"):
        service.log("apple 120g @8")
