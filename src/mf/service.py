"""What mf can do, as plain functions returning plain dicts. The CLI and the web server are thin shells over this.

Each write runs on the phone under the phone lock, is verified there, mirrored into the local
store, and journaled so it can be undone.

The local data a write changes (the mirrored entry, the journal line) and what `Service.finished` does with
its result are stored as one: a reader sees the write wholly done or not at all.

A write can be stopped (`Service.stop`): before anything is saved it backs out and raises `Cancelled`,
leaving phone, store and journal as they were; once something is saved it runs to its end, and
`take_back` undoes it.
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import json
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path
from zipfile import BadZipFile

from . import PACKAGE, trace
from . import screens as S
from .app import AppError, DayView, Entry, ExportRefused, Item, MacroFactor, move_steps, same_food, stamp
from .device import Cancelled, Phone, PhoneError, Uncertain, phone_environment, phone_lock, phone_serial
from .parse import Plan, SpecError, ingredients, parse, parse_time, plan_items, positive
from .store import DATA_DIR, MACROS, RECIPE_COLUMNS, ExportFormatError, Food, Store, affected_days, match_entries
from .text import norm

EXPORTS_A_DAY = 6  # MacroFactor refused after about ten in a day (2026-11-15), for 24 hours; mf stops well before
PERIODS = {"week": ("Last 7 Days", 7), "month": ("Last 30 Days", 30), "all": ("All Time", None)}


def operation(kind: str):
    """One phone lease through local commit, including compound writes and their failure outcome.

    The lease starts before the request is resolved: which entry "#3" is, or what the last change was, is read from
    the same state the operation then changes. A failure is journaled once the request is formed (`_request`); one
    that never was (an unknown food, no entry #99, nothing to undo) asked nothing of the phone and is only raised.
    """

    def decorate(call):
        @wraps(call)
        def run(self, *args, **kwargs):
            if self._owns_phone:
                return call(self, *args, **kwargs)
            with phone_lock():
                self._owner, self._saved, self._op = threading.get_ident(), False, None
                self._kind, self._request = kind, {}
                self._seen = {}
                try:
                    return call(self, *args, **kwargs)
                except BaseException as error:
                    status = "uncertain" if self._saved else "cancelled" if isinstance(error, Cancelled) else "failed"
                    with contextlib.suppress(sqlite3.Error):
                        if self._op is not None:
                            self.store.settle_op(self._op, status, error=str(error))
                        elif self._request:
                            self.store.record(kind, self._request, status, error=str(error))
                    if self._saved and not isinstance(error, Uncertain):
                        raise Uncertain(str(error)) from error
                    raise
                finally:
                    self._owner = None

        return run

    return decorate


class Service:
    def __init__(self, store: Store | None = None, say: Callable[[str], None] | None = None):
        self.store = store or Store()
        self.say = say or (lambda message: None)
        self.stop: Callable[[], bool] | None = None  # set by a front end: has the user asked to stop the current write?
        self.finished: Callable[[dict], None] = lambda result: None  # set by a front end: told the result of a write as it is stored
        self.saving: Callable[[int], None] = lambda op: None  # told the op about to save; must succeed before any saving input is sent
        self._owner: int | None = None
        self._op: int | None = None
        self._kind, self._request = "", {}
        self._phone: Phone | None = None
        self._nested = 0  # depth of `_steps`
        self._saved = False  # the current write has saved something on the phone: it can no longer just stop
        self._seen: dict[
            date, tuple[dict[str, int], dict[str, int]]
        ] = {}  # eaten and targets the phone showed for a day, after the current write

    def _step(self, message: str) -> None:
        trace.emit("phone", "step", text=message)
        self.say(message)

    @property
    def _owns_phone(self) -> bool:
        return self._owner == threading.get_ident()

    @contextlib.contextmanager
    def phone(self) -> Iterator[MacroFactor]:
        with contextlib.nullcontext() if self._owns_phone else phone_lock():
            if self._phone is None:
                self._phone = Phone()
                self._phone.wake()
            if not self._owns_phone:
                self._saved = False
            app = MacroFactor(
                self._phone,
                log=self._step,
                cancelled=None if self._saved else self.stop,
                saving=self._committing,
                staging=lambda plate: self.store.set("staging", json.dumps(plate) if plate is not None else ""),
                staged=lambda: json.loads(self.store.get("staging") or "null"),
            )
            try:
                yield app
                self._phone.checkpoint = None  # the write is over: reading the totals is not something to cancel
                if self._owns_phone and (seen := self._read_totals(app)):
                    self._seen[seen[0]] = seen[1:]
            finally:
                self._saved = self._saved or app.committed
                self._phone.checkpoint = None

    def _read_totals(self, app: MacroFactor) -> tuple[date, dict[str, int], dict[str, int]] | None:
        """What the Food Log, if that is where the app was left, shows as eaten, and for which day."""
        with contextlib.suppress(Exception):  # a courtesy to the page's totals: never a reason for an operation to fail
            today = app.now().date()
            log = S.food_log(app.phone.settled(3.0))
            shown = _shown_totals(log) if log else None
            day = S.title_date(log.title, today) if shown and today == app.now().date() else None
            if day is not None:
                return day, *shown
        return None

    def _keep_totals(self, kind: str, request: dict) -> None:
        """Once the local entries say what the phone says: measure them against the phone's own totals. A day this
        write changed and whose totals it could not read afterwards keeps none: the earlier reading is of another day."""
        if self._nested:
            return
        changed = {date.fromisoformat(day) for day in affected_days(request)} if kind in (*UNDOABLE, "undo") else set()
        for day in changed - self._seen.keys():
            self.store.forget_phone_totals(day)
        for day, (eaten, targets) in self._seen.items():
            self.store.set_phone_totals(day, eaten, targets)
        self._seen = {}

    # ------------------------------------------------------------ reading

    def day(self, day: date, live: bool = False) -> dict:
        """A day's entries from the local store, numbered for `rm`/`edit`/`mv`. `live` reads the phone instead."""
        if live:
            with self.phone() as app:
                view = app.read_day(day)
                totals = {k: v[0] for k, v in view.totals.items()}
                targets = {k: v[1] for k, v in view.totals.items()}
                with self.store.atomic():
                    self.store.set_phone_totals(day, totals, targets)
            entries = [e.as_dict() for e in view.entries]
            return {
                "date": day.isoformat(),
                "live": True,
                "entries": [{**e, "n": i} for i, e in enumerate(entries, start=1)],
                "totals": totals,
                "targets": targets,
            }
        data = self.store.day(day)
        for index, entry in enumerate(data["entries"], start=1):
            entry["n"] = index
        data["synced"] = self.store.get("last_sync")
        return data

    def plan(self, spec: str | list[dict], day: date | None = None, at: str | None = None) -> Plan:
        """What would be logged: `spec` is food-language text, or explicit items (see `plan_items`)."""
        plan = parse(spec, self.store) if isinstance(spec, str) else plan_items(spec, self.store)
        for item in plan.items:
            if item.quantity is not None:
                positive(item.quantity, "quantity")
        if day is not None:
            plan.day = day
        if at:
            plan.hour, plan.minute = _time(at)
        return plan

    # ------------------------------------------------------------ writing

    @contextlib.contextmanager
    def _write(self, request: dict) -> Iterator[MacroFactor]:
        """The phone step; the enclosing operation also owns verification and local storage."""
        if not self._request:
            self._request = request
        with self.phone() as app:
            yield app

    def _committing(self) -> None:
        """Persist uncertainty first. A crash from here on must never make this write retryable."""
        with self.store.atomic():
            op = self._op if self._op is not None else self.store.record(self._kind, self._request, "uncertain")
            if self._kind == "undo":
                self.store.mark_op(self._request["op"], "uncertain")
            self.saving(op)
        self._op = op
        self._saved = True

    @contextlib.contextmanager
    def _steps(self) -> Iterator[None]:
        """Writes made inside are steps of a larger one (a move, an undo): only that one is journaled as done,
        and once one step has saved something the rest runs without stopping."""
        self._nested += 1
        try:
            yield
        finally:
            self._nested -= 1

    def _done(self, kind: str, request: dict, journal: dict | None = None, **reply) -> dict:
        """A write succeeded: journal it (unless it is a step of a larger one) and return what the caller is told.
        Called inside `store.atomic()` with the local changes it goes with: they are stored as one, or it is a mistake."""
        if not self.store.in_atomic:
            raise RuntimeError("a write is settled inside store.atomic(), with the local changes it made")
        self._keep_totals(kind, request)
        if self._nested:
            return {"op": None, **reply}
        if self._op is None:
            self._op = self.store.record(kind, request, "done", journal)
        else:
            self.store.settle_op(self._op, "done", result=journal)
        result = {"op": self._op, **reply}
        self.finished(result)
        return result

    @operation("log")
    def log(self, spec: str | list[dict], day: date | None = None, at: str | None = None) -> dict:
        """Log foods given as food-language text or explicit items; returns the saved entries."""
        plan = self.plan(spec, day, at)
        text = spec if isinstance(spec, str) else ", ".join(i["name"] for i in spec)
        request = {"text": text, **plan.describe()}
        planned = [p for p in plan.items for _ in range(p.repeat)]
        items = [item_of(p.food, p.quantity, p.unit) for p in planned]
        with self._write(request) as app:
            saved = app.log(items, plan.day, plan.hour, plan.minute)
        with self.store.atomic():
            entries = [self._mirror(entry, p.food.phone_name, p.quantity, p.food) for entry, p in zip(saved, planned, strict=True)]
            return self._done("log", request, {"entries": entries}, entries=entries)

    def _mirror(self, entry: Entry, name: str, asked: float | None = None, food: Food | None = None) -> dict:
        """Record an entry the phone just confirmed, so it shows before the next sync. `food`: the recipe it was logged
        from, which among several of one name the entry alone may not tell."""
        serving = S.split_serving(entry.serving)
        if serving is None:
            raise AppError(f"unreadable serving for {entry.name}: {entry.serving}")
        quantity, unit = serving
        if asked is not None and abs(asked - quantity) < S.SAME_QUANTITY:
            quantity = asked  # the phone shows two decimals; keep the precision that was typed
        if entry.weighed is not None and name not in self.store.shared_recipes():  # a shared name says nothing of a weight
            self.store.note_weight(name, entry.weighed)  # before the food is looked up: it decides whether a weight is shown
        if food is None or not food.shared:  # looked up now: what the phone just showed of its weight is in it
            food = self.store.food_of({"name": name, "quantity": quantity, "unit": unit, "kcal": entry.kcal, "unit_grams": None})
        known = (food.per_unit.get(unit) or {}) if food else {}
        time = stamp(entry.hour, entry.minute)
        row = {
            "time": time,
            "name": name,
            "unit": unit,
            "quantity": quantity,
            "kcal": entry.kcal,
            "protein": entry.protein,
            "fat": entry.fat,
            "carbs": entry.carbs,
            "unit_grams": known.get("grams"),
        }
        return {**row, "date": entry.day.isoformat(), "id": self.store.add_entry(day=entry.day, **row), "display": entry.name}

    def _resolve(self, ref: int | dict, day: date | None = None) -> dict:
        """An entry from its store id, from its number in `day`'s listing, or from an earlier reading of it."""
        if isinstance(ref, dict):
            return self._current(ref)
        if day is not None:
            entries = self.store.day(day)["entries"]
            if not 1 <= ref <= len(entries):
                raise SpecError(f"{day} has {len(entries)} entries; there is no #{ref}")
            return entries[ref - 1]
        entry = self.store.entry(ref)
        if entry is None:
            raise SpecError(f"no entry with id {ref}")
        return entry

    def _current(self, entry: dict) -> dict:
        """An entry read earlier (queued, journaled) as it is stored now.

        Its id still names it if it is on the phone as it was (a sync keeps the ids of entries that did not change).
        If the row is gone, or is no longer that food, serving, day and hour, the entry it was no longer exists.
        """
        stored = self.store.entry(entry["id"])
        if stored and stored["date"] == entry["date"] and _same_entry(stored, entry):
            current, expected = stored["time"], entry["time"]
            if current == expected or (current[:2] == expected[:2] and "??" in (current[3:], expected[3:])):
                return stored
        raise _Gone(f"{serving_text(entry)} (entry {entry['id']}) is no longer in its recorded state")

    def _position(self, app: MacroFactor, entry: dict) -> tuple[list[Entry], int]:
        """Return the observed hour and the position of the requested entry within it."""
        day = date.fromisoformat(entry["date"])
        hour = int(entry["time"][:2])

        def alike(kcal: float | None) -> bool:
            return abs((kcal or 0) - (entry["kcal"] or 0)) <= 1

        twins = [e for e in self.store.day(day)["entries"] if int(e["time"][:2]) == hour and _same_entry(e, entry)]
        rows = app.hour_entries(day, hour)
        matches = [r for r in rows if same_food(r.name, entry["name"]) and S.same_serving(r.serving, entry["quantity"], entry["unit"])]
        if any(not alike(e["kcal"]) for e in twins):  # two recipes of one name, at one serving: told apart by their energy
            twins, matches = [e for e in twins if alike(e["kcal"])], [r for r in matches if alike(r.kcal)]
        nth = next((i for i, e in enumerate(twins) if e["id"] == entry["id"]), None)
        if nth is None or len(matches) != len(twins):
            raise AppError(f"{serving_text(entry)} at {hour:02d}:00 has changed on the phone; run `mf sync` before editing it")
        target = matches[nth]
        if target.minute is not None and entry["time"][3:] not in ("??", f"{target.minute:02d}"):
            raise AppError(f"the time of {entry['name']} has changed on the phone; run `mf sync` before editing it")
        return rows, target.position

    @operation("remove")
    def remove(self, ref: int | dict, day: date | None = None) -> dict:
        entry = self._resolve(ref, day)
        request = {"entry": entry}
        with self._write(request) as app:
            app.remove(*self._position(app, entry))
        with self.store.atomic():
            self.store.delete_entry(entry["id"])
            return self._done("remove", request, {"entry": entry}, removed=entry)

    @operation("edit")
    def edit(self, ref: int | dict, quantity: float, unit: str | None = None, day: date | None = None) -> dict:
        entry = self._resolve(ref, day)
        request = {"entry": entry, "quantity": quantity, "unit": unit}
        with self._write(request) as app:
            saved = app.edit(*self._position(app, entry), quantity, unit)  # confirms the quantity itself
        _, new_unit = S.split_serving(saved.serving) or (quantity, unit or entry["unit"])
        fields = {"quantity": quantity, "unit": new_unit, **{m: getattr(saved, m) for m in MACROS}}
        if norm(new_unit) != norm(entry["unit"]):
            fields["unit_grams"] = None
        with self.store.atomic():
            self.store.update_entry(entry["id"], **fields)
            after = self.store.entry(entry["id"])
            return self._done("edit", request, {"before": entry, "after": after}, entry=after)

    @operation("retime")
    def retime(self, ref: int | dict, time: str, day: date | None = None) -> dict:
        """Move an entry to another time on the same day."""
        entry = self._resolve(ref, day)
        hour, minute = _time(time)
        request = {"entry": entry, "time": f"{hour:02d}:{minute:02d}"}
        with self._write(request) as app:
            before, position = self._position(app, entry)
            app.retime(before, [position], hour, minute)
        with self.store.atomic():
            self.store.update_entry(entry["id"], time=request["time"])
            after = self.store.entry(entry["id"])
            return self._done("retime", request, {"before": entry, "after": after}, entry=after)

    @operation("copy")
    def copy(self, ref: int | dict, to_day: date | None = None, time: str | None = None, day: date | None = None) -> dict:
        """Log the same food and serving again (default: now)."""
        entry = self._resolve(ref, day)
        to_day = to_day or date.today()
        request = {"entry": entry, "date": to_day.isoformat(), "time": time}
        saved, food = self._copy_to_phone(entry, to_day, time, request)
        with self.store.atomic():
            copied = self._mirror(saved, entry["name"], entry["quantity"], food)
            return self._done("copy", request, {"entries": [copied]}, entries=[copied])

    @operation("move")
    def move(self, ref: int | dict, to_day: date, time: str | None = None, day: date | None = None) -> dict:
        """Move an entry to another day and time. The entry keeps what was logged, even if its recipe changed since."""
        entry = self._resolve(ref, day)
        time = time or _clock(entry)
        if to_day.isoformat() == entry["date"]:
            return self.retime(entry, time)
        hour, minute = _time(time)
        request = {"entry": entry, "date": to_day.isoformat(), "time": f"{hour:02d}:{minute:02d}"}
        source = date.fromisoformat(entry["date"])
        steps = move_steps(source, to_day)  # MacroFactor's Move reaches a few weeks: a longer way is gone in steps
        if len(steps) > 1:
            request["days"] = [d.isoformat() for d in steps[:-1]]  # where the entry passes: to be checked too if this stops midway
        with self._write(request) as app:
            before, position = self._position(app, entry)
            for step in steps:  # MacroFactor's Move: the entry itself
                (moved,), left = app.move(before, [position], step, hour)
                if step == steps[0] and (shown := _shown_totals(left)):  # not always readable (seen once); the move is saved all the same
                    self._seen[source] = shown
                before, position = app.hour_entries(step, hour, navigate=False), moved.position
            if moved.minute != minute:  # the hour is chosen in the move; the minute it leaves is another, or not shown
                app.retime(before, [position], hour, minute)
        with self.store.atomic():
            self.store.update_entry(entry["id"], date=to_day.isoformat(), time=request["time"])
            after = self.store.entry(entry["id"])
            return self._done("move", request, {"before": entry, "after": after}, entry=after, removed=entry)

    def _copy_to_phone(self, entry: dict, day: date, time: str | None, request: dict) -> tuple[Entry, Food | None]:
        """Log an entry's food again, at its amount. Returns what the phone saved, and the food it was logged from."""
        hour, minute = _time(time) if time else (None, None)
        food = self.store.food_of(entry)  # of several recipes of its name, the one it was logged from
        if food is None and entry["name"] in self.store.shared_recipes():
            raise SpecError(
                f"this {entry['name'].strip()} is of none of the recipes MacroFactor now lists under that name: it cannot be logged again"
            )
        again = (
            item_of(food, entry["quantity"], entry["unit"])
            if food and food.shared
            else Item(entry["name"], entry["quantity"], entry["unit"])
        )
        with self._write(request) as app:
            saved = app.log([again], day, hour, minute)
        return saved[0], food

    def _undo_targets(self, op: dict) -> list[dict]:
        """Resolve the whole reversal before its first write. Restoring a removal has no existing target."""
        result = op["result"]
        if op["kind"] == "remove":
            return []
        entries = result["entries"] if op["kind"] in ("log", "copy") else [result["after"]]
        return [self._current(entry) for entry in entries]

    def can_undo(self, op: dict | None) -> bool:
        if not undoable(op):
            return False
        try:
            self._undo_targets(op)
        except _Gone:
            return False
        return True

    @operation("undo")
    def undo(self, op_id: int | None = None) -> dict:
        """Reverse the most recent successful write made through mf, or the one journaled as `op_id`."""
        last = self.store.last_done(UNDOABLE) if op_id is None else self.store.op(op_id)
        if not undoable(last):
            raise SpecError("nothing to undo")
        result, kind = last["result"], last["kind"]
        self._request = {"op": last["id"], "original": last["request"], "result": result}
        targets = self._undo_targets(last)
        note = None
        with self._steps():  # the reversing writes are not themselves something to undo
            if kind in ("log", "copy"):
                for entry in reversed(targets):
                    self.remove(entry)
            elif kind == "remove":
                entry = result["entry"]
                saved, food = self._copy_to_phone(entry, date.fromisoformat(entry["date"]), _clock(entry), self._request)
                with self.store.atomic():
                    self._mirror(saved, entry["name"], entry["quantity"], food)
                # MacroFactor cannot give a removed entry back: it is logged again, as the food is now.
                if any(abs((entry[m] or 0) - getattr(saved, m)) > 1 for m in MACROS):
                    note = (
                        f"{entry['name']} was logged again as it is now: {saved.kcal:g} kcal, "
                        f"not the {entry['kcal']:g} kcal of the entry that was removed (its recipe has changed since)"
                    )
            else:
                before, current = result["before"], targets[0]
                if kind == "edit":
                    self.edit(current, before["quantity"], before["unit"])
                elif kind == "retime":
                    self.retime(current, _clock(before))
                else:
                    self.move(current, date.fromisoformat(before["date"]), _clock(before))
        with self.store.atomic():
            self._keep_totals("undo", self._request)
            self.store.mark_op(last["id"], "undone")
            result = {"undone": {"kind": kind, "at": last["at"], "request": last["request"]}, "note": note}
            if self._op is not None:
                self.store.settle_op(self._op, "done", result=result)
            self.finished(result)
            return result

    def take_back(self, result: dict) -> bool:
        """Reverse the write that just returned `result`. False when it is not of a kind that can be reversed."""
        if result.get("op") is not None and undoable(self.store.op(result["op"])):
            self.undo(result["op"])
            return True
        if "recipe" in result:
            self.delete_recipe(result["recipe"]["name"])
            return True
        return False

    # ------------------------------------------------------------ recipes

    def _ingredients(self, spec: str | list[str] | list[dict]) -> tuple[list[Item], list[dict]]:
        """Recipe ingredients as the driver takes them and as the journal tells them. `spec`: food-language text, several
        such texts where one starting with `=` names a food exactly as MacroFactor lists it, or explicit items."""
        if isinstance(spec, list) and all(isinstance(text, str) for text in spec):
            planned = ingredients(spec, self.store)
        else:
            planned = self.plan(spec).items
        described = [p.estimate() for p in planned]
        return [
            item_of(p.food, d["quantity"] * d["repeat"], d["unit"], p.exact) for p, d in zip(planned, described, strict=True)
        ], described

    def rehearse_recipe(
        self, name: str, spec: str | list[str] | list[dict], servings: float = 1, total_weight: float | None = None
    ) -> dict:
        """Everything `create_recipe` does on the phone short of creating: each ingredient is found and set, the
        totals are checked and read, and the wizard is left. Returns what MacroFactor showed. Nothing is changed."""
        items, _ = self._ingredients(spec)
        with self.phone() as app:
            return {"rehearsed": app.create_recipe(name, items, servings, total_weight, rehearse=True)}

    @operation("recipe")
    def create_recipe(self, name: str, spec: str | list[str] | list[dict], servings: float = 1, total_weight: float | None = None) -> dict:
        """Create a recipe from ingredients given like foods to log ("lentils 200g, oil 10g"), by exact name, or as items."""
        if any(norm(known) == norm(name) for known in (*self.store.listed_recipes(), *(r["name"] for r in self.store.recipes()))):
            raise SpecError(f"a recipe is already named {name!r}; MacroFactor would keep both, and a name could no longer tell them apart")
        items, described = self._ingredients(spec)
        request = {"name": name, "ingredients": described, "servings": servings, "total_weight": total_weight}
        with self._write(request) as app:
            made = app.create_recipe(name, items, servings, total_weight)
        data = {
            "Serving Qty": servings,
            "Total Weight (g)": total_weight,
            "Ingredients": "\n".join(f"{i['name']} ({i['quantity']:g} {i['unit']})" for i in made["ingredients"]),
            **{column: (made[m] or 0) * servings for m, column in zip(MACROS, RECIPE_COLUMNS, strict=True)},
        }
        weight = S.SERVING_GRAMS.search(made["serving"])
        with self.store.atomic():
            self.store.save_recipe(made["name"], data)
            # as the phone's list now shows it: the next reading of the recipes finds nothing new in it
            self.store.save_shown(
                {made["name"]: [{m: float(made[m]) for m in MACROS} | {"grams": float(weight[1]) if weight else None}]}, False
            )
            return self._done("recipe", request, made, recipe=made)

    def search(self, query: str) -> dict:
        """What MacroFactor's own food search lists for `query`, database included. Nothing is changed."""
        with self.phone() as app:
            rows, more = app.search(query)
        return {
            "query": query,
            "more": {section.removeprefix("From ").lower(): count for section, count in more.items()},  # behind "See N More"
            "found": [
                {
                    "name": r.name,
                    "from": r.section.removeprefix("From ").lower(),
                    "serving": r.serving,
                    "kcal": r.kcal,
                    "protein": r.protein,
                    "fat": r.fat,
                    "carbs": r.carbs,
                }
                for r in rows
            ],
        }

    @operation("recipe-delete")
    def delete_recipe(self, name: str) -> dict:
        """Delete a recipe in MacroFactor (it cannot be undone there). Entries logged from it stay."""
        name = next((r["name"] for r in self.store.recipes() if norm(r["name"]) == norm(name)), name)
        if (food := self.store.food(name)) and food.shared:
            name = food.phone_name
        if (count := len(self.store.listed_recipes().get(name, []))) > 1:
            raise SpecError(f"MacroFactor lists {count} recipes named {name!r}; delete the one meant on the phone")
        with self._write({"name": name}) as app:
            app.delete_recipe(name)
        with self.store.atomic():
            self.store.drop_recipe(name)
            return self._done("recipe-delete", {"name": name}, deleted=name)

    # ------------------------------------------------------------ data

    @operation("sync")
    def sync_days(self, day: date, *more: date) -> dict:
        """Make the local copy of `day` say what the phone's screen shows. This is how changes made on the phone
        itself are picked up, and it costs no export.

        An entry still on the phone keeps its id: the same hour, food and serving, or failing that the same hour and
        food, whose amount was then changed on the phone. One that is no longer there is removed from the copy; one
        the copy did not have is added. Figures and minutes are the screen's wherever it tells them (`_minutes`).
        The copy must then add up to the total the phone shows, or the day was not read whole and nothing is changed.

        A write left unconfirmed on one of these days is settled by reading every day it touched, so those are read
        too. Recipes made or edited on the phone are another reading (`read_recipes`).
        """
        asked = {d.isoformat() for d in (day, *more)}
        days = [date.fromisoformat(d) for d in sorted(self.store.unconfirmed_with(asked))]
        self._request = {"days": [d.isoformat() for d in days]}
        with self.phone() as app:
            views = [(d, app.read_day(d)) for d in days]
        with self.store.atomic():
            counts = [self._reconcile(d, view) for d, view in views]
            count = {k: sum(c[k] for c in counts) for k in counts[0]}
            self.store.reconciled(self._request["days"])
            return self._done("sync", self._request, count, day=day.isoformat(), **count)

    def _listed_recipes(self, app: MacroFactor, everything: bool) -> tuple[dict[str, dict], bool]:
        """The phone's recipe list as `save_shown` takes it: those created or edited since the last reading, or all."""

        def row_of(row: S.FoodRow) -> tuple[str, dict]:
            figures = S.one_serving(row)
            if figures is None:
                raise AppError(f"unexpected serving for the recipe {row.name}: {row.serving}")
            return self._known(row.name), figures

        have = self.store.listed_recipes()
        everything = everything or not have  # the first reading is a whole one
        rows, complete = app.read_recipes(None if everything else lambda row: (found := row_of(row))[1] in have.get(found[0], []))
        listed: dict[str, list[dict]] = {}
        for name, figures in map(row_of, rows):  # several recipes can share a name: each is kept
            listed.setdefault(name, []).append(figures)
        return listed, complete

    def read_recipes(self, everything: bool = False) -> dict:
        """Bring the recipes in line with the phone's own list, read from its screen (no export): those created or
        edited since the last reading, or with `everything` the whole list, which also tells the ones deleted.

        A recipe then shows on the page as it is now, before it is ever logged, with its real weight or none.
        """
        with self.phone() as app:  # the reading is stored before the phone is let go: nothing changes the list in between
            rows, complete = self._listed_recipes(app, everything)
            with self.store.atomic():
                return {**self.store.save_shown(rows, complete), "complete": complete}

    def _reconcile(self, day: date, view: DayView) -> dict:
        """Apply a complete reading, inside the caller's transaction."""
        known = self.store.day(day)["entries"]
        observed = []
        for row in view.entries:
            data = row.as_dict()
            if data["quantity"] is None:
                raise AppError(f"unreadable serving in the Food Log: {row.serving}")
            observed.append({**data, "name": self._known(row.name)})
        matched, gone = match_entries(known, observed, changed_amount=True)
        count = {"seen": len(observed), "added": 0, "changed": 0, "removed": len(gone)}
        rows: dict[int, list[dict]] = {}
        for shown, data, entry in zip(view.entries, observed, matched, strict=True):
            if entry is None:
                entry = self._mirror(shown, data["name"])
                count["added"] += 1
            else:
                fields = {m: getattr(shown, m) for m in MACROS}
                if not S.same_serving(shown.serving, entry["quantity"], entry["unit"]):
                    fields.update(quantity=data["quantity"], unit=data["unit"])
                    if norm(data["unit"]) != norm(entry["unit"]):
                        fields["unit_grams"] = None
                    count["changed"] += 1
                self.store.update_entry(entry["id"], **{k: v for k, v in fields.items() if entry[k] != v})
            rows.setdefault(shown.hour, []).append((entry, shown.minute))
        for identity in gone:
            self.store.delete_entry(identity)
        for hour, stamps in view.times.items():
            entries, shown = zip(*rows[hour], strict=True) if hour in rows else ((), ())
            was = [int(e["time"][3:]) if e["time"][3:].isdigit() else None for e in entries]
            now = shown if None not in shown else _minutes(stamps, was)  # as drawn beside the rows, else as can be deduced
            for entry, old, minute in zip(entries, was, now, strict=True):
                if minute != old:
                    self.store.update_entry(entry["id"], time=stamp(hour, minute))
        totals = {m: view.totals[m][0] for m in MACROS}
        if self.store.set_phone_totals(day, totals, {m: view.totals[m][1] for m in MACROS})["apart"]:
            raise AppError(f"{day} could not be read whole from the screen (its entries do not add up to its total); nothing was changed")
        return count

    def _known(self, shown: str) -> str:
        """The name the local data has for a food the phone shows ("Name By Brand" for "Name"); the phone's, for a new one."""
        names = [food.phone_name for food in self.store.foods() if same_food(shown, food.phone_name)]
        return max(names, key=len, default=shown)

    def exports(self) -> dict:
        """The exports MacroFactor can still be asked for: it gives only so many in a day, then refuses for 24 hours.
        `left` of `EXPORTS_A_DAY` in the last 24 hours; `refused_until` (epoch seconds) while it is refusing."""
        now = time.time()
        recent = [at for at in json.loads(self.store.get("exports") or "[]") if now - at < 86400]
        refused = float(self.store.get("exports_refused") or 0) + 86400
        return {"left": max(0, EXPORTS_A_DAY - len(recent)), "recent": recent, "refused_until": refused if refused > now else None}

    def may_export(self, count: int, force: bool) -> None:
        """Refuse, before the phone is touched, what would spend exports that are not there. `force` is for a person
        who has decided to spend them anyway; it does not override MacroFactor's own refusal, which nothing can."""
        state = self.exports()
        if state["refused_until"]:
            raise SpecError(f"MacroFactor is refusing exports until about {datetime.fromtimestamp(state['refused_until']):%a %H:%M}")
        if count > state["left"] and not force:
            free = datetime.fromtimestamp(min(state["recent"]) + 86400)
            raise SpecError(
                f"this needs {count} export{'s' if count > 1 else ''} and {state['left']} of {EXPORTS_A_DAY} are left for today "
                f"(MacroFactor refuses for 24 hours when asked too often); the next is free at {free:%a %H:%M}; --force overrides"
            )

    def _export(self, export: Callable[[], Path]) -> Path:
        """One export from MacroFactor, counted; its refusal is remembered, so that it is not asked again for a day."""
        try:
            path = export()
        except ExportRefused:
            self.store.set("exports_refused", str(time.time()))
            raise
        self.store.set("exports", json.dumps([*self.exports()["recent"], time.time()]))
        return path

    @operation("sync")
    def sync_export(self, period: str = "week", force: bool = False) -> dict:
        """Export the food log from the phone and load it (one export): for days long past, or everything. A day's
        changes are picked up by `sync_days`, which costs none."""
        label, days = PERIODS[period]
        folder = DATA_DIR / "exports"
        self.may_export(1, force)
        today = date.today()
        with self.phone() as app:
            path = self._export(lambda: app.export_food_log(folder, label))
            crossed_midnight = date.today() != today
            app.home()
        if days and crossed_midnight:
            raise AppError(f"date changed during the {label} export; file kept at {path}, local entries unchanged")
        period_bounds = (today - timedelta(days=days - 1), today) if days else (date.min, date.max)
        with self.store.atomic():
            result = self.store.import_food_log(path, period_bounds)
            return self._done("sync", {"period": period}, result, period=label, **result)

    @operation("backup")
    def backup(self, force: bool = False) -> dict:
        """Full snapshot: all-time food log plus the complete Granular Export workbook, kept and loaded (two exports)."""
        folder = DATA_DIR / "snapshots" / datetime.now().strftime("%Y-%m-%d-%H%M%S")
        self.may_export(2, force)
        with self.phone() as app:
            food_log = self._export(lambda: app.export_food_log(folder, "All Time"))
            library = self._export(lambda: app.export_library(folder))
            app.home()
        sums = [f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}" for p in (food_log, library)]
        (folder / "SHA256SUMS").write_text("\n".join(sums) + "\n")
        for path in (food_log, library):
            path.chmod(0o444)
        with self.store.atomic():
            result = {
                "folder": str(folder),
                "food_log": self.store.import_food_log(food_log, (date.min, date.max)),
                "library": self.store.import_library(library),
            }
            self._done("backup", {}, result)
        return result

    def connected(self) -> str | None:
        """The serial of the phone adb sees, if any."""
        try:
            return phone_serial()
        except Exception:  # noqa: BLE001 - adb missing or broken is a state to report, not a crash
            return None

    def setup(self) -> str:
        """The app version and screen size of the phone, as `S.VERIFIED` words those the screen readers were written on."""
        try:
            now = phone_environment(self.connected() or "", PACKAGE)
        except (PhoneError, OSError, subprocess.SubprocessError):
            return "not asked"
        return f"MacroFactor {now['app']}, screen {now['screen']}"

    def status(self) -> dict:
        count = self.store.db.execute("SELECT COUNT(*) n, MIN(date) first, MAX(date) last FROM entries").fetchone()
        return {
            "phone": self.connected(),
            "written_on": f"MacroFactor {S.VERIFIED['app']}, screen {S.VERIFIED['screen']}",
            "this_phone": self.setup(),
            "entries": count["n"],
            "first": count["first"],
            "last": count["last"],
            "foods": len(self.store.foods()),
            "recipes": len(self.store.recipes()),
            "last_sync": self.store.get("last_sync"),
            "last_library_sync": self.store.get("last_library_sync"),
            "exports_left": self.exports()["left"],
            "data": str(DATA_DIR),
        }

    def import_file(self, path: Path) -> dict:
        with phone_lock():
            try:
                match path.suffix.lower():
                    case ".csv":
                        return self.store.import_food_log(path)
                    case ".xlsx":
                        return self.store.import_library(path)
                    case _:
                        raise ExportFormatError("expected a Food Log CSV or a Granular Export XLSX workbook")
            except (OSError, csv.Error, UnicodeError, BadZipFile) as error:
                raise ExportFormatError(f"cannot read {path.name}: {error}") from error


UNDOABLE = ("log", "copy", "remove", "edit", "retime", "move")


def undoable(op: dict | None) -> bool:
    """Is this journal line a write that can still be reversed?"""
    return op is not None and op["status"] == "done" and op["kind"] in UNDOABLE


def serving_text(entry: dict) -> str:
    """An entry in a few words: "Apple, Fresh 120 g"."""
    return f"{entry['name']} {entry['quantity']:g} {entry['unit']}"


def _shown_totals(log: S.FoodLog) -> tuple[dict[str, int], dict[str, int]] | None:
    """Eaten and targets as a Food Log shows them; None when it does not show all four (the app sometimes exposes none)."""
    if any(m not in log.totals for m in MACROS):
        return None
    return {m: log.totals[m][0] for m in MACROS}, {m: log.totals[m][1] for m in MACROS}


def undo_parts(op: dict) -> list[dict]:
    """What undoing a journaled write will do to the day, as the changes a page can draw while it waits."""
    result, kind = op["result"], op["kind"]
    if kind in ("log", "copy"):
        return [{"kind": "remove", "id": entry["id"], "on": entry["date"]} for entry in result["entries"]]
    if kind == "remove":
        entry = result["entry"]
        items = [{"name": entry["name"], "quantity": entry["quantity"], "unit": entry["unit"]}]
        return [{"kind": "log", "date": entry["date"], "time": _clock(entry), "items": items}]
    before, after = result["before"], result["after"]
    target = {"id": after["id"], "on": after["date"]}
    if kind == "edit":
        return [{"kind": "edit", **target, "quantity": before["quantity"], "unit": before["unit"]}]
    moved = before["date"] if kind == "move" else None
    return [{"kind": "retime", **target, "time": _clock(before), "date": moved}]


class _Gone(SpecError):
    """An entry read earlier is no longer in the local data."""


def _time(text: str) -> tuple[int, int]:
    when = parse_time(text)
    if when is None:
        raise SpecError(f"not a time: {text}")
    return when


def _clock(entry: dict) -> str:
    """An entry's time as something to ask for again; an unknown minute ("08:??") becomes the hour."""
    return entry["time"].replace("??", "00")


def _same_entry(a: dict, b: dict) -> bool:
    return a["name"] == b["name"] and a["unit"] == b["unit"] and abs(a["quantity"] - b["quantity"]) < S.SAME_QUANTITY


def _minutes(stamps: list[int], known: list[int | None]) -> list[int | None]:
    """The minute of each row of one hour, top to bottom, wherever it is certain.

    The phone lists the hour's distinct minutes (`stamps`) and its rows in time order, but not which row has which;
    `known` is what the copy says of each row. Every stamp belongs to at least one row, so unknown rows between two
    rows of the same minute have that minute, and as many unknown rows as there are stamps no other row can have
    take them in order. What the copy says is dropped if the screen contradicts it: times were changed on the phone.
    """
    said = [minute for minute in known if minute is not None]
    out = list(known) if said == sorted(said) and set(said) <= set(stamps) else [None] * len(known)
    start = 0
    while start < len(out):
        end = start
        while end < len(out) and out[end] is None:
            end += 1
        low, high = (out[start - 1] if start else None), (out[end] if end < len(out) else None)
        between = [s for s in stamps if (low is None or s >= low) and (high is None or s <= high)]
        free = [s for s in between if s not in (low, high)]  # only the rows of this run can have these
        if len(between) == 1:
            out[start:end] = between * (end - start)
        elif len(free) == end - start:
            out[start:end] = free
        start = end + 1
    return out


def item_of(food: Food, quantity: float | None, unit: str | None, exact: bool = False) -> Item:
    """A food of the catalogue as the driver is asked for it. A recipe never logged is not in the app's search
    history, and one of several of a name is told from the others only there: both are picked in the Library."""
    shown = food.shared["serving"] if food.shared else None
    library = food.kind == "recipe" and (food.count == 0 or shown is not None)
    return Item(food.phone_name, quantity, unit, library=library, exact=exact, shown=shown)
