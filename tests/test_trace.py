"""The trace: lines from the page, the server and the phone, read back as one story per act."""

import gzip
import json
import time

from fakes import fake_service
from fastapi.testclient import TestClient

from mf import server, trace


def test_lines_are_appended_with_their_context_and_never_raise(trace_dir):
    trace.emit("cli", "run", argv=["day"], nothing=None)
    with trace.within(job="p1-1"):
        trace.emit("phone", "input", cmd="input tap 1 2", ms=40)
    trace.emit("phone", "wait", what=object())  # not JSON: written as text rather than lost or raised
    first, second, third = trace.read()
    assert (first["src"], first["ev"], first["argv"]) == ("cli", "run", ["day"]) and "nothing" not in first
    assert second["job"] == "p1-1" and "job" not in third
    assert first["run"] == trace.RUN


def test_a_line_cut_short_is_skipped(trace_dir):
    trace.emit("cli", "run", argv=[])
    with next(trace_dir.glob("*.jsonl")).open("a") as handle:
        handle.write('{"t": 12, "src": "pa')
    assert len(trace.read()) == 1


def test_screen_observations_preserve_context_and_reuse_identical_xml(trace_dir):
    for job, action, xml in [("p1-1", 1, "<hierarchy/>"), ("p1-1", 1, "<hierarchy/>"), ("p1-2", 2, "<hierarchy rotation='0'/>")]:
        with trace.within(job=job):
            trace.screen(xml, action=action, ms=3)
    events = trace.read()
    assert [e["job"] for e in events] == ["p1-1", "p1-1", "p1-2"]
    assert [e["action"] for e in events] == [1, 1, 2]
    assert events[0]["file"] == events[1]["file"] != events[2]["file"]
    assert len(list(trace_dir.rglob("*.gz"))) == 2
    assert gzip.decompress((trace_dir / events[0]["file"]).read_bytes()).decode() == "<hierarchy/>"
    assert not list(trace_dir.rglob("*.tmp"))


def test_screens_are_kept_for_a_week_of_days(trace_dir):
    for day in range(1, 11):
        (trace_dir / "screens" / f"2020-01-{day:02d}").mkdir(parents=True)
    trace.screen("<hierarchy/>", action=1, ms=3)  # the first screen of a new day
    kept = sorted(p.name for p in (trace_dir / "screens").iterdir())
    assert len(kept) == trace.SCREEN_DAYS and kept[0] == "2020-01-05" and len(list(trace_dir.rglob("*.gz"))) == 1


def story() -> list[dict]:
    t = 1_700_000_000.0
    page = {"src": "page", "run": "s", "page": "p1"}
    return [
        {**page, "t": t - 60, "ev": "key", "key": "z", "in": "food"},  # long before: not part of the gesture
        {**page, "t": t, "ev": "key", "key": "g", "in": "food"},
        {**page, "t": t + 0.2, "ev": "key", "key": "a", "in": "food"},
        {**page, "t": t + 0.9, "ev": "key", "key": "Enter", "in": "food"},
        {**page, "t": t + 0.9, "ev": "act", "act": "log", "via": "line", "what": "Galettes 7.67 g ×3", "op": "p1-1"},
        {
            "t": t + 1.0,
            "src": "server",
            "run": "s",
            "ev": "job",
            "job": "p1-1",
            "kind": "log",
            "label": "Galettes",
            "status": "queued",
        },
        {"t": t + 3.5, "src": "server", "run": "s", "ev": "job", "job": "p1-1", "kind": "log", "status": "running", "wait_ms": 2500},
        {"t": t + 4.0, "src": "phone", "run": "s", "ev": "input", "job": "p1-1", "cmd": "input tap 1 2", "ms": 50},
        {
            "t": t + 9.0,
            "src": "phone",
            "run": "s",
            "ev": "wait",
            "job": "p1-1",
            "what": "search results",
            "ms": 4100,
            "reads": 9,
            "ok": True,
        },
        {"t": t + 20.0, "src": "server", "run": "s", "ev": "job", "job": "p1-1", "kind": "log", "status": "done", "run_ms": 16500},
        {**page, "t": t + 30, "ev": "click", "in": "day", "on": "plus", "px": 412.4},
        {**page, "t": t + 30, "ev": "act", "act": "log", "via": "day", "what": "Apple 150 g", "op": "p1-2"},
    ]


def test_timeline_joins_an_act_with_its_inputs_and_its_job():
    typed, clicked = trace.timeline(story())
    assert typed["keys"] == ["g", "a", "Enter"] and typed["clicks"] == 0 and typed["seconds"] == 0.9
    assert typed["job"]["status"] == "done" and typed["job"]["inputs"] == 1 and typed["job"]["waits"][0]["what"] == "search results"
    assert (clicked["keys"], clicked["clicks"], clicked["px"]) == ([], 1, 412) and "job" not in clicked
    text = trace.render([typed, clicked])
    assert "ga↵" in text and "slowest: search results 4.1 s" in text and "1 click (412 px)" in text


def test_a_food_taken_to_the_line_keeps_its_inputs_for_the_log_that_follows():
    t, page = 1_700_000_000.0, {"src": "page", "run": "s", "page": "p1"}
    events = [
        {**page, "t": t, "ev": "click", "in": "day", "on": "n", "px": 300},
        {**page, "t": t, "ev": "act", "act": "take", "via": "day", "what": "Milk"},
        {**page, "t": t + 1, "ev": "key", "key": "6", "in": "amount"},
        {**page, "t": t + 2, "ev": "key", "key": "Enter", "in": "amount"},
        {**page, "t": t + 2, "ev": "act", "act": "log", "via": "line", "what": "Milk 6 g"},
    ]
    take, log = trace.timeline(events)
    assert (take["clicks"], log["clicks"], log["keys"], log["seconds"]) == (1, 1, ["6", "Enter"], 2.0)


def test_stats_group_acts_by_kind_and_route():
    data = trace.stats(trace.timeline(story()))
    assert data["acts"]["log/line"] == {"n": 1, "keys": 3, "clicks": 0, "seconds": 0.9, "phone_s": 16.5, "phone_max_s": 16.5}
    assert data["acts"]["log/day"]["clicks"] == 1
    assert data["waits"][0] == {"what": "search results", "n": 1, "total_s": 4.1, "max_s": 4.1}


def test_the_page_posts_its_events_and_names_the_job_it_starts(monkeypatch, store):
    monkeypatch.setattr(server, "GRACE", 0.0)
    client = TestClient(server.create_app(server.Worker(fake_service(store))))
    events = [{"t": 1_700_000_000.5, "ev": "key", "key": "a", "in": "food"}, {"ev": "act", "act": "log", "op": "p9-4"}, "junk"]
    assert client.post("/api/trace", content=json.dumps({"page": "p9", "events": events})).status_code == 200
    headers = {"X-Mf-Key": "p9-4", "X-Mf-Asked": str(time.time()), "X-Mf-Build": client.get("/api/state").json()["build"]}
    args = {"items": [{"name": "Apple, Fresh", "quantity": 120, "unit": "g"}], "time": "08:00"}
    assert client.post("/api/jobs", json={"kind": "log", "args": args}, headers=headers).status_code == 200
    deadline = time.monotonic() + 5
    lines = []
    while time.monotonic() < deadline and not any(e.get("status") == "done" for e in lines):
        time.sleep(0.02)
        lines = [e for e in trace.read() if e["src"] == "server"]
    events = [e for day in trace.TRACE_DIR.glob("*.jsonl") for e in map(json.loads, day.read_text().splitlines())]
    key = next(e for e in events if e["ev"] == "key")
    assert (key["page"], key["key"], key["t"]) == ("p9", "a", 1_700_000_000.5)
    assert [e["status"] for e in lines] == ["queued", "running", "done"]  # running and done come from the phone's thread
    assert all(e["job"] == "p9-4" for e in lines)
    act = next(a for a in trace.timeline(events) if a["act"] == "log" and a["src"] == "page")
    assert act["job"]["status"] == "done"  # the act and what the phone did about it read as one


def test_a_running_server_serves_the_page_it_started_with(monkeypatch, tmp_path, store):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text('<html lang="en">old</html>')
    (web / "app.js").write_text("old")
    monkeypatch.setattr(server, "WEB", web)
    worker = server.Worker(fake_service(store))
    client = TestClient(server.create_app(worker))
    build = client.get("/api/state").json()["build"]
    (web / "app.js").write_text("new")  # edited while the server runs
    assert client.get("/app.js").text == "old"
    assert client.get("/app.js").headers["content-type"].startswith("text/javascript")
    assert f'data-build="{build}"' in client.get("/").text
    assert client.get("/nothing.js").status_code == 404
    later = TestClient(server.create_app(worker))  # a server started now has the new page
    assert later.get("/app.js").text == "new" and later.get("/api/state").json()["build"] != build
