"""A MacroFactor that is not one: the service's own code runs against it, in tests and in the sandbox."""

import contextlib
from dataclasses import replace
from datetime import date
from types import SimpleNamespace

from mf.app import AppError, DayView, Entry
from mf.device import Cancelled
from mf.service import Service
from mf.store import MACROS


class FakeApp:
    """Keeps a food log in memory with the same surface the service uses on the real driver."""

    def __init__(self):
        self.rows: list[Entry] = []
        self.calls: list[str] = []
        self.fail_next = False
        self.fail_remove_of: str | None = None
        self.fail_retime = False
        self.recipes: list[tuple] = []  # (name, serving as listed, kcal, protein, fat, carbs), most recently modified first
        self.stop_next = False  # the next write is stopped before it saves anything
        self.hide_totals = False  # the Food Log exposes no totals (seen once on the phone)
        self.times_drawn = False  # beside the rows, where the driver could see them: every row's minute is then told
        self.saving = lambda: None
        self.fail_after_save = False

    def read_recipes(self, known=None):
        """The Library's list, most recently modified first: eight rows to a screen, stopping at a screen already known."""
        self.calls.append("recipes")
        rows = [SimpleNamespace(name=n, serving=sv, kcal=k, protein=p, fat=f, carbs=c) for n, sv, k, p, f, c in self.recipes]
        for end in range(8, len(rows) + 8, 8):
            if known is not None and all(known(row) for row in rows[end - 8 : end]):
                return rows[:end], False
        return rows, True

    def create_recipe(self, name, items, servings=1, total_weight=None, rehearse=False):
        """As the wizard answers: each ingredient as it showed when added, and the recipe's figures a serving."""
        self.calls.append("rehearse" if rehearse else "recipe")
        added = [
            {"name": i.name, "from": "common" if i.exact else "history", "quantity": i.quantity, "unit": i.unit, "grams": i.quantity}
            | dict(zip(MACROS, self.price(i), strict=True))
            for i in items
        ]
        figures = {m: round(sum(i[m] for i in added) / servings) for m in MACROS}
        if not rehearse:
            self.saving()
            self.recipes.insert(0, (name, "1 serving", *figures.values()))
        return {"name": name, **figures, "serving": "1 serving", "ingredients": added, "ingredients_weight": sum(i["grams"] for i in added)}

    def search(self, query):
        self.calls.append("search")
        return [SimpleNamespace(name="Cumin, Seed", section="Common", serving="1 tbsp (6 g)", kcal=23, protein=1, fat=1, carbs=3)], {
            "From History": 7
        }

    def price(self, item) -> tuple[float, float, float, float]:
        """kcal, protein, fat and carbs the app would show for it."""
        if item.shown and item.unit == "serving":  # one of several recipes of a name: as its own serving says
            return tuple(item.shown[m] * item.quantity for m in MACROS)
        return 10, 1, 1, 1

    def _hour(self, day: date, hour: int) -> list[Entry]:
        rows = [r for r in self.rows if r.day == day and r.hour == hour]
        for position, row in enumerate(rows):
            row.position = position
        return rows

    def read_day(self, day):
        """As the screen tells it: rows in time order, each hour's distinct minutes, and a row's own when its hour has
        one or when the times drawn beside the rows could be made out."""
        rows = sorted((r for r in self.rows if r.day == day), key=lambda r: (r.hour, r.minute or 0))
        totals = {m: (sum(getattr(r, m) for r in rows), 2000) for m in MACROS}
        times = {hour: sorted({r.minute for r in rows if r.hour == hour and r.minute is not None}) for hour in {r.hour for r in rows}}
        return DayView(totals, [replace(r, minute=r.minute if len(times[r.hour]) == 1 or self.times_drawn else None) for r in rows], times)

    def hour_entries(self, day, hour, navigate=True):
        return self._hour(day, hour)

    def log(self, items, day=None, hour=None, minute=None):
        self.calls.append("log")
        if self.stop_next:
            self.stop_next = False
            raise Cancelled()
        if self.fail_next:
            self.fail_next = False
            raise AppError("search results never appeared")
        self.saving()
        saved = []
        for item in items:
            quantity, unit = item.quantity or 150, item.unit or "g"
            row = Entry(day or date.today(), 9 if hour is None else hour, item.name, f"{quantity:g} {unit}", *self.price(item), minute or 0)
            self.rows.append(row)
            saved.append(row)
        if self.fail_after_save:
            self.fail_after_save = False
            raise AppError("saved, but the screen could not confirm it")
        return saved

    def remove(self, before, position):
        self.calls.append("remove")
        row = before[position]
        if row.name == self.fail_remove_of:
            raise AppError("Remove button not found")
        self.saving()
        self.rows.remove(row)

    def edit(self, before, position, quantity, unit=None):
        self.saving()
        row = before[position]
        row.serving = f"{quantity:g} {unit or row.serving.split(' ', 1)[1]}"
        row.kcal = 99
        return row

    def move(self, before, positions, to_day, new_hour):
        """As MacroFactor's Move was seen to do: the entry itself goes; its minute is kept within the same hour, else :00."""
        self.calls.append("move")
        source = before[positions[0]].day
        self.saving()
        moved = [before[p] for p in positions]
        for row in moved:
            row.minute = row.minute if row.hour == new_hour else 0
            row.day, row.hour = to_day, new_hour
            self.rows.remove(row)  # it arrives after what the hour already holds
            self.rows.append(row)
        self._hour(to_day, new_hour)
        return moved, SimpleNamespace(totals={} if self.hide_totals else self.read_day(source).totals)

    def retime(self, before, positions, new_hour, new_minute):
        self.calls.append("retime")
        if self.fail_retime:
            raise AppError("Modify Timestamp not found")
        self.saving()
        for row in [before[p] for p in positions]:
            row.hour, row.minute = new_hour, new_minute or 0


def fake_service(store, app: FakeApp | None = None) -> Service:
    """A real service whose phone is `app`."""
    service = Service(store)
    service.app = app or FakeApp()
    service.app.saving = lambda: service._committing() if service._owns_phone else None
    service.phone = lambda: contextlib.nullcontext(service.app)
    return service
