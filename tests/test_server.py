"""The server: one job at a time, in order, each asked once and never late; what a page is told is what the store holds."""

import threading
import time
from datetime import timedelta

import pytest
from conftest import TODAY
from fakes import fake_service
from fastapi.testclient import TestClient

from mf import server
from mf.app import AppError, Entry
from mf.device import Cancelled, PhoneError


@pytest.fixture
def worker(store, monkeypatch):
    monkeypatch.setattr(server, "GRACE", 0.0)
    service = fake_service(store)
    service.taken_back = []

    def take_back(result):
        assert service.stop is None  # the original cancellation must not stop the reversing write
        service.taken_back.append(result)
        if result.get("stuck"):
            raise AppError("Remove button not found")
        return "op" in result

    service.take_back = take_back
    service.calls = {}  # key -> what that job does, for the tests that drive the worker with plain functions
    return server.Worker(service, run=run)


def run(service, job):
    return service.calls[job["key"]](service) if job["key"] in service.calls else server.run_job(service, job)


def wait(worker, key, timeout=5.0):
    """The job once it has ended."""
    deadline = time.monotonic() + timeout
    while (job := worker.store.job(key))["status"] in ("queued", "running"):
        assert time.monotonic() < deadline, f"job {key} still {job['status']}"
        time.sleep(0.005)
    return job


def submit(worker, key, call, kind="log"):
    worker.service.calls.setdefault(key, call)
    worker.submit(key, kind, key, {})
    return key


def test_jobs_run_one_at_a_time_in_the_order_asked(worker):
    ran = []
    keys = [submit(worker, f"k{n}", lambda service, n=n: ran.append(n) or {"n": n * 10}) for n in range(5)]
    assert [wait(worker, key)["result"] for key in keys] == [{"n": n * 10} for n in range(5)]
    assert ran == [0, 1, 2, 3, 4]


def test_concurrent_submissions_run_in_database_order_even_if_the_first_notification_is_late(worker, monkeypatch):
    recorded, release = threading.Event(), threading.Event()
    trace_job = server.trace.emit

    def trace(*args, **fields):
        if fields.get("job") == "first" and fields.get("status") == "queued":
            recorded.set()
            assert release.wait(5)
        trace_job(*args, **fields)

    monkeypatch.setattr(server.trace, "emit", trace)
    ran = []
    first = threading.Thread(target=submit, args=(worker, "first", lambda s: ran.append("first")))
    first.start()
    try:
        assert recorded.wait(5)
        submit(worker, "second", lambda s: ran.append("second"))
        assert wait(worker, "second")["status"] == "done"
        assert ran == ["first", "second"]
    finally:
        release.set()
        first.join(5)


def test_a_failure_is_reported_and_the_next_job_still_runs(worker):
    def refuse(service):
        raise AppError("food not found")

    def crash(service):
        raise ZeroDivisionError("oops")

    submit(worker, "a", refuse)
    submit(worker, "b", crash)
    submit(worker, "c", lambda service: {"ok": True})
    assert wait(worker, "c")["status"] == "done"
    assert (wait(worker, "a")["status"], wait(worker, "a")["error"]) == ("failed", "food not found")
    assert wait(worker, "b")["error"] == "ZeroDivisionError: oops"
    assert [job["key"] for job in worker.store.open_jobs()] == ["a", "b"]  # they ask for attention until dismissed


def test_asked_again_under_the_same_name_nothing_is_done_twice(worker):
    ran = []
    submit(worker, "page-1", lambda s: ran.append(1))
    submit(worker, "page-1", lambda s: ran.append(2))
    submit(worker, "page-2", lambda s: ran.append(3))
    wait(worker, "page-2")
    assert ran == [1, 3]
    with pytest.raises(PhoneError, match="already running"):
        server.Worker(worker.service)  # one worker for one store
    worker.lock.close()  # the server went away
    again = server.Worker(worker.service, run=run)  # one started over the same store still knows the name
    submit(again, "page-1", lambda s: ran.append(4))
    submit(again, "page-3", lambda s: ran.append(5))
    wait(again, "page-3")
    assert ran == [1, 3, 5]


def held(started, release):
    """A job that says when the phone is on it, and stays there until released."""

    def body(service):
        started.set()
        release.wait(5)

    return body


def test_a_waiting_job_is_dropped_and_never_runs(worker):
    started, release, ran = threading.Event(), threading.Event(), []
    submit(worker, "first", held(started, release))
    submit(worker, "second", lambda service: ran.append("second"))
    submit(worker, "third", lambda service: ran.append("third"))
    assert started.wait(5)
    assert worker.cancel("second") and worker.store.job("second")["status"] == "cancelled"
    assert not worker.cancel("second") and not worker.cancel("nobody")
    release.set()
    wait(worker, "third")
    assert wait(worker, "first")["status"] == "done" and ran == ["third"]
    assert not worker.cancel("first")  # finished: that is what undo is for


def running(worker, key, body):
    """Submit a job that signals when the phone is on it, then does `body(service)`."""
    started = threading.Event()
    submit(worker, key, lambda service: (started.set(), body(service))[1])
    assert started.wait(5)
    return key


def stop_when_asked(service):
    deadline = time.monotonic() + 5
    while not service.stop():  # what the driver does between two actions
        assert time.monotonic() < deadline
        time.sleep(0.005)
    raise Cancelled()


def test_a_running_job_asked_in_time_stops_and_changes_nothing(worker):
    key = running(worker, "a", stop_when_asked)
    assert worker.cancel(key) and worker.store.job(key)["status"] == "running" and worker.store.job(key)["cancel"]
    job = wait(worker, key)
    assert (job["status"], job["note"]) == ("cancelled", "nothing was changed")
    assert worker.service.taken_back == []
    deadline = time.monotonic() + 5
    while worker.service.stop is not None:  # cleared a moment after the job is stored as ended
        assert time.monotonic() < deadline
        time.sleep(0.005)


def past_the_point_of_no_return(result):
    def body(service):
        deadline = time.monotonic() + 5
        while not service.stop():
            assert time.monotonic() < deadline
            time.sleep(0.005)
        service.finished(result)  # asked to stop, but it was already saved: the write finishes
        return result

    return body


def test_a_running_job_asked_too_late_is_finished_then_taken_back(worker):
    key = running(worker, "a", past_the_point_of_no_return({"op": 7}))
    worker.cancel(key)
    job = wait(worker, key)
    assert (job["status"], job["note"]) == ("cancelled", "it was already saved, and has been taken back")
    assert worker.service.taken_back == [{"op": 7}]


def test_what_cannot_be_taken_back_is_reported_as_done(worker):
    key = running(worker, "a", past_the_point_of_no_return({"deleted": "Lentil Stew"}))
    worker.cancel(key)
    job = wait(worker, key)
    assert job["status"] == "done" and "cannot be taken back" in job["note"]
    stuck = running(worker, "b", past_the_point_of_no_return({"op": 8, "stuck": True}))
    worker.cancel(stuck)
    job = wait(worker, stuck)
    assert job["status"] == "done" and "taking it back failed: Remove button not found" in job["note"]
    assert job["result"] == {"op": 8, "stuck": True}  # what was saved is still reported


def test_a_job_nobody_cancelled_is_never_taken_back(worker):
    submit(worker, "a", lambda service: {"op": 3})
    job = wait(worker, "a")
    assert (job["status"], job["note"], worker.service.taken_back) == ("done", None, [])


def test_a_new_log_waits_a_moment_in_which_it_can_be_cancelled(worker, monkeypatch):
    monkeypatch.setattr(server, "GRACE", 0.4)
    ran = []
    submit(worker, "slip", lambda service: ran.append("slip"))
    submit(worker, "sync", lambda service: ran.append("sync"), kind="sync")
    time.sleep(0.1)
    assert worker.store.job("slip")["status"] == "queued" and worker.cancel("slip")
    assert wait(worker, "sync")["status"] == "done" and ran == ["sync"]


def test_what_a_server_left_unfinished_is_failed_and_never_run_again(worker):
    started, release, ran = threading.Event(), threading.Event(), []
    submit(worker, "on-the-phone", held(started, release))
    submit(worker, "waiting", lambda service: ran.append("waiting"))
    assert started.wait(5)
    worker.store.interrupt_jobs()  # what the next server does as it starts
    jobs = {job["key"]: job for job in worker.store.open_jobs()}
    assert jobs["on-the-phone"]["status"] == jobs["waiting"]["status"] == "failed"
    assert "look at the day" in jobs["on-the-phone"]["error"] and "before the phone started" in jobs["waiting"]["error"]
    release.set()
    time.sleep(0.05)
    assert ran == []


# ---------------------------------------------------------------- over HTTP, as the page asks


@pytest.fixture
def page(worker):
    """Ask the server as the page does: `page(path, body)` posts with the three headers and returns the response."""
    client = TestClient(server.create_app(worker))
    build = client.get("/api/state").json()["build"]
    count = iter(range(1, 1000))

    def ask(path, body=None, key=None, asked=None, build=build):
        headers = {"X-Mf-Key": key or f"p-{next(count)}", "X-Mf-Asked": str(asked or time.time()), "X-Mf-Build": build}
        return client.post(f"{path}?day={TODAY}", json=body or {}, headers=headers)

    ask.state = lambda: client.get(f"/api/state?day={TODAY}").json()
    ask.worker = worker
    ask.get = client.get
    return ask


def log(page, text="Apple, Fresh", quantity=120, **more):
    args = {"items": [{"name": text, "quantity": quantity, "unit": "g"}], "date": TODAY.isoformat(), "time": "08:00"}
    return page("/api/jobs", {"kind": "log", "args": args}, **more)


def settled(page):
    """The state once the phone has nothing left to do."""
    deadline = time.monotonic() + 5
    while True:
        state = page.state()
        if not any(job["status"] in ("queued", "running") for job in state["jobs"]):
            return state
        assert time.monotonic() < deadline
        time.sleep(0.005)


def names(state):
    return [(entry["time"], entry["name"], entry["quantity"]) for entry in state["day"]["entries"]]


def test_a_log_shows_as_a_job_then_as_an_entry_and_never_as_both(page):
    page.worker.service.app.log = slow(page.worker.service.app.log)
    asked = log(page, key="p-a").json()
    assert [(job["key"], job["kind"], job["args"]["items"][0]["name"]) for job in asked["jobs"]] == [("p-a", "log", "Apple, Fresh")]
    seen = set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and seen != {(True, False), (False, True)}:
        state = page.state()
        seen.add((bool(state["jobs"]), ("08:00", "Apple, Fresh", 120.0) in names(state)))
    assert seen == {(True, False), (False, True)}  # on its way and not saved, or saved and no longer on its way
    last = page.state()["last"]
    assert (last["key"], last["status"], last["ids"]) == ("p-a", "done", [page.state()["day"]["entries"][-1]["id"]])
    assert last["undo"] is not None


def slow(call):
    def slowly(*args, **kwargs):
        time.sleep(0.15)
        return call(*args, **kwargs)

    return slowly


def test_asked_twice_or_too_late_or_by_another_build_it_is_done_once_or_not_at_all(page):
    assert log(page, key="p-a").status_code == 200
    assert log(page, key="p-a").status_code == 200  # an answer was lost and the page asked again
    assert log(page, asked=time.time() - 45).status_code == 409  # soon, or not at all: seconds, not minutes
    late = log(page, asked=time.time() - 75 * 60)  # it waited in a browser's queue
    assert late.status_code == 409 and "too old" in late.json()["detail"]
    other = log(page, build="an-older-page")
    assert other.status_code == 409 and "reload" in other.json()["detail"]
    assert names(settled(page)) == [("08:00", "Apple, Fresh", 120.0)]
    assert log(page, text="No Such Food").status_code == 422 and page.state()["jobs"] == []  # refused before queueing


def test_invalid_requests_cannot_change_the_queue(page):
    headers = {"X-Mf-Key": "bad-day", "X-Mf-Asked": str(time.time()), "X-Mf-Build": page.state()["build"]}
    response = TestClient(server.create_app(page.worker)).post("/api/jobs?day=oops", json={"kind": "sync"}, headers=headers)
    assert response.status_code == 400
    assert page.worker.store.job("bad-day") is None
    for kind, args in (
        ("edit", {"quantity": 1}),
        ("sync", {"day": 1}),
        ("log", {"items": [None]}),
        ("recipe", {"name": "Soup", "items": [], "servings": 0}),
        ("log", {"items": [{"name": "Apple, Fresh", "quantity": -1}]}),
        ("log", {"items": [{"name": "Apple, Fresh", "repeat": 21}]}),
        ("remove", {"id": 2**64, "on": TODAY.isoformat()}),
    ):
        assert page("/api/jobs", {"kind": kind, "args": args}, key="bad").status_code == 422
    for asked in ("nan", time.time() + 3600):
        assert page("/api/jobs", {"kind": "sync"}, key="bad", asked=asked).status_code == 409
    assert page.worker.store.job("bad") is None


def test_python_changes_require_a_server_restart_then_a_page_reload(worker, tmp_path, monkeypatch):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text('<html lang="en"></html>')
    source = tmp_path / "app.py"
    source.write_text("original")
    monkeypatch.setattr(server, "WEB", web)
    client = TestClient(server.create_app(worker))
    original = client.get("/").content
    before = client.get("/api/state").json()
    assert not before["update_available"]
    source.write_text("repaired")
    changed = client.get("/api/state").json()
    assert changed["update_available"] and changed["build"] == before["build"]
    assert client.get("/").content == original  # reloading alone cannot replace running Python
    restarted = TestClient(server.create_app(worker))
    state = restarted.get("/api/state").json()
    assert not state["update_available"] and state["build"] != before["build"]
    headers = {"X-Mf-Asked": str(time.time()), "X-Mf-Build": before["build"], "X-Mf-Key": "stale"}
    assert restarted.post("/api/jobs", json={"kind": "sync"}, headers=headers).status_code == 409
    assert worker.store.job("stale") is None


def test_a_failure_stays_until_dismissed_and_only_one_retry_of_it_is_taken(page):
    page.worker.service.app.fail_next = True
    log(page, key="p-a")
    failed = settled(page)["jobs"]
    assert [(job["key"], job["status"], job["error"]) for job in failed] == [("p-a", "failed", "search results never appeared")]
    assert page("/api/jobs/p-a/retry", key="p-b").status_code == 200  # this tab asks again
    assert page("/api/jobs/p-a/retry", key="q-b").status_code == 409  # another tab does too: refused
    assert page("/api/jobs/p-a/retry", key="p-b").status_code == 200  # the first one's answer was lost: the same job
    state = settled(page)
    assert state["jobs"] == [] and names(state) == [("08:00", "Apple, Fresh", 120.0)]
    page.worker.service.app.fail_next = True
    log(page, key="p-c")
    settled(page)
    assert page("/api/jobs/p-c/dismiss").json()["jobs"] == []


def test_a_sync_asked_from_a_page_answers_what_failed_on_that_day_before_it(page):
    """Seen: a log refused at 14:51 for a reason since put right was still told after the day was synced."""
    other = (TODAY + timedelta(days=1)).isoformat()
    page.worker.service.app.fail_next = True
    log(page, key="p-a")
    settled(page)
    page.worker.service.app.fail_next = True
    args = {"items": [{"name": "Apple, Fresh", "quantity": 120, "unit": "g"}], "date": other, "time": "08:00"}
    page("/api/jobs", {"kind": "log", "args": args}, key="p-b")
    assert [job["key"] for job in settled(page)["jobs"]] == ["p-a", "p-b"]
    page.worker.service.sync_days(TODAY)  # a read nobody at a page asked for: the failure has not been seen
    assert [job["key"] for job in page.state()["jobs"]] == ["p-a", "p-b"]
    page("/api/jobs", {"kind": "sync", "args": {"day": TODAY.isoformat()}}, key="p-s")
    assert [job["key"] for job in settled(page)["jobs"]] == ["p-b"]  # that day's is answered; another day's stays
    page.worker.service.app.fail_next = True
    log(page, key="p-c")
    assert [job["key"] for job in settled(page)["jobs"]] == ["p-b", "p-c"]  # what fails after it is told as ever
    # One still waiting its turn when Sync was pressed, failing after that: nobody has seen it.
    store = page.worker.store
    store.seen_failures(TODAY.isoformat(), store.job("p-c")["finished"] - 0.001)
    assert [job["key"] for job in page.state()["jobs"]] == ["p-b", "p-c"]


def test_undo_names_what_it_undoes(page):
    log(page, key="p-a")
    first = settled(page)["last"]["undo"]
    log(page, text="Skyr Nature 0%", quantity=100, key="p-b")
    assert settled(page)["last"]["undo"] != first
    asked = page("/api/jobs", {"kind": "undo", "args": {"op": first}}, key="p-u").json()  # the apple, though the skyr came after
    undo = next(job for job in asked["jobs"] if job["key"] == "p-u")
    assert undo["label"] == "log Apple, Fresh 120 g" and [part["kind"] for part in undo["args"]["parts"]] == ["remove"]
    state = settled(page)
    assert names(state) == [("08:00", "Skyr Nature 0%", 100.0)]
    assert (state["last"]["key"], state["last"]["undo"]) == ("p-u", None)
    again = page("/api/jobs", {"kind": "undo", "args": {"op": first}}, key="p-v")
    assert again.status_code == 409  # already undone


def test_a_retry_that_cannot_be_asked_leaves_the_failure_to_be_seen(page):
    page.worker.service.app.fail_next = True
    log(page, key="p-a")
    settled(page)
    page.worker.store.db.execute("DELETE FROM entries WHERE name='Apple, Fresh'")  # the food is no longer known
    page.worker.store._local.foods = None
    assert page("/api/jobs/p-a/retry", key="p-b").status_code == 422
    assert [job["key"] for job in page.state()["jobs"]] == ["p-a"]


def test_a_change_to_an_entry_is_told_as_asked_and_done_on_the_entry(page):
    log(page)
    entry = settled(page)["day"]["entries"][0]
    asked = page("/api/jobs", {"kind": "edit", "args": {"id": entry["id"], "on": TODAY.isoformat(), "quantity": 200}}, key="p-e").json()
    edit = next(job for job in asked["jobs"] if job["key"] == "p-e")["args"]
    assert edit == {"id": entry["id"], "name": "Apple, Fresh", "on": TODAY.isoformat(), "quantity": 200.0, "unit": None}
    page("/api/jobs", {"kind": "retime", "args": {"id": entry["id"], "on": TODAY.isoformat(), "time": "10:30"}})
    assert names(settled(page)) == [("10:30", "Apple, Fresh", 200.0)]
    page("/api/jobs", {"kind": "remove", "args": {"id": entry["id"], "on": TODAY.isoformat()}})
    assert names(settled(page)) == []
    assert page("/api/jobs", {"kind": "remove", "args": {"id": entry["id"], "on": TODAY.isoformat()}}).status_code == 404
    assert page("/api/jobs/nobody/cancel").status_code == 409


def test_the_sync_button_reads_the_day_from_the_phones_screen(page):
    log(page)
    settled(page)
    page.worker.service.app.rows.append(Entry(TODAY, 13, "Galettes De Riz", "30 g", 115, 2, 1, 24, 5))  # logged on the phone itself
    asked = page("/api/jobs", {"kind": "sync", "args": {"day": TODAY.isoformat()}}, key="p-s").json()
    assert [(job["kind"], job["args"]) for job in asked["jobs"] if job["key"] == "p-s"] == [("sync", {"day": TODAY.isoformat()})]
    state = settled(page)
    assert names(state) == [("08:00", "Apple, Fresh", 120.0), ("13:05", "Galettes De Riz", 30.0)]
    assert (state["last"]["key"], state["last"]["status"], state["last"]["undo"]) == ("p-s", "done", None)
    assert page("/api/jobs", {"kind": "sync", "args": {"day": "soon"}}).status_code == 400


def test_a_sync_is_of_a_day_or_of_the_recipes_or_an_export_of_the_week(page, monkeypatch):
    """Three readings, each asked for by name: the day's alone is the quick one, and it does not open the Library."""
    service = page.worker.service
    service.app.recipes = [("Bean Soup", "1 serving (450 g)", 364, 17, 6, 59)]
    page("/api/jobs", {"kind": "sync", "args": {"day": TODAY.isoformat()}}, key="p-d")
    settled(page)
    assert "recipes" not in service.app.calls and service.store.food("Bean Soup") is None
    page("/api/jobs", {"kind": "sync", "args": {"what": "recipes"}}, key="p-r")
    last = settled(page)["last"]
    assert (last["label"], last["status"]) == ("recipes with the phone's list", "done") and service.store.food("Bean Soup").kind == "recipe"
    exported = []

    def export(period):  # as the real one: it tells the front end what it stored, and cannot be stopped once asked
        exported.append((period, service.stop))
        service.finished({"rows": 0})
        return {"rows": 0}

    monkeypatch.setattr(service, "sync_export", export)
    page("/api/jobs", {"kind": "sync", "args": {"what": "week"}}, key="p-w")
    last = settled(page)["last"]
    assert (last["label"], last["status"]) == ("the last 7 days from an export", "done") and exported == [("week", None)]
    assert page("/api/jobs", {"kind": "sync", "args": {"what": "all"}}).status_code == 422  # all time is a decision for the terminal
    assert page.state()["exports_left"] > 0
    service.store.set("exports_refused", str(time.time()))  # MacroFactor said no: none is left, whatever was counted
    assert page.state()["exports_left"] == 0


def test_committed_screen_sync_retires_the_failed_reads_it_covers_and_from_a_page_what_failed_on_its_day(page, monkeypatch):
    service = page.worker.service
    store = service.store
    tomorrow = TODAY + timedelta(days=1)

    def unreadable(day):
        raise AppError("incomplete screen")

    with monkeypatch.context() as m:
        m.setattr(service.app, "read_day", unreadable)
        for key, day in (("today", TODAY), ("tomorrow", tomorrow)):
            page("/api/jobs", {"kind": "sync", "args": {"day": day.isoformat()}}, key=key)
        assert {j["key"] for j in settled(page)["jobs"]} == {"today", "tomorrow"}
    store.add_job("write", "log", "write", {"date": TODAY.isoformat()})
    store.end_job("write", "failed", error="before saving")

    service.day(TODAY, live=True)
    assert len(page.state()["jobs"]) == 3
    service.sync_days(TODAY)  # not asked from a page: the failed read is superseded, the failed write still to be seen
    assert {j["key"] for j in page.state()["jobs"]} == {"tomorrow", "write"}
    page("/api/jobs", {"kind": "sync", "args": {"day": TODAY.isoformat()}})
    assert {j["key"] for j in settled(page)["jobs"]} == {"tomorrow"}
    assert page("/api/jobs/today/retry").status_code == 409
    archived = store.job("today")
    assert (archived["status"], archived["error"], archived["dismissed"]) == ("failed", "incomplete screen", 1)
    service.sync_days(TODAY, tomorrow)
    assert page.state()["jobs"] == []


def test_the_state_says_when_the_catalog_has_changed_and_what_can_be_asked_again(page):
    was = page.state()["catalog"]
    assert page.get("/api/catalog").json()["version"] == was
    log(page)
    assert settled(page)["catalog"] > was and page.get("/api/catalog").json()["version"] == page.state()["catalog"]
    page.worker.store.set_alias("zz", "Apple, Fresh")  # as `mf alias` does, from another process
    assert page.get("/api/catalog").json()["version"] == page.state()["catalog"] > was + 1
    page.worker.service.app.fail_next = True
    log(page, key="p-f")
    (failed,) = settled(page)["jobs"]
    assert (failed["status"], failed["retry"]) == ("failed", True)


def test_a_saved_but_unconfirmed_log_is_settled_by_a_sync_of_its_day_never_retried(page):
    phone = page.worker.service.app
    phone.fail_after_save = True
    log(page, key="uncertain")
    (job,) = settled(page)["jobs"]
    assert (job["status"], job["retry"], job["check"]) == ("uncertain", False, TODAY.isoformat())
    assert page.state()["day"]["uncertain"]
    assert len(phone.rows) == 1 and names(page.state()) == []
    assert page("/api/jobs/uncertain/retry", key="duplicate").status_code == 409
    assert len(phone.rows) == 1
    assert page("/api/jobs", {"kind": "sync", "args": {"day": job["check"]}}, key="read").status_code == 200  # the page's Check
    state = settled(page)
    assert state["jobs"] == [] and names(state) == [("08:00", "Apple, Fresh", 120.0)]
    assert len(phone.rows) == 1 and state["last"]["kind"] == "sync" and state["last"]["undo"] is None
    assert not state["day"]["uncertain"]
    assert page("/api/jobs/uncertain/retry", key="still-no").status_code == 409
    assert log(page, key="uncertain").status_code == 200  # its identity still cannot start another write
    assert len(phone.rows) == 1


def test_dismiss_acknowledges_an_error_but_only_sync_clears_uncertainty(page):
    page.worker.service.app.fail_after_save = True
    log(page, key="uncertain")
    settled(page)
    page("/api/jobs/uncertain/dismiss")
    assert page.state()["jobs"] == [] and page.state()["day"]["uncertain"]
    page("/api/jobs", {"kind": "sync", "args": {"day": TODAY.isoformat()}}, key="sync")
    state = settled(page)
    assert not state["day"]["uncertain"] and len(state["day"]["entries"]) == 1
    assert page.worker.store.job("uncertain")["status"] == "reconciled"
    assert page("/api/jobs/uncertain/retry").status_code == 409


def test_failure_to_mirror_a_saved_write_is_uncertain_and_survives_restart(page, monkeypatch):
    store = page.worker.store
    real = store.add_entry

    def fail(**kwargs):
        raise OSError("disk full after the saving tap")

    monkeypatch.setattr(store, "add_entry", fail)
    log(page, key="disk-full")
    (job,) = settled(page)["jobs"]
    assert job["status"] == "uncertain" and len(page.worker.service.app.rows) == 1
    monkeypatch.setattr(store, "add_entry", real)
    store.interrupt_jobs()
    assert page("/api/jobs/disk-full/retry").status_code == 409


def test_restart_preserves_the_save_boundary_and_never_retries_uncertain_work(page):
    store = page.worker.store
    args = {"items": [{"name": "Apple, Fresh", "quantity": 120, "unit": "g"}], "date": TODAY.isoformat(), "time": "08:00"}
    for key in ("before", "after"):
        store.add_job(key, "log", "Apple", args)
        store.set_job(key, status="running", saving=int(key == "after"))
    store.interrupt_jobs()
    jobs = {j["key"]: j for j in page.state()["jobs"]}
    assert jobs["before"]["retry"] and jobs["before"]["status"] == "failed"
    assert not jobs["after"]["retry"] and jobs["after"]["status"] == "uncertain"
    assert page("/api/jobs/after/retry").status_code == 409


def test_cannot_save_when_the_job_marker_cannot_be_persisted(page, monkeypatch):
    store = page.worker.store
    real = store.set_job

    def fail(key, **fields):
        if fields.get("saving"):
            raise OSError("cannot persist save intent")
        return real(key, **fields)

    monkeypatch.setattr(store, "set_job", fail)
    log(page, key="before-save")
    assert settled(page)["jobs"][0]["status"] == "failed"
    assert page.worker.service.app.rows == []


def test_queued_edit_and_retry_cannot_follow_a_deleted_entry(page):
    log(page)
    old = settled(page)["day"]["entries"][0]
    target = {"id": old["id"], "on": old["date"]}
    started, release = threading.Event(), threading.Event()
    submit(page.worker, "hold", held(started, release))
    assert started.wait(5)
    try:
        assert page("/api/jobs", {"kind": "remove", "args": target}).is_success
        assert log(page).is_success
        assert page("/api/jobs", {"kind": "edit", "args": {**target, "quantity": 55}}, key="stale").is_success
    finally:
        release.set()
    state = settled(page)
    (replacement,) = state["day"]["entries"]
    assert replacement["id"] != old["id"] and replacement["quantity"] == old["quantity"]
    (failed,) = state["jobs"]
    assert failed["key"] == "stale" and failed["status"] == "failed" and not failed["retry"]
    assert page("/api/jobs/stale/retry").status_code == 409
    assert page("/api/jobs", {"kind": "remove", "args": target}).status_code == 404
    assert page("/api/jobs", {"kind": "remove", "args": {**target, "id": replacement["id"], "on": "2000-01-01"}}).status_code == 404


def test_undo_goes_back_through_the_changes_one_at_a_time(page):
    """Seen in use: two time changes, Undo, Undo. The second press must reach the first change, not cancel the undo."""
    log(page, key="u-a")
    entry = next(e for e in settled(page)["day"]["entries"] if e["id"] in page.state()["last"]["ids"])
    mine = lambda state: next((e for e in state["day"]["entries"] if e["id"] == entry["id"]), None)  # noqa: E731
    for key, at in (("u-b", "08:10"), ("u-c", "08:20")):
        page("/api/jobs", {"kind": "retime", "args": {"id": entry["id"], "on": entry["date"], "time": at}}, key=key)
        settled(page)
    state = page.state()
    assert state["undo"]["label"].endswith("08:20") and state["undo"]["op"] == state["last"]["undo"]
    page("/api/jobs", {"kind": "undo", "args": {"op": state["undo"]["op"]}}, key="u-1")
    state = settled(page)
    assert mine(state)["time"] == "08:10" and state["last"]["undo"] is None
    assert state["undo"]["label"].endswith("08:10")  # the change before: still there to take back
    page("/api/jobs", {"kind": "undo", "args": {"op": state["undo"]["op"]}}, key="u-2")
    state = settled(page)
    assert mine(state)["time"] == "08:00" and state["undo"]["kind"] == "log"
    page("/api/jobs", {"kind": "undo", "args": {"op": state["undo"]["op"]}}, key="u-3")
    state = settled(page)
    assert mine(state) is None and state["undo"] is None  # nothing left of this sitting


def test_a_sync_does_not_end_what_undo_can_reach(page):
    log(page, key="s-a")
    first = settled(page)["undo"]
    assert first["kind"] == "log" and first["days"] == [TODAY.isoformat()]
    page("/api/jobs", {"kind": "sync", "args": {"day": TODAY.isoformat()}}, key="s-s")
    state = settled(page)
    assert state["last"]["kind"] == "sync" and state["last"]["undo"] is None
    assert state["undo"] == first  # the log still stands, and is still the latest change


def test_the_line_says_when_a_restored_entry_came_back_with_other_figures(page):
    log(page, key="n-a")
    entry = next(e for e in settled(page)["day"]["entries"] if e["id"] in page.state()["last"]["ids"])
    page("/api/jobs", {"kind": "remove", "args": {"id": entry["id"], "on": entry["date"]}}, key="n-r")
    removed = settled(page)["undo"]
    page.worker.service.app.price = lambda item: (999, 9, 9, 9)  # its recipe was edited in between
    page("/api/jobs", {"kind": "undo", "args": {"op": removed["op"]}}, key="n-u")
    last = settled(page)["last"]
    assert last["status"] == "done" and "999 kcal" in (last["note"] or "")


def test_an_unconfirmed_save_is_settled_for_the_page_whoever_read_the_phone(page):
    """What is unconfirmed is the journal's to say; the job only tells the page of it."""
    service, store = page.worker.service, page.worker.service.store
    service.app.fail_after_save = True
    log(page, key="uncertain")
    assert settled(page)["jobs"][0]["status"] == "uncertain" and store.uncertain_days() == {TODAY.isoformat()}
    service.sync_days(TODAY)  # as `mf sync` does, from another process
    state = page.state()
    assert state["jobs"] == [] and not state["day"]["uncertain"] and store.job("uncertain")["status"] == "reconciled"
    # And if that reading came between the write's failure and its job's end, the job ends with nothing left to ask.
    store.add_job("late", "log", "late", {"date": TODAY.isoformat()})
    store.set_job("late", status="running", saving=store.job("uncertain")["saving"])
    store.end_job("late", "uncertain", error="could not be confirmed")
    assert store.job("late")["status"] == "reconciled" and page.state()["jobs"] == []


def test_one_of_several_recipes_of_a_name_is_asked_for_by_what_tells_it_apart_and_its_entry_says_which_it_is(page):
    service = page.worker.service
    service.app.recipes = [("Soup", "1 serving (687.5 g)", 201, 5, 4, 39), ("Soup", "1 serving", 315, 10, 16, 41)]
    service.read_recipes(everything=True)
    foods = {food["name"]: food for food in page.get("/api/catalog").json()["foods"]}
    assert foods["Soup (315 kcal)"]["shared"]["name"] == "Soup" and "Soup" not in foods
    args = {"items": [{"name": "Soup", "quantity": 1.0, "unit": "serving"}], "date": TODAY.isoformat(), "time": "08:00"}
    refused = page("/api/jobs", {"kind": "log", "args": args}, key="which")
    assert refused.status_code == 422 and "say which: Soup (201 kcal); Soup (315 kcal)" in refused.json()["detail"]
    args["items"][0]["name"] = "Soup (315 kcal)"
    assert page("/api/jobs", {"kind": "log", "args": args}, key="that").status_code == 200
    (entry,) = settled(page)["day"]["entries"]
    assert (entry["name"], entry["food"], entry["kcal"], entry["grams"]) == ("Soup", "Soup (315 kcal)", 315, None)
