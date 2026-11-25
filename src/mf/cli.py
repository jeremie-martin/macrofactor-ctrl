"""mf: MacroFactor from the desktop.

  mf log apple 150g, skyr 200g @8:30      log foods (now, unless @time / a day is given)
  mf day [yesterday|2026-11-12]           show a day; entries are numbered
  mf rm 3 · mf edit 3 180g · mf mv 3 20:00 [tomorrow] · mf cp 3
  mf undo                                 reverse the last change made through mf
  mf foods [words] · mf recipes [words] · mf alias monster "White Energy Drink ..."
  mf recipe new "Lentil stew" -i "lentils 200g, oil 10g" --servings 4 · mf recipe rm "Lentil stew"
  mf sync [DAY | week|month|all] · mf backup   a day read from the phone's screen, or an export / full snapshot
  mf serve                                web UI on http://127.0.0.1:8377
  mf status · mf ui · mf home             phone and data state, what the screen shows, reset the app

Every command accepts --json. Exit code 0 means the phone confirmed the change.
Ctrl+C stops a write cleanly: before anything is saved it backs out and changes nothing (exit code 130).
"""

from __future__ import annotations

import argparse
import json
import re
import signal
import sys
import time
from datetime import date
from pathlib import Path

from . import trace
from .device import Cancelled, PhoneError, Uncertain
from .parse import SpecError, parse_day, positive
from .service import PERIODS, Service
from .store import DatabaseFormatError, ExportFormatError


def _day(text: str | None) -> date | None:
    if not text:
        return None
    day = parse_day(text)
    if day is None:
        raise SpecError(f"not a day: {text!r} (use today, yesterday, mon..sun, -3 or YYYY-MM-DD)")
    return day


def _quantity(text: str) -> tuple[float, str | None]:
    match = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*(.*)", text.strip())
    if not match:
        raise SpecError(f"not a quantity: {text!r} (examples: 180, 180g, '2 serving')")
    return positive(float(match[1].replace(",", ".")), "quantity"), match[2].strip() or None


def _subject(request: dict) -> str:
    """What a journaled operation was about: the typed text, the entry's food, or the recipe."""
    return request.get("text") or (request.get("entry") or {}).get("name") or request.get("name") or ""


def _fmt_entry(entry: dict) -> str:
    number = f"{entry['n']:>3}  " if "n" in entry else "     "
    grams = f" ({entry['grams']:g} g)" if entry.get("grams") and entry.get("unit") != "g" else ""
    mark = "  *" if entry.get("source") == "mf" else ""
    name = entry.get("display") or entry["name"]
    return (
        f"{number}{entry['time']}  {name[:44]:<44} {entry['quantity']:>7g} {entry['unit'][:12]:<12}{grams}"
        f"  {entry['kcal'] or 0:>5.0f} kcal  P{entry['protein'] or 0:>4.0f} F{entry['fat'] or 0:>4.0f} C{entry['carbs'] or 0:>4.0f}{mark}"
    )


def _print_day(data: dict) -> None:
    title = f"{data['date']} ({date.fromisoformat(data['date']):%A})"
    print(title + ("  [live from phone]" if data.get("live") else ""))
    for entry in data["entries"]:
        print(_fmt_entry(entry))
    totals, targets = data["totals"], data.get("targets") or {}
    parts = []
    for key, label in (("kcal", "kcal"), ("protein", "P"), ("fat", "F"), ("carbs", "C")):
        goal = f"/{targets[key]:.0f}" if targets.get(key) else ""
        parts.append(f"{label} {totals.get(key, 0):.0f}{goal}")
    empty = "none in the local copy" if data.get("uncertain") else "nothing logged"
    count = f"{len(data['entries'])} entries" if data["entries"] else empty
    print("     " + "   ".join(parts) + f"   ({count})")
    if data.get("uncertain"):
        print("     Local entries only: a save is unconfirmed. Run mf sync for this day before relying on its totals.")
    if any(e.get("source") == "mf" for e in data["entries"]):
        print("     * logged through mf since the last sync")


def main(argv: list[str] | None = None) -> int:
    """Run a command; what was asked and how it ended goes to the trace."""
    words = list(sys.argv[1:] if argv is None else argv)
    command = next((word for word in words if not word.startswith("-")), "")
    if command in ("", "trace", "serve"):
        return _main(argv)
    started, code = time.monotonic(), None
    trace.emit("cli", "run", argv=words)
    try:
        code = _main(argv)
        return code
    finally:
        trace.emit("cli", "end", code=code, ms=round((time.monotonic() - started) * 1000))


def _trace(day: date, stats: bool, raw: bool, as_json: bool) -> int:
    events = trace.read(day)
    if raw:
        for event in events:
            print(json.dumps(event, ensure_ascii=False))
        return 0
    acts = trace.timeline(events)
    if stats:
        data = trace.stats(acts)
        if as_json:
            print(json.dumps(data, indent=2, ensure_ascii=False))
            return 0
        print(f"{'act':<22}{'n':>4}{'keys':>6}{'clicks':>8}{'you s':>7}{'phone s':>9}{'worst':>7}")
        for kind, row in data["acts"].items():
            cells = [row["keys"], row["clicks"], row["seconds"], row["phone_s"], row["phone_max_s"]]
            print(
                f"{kind:<22}{row['n']:>4}"
                + "".join(f"{'' if v is None else f'{v:g}':>{w}}" for v, w in zip(cells, (6, 8, 7, 9, 7), strict=True))
            )
        if data["waits"]:
            print("\nwaits on the phone's screen, longest in total first")
            for wait in data["waits"]:
                print(f"  {wait['total_s']:>6.1f} s in {wait['n']:>3} (worst {wait['max_s']:.1f} s)  {wait['what']}")
        for failure in data["failed"]:
            print(f"\nfailed: {failure['act']} {failure['what']} — {failure['error']}")
        return 0
    if as_json:
        print(json.dumps(acts, indent=2, ensure_ascii=False))
    else:
        print(trace.render(acts) or f"nothing traced on {day}  ({trace.TRACE_DIR})")
    return 0


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mf", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("-q", "--quiet", action="store_true", help="no progress lines")
    sub = parser.add_subparsers(dest="command")

    def add(name: str, help_text: str):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        command.add_argument("-q", "--quiet", action="store_true", default=argparse.SUPPRESS)
        return command

    c = add("log", "log foods: mf log apple 150g, 3x galette @8:30 yesterday")
    c.add_argument("spec", nargs="+")
    c.add_argument("-d", "--date", help="day (default today)")
    c.add_argument("--at", help="time HH[:MM] (default now)")
    c.add_argument("-n", "--dry-run", action="store_true", help="show what would be logged, touch nothing")

    c = add("day", "show a day's log")
    c.add_argument("date", nargs="?")
    c.add_argument("--live", action="store_true", help="read the phone screen instead of local data")

    for name, help_text in (("rm", "remove entry N of a day"), ("cp", "log entry N again (now, or at a time/day)")):
        c = add(name, help_text)
        c.add_argument("n", type=int)
        c.add_argument("-d", "--date", help="day the entry is on (default today)")
        if name == "cp":
            c.add_argument("time", nargs="?")
            c.add_argument("to", nargs="?", help="destination day")

    c = add("edit", "change the quantity of entry N: mf edit 3 180g")
    c.add_argument("n", type=int)
    c.add_argument("quantity", nargs="+")
    c.add_argument("-d", "--date")

    c = add("mv", "move entry N to another time (and day): mf mv 3 20:00 [tomorrow]")
    c.add_argument("n", type=int)
    c.add_argument("time")
    c.add_argument("to", nargs="?", help="destination day")
    c.add_argument("-d", "--date")

    add("undo", "reverse the last change made through mf")

    c = add("foods", "search your food history")
    c.add_argument("query", nargs="*")
    c.add_argument("--limit", type=int, default=20)

    c = add("search", "what MacroFactor's own food search lists, its database included: mf search cumin seed")
    c.add_argument("query", nargs="+")

    c = add("recipes", "list your recipes")
    c.add_argument("query", nargs="*")

    c = add(
        "recipe",
        "mf recipe new NAME -i 'lentils 200g, oil 10g' -i '=Cumin, Seed = 6 g' [--servings 4] [--weight 900] · "
        "mf recipe try ... (the same on the phone, short of creating) · mf recipe rm NAME",
    )
    c.add_argument("action", choices=["new", "try", "rm"])
    c.add_argument("name")
    c.add_argument(
        "-i",
        "--ingredients",
        action="append",
        help="foods of your history in the food language ('lentils 200g, oil 10g'), or one food by the exact name "
        "MacroFactor's search lists ('=Cumin, Seed = 6 g'; see mf search). Repeat -i: ingredients are added in that order",
    )
    c.add_argument("--servings", type=float, default=1)
    c.add_argument("--weight", type=float, help="total cooked weight in grams")

    c = add("alias", "short names: mf alias monster 'White Energy...' · mf alias (list) · mf alias monster --rm")
    c.add_argument("alias", nargs="?")
    c.add_argument("name", nargs="*")
    c.add_argument("--rm", action="store_true")

    c = add(
        "sync", "make the local data say what the phone does: a day or the recipes, read from its screen; or week, month, all: an export"
    )
    c.add_argument(
        "what",
        nargs="?",
        default="today",
        help="a day (today, yesterday, mon, 2026-11-14); recipes (those created or edited since) | all-recipes; or week | month | all",
    )
    c.add_argument("--force", action="store_true", help="spend an export even if the day's allowance is used up")

    c = add("backup", "full snapshot of MacroFactor data into the data folder (two exports)")
    c.add_argument("--force", action="store_true", help="spend the exports even if the day's allowance is used up")
    c = add("import", "load an export file (CSV food log or XLSX workbook) without the phone")
    c.add_argument("path")
    add("status", "phone, data and sync state")
    c = add("history", "recent changes made through mf")
    c.add_argument("--limit", type=int, default=15)
    add("ui", "print what MacroFactor currently shows (for debugging and agents)")
    add("home", "bring MacroFactor back to the Food Log")
    c = add("trace", "what was done, and what it cost: you on the page, mf on the phone (see mf/trace.py)")
    c.add_argument("date", nargs="?", help="day (default today)")
    c.add_argument(
        "--stats", action="store_true", help="per kind of act: count, median keys, clicks, seconds, phone time; slowest waits; failures"
    )
    c.add_argument("--raw", action="store_true", help="the lines themselves, one JSON object each (pipe to jq)")
    c = add("serve", "run the web UI")
    c.add_argument("--port", type=int, default=8377)
    c.add_argument("--host", default="127.0.0.1")

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    as_json = args.json

    def say(message: str) -> None:
        if not args.quiet and not as_json:
            print(f"  · {message}", file=sys.stderr, flush=True)

    if args.command == "trace":
        return _trace(_day(args.date) or date.today(), args.stats, args.raw, as_json)
    if args.command == "serve":  # the server builds its own service, on the thread that owns the phone
        from .server import serve

        serve(args.host, args.port)
        return 0
    interrupts: list[int] = []

    def interrupted(signum, _frame) -> None:
        """First Ctrl+C: ask the write to stop at its next safe point. Second: give up waiting."""
        if interrupts:
            raise KeyboardInterrupt
        interrupts.append(signum)
        print("mf: stopping…", file=sys.stderr, flush=True)

    signal.signal(signal.SIGINT, interrupted)

    def out(data: dict, human) -> None:
        if as_json:
            print(json.dumps(data, indent=2, default=str, ensure_ascii=False))
        else:
            human(data)

    try:
        service = Service(say=say)
        service.stop = lambda: bool(interrupts)
        match args.command:
            case "log":
                text = " ".join(args.spec)
                if args.dry_run:
                    plan = service.plan(text, _day(args.date), args.at)
                    out(
                        plan.describe(),
                        lambda d: (
                            [print(f"would log on {d['date']} at {d['time']}:")]
                            + [
                                print(f"  {i['repeat']}x {i['name']}  {i['quantity']:g} {i['unit']}  ~{i['kcal']:.0f} kcal")
                                for i in d["items"]
                            ]
                        ),
                    )
                else:
                    result = service.log(text, _day(args.date), args.at)
                    out(result, lambda d: [print(_fmt_entry(e)) for e in d["entries"]])
            case "day":
                out(service.day(_day(args.date) or date.today(), args.live), _print_day)
            case "rm":
                result = service.remove(args.n, _day(args.date) or date.today())
                out(result, lambda d: print("removed" + _fmt_entry(d["removed"])[4:]))
            case "cp":
                result = service.copy(args.n, _day(args.to), args.time, _day(args.date) or date.today())
                out(result, lambda d: [print(_fmt_entry(e)) for e in d["entries"]])
            case "edit":
                quantity, unit = _quantity(" ".join(args.quantity))
                result = service.edit(args.n, quantity, unit, _day(args.date) or date.today())
                out(result, lambda d: print(_fmt_entry(d["entry"])))
            case "mv":
                source = _day(args.date) or date.today()
                if args.to and _day(args.to) != source:
                    result = service.move(args.n, _day(args.to), args.time, source)
                else:
                    result = service.retime(args.n, args.time, source)
                out(result, lambda d: print(_fmt_entry(d["entry"])))
            case "undo":
                out(service.undo(), lambda d: print(f"undone: {d['undone']['kind']} from {d['undone']['at']}"))
            case "foods":
                foods = [f.as_dict() for f in service.store.search(" ".join(args.query), args.limit)]
                used = [{"name": name, "recipes": count} for name, count in service.store.ingredients(" ".join(args.query))[: args.limit]]

                def tell(d: dict) -> None:
                    for f in d["foods"]:
                        print(
                            f"{f['count']:>5}x  {f['display'][:52]:<52} "
                            + ", ".join(f"{s['quantity']:g} {s['unit']}" for s in f["servings"][:3])
                        )
                    if args.query and d["ingredients"]:
                        print("in your recipes:")
                        for i in d["ingredients"]:
                            print(f"{i['recipes']:>5}x  {i['name']}")

                out({"foods": foods, "ingredients": used}, tell)
            case "recipes":
                words = [w.lower() for w in args.query]
                # the recipes the page offers (workbook, created through mf, read from the phone's list), with a serving's
                # figures as last known; how many servings a recipe makes is not told: the phone's list does not say
                recipes = [
                    {"name": f.name, "logged": f.count, **f.per_unit["serving"]}
                    for f in service.store.foods()
                    if f.kind == "recipe" and "serving" in f.per_unit and all(w in f.name.lower() for w in words)
                ]
                recipes.sort(key=lambda r: r["name"].lower())

                def listed(d: dict) -> None:
                    for r in d["recipes"]:
                        weight = f"{r['grams']:g} g" if r["grams"] else "no weight"
                        print(f"{r['logged']:>4}x  {r['name'][:44]:<44} {r['kcal']:>5.0f} kcal a serving, {weight}")

                out({"recipes": recipes}, listed)
            case "search":

                def found(d: dict) -> None:
                    for f in d["found"]:
                        figures = f"{f['kcal']:>5} kcal {f['protein']:>3}P {f['fat']:>3}F {f['carbs']:>3}C"
                        print(f"{f['from']:<8} {f['name'][:64]:<64} {figures}  {f['serving']}")
                    for section, count in d["more"].items():
                        print(f"{section}: {count} more on the phone, not listed here")
                    if not d["found"]:
                        print("nothing found")

                out(service.search(" ".join(args.query)), found)
            case "recipe":
                if args.action in ("new", "try"):
                    if not args.ingredients:
                        raise SpecError("give the ingredients: -i 'lentils 200g, oil 10g' -i '=Cumin, Seed = 6 g'")
                    positive(args.servings, "servings")
                    if args.weight is not None:
                        positive(args.weight, "weight")
                    make = service.create_recipe if args.action == "new" else service.rehearse_recipe
                    result = make(args.name, args.ingredients, args.servings, args.weight)

                    def tell(d: dict) -> None:
                        recipe = d.get("recipe") or d["rehearsed"]
                        for i in recipe["ingredients"]:
                            print(
                                f"  {i['quantity']:>7g} {i['unit']:<8} {i['name'][:56]:<56} {i['kcal']:>6g} kcal "
                                f"{i['protein']:>5g}P {i['fat']:>5g}F {i['carbs']:>5g}C  ({i['from']})"
                            )
                        made = "created" if "recipe" in d else "would create"
                        figures = f"{recipe['kcal']:g} kcal {recipe['protein']:g}P {recipe['fat']:g}F {recipe['carbs']:g}C"
                        weight = recipe["ingredients_weight"]  # None: MacroFactor shows no weight for one of them
                        print(
                            f"{made} {recipe['name']}: {figures} per {recipe['serving']}"
                            + (f"; {weight:g} g of ingredients" if weight else "")
                        )

                    out(result, tell)
                else:
                    out(service.delete_recipe(args.name), lambda d: print(f"deleted recipe {d['deleted']}"))
            case "alias":
                if args.alias and (args.name or args.rm):
                    target = None
                    if not args.rm:
                        food = service.store.food(" ".join(args.name)) or next(iter(service.store.search(" ".join(args.name), 1)), None)
                        if food is None:
                            raise SpecError(f"no food matches {' '.join(args.name)!r}")
                        target = food.name
                    service.store.set_alias(args.alias, target)
                out(
                    {"aliases": service.store.aliases()},
                    lambda d: [print(f"{k:<16} {v}") for k, v in d["aliases"].items()],
                )
            case "sync":
                if args.what in ("recipes", "all-recipes"):
                    out(
                        service.read_recipes(everything=args.what == "all-recipes"),
                        lambda d: print(
                            f"recipes: {d['seen']} read ({'the whole list' if d['complete'] else 'the most recently modified'}); "
                            f"{d['new']} new, {d['changed']} changed, {d['gone']} gone"
                        ),
                    )
                elif args.what in PERIODS:
                    out(
                        service.sync_export(args.what, args.force),
                        lambda d: print(f"synced {d['rows']} entries ({d['period']}, one export)"),
                    )
                else:

                    def synced(d: dict) -> None:
                        print(
                            f"{d['day']}: {d['seen']} on the phone; here {d['added']} added, {d['changed']} changed, {d['removed']} removed"
                        )

                    out(service.sync_days(_day(args.what)), synced)
            case "backup":
                out(service.backup(args.force), lambda d: print(f"snapshot saved in {d['folder']}"))
            case "import":
                out(service.import_file(Path(args.path).expanduser()), lambda d: print(d))
            case "status":
                out(service.status(), lambda d: [print(f"{k:<18} {v}") for k, v in d.items()])
            case "history":
                ops = service.store.ops(args.limit)
                out(
                    {"ops": ops},
                    lambda d: [
                        print(
                            f"{o['at']}  {o['kind']:<7} {o['status']:<7} {_subject(o['request'])}"
                            + (f"  !! {o['error']}" if o["error"] else "")
                        )
                        for o in d["ops"]
                    ],
                )
            case "ui":
                with service.phone() as app:
                    snap = app.phone.snapshot()
                out({"foreground": snap.foreground, "screen": snap.dump()}, lambda d: print(d["screen"]))
            case "home":
                with service.phone() as app:
                    log = app.home()
                out({"title": log.title, "totals": log.totals}, lambda d: print(f"Food Log: {d['title']}"))
    except (SpecError, PhoneError, DatabaseFormatError, ExportFormatError) as error:
        if as_json:
            print(
                json.dumps(
                    {
                        "error": str(error),
                        "outcome": "uncertain" if isinstance(error, Uncertain) else "failed",
                        "candidates": getattr(error, "candidates", []),
                    },
                    ensure_ascii=False,
                )
            )
        else:
            print(f"mf: {error}", file=sys.stderr)
        return 2 if isinstance(error, Uncertain) else 1
    except Cancelled:
        print("mf: stopped; nothing was changed", file=sys.stderr)
        return 130
    except KeyboardInterrupt:
        return 130
    if interrupts:
        print("mf: too late to stop, it is done; `mf undo` takes a change back", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
