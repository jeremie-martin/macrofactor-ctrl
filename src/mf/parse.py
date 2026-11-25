"""The one-line food language of the CLI and of agents (the page sends explicit items: `plan_items`).

  apple 150g, skyr 200 g, 3x galette @8:30 yesterday

* items are separated by commas;
* a trailing number is the quantity, with an optional unit:  `apple 150`, `apple 150g`, `egg 2 large egg`;
* a leading number multiplies the usual serving into one entry:  `2 monster`;
* `3x galette` logs three separate entries of the usual serving;
* no number at all means the usual serving;
* `@8`, `@8:30`, `@20h15` set the time (default: now); `yesterday`, `today`, `tomorrow`,
  a weekday name or `2026-11-12` set the day (default: today).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .store import MACROS, Food, Store
from .text import norm

NUMBER = r"\d+(?:[.,]\d+)?"
TIME = re.compile(r"@\s*(\d{1,2})(?:[:h](\d{2}))?\b|@\s*now\b", re.I)
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
GRAM_UNITS = {"g": 1.0, "gr": 1.0, "gram": 1.0, "grams": 1.0, "kg": 1000.0}
COUNT_WORDS = {
    "x",
    "pc",
    "pcs",
    "piece",
    "pieces",
    "unit",
    "units",
    "serving",
    "servings",
    "portion",
    "portions",
    "each",
    "can",
    "cans",
}


class SpecError(ValueError):
    """The text could not be turned into foods. `candidates` lists what it might have meant."""

    def __init__(self, message: str, candidates: list[str] | None = None):
        super().__init__(message)
        self.candidates = candidates or []


def positive(value: float, label: str) -> float:
    if not math.isfinite(value) or value <= 0:
        raise SpecError(f"{label} must be a finite number greater than zero")
    return value


@dataclass
class Planned:
    food: Food
    quantity: float | None  # None: keep MacroFactor's last-used serving
    unit: str | None
    repeat: int = 1
    exact: bool = False  # named as MacroFactor's search lists it, in history or not (`exact_item`)

    def estimate(self) -> dict:
        """Expected nutrition, from history (the app's own figures are used once logged)."""
        quantity, unit = self.quantity, self.unit
        if quantity is None and self.food.servings:
            quantity, unit = self.food.servings[0]["quantity"], self.food.servings[0]["unit"]
        per = self.food.per_unit.get(unit or "", {})
        return {
            "name": self.food.display,
            "quantity": quantity,
            "unit": unit,
            "repeat": self.repeat,
            **{m: round((per.get(m) or 0) * (quantity or 0), 1) for m in MACROS},
        }


@dataclass
class Plan:
    items: list[Planned] = field(default_factory=list)
    day: date | None = None  # None: today
    hour: int | None = None  # None: now
    minute: int | None = None

    def describe(self, today: date | None = None) -> dict:
        today = today or date.today()
        return {
            "date": (self.day or today).isoformat(),
            "time": f"{self.hour:02d}:{self.minute or 0:02d}" if self.hour is not None else "now",
            "items": [item.estimate() for item in self.items],
        }


def parse_day(word: str, today: date | None = None) -> date | None:
    today = today or date.today()
    word = word.lower()
    if word in ("today", "yesterday", "tomorrow"):
        return today + timedelta(days={"today": 0, "yesterday": -1, "tomorrow": 1}[word])
    for index, name in enumerate(WEEKDAYS):
        if word in (name, name[:3]):
            return today - timedelta(days=(today.weekday() - index) % 7 or 7)  # the most recent such day
    match = re.fullmatch(r"-(\d+)d?", word)
    if match:
        try:
            return today - timedelta(days=int(match[1]))
        except (OverflowError, ValueError):
            return None
    for text, fmt in ((word, "%Y-%m-%d"), (f"{today.year}-{word}", "%Y-%m-%d")):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_time(text: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"@?\s*(\d{1,2})(?:[:h](\d{2}))?", text.strip(), re.I)
    if not match or int(match[1]) > 23 or int(match[2] or 0) > 59:
        return None
    return int(match[1]), int(match[2] or 0)


def parse(text: str, store: Store, today: date | None = None) -> Plan:
    plan = Plan()
    match = TIME.search(text)
    if match:
        if match[1] is not None:
            when = parse_time(match[0])
            if when is None:
                raise SpecError(f"not a time: {match[0]}")
            plan.hour, plan.minute = when
        text = text[: match.start()] + " " + text[match.end() :]
    words = text.split()
    while words and not words[-1].endswith(",") and (day := parse_day(words[-1], today)) is not None and len(words) > 1:
        plan.day = day
        words.pop()
    chunks = [chunk.strip() for chunk in " ".join(words).split(",") if chunk.strip()]
    while chunks:
        size, item = _comma_name(chunks, store)
        plan.items.append(item or resolve_item(chunks[0], store))
        del chunks[:size]
    if not plan.items:
        raise SpecError("no food given")
    return plan


def exact_item(text: str) -> Planned:
    """`=Cumin, Seed = 6 g`: a food by the exact name MacroFactor's search lists it under (see `mf search`), then its
    amount after a second `=`.

    This is how a recipe takes a food that was never logged: nothing is known of it here, so nothing is guessed, and
    nothing in a name or a unit is mistaken for the amount (`Maggi ... 120g By Nestlé`, `each - 2" diameter`). The
    unit is everything after the number, and must be one the phone offers for that food.
    """
    name, equals, amount = text.strip()[1:].rpartition(" = ")
    match = re.fullmatch(rf"({NUMBER})\s*(\S.*)", amount.strip())
    if not equals or not name.strip() or not match:
        raise SpecError(f"{text!r}: give the exact name, '=', then the amount and its unit, as in '=Cumin, Seed = 6 g'")
    name, quantity, unit = name.strip(), positive(float(match[1].replace(",", ".")), "quantity"), match[2].strip()
    if norm(unit) in GRAM_UNITS:
        quantity, unit = quantity * GRAM_UNITS[norm(unit)], "g"
    return Planned(Food(name, name, 0, "", 0.0, [], {}), quantity, unit, exact=True)


def ingredients(specs: list[str], store: Store) -> list[Planned]:
    """A recipe's ingredients, in order: each text is foods of the history in the food language, or one `=name = amount`."""
    planned: list[Planned] = []
    for text in specs:
        planned += [exact_item(text)] if text.lstrip().startswith("=") else parse(text, store).items
    if any(item.repeat != 1 for item in planned):
        raise SpecError("an ingredient is given once, with its amount")
    return planned


class SharedName(SpecError):
    """A name MacroFactor lists several recipes under: none is meant until one is said."""


def _no_food(name: str, store: Store) -> SpecError:
    for known, each in store.shared_recipes().items():
        if norm(known) == norm(name):
            return SharedName(f"MacroFactor has {len(each)} recipes named {known.strip()!r}; say which: " + "; ".join(each), list(each))
    return SpecError(f"no food named {name!r} in your history")


def plan_items(items: list[dict], store: Store) -> Plan:
    """A plan from explicit {"name", "quantity", "unit", "repeat"} items: what the web UI sends, nothing to guess."""
    plan = Plan()
    for item in items:
        food = store.food(item["name"])
        if food is None:
            raise _no_food(item["name"], store)
        quantity, unit = item.get("quantity"), None
        if quantity is not None:
            quantity, unit = _unit_for(food, float(quantity), item.get("unit") or None)
        repeat = item.get("repeat", 1)
        if type(repeat) is not int or not 1 <= repeat <= 20:
            raise SpecError("repeat must be a whole number from 1 to 20")
        plan.items.append(Planned(food, quantity, unit, repeat))
    if not plan.items:
        raise SpecError("no food given")
    return plan


def _comma_name(chunks: list[str], store: Store) -> tuple[int, Planned | None]:
    """Items are separated by commas, yet names like "Apple, Fresh, With Skin" contain some.

    Several leading chunks are one item only when, joined, they start with a food's full name.
    """
    for size in range(len(chunks), 1, -1):
        joined = ", ".join(chunks[:size])
        try:
            item = resolve_item(joined, store)
        except SpecError:
            continue
        if any("," in name and norm(joined).startswith(norm(name)) for name in (item.food.name, item.food.display)):
            return size, item
    return 1, None


def resolve_food(text: str, store: Store) -> Food:
    aliased = store.alias(text)
    if aliased and (food := store.food(aliased)):
        return food
    exact = store.food(text)
    if exact:
        return exact
    if isinstance(shared := _no_food(text, store), SharedName):
        raise shared
    found = store.search(text, limit=8)
    if not found:
        raise SpecError(f"no food in your history matches {text!r}")
    alike = found[0].shared is not None and any(f.phone_name == found[0].phone_name for f in found[1:])  # recipes of one name
    if len(found) == 1 or (found[0].score > 0 and found[0].score >= 3 * found[1].score and not alike):
        return found[0]
    raise SpecError(
        f"{text!r} is ambiguous: " + "; ".join(f.display for f in found[:6]) + ". Be more specific or add an alias.",
        [f.display for f in found],
    )


def _plausible_unit(food: Food, quantity: float) -> str:
    """The unit in which a bare number is a plausible amount of this food.

    "skyr 150" means grams and "yaourt 2" means servings: the unit is the one whose usual quantities
    are closest in magnitude to the number (grams count as a candidate whenever the food's weight is known).
    """
    options = [(s["unit"], s["quantity"]) for s in food.servings if s["quantity"]]
    if all(unit != "g" for unit, _ in options) and any(per.get("grams") for per in food.per_unit.values()):
        options.append(("g", 100.0))
    if not options or quantity <= 0:
        return food.servings[0]["unit"] if food.servings else next(iter(food.per_unit), "g")
    return min(options, key=lambda option: abs(math.log(quantity / option[1])))[0]


def _unit_for(food: Food, quantity: float, unit: str | None) -> tuple[float, str]:
    """Validate a spoken unit against the units this food has been logged with."""
    known = {norm(u): u for u in food.per_unit}
    if unit is None:
        return quantity, _plausible_unit(food, quantity)
    key = norm(unit)
    if key in known:
        return quantity, known[key]
    if key in GRAM_UNITS and "g" in known:
        return quantity * GRAM_UNITS[key], known["g"]
    if key in GRAM_UNITS and food.weightless:  # known from the phone: as the page, which does not offer grams for it
        raise SpecError(f"{food.display} has no weight in MacroFactor; its units: {', '.join(food.per_unit)}")
    if key in GRAM_UNITS:
        return quantity * GRAM_UNITS[key], "g"  # most foods can be weighed; MacroFactor will confirm
    if key in COUNT_WORDS and food.servings:
        return quantity * food.servings[0]["quantity"], food.servings[0]["unit"]
    raise SpecError(f"{food.display} has no unit {unit!r}; known units: {', '.join(food.per_unit) or 'g'}")


def resolve_item(chunk: str, store: Store) -> Planned:
    repeat = 1
    match = re.match(r"^(\d+)\s*x\s+(.+)$", chunk, re.I)
    if match:
        repeat, chunk = int(match[1]), match[2].strip()
        if not 1 <= repeat <= 20:
            raise SpecError(f"{repeat}x is out of range (1-20)")
    # The whole text naming a food wins: "Galettes 4 Céréales" and "Jello 1.5L" contain numbers.
    if store.alias(chunk) or store.food(chunk):
        return Planned(resolve_food(chunk, store), None, None, repeat)
    errors: list[SpecError] = []
    tokens = chunk.split()
    # A trailing quantity: "apple 150", "apple 150g", "egg 2 large egg".
    for index in range(len(tokens) - 1, 0, -1):
        number = re.fullmatch(rf"({NUMBER})([^\d\s].*)?", tokens[index])
        if not number:
            continue
        unit = " ".join(filter(None, [number[2], *tokens[index + 1 :]])) or None
        try:
            food = resolve_food(" ".join(tokens[:index]), store)
            quantity, unit = _unit_for(food, float(number[1].replace(",", ".")), unit)
            return Planned(food, quantity, unit, repeat)
        except SpecError as error:
            errors.append(error)
    # A leading quantity: "150g apple" is explicit, "2 monster" multiplies the usual serving.
    lead = re.fullmatch(rf"({NUMBER})([^\d\s]\S*)?", tokens[0]) if len(tokens) > 1 else None
    if lead:
        try:
            food = resolve_food(" ".join(tokens[1:]), store)
            if lead[2]:
                quantity, unit = _unit_for(food, float(lead[1].replace(",", ".")), lead[2])
            elif food.servings:
                quantity = float(lead[1].replace(",", ".")) * food.servings[0]["quantity"]
                unit = food.servings[0]["unit"]
            else:
                raise SpecError(f"{food.display} has no usual serving to multiply")
            return Planned(food, quantity, unit, repeat)
        except SpecError as error:
            errors.append(error)
    try:
        return Planned(resolve_food(chunk, store), None, None, repeat)
    except SpecError as error:
        raise (errors[0] if errors else error) from None
