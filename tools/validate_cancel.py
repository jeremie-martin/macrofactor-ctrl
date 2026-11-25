"""Validation on the real phone: stop every kind of write at many different moments and check what is left.

    uv run python tools/validate_cancel.py            # uses the day three days from now
    uv run python tools/validate_cancel.py --ahead 5

This drives MacroFactor for about twenty minutes. It is built to be safe on a log in daily use:
  * it takes one full backup (`mf backup`); subsequent checks read the screen and spend no exports;
  * it only ever writes to one future day, which must be empty on the phone when it starts;
  * every stopped write is followed by the driver's own re-reading of the hour, and each phase by a full
    reading of the day from the screen;
  * whatever happens, it finishes by removing what it logged and reading the day once more.
Run it after changing the driver or after a MacroFactor update.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
from datetime import date, timedelta
from pathlib import Path

from mf.app import same_food
from mf.device import Cancelled
from mf.service import Service
from mf.store import DATA_DIR

HOUR = "12:00"


class Run:
    def __init__(self, day: date):
        self.day = day
        self.said: list[str] = []
        self.service = Service(say=self.said.append)
        self.checks = 0
        self.stop_at: int | None = None
        self.service.stop = self._stop
        self.failures: list[str] = []

    def _stop(self) -> bool:
        self.checks += 1
        return self.checks == self.stop_at

    # ---- observations

    def stored(self) -> list[tuple]:
        return [(e["time"], e["name"], e["quantity"], e["unit"]) for e in self.service.store.day(self.day)["entries"]]

    def on_phone(self) -> list[tuple]:
        self.service.stop = None
        try:
            return [(e["time"][:2], e["name"], e["quantity"], e["unit"]) for e in self.service.day(self.day, live=True)["entries"]]
        finally:
            self.service.stop = self._stop

    def expect(self, what: str, ok: bool) -> None:
        print(f"    {'ok  ' if ok else 'FAIL'} {what}", flush=True)
        if not ok:
            self.failures.append(what)

    def expect_day(self, what: str) -> None:
        """The phone's screen and the local data agree on the day."""
        stored, shown = self.stored(), self.on_phone()
        same = len(stored) == len(shown) and all(
            s[0][:2] == p[0] and same_food(p[1], s[1]) and abs(s[2] - p[2]) < 0.01 and s[3] == p[3]
            for s, p in zip(stored, shown, strict=False)
        )
        self.expect(f"{what}: phone shows {len(shown)} entr{'y' if len(shown) == 1 else 'ies'}, as stored {stored}", same)

    # ---- one attempt

    def attempt(self, call, stop_at: int | None):
        """Run a write, asking it to stop at its `stop_at`-th checkpoint. Returns (result or None if it stopped, checks made)."""
        self.checks, self.stop_at = 0, stop_at
        self.said.clear()
        started = time.monotonic()
        try:
            result = call()
        except Cancelled:
            result = None
        self.stop_at = None  # what follows (taking back, reading) is not to be stopped
        where = next((m for m in reversed(self.said) if m != "cancelling"), "before anything was opened")
        outcome = "stopped" if result is None else "completed"
        took = time.monotonic() - started
        print(f"  stop at {stop_at or '-':>3}: {outcome} after {self.checks:>3} checks, {took:4.1f} s, last step: {where}", flush=True)
        return result, self.checks

    def sweep(self, name: str, call, total: int) -> None:
        """Stop `call` at checkpoints spread over a run of about `total`, including the very first and the very last."""
        picks = sorted({1, 2, total - 1, total, total + 1, *(round(total * i / 10) for i in range(1, 10))} - {0})
        print(f"{name}: stopping at checkpoints {picks} of about {total}", flush=True)
        before = self.stored()
        for stop_at in picks:
            result, _ = self.attempt(call, stop_at)
            if result is None:
                self.expect("nothing changed in the local data", self.stored() == before)
            else:  # this run was quicker than the measured one and got past its saving tap first
                self.expect("a write that could not be stopped any more is taken back", self.service.take_back(result))
        self.expect_day(f"after the {name} sweep")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ahead", type=int, default=3, help="days from today of the (empty) day to use")
    parser.add_argument("--baseline", type=Path, help="reuse this session's checksummed backup when rerunning; spend no more exports")
    parser.add_argument("--only", choices=("log", "edit", "retime", "remove", "long-hour", "plate"), help="rerun just the affected phase")
    parser.add_argument("--recipe", help="include plate validation with this previously logged recipe (exact name, serving unit)")
    args = parser.parse_args()
    if args.ahead < 1:
        parser.error("the day must be in the future")
    today = date.today()
    run = Run(today + timedelta(days=args.ahead))
    service, day = run.service, run.day
    food = next(f for f in service.store.foods() if "g" in f.per_unit and f.kind == "food")
    one = [{"name": food.name, "quantity": 111, "unit": "g"}]
    recipe = next((f for f in service.store.foods() if f.name == args.recipe and f.kind == "recipe" and "serving" in f.per_unit), None)
    if (args.only == "plate" or args.recipe) and recipe is None:
        parser.error("plate validation needs --recipe with the exact name of a previously logged recipe offering servings")
    allowed = [{"name": food.name, "quantity": q, "unit": "g"} for q in (111, 112, 113, 222, 333)]
    if recipe:
        allowed += [{"name": recipe.name, "quantity": q, "unit": "serving"} for q in (0.5, 1.5)]
    print(f"day {day}, food {food.display!r}", flush=True)

    service.stop = None
    if run.stored() or run.on_phone():
        print(f"{day} is not empty; choose another day with --ahead")
        return 2
    service.stop = None
    if args.baseline:
        folder = args.baseline.resolve()
        if folder.parent != (DATA_DIR / "snapshots").resolve():
            parser.error("the baseline must be an original mf snapshot")
        checksums = (folder / "SHA256SUMS").read_text().splitlines()
        if len(checksums) != 2:
            parser.error("the baseline must contain both exports")
        for line in checksums:
            digest, name = line.split("  ", 1)
            if Path(name).name != name or hashlib.sha256((folder / name).read_bytes()).hexdigest() != digest:
                parser.error("the baseline failed its checksum")
        baseline = str(folder)
        print(f"reusing baseline {baseline}; no exports", flush=True)
    else:
        print("backup before (the only exports this validation uses)", flush=True)
        baseline = service.backup()["folder"]

    def elsewhere():
        return [
            tuple(row)
            for row in service.store.db.execute(
                "SELECT * FROM entries WHERE date NOT IN (?,?) ORDER BY id", (today.isoformat(), day.isoformat())
            )
        ]

    original = elsewhere()
    service.stop = run._stop

    def entry() -> dict:
        return service.store.day(day)["entries"][0]

    def log():
        return service.log(one, day, HOUR)

    try:
        if args.only in (None, "log"):
            result, total = run.attempt(log, None)
            run.expect("a completed log is in the local data", len(run.stored()) == 1)
            run.expect("taking the log back", service.take_back(result))
            run.expect_day("after taking a log back")
            run.sweep("log", log, total)

        # Measure exactly the call being swept, so the checkpoints bracket its saving tap.
        changes = {
            "edit": lambda: service.edit(entry(), 113),
            "retime": lambda: service.retime(entry(), "12:45"),
            "remove": lambda: service.remove(entry()),
        }
        for kind, call in changes.items():
            if args.only not in (None, kind):
                continue
            if not run.stored():
                run.attempt(log, None)
                run.expect_day("with one entry")
            before = run.stored()
            result, total = run.attempt(call, None)
            run.expect(f"taking the {kind} back", service.take_back(result) and run.stored() == before)
            run.sweep(kind, call, total)

        # -- the regression: a long hour of identical rows, then another add from its scrolled tail
        if args.only in (None, "long-hour"):
            service.stop = None
            for left in service.store.day(day)["entries"]:
                service.remove(left)
            print("long hour with repeated rows", flush=True)
            service.log(one * 7, day, HOUR)
            service.log(one, day, HOUR)
            run.expect("eight identical entries confirmed", len(run.stored()) == 8)
            run.expect_day("after adding to a long hour")

        if recipe and args.only in (None, "plate"):
            service.stop = None
            for left in service.store.day(day)["entries"]:
                service.remove(left)
            half, large = allowed[-2:]
            cases = {
                "fractional recipe": [half],
                "mixed foods and amounts": [one[0], half, allowed[3], large, allowed[4]],
                "long plate with recipe": one * 7 + [half],
            }
            for label, items in cases.items():
                print(f"plate: {label}", flush=True)
                result = service.log(items, day, HOUR)
                run.expect("every requested entry confirmed", len(run.stored()) == len(items))
                run.expect_day(label)
                run.expect(f"taking back {label}", service.take_back(result))
                run.expect_day(f"after taking back {label}")
            for at in ("checking plate", "saving"):
                run.said.clear()
                service.stop = lambda at=at: at in run.said
                try:
                    result = service.log([half, one[0]], day, HOUR)
                except Cancelled:
                    run.expect(f"recipe cancelled at {at}", not run.stored())
                else:
                    run.expect(f"recipe cancelled at {at}", False)
                    service.stop = None
                    service.take_back(result)
                finally:
                    service.stop = None
                run.expect_day(f"after recipe cancellation at {at}")
    finally:
        print("cleaning up", flush=True)
        service.stop = None
        service.sync_days(day)  # include anything saved even if its verification failed
        remaining = service.store.day(day)["entries"]
        if any(
            not any(same_food(e["name"], i["name"]) and e["quantity"] == i["quantity"] and e["unit"] == i["unit"] for i in allowed)
            for e in remaining
        ):
            raise RuntimeError(f"unexpected food on {day}; inspect it before cleanup")
        for left in remaining:
            service.remove(left)
        shown = service.day(day, live=True)["entries"]
        run.expect(f"{day} is empty again on the phone and in the local data", not shown and not run.stored())
        service.day(today, live=True)  # leave the app on today

    run.expect("local entries outside today and the validation day are unchanged", original == elsewhere())
    print(f"\nbaseline {baseline}\nFinal verification used screen reads, not another export.")
    print("ALL GOOD" if not run.failures else "FAILED:\n  " + "\n  ".join(run.failures))
    return 1 if run.failures else 0


if __name__ == "__main__":
    sys.exit(main())
