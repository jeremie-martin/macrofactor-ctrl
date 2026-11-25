"""Characterization on the real phone, without saving anything: can mf stage and verify each kind of food?

    uv run python tools/probe.py                      # one or two foods of every kind found in the history
    uv run python tools/probe.py "Light Cake" "Egg"   # these foods, at their last-used serving
    uv run python tools/probe.py --item "Light Cake=0.5 serving" --item "Skyr Nature 0%=1 serving"

For each food it runs the real logging sequence on a day a few days ahead (search, open, set the serving,
expose and verify the plate) and stops at the saving tap, the way a cancel at the last moment does: the draft
is discarded and the hour is read again. Nothing is logged, no export is spent, the journal is not written.

    uv run python tools/probe.py --save               # then really save the ones that staged, and take them out again

With `--save` the foods that staged are then logged for real on that day, which must be empty: eight to a plate,
read back from the screen, each one edited to another amount, the day synced (which must find nothing to change),
everything removed and the day read empty again. This is what tells how a saved row of each kind is drawn. Take
a backup first if today's is not recent (`mf backup`, two exports); the checks themselves read the screen.

What the phone showed for each food (its search row, its detail, its plate row) is appended to
`~/.local/state/mf/probes/DATE.jsonl`. The point is to learn how MacroFactor presents the foods actually in
this log, in all their variety, rather than to assume it from the few that were tried by hand.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

from mf import screens as S
from mf.app import MacroFactor, same_food
from mf.device import Cancelled, Phone, PhoneError, phone_lock
from mf.service import Service, item_of
from mf.store import Food, Store
from mf.text import norm
from mf.trace import STATE_DIR

ROW = re.compile(r"\d+P\s+\d+F\s+\d+C")


def kind_of(food: Food, recipes: dict[str, dict]) -> tuple:
    """What could make the phone present this food differently from another."""
    unit = food.servings[0]["unit"] if food.servings else "?"
    recipe = recipes.get(food.name)
    return (
        "recipe" if recipe is not None else "food",
        "never logged" if not food.count else "logged",
        "g" if unit == "g" else "serving" if unit == "serving" else "other unit",
        "weight" if (recipe.get("Total Weight (g)") if recipe else (food.per_unit.get(unit) or {}).get("grams")) else "no weight",
        "ascii" if food.display.isascii() else "accents",
        "brand" if " By " in food.display else "no brand",
    )


def pick(store: Store, per_kind: int) -> list[Food]:
    recipes = {r["name"]: r for r in store.recipes()}
    kinds: dict[tuple, list[Food]] = defaultdict(list)
    for food in store.foods():  # best first: the foods really in use come first within a kind
        kinds[kind_of(food, recipes)].append(food)
    return [food for foods in kinds.values() for food in foods[:per_kind]]


def observe(snap, seen: dict) -> None:
    """Keep the distinct ways the food under test was drawn, by screen."""
    place = "plate" if S.plate(snap) else "log" if S.food_log(snap) else "logger"
    for node in snap.app():
        if ROW.search(node.label) and "in plate" not in node.label and node.clickable:
            seen.setdefault(f"{place} row", set()).add(node.label.replace("\n", " / "))
    pad = S._keypad(snap)
    if pad is not None:
        labels = [c.label for c in pad.children if c.label and not c.clickable]
        field = next((c for c in pad.children if c.cls == "EditText"), None)
        seen.setdefault("detail", set()).add(f"{field.text if field else '?'} | {' | '.join(labels)}")
        chips = (S.detail(snap) or S.Detail("", 0, 0, 0, 0, "", "", False, pad)).units
        if chips:
            seen.setdefault("units", set()).update(chips)


def save_and_check(items: list[dict], day: date) -> list[str]:
    """Log `items` for real on the empty `day`, exercise each saved entry, and leave the day empty. Returns what failed."""
    service = Service()
    failures: list[str] = []

    def expect(what: str, ok: bool) -> None:
        print(f"    {'ok  ' if ok else 'FAIL'} {what}", flush=True)
        if not ok:
            failures.append(what)

    def stored() -> list[dict]:
        return service.store.day(day)["entries"]

    def agree(what: str) -> None:
        """The screen and the local copy tell the same day."""
        shown = service.day(day, live=True)["entries"]
        same = len(shown) == len(stored()) and all(
            p["time"][:2] == s["time"][:2]
            and same_food(p["name"], s["name"])
            and abs(p["quantity"] - s["quantity"]) < 0.006
            and p["unit"] == s["unit"]
            for p, s in zip(shown, stored(), strict=False)
        )
        expect(f"{what}: the phone shows the {len(stored())} entries of the local copy", same)
        if not same:
            print(f"      phone  {[(e['name'], e['serving']) for e in shown]}")
            print(f"      stored {[(e['name'], e['quantity'], e['unit']) for e in stored()]}")

    if stored() or service.day(day, live=True)["entries"]:
        return [f"{day} is not empty; choose another day with --ahead"]
    try:
        for start in range(0, len(items), 8):
            plate = items[start : start + 8]
            print(f"saving {', '.join(i['name'] for i in plate)}", flush=True)
            before = len(stored())
            result = service.log(plate, day, "12:00")
            expect(f"{len(plate)} foods saved and confirmed", len(result["entries"]) == len(plate))
            agree("after saving")
            for entry in stored()[before:]:
                other = entry["quantity"] * 2 if entry["quantity"] < 50 else entry["quantity"] + 1
                edited = service.edit(entry, other)["entry"]
                expect(
                    f"{entry['name']} {entry['quantity']:g} {entry['unit']} edited to {other:g}", abs(edited["quantity"] - other) < 0.006
                )
            agree("after editing each")
        count = service.sync_days(day)
        expect(
            f"a sync of the day finds nothing to change ({count['seen']} seen)",
            not (count["added"] or count["changed"] or count["removed"]),
        )
    except (PhoneError, ValueError) as error:
        expect(f"no failure ({error})", False)
    finally:
        print("removing what was saved", flush=True)
        service.sync_days(day)  # whatever is there, including a save whose verification failed
        wanted = {service.store.food(i["name"]).phone_name for i in items}  # entries carry the phone's name, not "Soup (201 kcal)"
        if any(entry["name"] not in wanted for entry in stored()):
            raise RuntimeError(f"unexpected food on {day}; look at it before removing anything")
        for entry in stored():
            service.remove(entry)
        expect(f"{day} is empty again on the phone and in the local copy", not service.day(day, live=True)["entries"] and not stored())
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("foods", nargs="*", help="exact food names; default: a sample of every kind in the history")
    parser.add_argument("--item", action="append", default=[], metavar="NAME=QTY UNIT", help="a food at an explicit serving")
    parser.add_argument("--per-kind", type=int, default=1)
    parser.add_argument("--units", action="store_true", help="also try every other unit each sampled food was logged with")
    parser.add_argument("--ahead", type=int, default=3, help="days from today of the day whose 12:00 the drafts are aimed at")
    parser.add_argument("--list", action="store_true", help="show what would be probed and stop")
    parser.add_argument("--save", action="store_true", help="then log for real the foods that staged, check them, and remove them")
    args = parser.parse_args()
    store = Store()
    recipes = {r["name"]: r for r in store.recipes()}
    items: list[tuple[Food, float | None, str | None]] = []
    for name in args.foods:
        food = store.food(name)
        if food is None:
            parser.error(f"no food named {name!r}")
        items.append((food, None, None))
    for text in args.item:
        name, _, serving = text.partition("=")
        quantity, _, unit = serving.strip().partition(" ")
        food = store.food(name.strip())
        if food is None:
            parser.error(f"no food named {name!r}")
        items.append((food, float(quantity), unit.strip()))
    if not items:
        for food in pick(store, args.per_kind):
            items.append((food, None, None))
            if args.units:  # the last-used unit is already selected when the food opens; another one has to be chosen
                items += [(food, s["quantity"], s["unit"]) for s in food.servings[1:] if s["unit"] != food.servings[0]["unit"]][:2]
    # no amount given: the food's usual serving
    items = [(f, *((f.servings[0]["quantity"], f.servings[0]["unit"]) if q is None and f.servings else (q, u))) for f, q, u in items]
    for food, quantity, unit in items:
        print(f"{' / '.join(kind_of(food, recipes)):<62} {food.display} {quantity:g} {unit}")
    if args.list:
        return 0

    day = date.today() + timedelta(days=args.ahead)
    report = STATE_DIR / "probes" / f"{date.today().isoformat()}.jsonl"
    report.parent.mkdir(parents=True, exist_ok=True)
    failures = 0
    staged: list[dict] = []
    phone = Phone()
    read = phone.snapshot
    seen: dict[str, set] = {}

    def snapshot():
        snap = read()
        observe(snap, seen)
        return snap

    phone.snapshot = snapshot
    for food, quantity, unit in items:
        time.sleep(1)  # between foods the phone is anyone's: whoever waits for it polls, and must find it free (a log asked
        # from the page during a ten-minute run was refused as "busy" after two minutes, the lock retaken each time)
        said: list[str] = []
        seen.clear()
        started = time.monotonic()
        with phone_lock():  # one food at a time: a log asked from the page meanwhile waits for one draft, not for all
            phone.wake()
            app = MacroFactor(phone, log=said.append, cancelled=lambda said=said: "saving" in said)
            try:
                app.log([item_of(food, quantity, unit)], day, 12, 0)
                outcome = "SAVED"  # cannot happen: the cancel is honoured before the saving tap
            except Cancelled:
                outcome = "ok"
            except PhoneError as error:
                outcome = f"FAILED: {error}"
            except Exception as error:  # noqa: BLE001 - a defect in the driver is a finding too; its draft was backed out
                outcome = f"CRASHED: {type(error).__name__}: {error}"
            phone.checkpoint = None
        failures += outcome != "ok"
        if outcome == "ok" and food.count:  # saving a recipe never logged would put it in the phone's history for good
            staged.append({"name": food.name, "quantity": quantity, "unit": unit})
        mine = {
            key: sorted(v for v in values if key in ("detail", "units") or norm(food.phone_name) in norm(v)) for key, values in seen.items()
        }
        line = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "food": food.name,
            "kind": kind_of(food, recipes),
            "asked": [quantity, unit],
            "outcome": outcome,
            "seconds": round(time.monotonic() - started, 1),
            "steps": said,
            "seen": mine,
        }
        with report.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")
        print(f"{outcome[:150]:<40} {line['seconds']:5.1f} s  {food.display} {quantity:g} {unit}", flush=True)
        if outcome == "SAVED":
            print("a probe saved food: stopping; look at the phone")
            return 3
    phone.snapshot = read
    with phone_lock():
        MacroFactor(phone).goto(date.today())  # leave the app on today
    print(f"\n{len(items) - failures} of {len(items)} staged and verified; details in {report}")
    if args.save and staged:
        failed = save_and_check(staged, day)
        with phone_lock():
            MacroFactor(phone).goto(date.today())
        print("saved, checked and removed: ALL GOOD" if not failed else "FAILED:\n  " + "\n  ".join(failed))
        failures += len(failed)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
