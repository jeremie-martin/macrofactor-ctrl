"""Local copy of MacroFactor data: fast to search, never the source of truth.

MacroFactor's exports fill it (`import_food_log`, `import_library`); operations done through
mf add rows marked `source='mf'` so they show up at once, and the next export confirms them (same id) or removes them.
"""

from __future__ import annotations

import contextlib
import csv
import fcntl
import json
import math
import os
import re
import sqlite3
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .device import PhoneError
from .screens import SAME_QUANTITY, is_amount_of, weighs_as
from .text import norm

DATA_DIR = Path(os.environ.get("MF_DATA_DIR", Path.home() / ".local/share/macrofactor"))
MACROS = ("kcal", "protein", "fat", "carbs")
ENTRY_COLUMNS = "date,time,name,unit,quantity,unit_grams,kcal,protein,fat,carbs,nutrients"  # what an export says of an entry
QUICK_ADD = "Quick Add"  # how the export names calories entered by hand: entries of a day, not a food that can be logged again
RECIPE_COLUMNS = ("Calories (kcal)", "Protein (g)", "Fat (g)", "Carbs (g)")  # recipe totals in the export, same order

FORMAT_ID = 0x4D460001  # SQLite application_id for this mf file format
SCHEMA = """CREATE TABLE entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,            -- YYYY-MM-DD
    time TEXT NOT NULL,            -- HH:MM; minute '??' when only the hour is known
    name TEXT NOT NULL,            -- as exported: no brand
    unit TEXT NOT NULL,
    quantity REAL NOT NULL,
    unit_grams REAL,               -- grams per unit, when MacroFactor knows it
    kcal REAL, protein REAL, fat REAL, carbs REAL,
    nutrients TEXT,                -- JSON of the remaining export columns
    source TEXT NOT NULL DEFAULT 'export'
);
CREATE INDEX entries_date ON entries(date);
CREATE INDEX entries_name ON entries(name);
CREATE TABLE library (
    kind TEXT NOT NULL,            -- recipe | custom | history (from the export) | shown (the phone's recipe list) | weightless
    name TEXT NOT NULL,
    data TEXT NOT NULL,            -- JSON
    PRIMARY KEY (kind, name)
);
CREATE TABLE aliases (alias TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE targets (
    updated TEXT NOT NULL,         -- day the nutrition program changed
    weekday TEXT NOT NULL,         -- Monday..Sunday
    kcal REAL, protein REAL, fat REAL, carbs REAL,
    PRIMARY KEY (updated, weekday)
);
CREATE TABLE ops (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    request TEXT NOT NULL,         -- JSON: what was asked
    status TEXT NOT NULL,          -- done | failed | cancelled | undone | uncertain | reconciled
    result TEXT,                   -- JSON: what happened, enough to undo it
    error TEXT
);
CREATE TABLE jobs (          -- what a page asked the server to do on the phone
    key TEXT PRIMARY KEY,          -- the name the page gave it: asked again under it, it is this row
    created REAL NOT NULL,         -- seconds since the epoch
    kind TEXT NOT NULL,            -- log | remove | edit | retime | undo | recipe | recipe-delete | sync
    label TEXT NOT NULL,           -- what it is about, without the verb
    args TEXT NOT NULL,            -- JSON: what was asked, enough to draw it while it waits and to ask it again
    status TEXT NOT NULL,          -- queued | running | done | failed | cancelled | uncertain | reconciled
    cancel INTEGER NOT NULL DEFAULT 0,     -- asked to stop; how that ended is in status and note
    saving INTEGER NOT NULL DEFAULT 0,     -- the op (journal id) it is saving, persisted before any input that can save; 0: none
    dismissed INTEGER NOT NULL DEFAULT 0,  -- failure no longer needs attention (seen, retried, or read superseded)
    progress TEXT,                 -- the driver's last word
    result TEXT,                   -- JSON
    error TEXT,
    note TEXT,
    finished REAL
);
CREATE INDEX jobs_queued ON jobs(status) WHERE status='queued';
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE day_totals (   -- what MacroFactor itself showed for a day, last time mf looked
    date TEXT PRIMARY KEY,
    read_at TEXT NOT NULL,
    totals TEXT NOT NULL,          -- JSON {kcal, protein, fat, carbs}: the phone's figures
    targets TEXT,                  -- JSON, same keys: the day's targets as the phone shows them
    offset TEXT NOT NULL,          -- JSON, same keys: those figures minus the sum of the local entries at that moment
    apart INTEGER NOT NULL         -- 1 when that difference is more than rounding can explain: the phone and mf disagree on the entries
);
"""


@dataclass
class Food:
    """A food known from history, with how it is usually logged."""

    name: str  # what it is asked for by, one food to a name: MacroFactor's name for it, but see `shared`
    display: str  # with brand when known: "Skyr Nature 0% By Yoplait"
    count: int
    last: str  # date last logged
    score: float  # frequency weighted by recency
    servings: list[dict]  # [{"quantity": 150.0, "unit": "g", "count": 12, "kcal": 78.0}], most used first
    per_unit: dict[str, dict]  # unit -> {"kcal":..., "protein":..., "fat":..., "carbs":..., "grams":...} for 1 unit
    kind: str = "food"  # food | recipe
    last_at: str = ""  # "YYYY-MM-DD HH:MM" last logged: orders the page's Latest
    hours: list[int] | None = None  # times logged in each hour of the day over the last 30 days: the page's picks for the hour
    weightless: bool = False  # MacroFactor shows no weight for it: it cannot be logged in grams
    # One of several recipes MacroFactor lists under one name: that name ("name") and its "serving" as the list shows it
    # (kcal, protein, fat, carbs, grams). Its own `name` then says which, "Soup (201 kcal)": a description, which
    # changes with the recipe, and with its namesakes when they come to show the same energy.
    shared: dict | None = None

    @property
    def phone_name(self) -> str:
        """The name MacroFactor has for it: what is searched on the phone, and what its entries are called."""
        return self.shared["name"] if self.shared else self.name

    def as_dict(self) -> dict:
        return self.__dict__.copy()


class DatabaseFormatError(ValueError):
    """An existing database is outside the supported mf file format."""


class ExportFormatError(ValueError):
    """A file cannot identify the MacroFactor data it would replace."""


class _ThreadState(threading.local):
    """A connection, and the catalog it last built, belong to one thread."""

    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, autocommit=True)
        self.db.row_factory = sqlite3.Row
        self.depth = 0  # of `Store.atomic`
        self.foods: tuple[int, list[Food]] | None = None


class Store:
    def __init__(self, path: Path | None = None):
        self.path = path or DATA_DIR / "mf.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = _ThreadState(self.path)
        if self.db.execute("PRAGMA application_id").fetchone()[0] != FORMAT_ID:
            # WAL must be enabled outside a transaction, before publishing the initialized schema.
            with self.path.with_suffix(".init").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                marker = self.db.execute("PRAGMA application_id").fetchone()[0]
                if marker == FORMAT_ID:
                    return
                if marker or self.db.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone():
                    raise DatabaseFormatError(f"{self.path}: unsupported database format")
                self.db.execute("PRAGMA journal_mode=WAL")
                with self.atomic():
                    self.db.executescript(SCHEMA)
                    self.db.execute(f"PRAGMA application_id={FORMAT_ID}")

    def server_lock(self):
        """Exclusive ownership of the desktop job queue."""
        handle = self.path.with_suffix(".serving").open("a")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise PhoneError("mf serve is already running on this data; stop it before starting another server") from None
        return handle

    @property
    def db(self) -> sqlite3.Connection:
        # FastAPI runs overlapping reads on worker threads; never share a connection.
        return self._local.db

    @property
    def in_atomic(self) -> bool:
        return self._local.depth > 0

    @contextlib.contextmanager
    def atomic(self, write: bool = True) -> Iterator[None]:
        """What is written inside becomes visible to other readers all at once, or not at all; what is read inside
        is one moment of the data. Blocks nest: only the outermost begins and commits."""
        local = self._local
        outer = local.depth == 0
        if outer:
            self.db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        local.depth += 1
        try:
            yield
            if outer:
                self.db.execute("COMMIT")
        except BaseException:
            if outer:
                local.foods = None  # a rolled-back catalog version can later be committed with different foods
                if self.db.in_transaction:
                    self.db.execute("ROLLBACK")
            raise
        finally:
            local.depth -= 1

    # ---- meta

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def catalog(self) -> int:
        """Counts the changes to what `foods`, `aliases` and `recipes` say: whoever holds a copy knows by it that it is old."""
        return int(self.get("catalog") or 0)

    def _changed(self) -> None:
        self.db.execute("INSERT INTO meta(key,value) VALUES('catalog','1') ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1")

    # ---- imports

    def import_food_log(self, path: Path, period: tuple[date, date] | None = None) -> dict:
        """Load a Food Log CSV export: replace the closed period requested, or the dates spanned by a standalone file.

        An entry that is still in the file keeps its id: the same day, hour, food, unit and quantity (the export
        knows the minute, a row mirrored from the screen may not). So an id means one entry for as long as that
        entry is on the phone unchanged, across any number of syncs. Rows no longer in the file are deleted."""
        fields = {
            "Date": "date",
            "Time": "time",
            "Food Name": "name",
            "Serving Size": "unit",
            "Serving Qty": "quantity",
            "Serving Weight (g)": "unit_grams",
            "Calories (kcal)": "kcal",
            "Protein (g)": "protein",
            "Fat (g)": "fat",
            "Carbs (g)": "carbs",
        }
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, strict=True)
            required = {"Date", "Time", "Food Name", "Serving Size", "Serving Qty", "Calories (kcal)"}
            headers = reader.fieldnames or []
            if not required.issubset(headers) or len(headers) != len(set(headers)):
                raise ExportFormatError(f"{path.name} is not a MacroFactor Food Log export")
            rows = []
            for record in reader:
                if not any(record.values()):  # MacroFactor emits a blank line for an emptied day
                    continue
                if None in record or None in record.values():
                    raise ExportFormatError(f"{path.name}: wrong number of columns on line {reader.line_num}")
                try:
                    row = {column: record.get(header) for header, column in fields.items()}
                    row["date"] = date.fromisoformat(row["date"]).isoformat()
                    row["time"] = datetime.strptime(row["time"], "%H:%M").strftime("%H:%M")
                    if not row["name"] or not row["unit"] or not row["quantity"]:
                        raise ValueError
                    for key in ("quantity", "unit_grams", *MACROS):
                        row[key] = _number(row[key])
                    extra = {k: _number(v) for k, v in record.items() if k not in fields and v}
                    row["nutrients"] = json.dumps(extra) if extra else None
                except (ValueError, TypeError) as error:
                    raise ExportFormatError(f"{path.name}: invalid food entry on line {reader.line_num}") from error
                rows.append(row)
        if not rows and period is None:
            raise ExportFormatError(f"{path.name} contains no dated entries to establish the import period")
        dates = [r["date"] for r in rows]
        start, end = (d.isoformat() for d in period) if period else (min(dates), max(dates))
        rows = [r for r in rows if start <= r["date"] <= end]
        columns = ENTRY_COLUMNS.split(",")
        assignments = ",".join(f"{c}=?" for c in columns)
        with self.atomic():
            self.db.execute("DELETE FROM day_totals WHERE date BETWEEN ? AND ?", (start, end))
            known = self.db.execute(
                "SELECT id,date,time,name,quantity,unit FROM entries WHERE date BETWEEN ? AND ? ORDER BY date,time,id",
                (start, end),
            ).fetchall()
            matched, gone = match_entries(known, rows)
            # Separate insert and update: AUTOINCREMENT allocates an identity only for a new entry.
            for row, old in zip(rows, matched, strict=True):
                values = tuple(row[c] for c in columns)
                if old is None:
                    self.db.execute(f"INSERT INTO entries({ENTRY_COLUMNS},source) VALUES(?,?,?,?,?,?,?,?,?,?,?,'export')", values)
                else:
                    self.db.execute(f"UPDATE entries SET {assignments},source='export' WHERE id=?", (*values, old["id"]))
            self.db.executemany("DELETE FROM entries WHERE id=?", [(i,) for i in gone])
            self._changed()
            self.set("last_sync", datetime.now().isoformat(timespec="seconds"))
        return {"rows": len(rows), "from": start, "through": end, "file": str(path)}

    def import_library(self, path: Path) -> dict:
        """Load recipes, custom foods, history names and nutrition targets from a Granular Export workbook."""
        import openpyxl

        book = openpyxl.load_workbook(path, read_only=True, data_only=True)
        counts: dict[str, int] = {}

        def table(sheet: str, *required: str) -> list[dict] | None:
            if sheet not in book.sheetnames:
                return None
            rows = iter(book[sheet].values)
            header = next(rows, ())
            names = [h for h in header if h is not None]
            if len(names) != len(set(names)) or not set(required).issubset(names):
                raise ExportFormatError(f"{path.name}: {sheet} needs columns {', '.join(required)}")
            return [{k: v for k, v in zip(header, row, strict=True) if k is not None} for row in rows if any(v is not None for v in row)]

        with contextlib.closing(book), self.atomic():
            for kind, sheet, key in (
                ("recipe", "Recipes", "Recipe Name"),
                ("history", "History", "Food Name"),
            ):
                items = table(sheet, key)
                if items is None:
                    continue
                self.db.execute("DELETE FROM library WHERE kind=?", (kind,))
                for item in items:
                    name = item.get(key)
                    if not name:
                        raise ExportFormatError(f"{path.name}: missing {key} in {sheet}")
                    data = {k: v for k, v in item.items() if v not in (None, "") and k != key}
                    self.db.execute(
                        "INSERT OR REPLACE INTO library(kind,name,data) VALUES(?,?,?)",
                        (kind, str(name), json.dumps(data, default=str)),
                    )
                counts[kind] = len(items)
            program = table("Nutrition Program Settings", "Program Update Date", "Program Weekday", *RECIPE_COLUMNS)
            if program is not None:
                self.db.execute("DELETE FROM targets")
                for row in program:
                    try:
                        updated = datetime.strptime(str(row["Program Update Date"]), "%m/%d/%Y").date()
                        weekday = row["Program Weekday"]
                        if not weekday:
                            raise ValueError
                        targets = [_number(row[column]) for column in RECIPE_COLUMNS]
                    except (TypeError, ValueError) as error:
                        raise ExportFormatError(f"{path.name}: invalid Nutrition Program Settings row") from error
                    self.db.execute(
                        "INSERT OR REPLACE INTO targets(updated,weekday,kcal,protein,fat,carbs) VALUES(?,?,?,?,?,?)",
                        (updated.isoformat(), weekday, *targets),
                    )
                counts["targets"] = len(program)
            if not counts:
                raise ExportFormatError(f"{path.name} has no MacroFactor library or nutrition sheets")
            self._changed()
            self.set("last_library_sync", datetime.now().isoformat(timespec="seconds"))
        return counts

    # ---- entries

    def add_entry(self, *, day: date, time: str, name: str, unit: str, quantity: float, kcal, protein, fat, carbs, unit_grams=None) -> int:
        cursor = self.db.execute(
            "INSERT INTO entries(date,time,name,unit,quantity,unit_grams,kcal,protein,fat,carbs,source) VALUES(?,?,?,?,?,?,?,?,?,?,'mf')",
            (day.isoformat(), time, name, unit, quantity, unit_grams, kcal, protein, fat, carbs),
        )
        self._changed()
        return int(cursor.lastrowid or 0)

    def update_entry(self, entry_id: int, **fields) -> None:
        if fields:
            assignments = ", ".join(f"{key}=?" for key in fields)
            self.db.execute(f"UPDATE entries SET {assignments}, source='mf' WHERE id=?", (*fields.values(), entry_id))
            self._changed()

    def delete_entry(self, entry_id: int) -> None:
        self.db.execute("DELETE FROM entries WHERE id=?", (entry_id,))
        self._changed()

    def entry(self, entry_id: int) -> dict | None:
        row = self.db.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
        return _entry(row) if row else None

    def day(self, day: date) -> dict:
        rows = self.db.execute("SELECT * FROM entries WHERE date=? ORDER BY time, id", (day.isoformat(),)).fetchall()
        entries = [_entry(r) for r in rows]
        weightless, several = self.weightless(), self.shared_recipes()
        for entry in entries:
            entry["food"] = entry["name"]  # what the catalogue calls it; for a name several recipes share, the one it is of
            if entry["name"] in several:  # its weight is its recipe's: the name's says nothing
                entry["food"] = label = recipe_of(entry, several[entry["name"]])
                weight = several[entry["name"]][label]["grams"] if label else None
                light = label is not None and weight is None  # of no recipe known: as the entry itself says
                if weight and entry["unit"] == "serving":
                    entry["grams"] = round(entry["quantity"] * weight, 1)
            else:
                light = entry["name"] in weightless
            if light and entry["unit"] != "g":  # the export gives a food without a weight a made-up one: the phone says it has none
                entry["grams"] = None
        totals = {m: round(sum(e[m] or 0 for e in entries), 1) for m in MACROS}
        phone = self.phone_totals(day)
        targets = (phone or {}).get("targets") or self.targets(day)  # the phone's own, when it has been seen
        return {
            "date": day.isoformat(),
            "entries": entries,
            "totals": totals,
            "targets": targets,
            "phone": phone,
            "uncertain": day.isoformat() in self.uncertain_days(),
        }

    def set_phone_totals(self, day: date, totals: dict[str, float], targets: dict[str, float] | None = None) -> dict:
        """Remember what MacroFactor shows as eaten on `day`. Its export rounds every entry to whole numbers while
        the app adds the exact ones, so a day's entries rarely add up to the app's figure; the difference (`offset`)
        lets the page show the app's. Beyond what rounding explains (half a unit an entry), the two are `apart`."""
        rows = self.db.execute("SELECT kcal, protein, fat, carbs FROM entries WHERE date=?", (day.isoformat(),)).fetchall()
        offset = {m: round(float(totals[m]) - sum(r[m] or 0 for r in rows), 1) for m in MACROS}
        apart = any(abs(offset[m]) > 0.5 * len(rows) + 1 for m in MACROS)
        self.db.execute(
            "INSERT OR REPLACE INTO day_totals(date, read_at, totals, targets, offset, apart) VALUES(?,?,?,?,?,?)",
            (
                day.isoformat(),
                datetime.now().isoformat(timespec="seconds"),
                json.dumps({m: totals[m] for m in MACROS}),
                json.dumps({m: targets[m] for m in MACROS}) if targets and all(targets.get(m) for m in MACROS) else None,
                json.dumps(offset),
                int(apart),
            ),
        )
        return {"offset": offset, "apart": apart}

    def forget_phone_totals(self, day: date) -> None:
        """The day changed on the phone and its new totals were not readable: what was read before says nothing now."""
        self.db.execute("DELETE FROM day_totals WHERE date=?", (day.isoformat(),))

    def phone_totals(self, day: date) -> dict | None:
        row = self.db.execute("SELECT * FROM day_totals WHERE date=?", (day.isoformat(),)).fetchone()
        if row is None:
            return None
        return {
            "read_at": row["read_at"],
            "totals": json.loads(row["totals"]),
            "targets": json.loads(row["targets"]) if row["targets"] else None,
            "offset": json.loads(row["offset"]),
            "apart": bool(row["apart"]),
        }

    def targets(self, day: date) -> dict | None:
        """Calorie and macro targets of the nutrition program in force on `day`."""
        row = self.db.execute(
            "SELECT * FROM targets WHERE weekday=? AND updated <= ? ORDER BY updated DESC LIMIT 1",
            (f"{day:%A}", day.isoformat()),
        ).fetchone()
        return {m: row[m] for m in MACROS} if row else None

    # ---- catalog

    def foods(self) -> list[Food]:
        """Every food ever logged, best first (often and recently), then the recipes never logged.

        A name MacroFactor lists several recipes under gives one food to each (`Food.shared`), named for what tells
        them apart; an entry of that name counts for the one recipe it is an amount of, or for none.
        """
        version = self.catalog()
        if self._local.foods is not None and self._local.foods[0] == version:
            return self._local.foods[1]
        today = date.today()
        history = [r["name"] for r in self.db.execute("SELECT name FROM library WHERE kind='history'")]
        weightless = self.weightless()
        several = self.shared_recipes()
        listed = self.shown_recipes()  # what the phone's own recipe list says of one serving: newer than any entry or export
        told = self.recipes()  # by an export, or by their creation through mf
        recipes = {r["name"] for r in told} | set(listed)
        display: dict[str, str] = {}
        for shown in history:
            base = shown.split(" By ")[0]
            display.setdefault(norm(base), shown)
        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in self.db.execute("SELECT * FROM entries WHERE name != ? ORDER BY date, time", (QUICK_ADD,)):
            grouped.setdefault(row["name"], []).append(row)

        def food(name: str, rows: list[sqlite3.Row], serving: dict | None = None, shared: dict | None = None) -> Food:
            """`serving`: one serving as the phone's recipe list shows it, when it does."""
            servings: dict[tuple[float, str], dict] = {}
            per_unit: dict[str, dict] = {}
            hours = [0] * 24
            pooled: dict[str, list[sqlite3.Row]] = {}
            for r in rows:
                if (today - date.fromisoformat(r["date"])).days <= 30 and r["time"][:2].isdigit():
                    hours[int(r["time"][:2]) % 24] += 1
                key = (r["quantity"], r["unit"])
                entry = servings.setdefault(key, {"quantity": r["quantity"], "unit": r["unit"], "count": 0, "last": ""})
                entry["count"] += 1
                entry["last"] = f"{r['date']} {r['time']}"
                entry["kcal"] = r["kcal"]
                if r["quantity"]:
                    per_unit[r["unit"]] = {m: (r[m] or 0) / r["quantity"] for m in MACROS} | {"grams": r["unit_grams"]}
                    pooled.setdefault(r["unit"], []).append(r)
            for unit, same in pooled.items():
                per_unit[unit] |= _pooled(same, today)
            if serving:
                per_unit |= _per_shown(serving)
            light = shared["serving"]["grams"] is None if shared else name in weightless
            if light:  # nothing of it weighs anything on the phone: no grams to show, none to log it in
                per_unit = {unit: per | {"grams": None} for unit, per in per_unit.items() if unit != "g"} or per_unit
            recent = sorted(servings.values(), key=lambda s: (s["last"], s["count"]), reverse=True)[:1]
            usual = sorted(servings.values(), key=lambda s: s["count"], reverse=True)
            ordered = recent + [s for s in usual if s is not recent[0]]
            if light:  # amounts once logged in grams are not amounts to offer again
                ordered = [s for s in ordered if s["unit"] != "g"] or ordered
            if not ordered:  # never logged: a recipe, loggable all the same, picked in the app's Library
                ordered = [{"quantity": 1.0, "unit": "serving", "count": 0, "last": "", "kcal": per_unit["serving"]["kcal"]}]
            return Food(
                name=name,
                display=display.get(norm(name), name),
                count=len(rows),
                last=rows[-1]["date"] if rows else "",
                score=round(sum(math.exp(-(today - date.fromisoformat(r["date"])).days / 45) for r in rows), 3),
                servings=ordered[:6],
                per_unit=per_unit,
                kind="recipe" if shared or name in recipes else "food",
                weightless=light,
                last_at=f"{rows[-1]['date']} {rows[-1]['time'].replace('?', '0')}" if rows else "",
                hours=hours if rows else None,
                shared=shared,
            )

        foods = [food(name, rows, listed.get(name)) for name, rows in grouped.items() if name not in several]
        for name, each in several.items():
            own: dict[str | None, list] = {}
            for row in grouped.get(name, []):
                own.setdefault(recipe_of(row, each), []).append(row)
            foods += [food(label, own.get(label, []), serving, {"name": name, "serving": serving}) for label, serving in each.items()]
        foods.sort(key=lambda f: (f.count > 0, f.score), reverse=True)  # the never logged after the others, as listed
        foods += [food(name, [], serving) for name, serving in listed.items() if name not in grouped]
        for recipe in told:  # and those only an export has told of
            if recipe["name"] not in grouped and recipe["name"] not in listed and recipe["name"] not in several:
                servings = float(recipe.get("Serving Qty") or 1)
                per = {m: float(recipe.get(c) or 0) / servings for m, c in zip(MACROS, RECIPE_COLUMNS, strict=True)}
                usual = {"quantity": 1.0, "unit": "serving", "count": 0, "last": "", "kcal": per["kcal"]}
                foods.append(Food(recipe["name"], recipe["name"], 0, "", 0.0, [usual], {"serving": per | {"grams": None}}, "recipe"))
        self._local.foods = (version, foods)
        return foods

    def search(self, text: str, limit: int = 20) -> list[Food]:
        """Foods whose name (or alias) contains every word of `text`, best first."""
        words = norm(text).split()
        if not words:
            return self.foods()[:limit]
        aliased = self.alias(text)
        found: list[tuple[float, Food]] = []
        for food in self.foods():
            hay = norm(food.phone_name if food.shared else food.display)  # not its label: "2 soup" is not the soup of 201 kcal
            if aliased and food.name == aliased:
                found.append((1e9, food))
                continue
            if all(w in hay for w in words):
                tokens = hay.replace(",", " ").split()
                starts = sum(1 for w in words if any(t.startswith(w) for t in tokens))
                exact = 1e6 if hay == " ".join(words) or norm(food.name) == " ".join(words) else 0
                found.append((exact + starts * 1000 + food.score, food))
        found.sort(key=lambda pair: pair[0], reverse=True)
        return [food for _, food in found[:limit]]

    def food(self, name: str) -> Food | None:
        return next(
            (f for f in self.foods() if f.name == name or norm(f.display) == norm(name) or norm(f.name) == norm(name)),
            None,
        )

    def food_of(self, entry: dict) -> Food | None:
        """The food an entry is of: the one of its name, or among the recipes sharing that name the one it is an amount of."""
        several = self.shared_recipes().get(entry["name"])
        if several is None:
            return self.food(entry["name"])
        label = recipe_of(entry, several)
        return self.food(label) if label else None

    # ---- aliases

    def alias(self, text: str) -> str | None:
        row = self.db.execute("SELECT name FROM aliases WHERE alias=?", (norm(text),)).fetchone()
        return row["name"] if row else None

    def set_alias(self, alias: str, name: str | None) -> None:
        if name is None:
            self.db.execute("DELETE FROM aliases WHERE alias=?", (norm(alias),))
        else:
            self.db.execute("INSERT OR REPLACE INTO aliases(alias,name) VALUES(?,?)", (norm(alias), name))
        self._changed()

    def aliases(self) -> dict[str, str]:
        return {r["alias"]: r["name"] for r in self.db.execute("SELECT alias,name FROM aliases ORDER BY alias")}

    # ---- library

    def recipes(self) -> list[dict]:
        rows = self.db.execute("SELECT name,data FROM library WHERE kind='recipe' ORDER BY name").fetchall()
        return [{"name": r["name"], **json.loads(r["data"])} for r in rows]

    def ingredients(self, text: str = "") -> list[tuple[str, int]]:
        """Foods your recipes are made of, by the name MacroFactor has for them (brand left out), with the number of
        recipes using each: most used first. Many were never logged on their own, so `foods()` does not know them."""
        words, uses = norm(text).split(), Counter()
        for recipe in self.recipes():
            names = {re.sub(r"\s*\([^()]*\)$", "", line.strip()) for line in (recipe.get("Ingredients") or "").split("\n")}
            uses.update(name for name in names if name and all(word in norm(name) for word in words))
        return uses.most_common()

    def save_recipe(self, name: str, data: dict) -> None:
        self.db.execute("INSERT OR REPLACE INTO library(kind,name,data) VALUES('recipe',?,?)", (name, json.dumps(data)))
        self._changed()

    def weightless(self) -> set[str]:
        """Foods MacroFactor itself shows without a weight: recipes listed as "1 serving" alone, and foods whose
        amount showed none when they were logged. Its export gives them a made-up one (about 100 g in all)."""
        seen = {r["name"] for r in self.db.execute("SELECT name FROM library WHERE kind='weightless'")}
        return seen | {name for name, row in self.shown_recipes().items() if row["grams"] is None}

    def note_weight(self, name: str, weighed: bool) -> None:
        """What the phone showed when `name` was logged: a weight for its amount, or none."""
        had = self.db.execute("SELECT 1 FROM library WHERE kind='weightless' AND name=?", (name,)).fetchone() is not None
        if weighed and had:
            self.db.execute("DELETE FROM library WHERE kind='weightless' AND name=?", (name,))
        elif not weighed and not had:
            self.db.execute("INSERT INTO library(kind,name,data) VALUES('weightless',?,'{}')", (name,))
        if weighed == had:
            self._changed()

    def listed_recipes(self) -> dict[str, list[dict]]:
        """One serving of each recipe as the phone's recipe list showed it: kcal, protein, fat, carbs, and `grams`
        (None when the recipe has no weight; the export invents one). MacroFactor lets several recipes share a
        name: each is there, and nothing here can tell which of them a name means."""
        rows = self.db.execute("SELECT name,data FROM library WHERE kind='shown'").fetchall()
        return {r["name"]: json.loads(r["data"]) for r in rows}

    def shown_recipes(self) -> dict[str, dict]:
        """The recipes whose name tells what a serving is: listed once (or several times with the same figures)."""
        return {name: listed[0] for name, listed in self.listed_recipes().items() if len(_distinct(listed)) == 1}

    def shared_recipes(self) -> dict[str, dict[str, dict]]:
        """The names the phone lists several different recipes under: for each, its recipes in the list's order (the
        most recently modified first), each under the name that says which it is: "Soup (201 kcal)"."""
        shared = {}
        for name, listed in self.listed_recipes().items():
            each = _distinct(listed)
            if len(each) > 1:
                shared[name] = dict(zip(_labels(name, each), each, strict=True))
        return shared

    def save_shown(self, rows: dict[str, list[dict]], complete: bool) -> dict:
        """Record recipes read from the phone's list, each name with every recipe it listed under it; `complete` when
        the list was read to its end: the others are gone. Short of that, one recipe read under a name that had one
        is that recipe edited; any other is kept beside those known, until a whole reading says which still exist."""
        known = self.listed_recipes()
        if not complete:
            for name, listed in rows.items():
                had = known.get(name, [])
                if not (len(listed) == 1 and len(had) <= 1):
                    rows = rows | {name: listed + [r for r in had if r not in listed]}  # just read: the most recently modified
        count = {
            "seen": len(rows),
            "new": sum(n not in known for n in rows),
            "changed": sum(n in known and known[n] != r for n, r in rows.items()),
        }
        count["gone"] = sum(n not in rows for n in known) if complete else 0
        if complete:  # the phone's whole list: a recipe it lacks is gone, whatever an older workbook said of it
            listed = {norm(n) for n in rows}
            for (name,) in self.db.execute("SELECT name FROM library WHERE kind='recipe'").fetchall():
                if norm(name) not in listed:
                    self.db.execute("DELETE FROM library WHERE kind='recipe' AND name=?", (name,))
                    count["gone"] += name not in known
            self.db.execute("DELETE FROM library WHERE kind='shown'")
        for name, row in rows.items():
            self.db.execute("INSERT OR REPLACE INTO library(kind,name,data) VALUES('shown',?,?)", (name, json.dumps(row)))
        if count["new"] or count["changed"] or count["gone"]:
            self._changed()
        return count

    def drop_recipe(self, name: str) -> None:
        self.db.execute("DELETE FROM library WHERE kind IN ('recipe','shown') AND name=?", (name,))
        self._changed()

    # ---- jobs (the server's: what pages asked for, and how it went)

    def add_job(self, key: str, kind: str, label: str, args: dict) -> bool:
        """Record a job as queued. False when one of that key exists: it was already asked."""
        cursor = self.db.execute(
            "INSERT OR IGNORE INTO jobs(key,created,kind,label,args,status) VALUES(?,?,?,?,?,'queued')",
            (key, time.time(), kind, label, json.dumps(args, ensure_ascii=False)),
        )
        return cursor.rowcount == 1

    def job(self, key: str) -> dict | None:
        row = self.db.execute("SELECT * FROM jobs WHERE key=?", (key,)).fetchone()
        return _job(row) if row else None

    def set_job(self, key: str, only: str | None = None, **fields) -> bool:
        """Change a job that has not ended; with `only`, just if its status is still that one. True when it was changed."""
        assignments = ", ".join(f"{name}=?" for name in fields)
        where, values = ("key=? AND status=?", (key, only)) if only else ("key=?", (key,))
        return self.db.execute(f"UPDATE jobs SET {assignments} WHERE {where}", (*fields.values(), *values)).rowcount == 1

    def end_job(self, key: str, status: str, only: str | None = None, **fields) -> bool:
        """A job ends, once: False (and nothing changed) if it had already ended, or was not in the `only` status asked for."""
        fields = {"status": status, "progress": None, "finished": time.time(), **fields}
        if "result" in fields:
            fields["result"] = json.dumps(fields["result"], ensure_ascii=False, default=str)
        assignments = ", ".join(f"{name}=?" for name in fields)
        still = "status=?" if only else "status IN ('queued','running')"
        values = (*fields.values(), key, *([only] if only else []))
        ended = self.db.execute(f"UPDATE jobs SET {assignments} WHERE key=? AND {still}", values).rowcount == 1
        if ended and status == "uncertain":
            self.settle_jobs()  # another front end may have read the phone between the op's failure and this
        return ended

    def next_queued(self) -> str | None:
        row = self.db.execute("SELECT key FROM jobs WHERE status='queued' ORDER BY rowid LIMIT 1").fetchone()
        return row[0] if row else None

    def open_jobs(self) -> list[dict]:
        """Jobs that still ask for attention, in the order asked: on their way, or failed and not yet dismissed."""
        rows = self.db.execute(
            "SELECT * FROM jobs WHERE status IN ('queued','running') OR (status IN ('failed','uncertain') AND NOT dismissed) ORDER BY rowid"
        )
        return [_job(row) for row in rows]

    def last_job(self, since: float) -> dict | None:
        """The job that ended last, done or cancelled, if that was after `since`."""
        row = self.db.execute(
            "SELECT * FROM jobs WHERE status IN ('done','cancelled') AND finished >= ? ORDER BY finished DESC LIMIT 1", (since,)
        ).fetchone()
        return _job(row) if row else None

    def done_jobs(self, since: float, limit: int = 20) -> list[dict]:
        """The changes asked from a page that the phone confirmed after `since`, latest first (undos themselves left out)."""
        rows = self.db.execute(
            "SELECT * FROM jobs WHERE status='done' AND kind!='undo' AND finished >= ? ORDER BY finished DESC LIMIT ?", (since, limit)
        ).fetchall()
        return [_job(row) for row in rows]

    def dismiss_job(self, key: str) -> bool:
        """A failed job has been seen (or asked again). False when it was not failed, or already dismissed."""
        return (
            self.db.execute(
                "UPDATE jobs SET dismissed=1 WHERE key=? AND status IN ('failed','uncertain') AND NOT dismissed", (key,)
            ).rowcount
            == 1
        )

    def interrupt_jobs(self) -> None:
        """A server starts: whatever an earlier one left unfinished will not be finished, and is never run again."""
        now = time.time()
        self.db.execute(
            "UPDATE jobs SET status='failed', finished=?, error='mf was restarted before the phone started on it' WHERE status='queued'",
            (now,),
        )
        self.db.execute(
            "UPDATE jobs SET status=CASE WHEN saving THEN 'uncertain' ELSE 'failed' END, finished=?, "
            "error='mf was restarted while the phone was on it: look at the day' "
            "WHERE status='running'",
            (now,),
        )
        # Request identities are durable. Deleting old jobs would allow a delayed retry to execute them again.

    def unconfirmed_with(self, days: set[str]) -> set[str]:
        """`days`, and every other day of an unconfirmed write that touched one of them (a move, the undo of one).
        Such a write is settled by one reading of all its days: this is the set a sync of `days` has to read."""
        writes = [set(affected_days(json.loads(row[0]))) for row in self.db.execute("SELECT request FROM ops WHERE status='uncertain'")]
        found = set(days)
        while more := {day for write in writes if write & found for day in write} - found:
            found |= more
        return found

    def reconciled(self, days: list[str]) -> None:
        """A committed screen sync of `days` (closed under `unconfirmed_with`) settles the unconfirmed writes on them
        and supersedes failed syncs of them."""
        checked = set(days)
        for row in self.db.execute("SELECT id,request FROM ops WHERE status='uncertain'").fetchall():
            affected = affected_days(json.loads(row["request"]))
            if affected and set(affected) <= checked:
                self.mark_op(row["id"], "reconciled")
        self.settle_jobs()
        for row in self.db.execute("SELECT key,args FROM jobs WHERE status='failed' AND kind='sync' AND NOT dismissed").fetchall():
            affected = affected_days(json.loads(row["args"]))
            if affected and set(affected) <= checked:
                self.dismiss_job(row["key"])

    def seen_failures(self, day: str, before: float) -> None:
        """A page asked (at `before`) to read `day` again and now shows it as the phone has it: what had failed there by then
        changed nothing, and has no more to say. (Not for a read nobody at a page asked for: a failure nobody saw
        must stay. What may have been saved is 'uncertain', and the journal's to settle.)"""
        rows = self.db.execute("SELECT key,args FROM jobs WHERE status='failed' AND NOT dismissed AND finished<?", (before,)).fetchall()
        for row in rows:
            if affected_days(json.loads(row["args"])) == [day]:
                self.dismiss_job(row["key"])

    def settle_jobs(self) -> None:
        """What is unconfirmed is the journal's to say (an op 'uncertain'). A job only tells a page of it: once the
        op it was saving is settled, whichever front end read the phone, the job has nothing left to ask."""
        self.db.execute(
            "UPDATE jobs SET status='reconciled', dismissed=1 "
            "WHERE status='uncertain' AND saving IN (SELECT id FROM ops WHERE status != 'uncertain')"
        )

    def uncertain_days(self) -> set[str]:
        """Unconfirmed saves affect the day's truth even after their notification is dismissed."""
        return {
            day for row in self.db.execute("SELECT request FROM ops WHERE status='uncertain'") for day in affected_days(json.loads(row[0]))
        }

    # ---- journal

    def op(self, op_id: int) -> dict | None:
        row = self.db.execute("SELECT * FROM ops WHERE id=?", (op_id,)).fetchone()
        return _op(row) if row else None

    def record(self, kind: str, request: dict, status: str, result: dict | None = None, error: str | None = None) -> int:
        cursor = self.db.execute(
            "INSERT INTO ops(at,kind,request,status,result,error) VALUES(?,?,?,?,?,?)",
            (
                datetime.now().isoformat(timespec="seconds"),
                kind,
                json.dumps(request),
                status,
                json.dumps(result) if result else None,
                error,
            ),
        )
        return int(cursor.lastrowid or 0)

    def last_done(self, kinds: tuple[str, ...]) -> dict | None:
        placeholders = ",".join("?" for _ in kinds)
        row = self.db.execute(
            f"SELECT * FROM ops WHERE status='done' AND kind IN ({placeholders}) ORDER BY id DESC LIMIT 1", kinds
        ).fetchone()
        return _op(row) if row else None

    def ops(self, limit: int = 30) -> list[dict]:
        rows = self.db.execute("SELECT * FROM ops ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [_op(row) for row in rows]

    def mark_op(self, op_id: int, status: str) -> None:
        self.db.execute("UPDATE ops SET status=? WHERE id=?", (status, op_id))

    def settle_op(self, op_id: int, status: str, result: dict | None = None, error: str | None = None) -> None:
        self.db.execute(
            "UPDATE ops SET status=?,result=?,error=? WHERE id=?",
            (status, json.dumps(result) if result is not None else None, error, op_id),
        )


def affected_days(value) -> list[str]:
    """Days named by an operation, including the source and destination of compound writes."""
    found = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("date", "day", "on") and isinstance(item, str):
                with contextlib.suppress(ValueError):
                    found.add(date.fromisoformat(item).isoformat())
            elif key == "days" and isinstance(item, list):
                for day in item:
                    found.update(affected_days({"day": day}))
            elif isinstance(item, (dict, list)):
                found.update(affected_days(item))
    elif isinstance(value, list):
        for item in value:
            found.update(affected_days(item))
    return sorted(found)


def _labels(name: str, recipes: list[dict]) -> list[str]:
    """Names for the different recipes of one name, by the least that tells each from the others: its energy, then its
    weight, then all its figures."""
    energy = [f"{r['kcal']:g} kcal" for r in recipes]  # no comma in them: commas separate the foods of a request
    weight = [f"{e} {r['grams']:g} g" if r["grams"] else f"{e} no weight" for e, r in zip(energy, recipes, strict=True)]
    whole = [f"{w} {r['protein']:g}P {r['fat']:g}F {r['carbs']:g}C" for w, r in zip(weight, recipes, strict=True)]
    told = [next(form[at] for form in (energy, weight, whole) if form.count(form[at]) == 1) for at in range(len(recipes))]
    return [f"{name.strip()} ({tell})" for tell in told]


def _distinct(listed: list[dict]) -> list[dict]:
    """The different recipes among those listed under one name, in the list's order."""
    return [row for at, row in enumerate(listed) if row not in listed[:at]]


def recipe_of(entry: sqlite3.Row | dict, recipes: dict[str, dict]) -> str | None:
    """Which of the recipes of one name an entry is an amount of: the only one it can be, or None."""
    fits = [label for label, serving in recipes.items() if is_amount_of(serving, entry["quantity"], entry["unit"], entry["kcal"] or 0)]
    if len(fits) > 1 and entry["unit"] == "serving":  # alike in energy: the weight of a serving tells, where the entry has it
        weight = entry["unit_grams"]  # None: not recorded, which says nothing (and an export invents one for a recipe without)
        fits = [label for label in fits if weight and weighs_as(recipes[label], 1, weight)]
    return fits[0] if len(fits) == 1 else None


def _per_shown(row: dict) -> dict[str, dict]:
    """Per-unit figures of a recipe from its row in the phone's list: a serving, and a gram when it has a weight."""
    per = {"serving": {m: row[m] for m in MACROS} | {"grams": row["grams"]}}
    if row["grams"]:
        per["g"] = {m: row[m] / row["grams"] for m in MACROS} | {"grams": 1.0}
    return per


def _pooled(rows: list[sqlite3.Row], today: date) -> dict:
    """Nutrition of one unit from all recent entries rather than the last one alone. The export rounds each entry to
    whole numbers (7.67 g of galette: 29 kcal, 1 g protein); many entries together give the figure back. Only entries
    of the last 60 days whose energy agrees with the latest within that rounding count, so a recipe that changed
    is not averaged with what it used to be."""
    last = rows[-1]
    density = (last["kcal"] or 0) / last["quantity"]
    keep = [
        r
        for r in rows
        if (today - date.fromisoformat(r["date"])).days <= 60
        and abs((r["kcal"] or 0) - density * r["quantity"]) <= 0.5 + 0.5 * r["quantity"] / last["quantity"] + 1e-9
    ] or [last]
    quantity = sum(r["quantity"] for r in keep)
    return {m: sum(r[m] or 0 for r in keep) / quantity for m in MACROS}


def _number(value) -> float | None:
    if value in (None, ""):
        return None
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite nutrition or quantity")
    return number


def match_entries(known, shown, *, changed_amount=False):
    """Match observations to stored identities within one food and hour, in list order.

    A known minute excludes other known minutes. An unknown minute can compete with any of them.
    Rows that nothing observable tells apart are a group. When the phone shows at least as many as are stored, the
    stored ones keep their ids, in list order, rows of a known minute first: they are all still there, and twins
    logged since are the new ones. When it shows fewer, nothing tells which were removed: none keeps its id, so
    that an action aimed at one of them cannot land on another. An amount edit needs exactly one unmatched
    candidate on each side.
    """
    available = {row["id"]: row for row in known}
    by_food = defaultdict(list)
    for r in known:
        by_food[r["date"], r["time"][:2], r["name"]].append(r["id"])
    matches = [None] * len(shown)
    for compare_serving in [True, False] if changed_amount else [True]:
        groups = defaultdict(list)
        demand = Counter()
        for index, row in enumerate(shown):
            if matches[index] is not None:
                continue
            candidates = []
            for identity in by_food[row["date"], row["time"][:2], row["name"]]:
                old = available.get(identity)
                if old is None:
                    continue
                if old["time"] != row["time"] and not (old["time"].endswith("??") or row["time"].endswith("??")):
                    continue
                if compare_serving and (norm(old["unit"]) != norm(row["unit"]) or abs(old["quantity"] - row["quantity"]) >= SAME_QUANTITY):
                    continue
                candidates.append(identity)
            if candidates:
                groups[tuple(candidates)].append(index)
                demand.update(candidates)
        # Counts alone are insufficient if an observed row outside the group also claims one of these ids.
        for candidates, indices in groups.items():
            count = len(indices)
            if count < len(candidates) or any(demand[i] != count for i in candidates) or (not compare_serving and count != 1):
                continue
            indices.sort(key=lambda index: shown[index]["time"].endswith("??"))  # stable: list order within each kind
            for i, index in zip(candidates, indices, strict=False):
                matches[index] = available.pop(i)
    return matches, list(available)


def _entry(row: sqlite3.Row) -> dict:
    data = {k: row[k] for k in row.keys() if k != "nutrients"}  # noqa: SIM118 - sqlite3.Row has no items()
    data["grams"] = round(row["quantity"] * row["unit_grams"], 1) if row["unit_grams"] else None
    return data


def _job(row: sqlite3.Row) -> dict:
    job = dict(row)
    job["args"] = json.loads(job["args"])
    job["result"] = json.loads(job["result"]) if job["result"] else None
    job["cancel"] = bool(job["cancel"])
    return job


def _op(row: sqlite3.Row) -> dict:
    return {**dict(row), "request": json.loads(row["request"]), "result": json.loads(row["result"]) if row["result"] else None}
