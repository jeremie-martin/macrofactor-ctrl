"""The food language: what `mf log ...` understands, and how the web UI's explicit items are checked."""

from datetime import timedelta

import pytest
from conftest import TODAY

from mf.parse import SpecError, parse, parse_day, parse_time


def items(plan):
    return [(i.food.name, i.quantity, i.unit, i.repeat) for i in plan.items]


def test_bare_name_keeps_usual_serving(store):
    plan = parse("apple", store)
    assert items(plan) == [("Apple, Fresh", None, None, 1)]
    assert (plan.day, plan.hour, plan.minute) == (None, None, None)
    assert plan.describe()["items"][0]["quantity"] == 150
    assert plan.describe()["time"] == "now"


def test_trailing_number_is_a_quantity(store):
    assert items(parse("apple 180g", store)) == [("Apple, Fresh", 180.0, "g", 1)]
    assert items(parse("apple 180 g", store)) == [("Apple, Fresh", 180.0, "g", 1)]
    assert items(parse("lentil 0.5 serving", store)) == [("Lentil Stew", 0.5, "serving", 1)]


def test_leading_number_multiplies_usual_serving(store):
    assert items(parse("2 apple", store)) == [("Apple, Fresh", 300.0, "g", 1)]


def test_repeat_makes_separate_entries(store):
    plan = parse("3x galettes 4 @8:30 yesterday", store)
    assert items(plan) == [("Galettes 4 Céréales", 4.0, "galette", 3)]
    assert (plan.day, plan.hour, plan.minute) == (TODAY - timedelta(days=1), 8, 30)


def test_several_foods_share_one_time(store):
    plan = parse("skyr 200 g, apple @20h15", store)
    assert [i[0] for i in items(plan)] == ["Skyr Nature 0%", "Apple, Fresh"]
    assert (plan.hour, plan.minute) == (20, 15)
    assert plan.describe()["time"] == "20:15"


def test_most_logged_food_wins_an_ambiguous_word(store):
    assert items(parse("galette", store))[0][0] == "Galettes 4 Céréales"


def test_alias_beats_search(store):
    store.set_alias("galette", "Galettes De Riz")
    assert items(parse("galette 40g", store)) == [("Galettes De Riz", 40.0, "g", 1)]


def test_unknown_food_and_unknown_unit_are_refused(store):
    with pytest.raises(SpecError, match="no food"):
        parse("unobtainium 3g", store)
    with pytest.raises(SpecError, match="no unit"):
        parse("apple 2 galette", store)


def test_days_and_times():
    assert parse_day("yesterday", TODAY) == TODAY - timedelta(days=1)
    assert parse_day("-3", TODAY) == TODAY - timedelta(days=3)
    assert parse_day("2026-01-05", TODAY).isoformat() == "2026-01-05"
    assert parse_day("apple", TODAY) is None
    weekday = parse_day(f"{TODAY:%a}".lower(), TODAY)
    assert weekday == TODAY - timedelta(days=7)  # a weekday name means the most recent past one
    assert parse_time("8") == (8, 0)
    assert parse_time("20:05") == (20, 5)
    assert parse_time("8h30") == (8, 30)
    assert parse_time("25:00") is None


def test_commas_inside_food_names_do_not_split_items(store):
    plan = parse("Apple, Fresh 120 g, skyr 100g, apple, fresh", store)
    assert items(plan) == [("Apple, Fresh", 120.0, "g", 1), ("Skyr Nature 0%", 100.0, "g", 1), ("Apple, Fresh", None, None, 1)]
    assert items(parse("apple, skyr", store)) == [("Apple, Fresh", None, None, 1), ("Skyr Nature 0%", None, None, 1)]


def test_recipe_never_logged_resolves_by_name(store):
    store.save_recipe("Winter Soup", {"Serving Qty": 2, "Calories (kcal)": 500})
    plan = parse("winter soup 1.5 serving", store)
    assert items(plan) == [("Winter Soup", 1.5, "serving", 1)]
    assert plan.items[0].food.count == 0


def test_explicit_items_need_no_guessing(store):
    from mf.parse import plan_items

    plan = plan_items([{"name": "Apple, Fresh", "quantity": 120, "unit": "g"}, {"name": "Skyr Nature 0%", "repeat": 2}], store)
    assert items(plan) == [("Apple, Fresh", 120.0, "g", 1), ("Skyr Nature 0%", None, None, 2)]
    with pytest.raises(SpecError, match="no food named"):
        plan_items([{"name": "Unobtainium"}], store)
    with pytest.raises(SpecError, match="no unit"):
        plan_items([{"name": "Apple, Fresh", "quantity": 1, "unit": "galette"}], store)


def test_bare_number_takes_the_unit_in_which_it_is_plausible(store):
    assert items(parse("lentil 300", store)) == [("Lentil Stew", 300.0, "g", 1)]  # 300 servings would be absurd
    assert items(parse("lentil 2", store)) == [("Lentil Stew", 2.0, "serving", 1)]
    assert items(parse("galettes 4 3", store)) == [("Galettes 4 Céréales", 3.0, "galette", 1)]
    assert items(parse("galettes 4 45", store)) == [("Galettes 4 Céréales", 45.0, "g", 1)]


@pytest.mark.parametrize(
    "text, name, quantity, unit",
    [
        ("=Cumin, Seed = 6 g", "Cumin, Seed", 6, "g"),
        ("=15% Creme Legere Epaisse By Auchan = 203g", "15% Creme Legere Epaisse By Auchan", 203, "g"),
        (
            "= Maggi Bouillon Kub Volaille Réduit En Sel De -25% 120g By Nestlé = 20 g",
            "Maggi Bouillon Kub Volaille Réduit En Sel De -25% 120g By Nestlé",
            20,
            "g",
        ),
        ("=Olive Oil = 45 ml", "Olive Oil", 45, "ml"),
        ("=Butternut Squash = 1.5 cup, cubes", "Butternut Squash", 1.5, "cup, cubes"),
        ("=Lentils Red Raw = 0,3 kg", "Lentils Red Raw", 300, "g"),
        ('=Biscuit, Plain 1 = 2 each - 2" diameter', "Biscuit, Plain 1", 2, 'each - 2" diameter'),  # digits in a name, in a unit
    ],
)
def test_a_food_named_exactly_is_taken_as_written_with_its_amount(text, name, quantity, unit):
    from mf.parse import exact_item

    item = exact_item(text)
    assert (item.food.name, item.quantity, item.unit, item.exact) == (name, quantity, unit, True)


def test_a_food_named_exactly_needs_its_amount_and_unit(store):
    from mf.parse import exact_item, ingredients

    for text in ("=Cumin, Seed", "=Cumin, Seed 6 g", "=Cumin, Seed = 6", "= = 6 g", "=Cumin, Seed = 0 g"):
        with pytest.raises(SpecError):
            exact_item(text)
    planned = ingredients(["apple 120g, skyr 100g", "=Cumin, Seed = 6 g"], store)
    assert [(p.food.name, p.exact) for p in planned] == [("Apple, Fresh", False), ("Skyr Nature 0%", False), ("Cumin, Seed", True)]
    with pytest.raises(SpecError):
        ingredients(["3x apple"], store)
