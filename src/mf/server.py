"""Web UI backend. The server holds everything that outlives a keystroke; the page draws what it is told.

    GET  /api/state?day=D           one consistent reading: the day, the jobs that still ask for attention, the last
                                    one that ended (and whether it can be undone), the phone, the page's build, the
                                    catalog's number (another than the page's: it fetches the catalog again), and
                                    `at`, which orders readings: a page never replaces one with an earlier one
    POST /api/jobs                  {kind, args}: ask for something on the phone
    POST /api/jobs/K/cancel         stop it: dropped, stopped before saving, or taken back
    POST /api/jobs/K/retry          a failed one, asked again as a new job
    POST /api/jobs/K/dismiss        a failed one, seen
    GET  /api/catalog               the foods, to search and price them without a round trip
    POST /api/trace                 what was done on the page (see trace.py)

Every POST to /api/jobs… answers with the state, and carries:
    X-Mf-Asked   when it was asked (epoch seconds). Older than `FRESH`, it is refused: nothing acts late.
    X-Mf-Build   the page's build. Another one than the server's is refused: the two speak the same language or not at all.
    X-Mf-Key     where it asks for a job (a new one, or a retry): the page's name for it. A job's key is that name, and
                 the jobs are kept in the store: asked again under it (an answer was lost, a server restarted), it is
                 the same job, never a second.

The phone does one thing at a time, so jobs run one after another on a thread that owns it. A job a server left
unfinished is marked failed or uncertain according to its durable save marker, and is never run again.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import mimetypes
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from . import trace
from .device import Cancelled, PhoneError, Uncertain
from .parse import SpecError, parse_day, parse_time
from .requests import JobRequest
from .service import Service, serving_text, undo_parts, undoable
from .store import DatabaseFormatError, Store, affected_days

WEB = Path(__file__).parent / "web"
GRACE = 2.5  # seconds a new log waits before the phone starts on it; the phone needs 15-30 s anyway
FRESH = 30.0  # seconds after which what a page asked is no longer acted on
RECENT = 3600.0  # seconds the last finished job is still told (and can be undone from the page)
Key = Annotated[
    str, Header(alias="X-Mf-Key", min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
]  # the page's name for the job it asks for


class Worker:
    """Runs jobs one after another on a thread that owns the phone. What a job is and how it stands is in the store."""

    def __init__(self, service: Service | None = None, run: Callable[[Service, dict], Any] | None = None) -> None:
        self.service = service or Service()
        self.run = run or run_job  # how a job is carried out, from what the store holds of it
        self.store = self.service.store
        # One worker for one store: it owns the jobs there, and as it starts it fails those an earlier one left unfinished.
        self.lock = self.store.server_lock()
        self.service.say = self._say
        self.service.finished = self._finished
        self.service.saving = self._saving
        self._wake = threading.Event()  # SQLite holds the queue; this only wakes an idle worker
        self._current: str | None = None  # the job the phone is on; touched by the phone's thread only
        self.store.interrupt_jobs()
        threading.Thread(target=self._run, daemon=True, name="mf-phone").start()

    def submit(self, key: str, kind: str, label: str, args: dict, retry_of: str | None = None) -> None:
        """Queue a job, unless one of that key exists: then it was already asked, and nothing is done twice."""
        with self.store.atomic():
            if self.store.job(key) is not None:
                return
            if retry_of is not None:
                failed = self.store.job(retry_of)
                if failed is None or not _told(failed, self.store)["retry"] or not self.store.dismiss_job(retry_of):
                    raise HTTPException(409, "that cannot be retried; check the phone if the save is uncertain")
            added = self.store.add_job(key, kind, label, args)
        if added:
            trace.emit("server", "job", job=key, kind=kind, label=label, status="queued")
            self._wake.set()

    def _saving(self, op: int) -> None:
        if self._current is not None and not self.store.set_job(self._current, only="running", saving=op):
            raise PhoneError("the job is no longer running; no saving input was sent")

    def cancel(self, key: str) -> bool:
        """Cancel a job: dropped at once if the phone has not started on it, asked to stop if it is running (its
        final status tells how that went). False when it has already finished."""
        note = "the phone had not started on it"
        if self.store.end_job(key, "cancelled", only="queued", cancel=1, note=note):
            trace.emit("server", "job", job=key, status="cancelled", note=note, run_ms=0)
            return True
        return self.store.set_job(key, only="running", cancel=1)

    def _say(self, message: str) -> None:
        if self._current is not None:
            with contextlib.suppress(sqlite3.Error):  # a word of progress that could not be stored must not fail the write it tells of
                self.store.set_job(self._current, progress=message)

    def _finished(self, result: dict) -> None:
        """Called by the service as it stores what a write changed: the job ends in that same instant, so no reader
        sees an entry both saved and still on its way. A job asked to stop is left for `_do` to take back."""
        if self._current is not None and not (job := self.store.job(self._current))["cancel"]:
            if (
                job["kind"] == "sync" and "day" in job["args"]
            ):  # the page will show the day as the phone has it: that answers what failed there
                self.store.seen_failures(job["args"]["day"], job["created"])
            self.store.end_job(self._current, "done", result=result, note=result.get("note"))  # what the service has to say about it

    def _run(self) -> None:
        while True:
            self._wake.clear()
            key = self.store.next_queued()
            if key is None:
                self._wake.wait()
                continue
            try:
                with trace.within(job=key):  # this thread's phone lines belong to this job
                    self._do(key)
            except Exception as failure:  # noqa: BLE001 - the store could not be read or written: this job is lost, the thread is not
                error = f"{type(failure).__name__}: {failure}"
                status = "uncertain"
                with contextlib.suppress(Exception):
                    status = "uncertain" if self.store.job(key)["saving"] else "failed"
                    self.store.end_job(key, status, error=error)
                trace.emit("server", "job", job=key, status=status, error=error)
            finally:
                self._current, self.service.stop = None, None

    def _do(self, key: str) -> None:
        """One job, from its turn to its end. A job ends once (`store.end_job`): what `_finished` stored stands."""
        job = self.store.job(key)
        while job["kind"] == "log" and job["status"] == "queued" and time.time() - job["created"] < GRACE:
            time.sleep(0.1)  # a moment in which a slip of the hand can still be cancelled
            job = self.store.job(key)
        if not self.store.set_job(key, only="queued", status="running"):
            return  # cancelled while it waited
        self._current = key
        started = time.time()
        trace.emit("server", "job", kind=job["kind"], status="running", wait_ms=round((started - job["created"]) * 1000))
        result, error, note = None, None, None
        self.service.stop = lambda: self.store.job(key)["cancel"]
        try:
            result, status = self.run(self.service, job), "done"
        except Cancelled:
            status, note = "cancelled", "nothing was changed"
        except (SpecError, PhoneError) as failure:
            status, error = "failed", failure.reason if isinstance(failure, Uncertain) else str(failure)
        except Exception as failure:  # noqa: BLE001 - a job must never kill the worker
            status, error = "failed", f"{type(failure).__name__}: {failure}"
        if status in ("failed", "cancelled") and self.store.job(key)["saving"]:
            status = "uncertain"
            error = error or "saving may already have happened; check the phone"
        if status == "done" and self.store.job(key)["cancel"]:  # asked too late to simply stop: it is saved, so take it back
            self.service.stop = None  # the original cancel asks for this reversal; it must not cancel the reversal itself
            status, note = self._take_back(result)
        self.store.end_job(key, status, result=result, error=error, note=note)
        trace.emit("server", "job", kind=job["kind"], status=status, run_ms=round((time.time() - started) * 1000), error=error, note=note)

    def _take_back(self, result: Any) -> tuple[str, str]:
        try:
            if isinstance(result, dict) and self.service.take_back(result):
                return "cancelled", "it was already saved, and has been taken back"
            return "done", "it was too late to cancel, and this cannot be taken back"
        except Uncertain as failure:
            return "uncertain", f"taking it back could not be confirmed: {failure}"
        except Exception as failure:  # noqa: BLE001 - whatever went wrong, the change is still there and must be reported so
            return "done", f"it was too late to cancel, and taking it back failed: {failure}"


def _day(text: str | None) -> date:
    day = parse_day(text) if text else date.today()
    if day is None:
        raise HTTPException(400, f"not a day: {text}")
    return day


def _requested_day(day: str | None = None) -> date:
    return _day(day)


RequestedDay = Annotated[date, Depends(_requested_day)]


def _target(store: Store, args: dict) -> dict | None:
    entry = store.entry(int(args["id"]))
    return entry if entry and entry["date"] == args.get("on") else None


UNDO_WORDS = {"log": "log", "copy": "log", "edit": "set", "retime": "move", "move": "move", "remove": "remove"}


def _undo_label(store: Store, journaled: dict) -> str:
    """A change as its Undo names it: in the words it was asked with from a page ("move Light Cake to 17:25")."""
    asked = next((job for job in store.done_jobs(0, 200) if (job["result"] or {}).get("op") == journaled["id"]), None)
    return (
        f"{UNDO_WORDS[asked['kind']]} {asked['label']}"
        if asked and asked["kind"] in UNDO_WORDS
        else f"{journaled['kind']} {_about(journaled)}"
    )


def _undoing(jobs: list[dict]) -> set[int]:
    """The journal operations an undo is on its way for (asked and not failed)."""
    return {job["args"]["op"] for job in jobs if job["kind"] == "undo" and job["status"] != "failed"}


def _next_undo(service: Service, store: Store, jobs: list[dict]) -> dict | None:
    """What Undo reverses now: the latest change asked from a page in the last hour that still stands and is not
    already being undone. Undo after Undo goes back through them, one change at a time, the way it does in an editor."""
    busy = _undoing(jobs)
    for job in store.done_jobs(time.time() - RECENT, 500):
        journaled = store.op((job["result"] or {}).get("op") or 0)
        if not undoable(journaled) or journaled["id"] in busy:
            continue  # not a change to the log (a sync, a recipe), taken back already, or its undo is on its way
        if not service.can_undo(journaled):
            return None  # it was changed since by other means: nothing older is offered behind its back
        return {"op": journaled["id"], "kind": job["kind"], "label": job["label"], "days": affected_days(journaled)}
    return None


def _told(job: dict, store: Store) -> dict:
    """A job as a page is told it."""
    told = {k: job[k] for k in ("key", "kind", "label", "args", "status", "cancel", "progress", "error", "note", "created")}
    told["ids"] = [entry["id"] for entry in (job["result"] or {}).get("entries", [])]  # the entries it saved
    told["retry"] = job["status"] == "failed" and not job["saving"] and not job["dismissed"] and job["kind"] != "undo"
    if told["retry"] and job["kind"] in ("edit", "retime", "remove"):
        told["retry"] = _target(store, job["args"]) is not None
    # an unconfirmed save is settled by a sync of a day it touched (which reads all of them): the day to ask it for
    told["check"] = next(iter(affected_days(job["args"])), None) if job["status"] == "uncertain" else None
    return told


def _about(journaled: dict) -> str:
    """What a journaled write was about, in a few words."""
    result = journaled["result"]
    entries = result.get("entries") or [result.get("entry") or result.get("after")]
    return ", ".join(serving_text(entry) for entry in entries)


def prepare(service: Service, kind: str, args: dict) -> tuple[dict, str]:
    """What a page asks for, as it is stored: checked, and understood (foods by their names, the day by its date, an
    entry with what it was), so that it can be drawn while it waits, carried out later from the store alone, and
    asked again exactly. Returns (args, label). Raises before anything is queued."""
    store = service.store
    match kind:
        case "log":
            when = args.get("time") or None
            plan = service.plan(args["items"], _day(args.get("date")), when)  # fails on an unknown food
            each = [(p, told) for p, told in zip(plan.items, plan.describe()["items"], strict=True) for _ in range(p.repeat)]
            items = [{"name": p.food.name, "quantity": told["quantity"], "unit": told["unit"]} for p, told in each]
            return {"items": items, "date": plan.day.isoformat(), "time": when}, ", ".join(serving_text(told) for _p, told in each)
        case "remove" | "edit" | "retime":
            entry = _target(store, args)
            if entry is None:
                raise HTTPException(404, "that entry is no longer on the requested day")
            was = {"id": entry["id"], "name": entry["name"], "on": entry["date"]}
            if kind == "remove":
                return was, entry["name"]
            if kind == "edit":
                quantity, unit = float(args["quantity"]), args.get("unit") or None
                return {**was, "quantity": quantity, "unit": unit}, f"{entry['name']} to {quantity:g} {unit or ''}".rstrip()
            when, target = args["time"], _day(args["date"]) if args.get("date") else None
            if parse_time(when) is None:
                raise HTTPException(400, f"not a time: {when}")
            label = f"{entry['name']} to {f'{target:%a} {target.day} ' if target else ''}{when}"
            return {**was, "time": when, "date": target.isoformat() if target else None}, label
        case "undo":
            journaled = store.op(int(args["op"]))
            if not service.can_undo(journaled):
                raise HTTPException(409, "there is nothing to undo there any more")
            if journaled["id"] in _undoing(store.open_jobs()):
                raise HTTPException(409, "that is already being undone")
            return {
                "op": journaled["id"],
                "parts": undo_parts(journaled),
                "days": affected_days(journaled),
            }, _undo_label(store, journaled)
        case "recipe":
            name, items, servings = (args.get("name") or "").strip(), args["items"], float(args.get("servings") or 1)
            if not name:
                raise HTTPException(400, "the recipe needs a name")
            service.plan(items)  # fails on an unknown ingredient
            return {"name": name, "items": items, "servings": servings}, f"recipe {name}"
        case "recipe-delete":
            return {"name": args["name"]}, f"recipe {args['name']}"
        case "sync":
            if args.get("what") == "recipes":
                return {"what": "recipes"}, "recipes with the phone's list"
            if args.get("what") == "week":
                return {"what": "week"}, "the last 7 days from an export"
            day = _day(args.get("day"))
            return {"day": day.isoformat()}, f"{day:%a} {day.day} with the phone's screen"
    raise HTTPException(400, f"not something mf does: {kind}")


def run_job(service: Service, job: dict) -> Any:
    """Carry out a job from what `prepare` stored of it."""
    kind, args = job["kind"], job["args"]
    match kind:
        case "log":
            return service.log(args["items"], date.fromisoformat(args["date"]), args["time"])
        case "remove" | "edit" | "retime":
            now = _target(service.store, args)
            if now is None:
                raise SpecError(f"{args['name']} was not changed: it is no longer there")
            if kind == "remove":
                return service.remove(now)
            if kind == "edit":
                return service.edit(now, args["quantity"], args["unit"])
            if args["date"]:
                return service.move(now, date.fromisoformat(args["date"]), args["time"])
            return service.retime(now, args["time"])
        case "undo":
            return service.undo(args["op"])
        case "recipe":
            return service.create_recipe(args["name"], args["items"], args["servings"])
        case "recipe-delete":
            return service.delete_recipe(args["name"])
        case "sync":
            if args.get("what") == "recipes":
                return service.read_recipes()
            if args.get("what") == "week":
                service.stop = None  # MacroFactor counts an export once it is asked: it is not left halfway
                return service.sync_export("week")
            return service.sync_days(date.fromisoformat(args["day"]))
    raise SpecError(f"not something mf does: {kind}")


def create_app(worker: Worker | None = None) -> FastAPI:
    app = FastAPI(title="mf", docs_url=None, redoc_url=None)
    worker = worker or Worker()
    service, store = worker.service, worker.store  # requests only read through them (a connection per thread)
    # The page is read once, here: a running server only ever serves the page it was started with, whatever happens
    # to the files since. `build` names that page and the Python sources it runs with.
    page = {path.name: path.read_bytes() for path in sorted(WEB.iterdir()) if path.is_file()}

    hashed: list = [None, ""]  # the sources as last listed (name, size, time written), and their build

    def version():
        """The build of the sources on disk. They are read again only when their listing has changed: the page asks often."""
        sources = [path for path in sorted(WEB.parent.glob("*.py")) + sorted(WEB.iterdir()) if path.is_file()]
        listing = [(str(path), (stat := path.stat()).st_size, stat.st_mtime_ns) for path in sources]
        if listing != hashed[0]:
            digest = hashlib.sha256()
            for path in sources:
                digest.update(str(path.relative_to(WEB.parent)).encode() + b"\0" + path.read_bytes() + b"\0")
            hashed[:] = listing, digest.hexdigest()[:12]
        return hashed[1]

    build = version()
    page["index.html"] = page["index.html"].replace(b"<html ", f'<html data-build="{build}" '.encode(), 1)
    phone: dict[str, Any] = {"at": 0.0, "serial": None}  # adb is asked at most every 10 s
    reading = threading.Lock()

    @app.exception_handler(SpecError)
    async def spec_error(_request: Request, error: SpecError):
        return JSONResponse({"detail": str(error), "candidates": error.candidates}, status_code=422)

    def fresh(asked: float = Header(alias="X-Mf-Asked"), page_build: str = Header(alias="X-Mf-Build")) -> None:
        if page_build != build:
            raise HTTPException(409, "this page is older than the mf that is running: reload it")
        if not -FRESH <= time.time() - asked <= FRESH:
            raise HTTPException(409, "this request is too old or its clock is ahead; check the time and ask again")

    def state(day: date) -> dict:
        if time.time() - phone["at"] > 10:
            phone["at"] = time.time()  # stamped first: requests that overlap do not each ask adb
            phone["serial"] = service.connected()
        with reading, store.atomic(write=False):  # one at a time, so that `at` orders readings as they were taken
            at = time.monotonic_ns()
            data = service.day(day)
            catalog = store.catalog()
            jobs = store.open_jobs()
            last = store.last_job(time.time() - RECENT)
            journaled = store.op(last["result"]["op"]) if last and (last["result"] or {}).get("op") else None
            if last is not None:
                free = service.can_undo(journaled) and journaled["id"] not in _undoing(jobs)
                last = {**_told(last, store), "undo": journaled["id"] if free else None}
            undo = _next_undo(service, store, jobs)
            jobs = [_told(job, store) for job in jobs]
        return {
            "build": build,
            "update_available": version() != build,
            "at": at,
            "phone": phone["serial"],
            "day": data,
            "catalog": catalog,
            "exports_left": 0 if (exports := service.exports())["refused_until"] else exports["left"],
            "jobs": jobs,
            "last": last,
            "undo": undo,
        }

    @app.get("/api/state")
    def read(day: RequestedDay):
        return state(day)

    @app.post("/api/jobs", dependencies=[Depends(fresh)])
    def ask(body: JobRequest, key: Key, day: RequestedDay):
        # not async: it reads the store and may ask adb, which must not hold up the other requests
        if store.job(key) is None:
            args, label = prepare(service, body.kind, body.arguments())
            worker.submit(key, body.kind, label, args)
        return state(day)

    @app.post("/api/jobs/{job}/cancel", dependencies=[Depends(fresh)])
    def cancel(job: str, day: RequestedDay):
        if not worker.cancel(job):
            raise HTTPException(409, "it has already finished; use Undo")
        return state(day)

    @app.post("/api/jobs/{job}/retry", dependencies=[Depends(fresh)])
    def retry(job: str, key: Key, day: RequestedDay):
        """Ask a failed job again, as a new one. Only one page gets to: the failed one is dismissed by it."""
        if store.job(key) is None:
            failed = store.job(job)
            if failed is None or not _told(failed, store)["retry"]:
                raise HTTPException(409, "that cannot be asked again")
            args, label = prepare(service, failed["kind"], failed["args"])  # refused here, the failure stays to be seen
            worker.submit(key, failed["kind"], label, args, retry_of=job)
        return state(day)

    @app.post("/api/jobs/{job}/dismiss", dependencies=[Depends(fresh)])
    def dismiss(job: str, day: RequestedDay):
        store.dismiss_job(job)
        return state(day)

    @app.get("/api/catalog")
    def catalog():
        """Everything the page needs to search and price foods without another round trip."""
        with store.atomic(write=False):
            recipes = {
                r["name"]: {"servings": r.get("Serving Qty") or 1, "ingredients": str(r.get("Ingredients") or "")} for r in store.recipes()
            }
            foods = [f.as_dict() for f in store.foods()]  # most eaten first
            return {"version": store.catalog(), "foods": foods, "aliases": store.aliases(), "recipes": recipes}

    @app.post("/api/trace")
    async def page_trace(request: Request):
        """What was done on the page: key presses, clicks and what they meant, in batches."""
        body = json.loads(await request.body())  # sent as text by sendBeacon when the page closes
        name = str(body.get("page", ""))[:40]
        for event in body.get("events", [])[:2000]:
            if isinstance(event, dict) and isinstance(event.get("ev"), str):
                fields = {k: v for k, v in event.items() if k not in ("t", "ev", "src", "run")}
                trace.emit("page", event["ev"], t=event.get("t"), page=name, **fields)
        return {}

    @app.get("/")
    @app.get("/{name}")
    def file(name: str = "index.html"):
        if name not in page:
            raise HTTPException(404)
        return Response(page[name], media_type=mimetypes.guess_type(name)[0], headers={"Cache-Control": "no-cache"})

    return app


def serve(host: str = "127.0.0.1", port: int = 8377) -> None:
    import uvicorn

    try:
        app = create_app()
    except (DatabaseFormatError, PhoneError) as error:
        raise SystemExit(f"mf: {error}") from None
    print(f"mf web UI: http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning", timeout_graceful_shutdown=1)
