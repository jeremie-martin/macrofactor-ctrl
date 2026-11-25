// Run with: node --test tests/   (pytest runs it too, see tests/test_web_core.py)
const test = require("node:test");
const assert = require("node:assert/strict");
const MF = require("../src/mf/web/core.js");

const food = (name, servings, per_unit, count = 5, kind = "food") => ({ name, display: name, servings, per_unit, count, kind });
const skyr = food("Skyr Nature 0% By Laiterie", [{ quantity: 150, unit: "g" }], { g: { kcal: 0.6, protein: 0.1, fat: 0, carbs: 0.04, grams: 1 } }, 90);
const yog = food("Yaourt Nature", [{ quantity: 1, unit: "serving" }], { serving: { kcal: 48, protein: 6, fat: 0, carbs: 6, grams: 125 } }, 40);
const drink = food("White Energy Drink", [{ quantity: 1, unit: "serving" }, { quantity: 16, unit: "fl oz" }], { serving: { kcal: 10, protein: 0, fat: 0, carbs: 2, grams: null }, "fl oz": { kcal: 0.6, protein: 0, fat: 0, carbs: 0.1, grams: null } }, 20);
const apple = food("Apple, Fresh, With Skin", [{ quantity: 182, unit: "g" }], { g: { kcal: 0.52, protein: 0, fat: 0, carbs: 0.14, grams: 1 } }, 300);
const soup = food("Winter Soup", [{ quantity: 1, unit: "serving" }], { serving: { kcal: 200, protein: 10, fat: 5, carbs: 20, grams: null } }, 0, "recipe");
const foods = MF.prepare([apple, skyr, yog, drink, soup]); // most eaten first

test("times of day", () => {
  assert.equal(MF.parseTime("8"), "08:00");
  assert.equal(MF.parseTime("830"), "08:30");
  assert.equal(MF.parseTime("8:05"), "08:05");
  assert.equal(MF.parseTime("20h15"), "20:15");
  assert.equal(MF.parseTime("0830"), "08:30");
  assert.equal(MF.parseTime("24"), null);
  assert.equal(MF.parseTime("8:61"), null);
  assert.equal(MF.parseTime("soon"), null);
  assert.equal(MF.parseTime(""), null);
});

test("numbers are shown as typed", () => {
  assert.equal(MF.qty(7.666), "7.67");
  assert.equal(MF.qty(150), "150");
  assert.equal(MF.gram(14.4), "14");
  assert.equal(MF.gram(5.24), "5.2");
  assert.equal(MF.gram(0), "0");
  assert.equal(MF.number("12,5"), 12.5);
  assert.equal(MF.number("x"), null);
  assert.equal(MF.number("12abc"), null);
  assert.equal(MF.number("1.2.3"), null);
  assert.equal(MF.number(" .5 "), 0.5);
});

test("a food can always be weighed, and its nutrition follows when the weight is known", () => {
  assert.deepEqual(MF.unitsOf(yog), ["serving", "g"]);
  assert.deepEqual(MF.unitsOf(skyr), ["g"]);
  assert.deepEqual(MF.unitsOf({ ...yog, weightless: true }), ["serving"]); // MacroFactor shows no weight for it: no grams to log it in
  assert.equal(Math.round(MF.macros(yog, 250, "g").kcal), 96);
  assert.equal(MF.macros(drink, 100, "g"), null); // weight of a serving unknown: no invented numbers
  assert.equal(MF.macros(skyr, 200, "g").protein, 20);
});

test("a bare number takes the unit in which it is plausible", () => {
  assert.equal(MF.plausibleUnit(skyr, 137), "g");
  assert.equal(MF.plausibleUnit(yog, 2), "serving");
  assert.equal(MF.plausibleUnit(yog, 125), "g");
  assert.equal(MF.plausibleUnit(drink, 16), "fl oz");
  assert.equal(MF.plausibleUnit(drink, 300), "fl oz"); // cannot be weighed: stays with a known unit
});

test("units by a typed prefix, cycling on repeat", () => {
  assert.equal(MF.unitByPrefix(yog, "g"), "g");
  assert.equal(MF.unitByPrefix(yog, "s"), "serving");
  assert.equal(MF.unitByPrefix(drink, "f"), "fl oz");
  assert.equal(MF.unitByPrefix(drink, "x"), null);
  assert.equal(MF.unitByPrefix(MF.prepare([food("Egg", [], { large: {}, "large - 3in": {} })])[0], "l", "large"), "large - 3in");
  assert.equal(MF.shortUnit('large - 3 1/4" diameter'), "large");
  assert.deepEqual([MF.unitLabel("serving", 1), MF.unitLabel("serving", 0.5), MF.unitLabel("serving", 2), MF.unitLabel("g", 2), MF.unitLabel("fl oz", 16)], ["serving", "servings", "servings", "g", "fl oz"]);
});

test("search: every word, accents ignored, word starts first, then the most eaten", () => {
  const names = (query) => MF.search(foods, {}, query).map((f) => f.name);
  assert.deepEqual(names("sk"), ["Skyr Nature 0% By Laiterie", "Apple, Fresh, With Skin"]);
  assert.deepEqual(names("nature"), ["Skyr Nature 0% By Laiterie", "Yaourt Nature"]);
  assert.deepEqual(names("yaourt nat"), ["Yaourt Nature"]);
  assert.deepEqual(names("zzz"), []);
  assert.deepEqual(names(""), []);
  assert.equal(MF.search(foods, { monster: "White Energy Drink" }, "monster")[0].name, "White Energy Drink");
});

test("the resting index is alphabetical and leaves out what was never eaten", () => {
  assert.deepEqual(MF.pickIndex(foods, 3).map((f) => f.short), ["Apple, Fresh, With Skin", "Skyr Nature 0%", "Yaourt Nature"]);
  assert.equal(MF.pickIndex(foods, 10).includes(soup), false);
});

test("the day view lays what is on its way over the saved entries", () => {
  const entries = [
    { id: 1, time: "08:00", name: skyr.name, quantity: 150, unit: "g", kcal: 90, protein: 15, fat: 0, carbs: 6 },
    { id: 2, time: "12:30", name: yog.name, quantity: 1, unit: "serving", kcal: 48, protein: 6, fat: 0, carbs: 6 },
    { id: 3, time: "12:30", name: apple.name, quantity: 100, unit: "g", kcal: 52, protein: 0, fat: 0, carbs: 14 },
  ];
  const foodOf = (name) => foods.find((f) => f.name === name);
  const ops = [
    { key: "p1", kind: "log", state: "queued", day: "D", time: "12:30", items: [{ name: skyr.name, quantity: 200, unit: "g" }] },
    { key: "p2", kind: "edit", state: "running", id: 2, quantity: 2, unit: "serving" },
    { key: "p3", kind: "remove", state: "queued", id: 1 },
    { key: "p4", kind: "retime", state: "queued", id: 3, time: "07:15" },
    { key: "p5", kind: "log", state: "queued", day: "other", time: "09:00", items: [{ name: apple.name, quantity: 1, unit: "g" }] },
  ];
  const rows = MF.view(entries, ops, "D", foodOf);
  assert.deepEqual(rows.map((r) => [r.time, r.name === skyr.name ? "skyr" : r.name === yog.name ? "yog" : "apple", r.pending || ""]), [
    ["07:15", "apple", "retime"],
    ["12:30", "yog", "edit"],
    ["12:30", "skyr", "log"], // new rows come after the saved ones of the same minute
  ]);
  assert.equal(rows[1].kcal, 96);
  assert.equal(rows[2].kcal, 120);
  assert.equal(MF.totals(rows).kcal, 52 + 96 + 120);
  assert.deepEqual(MF.view(entries, [], "D", foodOf).map((r) => r.id), [1, 2, 3]);
  // a failed log stays as a row but is not counted; a failed change leaves its entry as it was
  const failed = MF.view(entries, [{ ...ops[0], state: "failed" }], "D", foodOf);
  assert.equal(MF.totals(failed).kcal, 90 + 48 + 52);
  assert.deepEqual(MF.view(entries, [{ ...ops[1], state: "failed" }, { ...ops[2], state: "failed" }], "D", foodOf), MF.view(entries, [], "D", foodOf));
  // moving to another day takes the row out of this one
  assert.deepEqual(MF.view(entries, [{ key: "m", kind: "retime", state: "queued", id: 2, time: "10:00", toDay: "E" }], "D", foodOf).map((r) => r.id), [1, 3]);
});

test("rows never swap when the phone confirms logs made 'now'", () => {
  const foodOf = (name) => foods.find((f) => f.name === name);
  const entries = [{ id: 1, time: "18:19", name: apple.name, quantity: 100, unit: "g", kcal: 52 }];
  const log = (seq, name, state, extra = {}) => ({ key: `p${seq}`, seq, kind: "log", state, day: "D", now: true, time: "18:19", items: [{ name, quantity: 1, unit: "g" }], ...extra });
  const order = (ops, saved = entries) => MF.view(saved, ops, "D", foodOf).map((r) => (r.name === apple.name ? "apple" : r.name === skyr.name ? "skyr" : "yog") + (r.pending ? "?" : ""));
  // two pending, typed in this order
  assert.deepEqual(order([log(1, skyr.name, "running"), log(2, yog.name, "queued")]), ["apple", "skyr?", "yog?"]);
  // the first is saved a minute later than the page's clock said: still above the one that waits
  const saved = [...entries, { id: 2, time: "18:20", name: skyr.name, quantity: 1, unit: "g", kcal: 1 }];
  assert.deepEqual(order([log(2, yog.name, "running")], saved), ["apple", "skyr", "yog?"]);
  // an entry planned for later tonight stays under what is logged now, before and after the phone confirms
  const later = [...entries, { id: 9, time: "23:50", name: yog.name, quantity: 1, unit: "g", kcal: 1 }];
  const at = (ops, saved) => MF.view(saved, ops, "D", foodOf, "18:19").map((r) => r.id || "new");
  assert.deepEqual(at([log(1, skyr.name, "queued")], later), [1, "new", 9]);
  // a failed log stays at the minute it was asked for, under what is saved in that minute; what is asked after it comes below
  assert.deepEqual(order([log(1, skyr.name, "failed"), log(2, yog.name, "queued")]), ["apple", "skyr?", "yog?"]);
  assert.deepEqual(order([log(1, skyr.name, "failed")], [...entries, { id: 2, time: "18:19", name: yog.name, quantity: 1, unit: "g", kcal: 1 }]), ["apple", "yog", "skyr?"]);
  assert.deepEqual(order([log(1, skyr.name, "failed")], [...entries, { id: 3, time: "18:21", name: yog.name, quantity: 1, unit: "g" }]), ["apple", "skyr?", "yog"]);
  // an explicit time sorts by that time at once
  assert.deepEqual(order([log(1, skyr.name, "queued", { now: false, time: "08:00" })]), ["skyr?", "apple"]);
});

test("a change being cancelled is shown as if it had never been asked", () => {
  const foodOf = (name) => foods.find((f) => f.name === name);
  const entries = [{ id: 1, time: "08:00", name: skyr.name, quantity: 150, unit: "g", kcal: 90 }];
  const ops = [
    { key: "p1", kind: "log", state: "cancelling", day: "D", time: "09:00", items: [{ name: yog.name, quantity: 1, unit: "serving" }] },
    { key: "p2", kind: "edit", state: "cancelling", id: 1, quantity: 300, unit: "g" },
  ];
  const rows = MF.view(entries, ops, "D", foodOf);
  assert.deepEqual(rows.map((r) => [r.id, r.quantity, !!r.pending]), [[1, 150, false]]);
  assert.equal(MF.view(entries, [{ ...ops[1], state: "running" }], "D", foodOf)[0].quantity, 300);
});

test("an undo is several changes confirmed by one job", () => {
  const foodOf = (name) => foods.find((f) => f.name === name);
  const entries = [{ id: 1, time: "08:00", name: skyr.name, quantity: 150, unit: "g", kcal: 90 }, { id: 2, time: "09:00", name: apple.name, quantity: 100, unit: "g", kcal: 52 }];
  const undo = { key: "p9", seq: 9, kind: "undo", state: "queued", parts: [{ kind: "remove", id: 2 }, { kind: "edit", id: 1, quantity: 100, unit: "g" }] };
  const rows = MF.view(entries, [undo], "D", foodOf);
  assert.deepEqual(rows.map((r) => [r.id, r.quantity, r.pending, r.op === undo]), [[1, 100, "edit", true]]);
  // a failed undo draws nothing, not even the entry it would have logged again: the day is as it was
  const relog = { key: "p8", seq: 8, kind: "undo", state: "failed", parts: [{ kind: "log", day: "D", time: "09:00", items: [{ name: entries[0].name, quantity: 1, unit: "g" }] }] };
  assert.deepEqual(MF.view(entries, [relog], "D", foodOf), MF.view(entries, [], "D", foodOf));
});

test("the server's jobs become the changes the day shows", () => {
  const job = (key, kind, status, args, more = {}) => ({ key, kind, status, args, cancel: false, created: 1000, ...more });
  const clock = ((d) => `${MF.pad(d.getHours())}:${MF.pad(d.getMinutes())}`)(new Date(1000 * 1000));
  const items = [{ name: skyr.name, quantity: 200, unit: "g" }];
  const ops = MF.ops([
    job("a", "log", "queued", { items, date: "D", time: null }),
    job("b", "log", "running", { items, date: "D", time: "08:00" }, { cancel: true }),
    job("c", "retime", "queued", { id: 3, time: "07:15", date: "E" }),
    job("d", "edit", "failed", { id: 2, quantity: 2, unit: "serving" }, { error: "no" }),
    job("e", "undo", "running", { op: 7, parts: [{ kind: "remove", id: 1 }, { kind: "log", date: "D", time: "09:00", items }] }),
    job("f", "log", "done", { items, date: "D", time: null }),
    job("g", "sync", "running", { period: "week" }),
  ], "D");
  assert.deepEqual(ops.map((op) => [op.key, op.kind, op.state]), [["a", "log", "queued"], ["b", "log", "cancelling"], ["c", "retime", "queued"], ["d", "edit", "failed"], ["e", "undo", "running"], ["g", "sync", "running"]]);
  assert.deepEqual([ops[0].day, ops[0].time, ops[0].now, ops[1].time, ops[1].now], ["D", clock, true, "08:00", false]);
  assert.deepEqual([ops[2].id, ops[2].toDay, ops[3].job.error], [3, "E", "no"]);
  assert.deepEqual(ops[4].parts.map((part) => [part.kind, part.id ?? part.day]), [["remove", 1], ["log", "D"]]);
  // drawn: the one being cancelled is not there, the failed change leaves its entry alone, the sync draws nothing
  const foodOf = (name) => foods.find((f) => f.name === name);
  const entries = [{ id: 1, time: "08:00", name: apple.name, quantity: 100, unit: "g", kcal: 52 }, { id: 2, time: "12:30", name: yog.name, quantity: 1, unit: "serving", kcal: 48 }, { id: 3, time: "19:00", name: skyr.name, quantity: 150, unit: "g", kcal: 90 }];
  assert.deepEqual(MF.view(entries, ops, "D", foodOf, "20:00").map((r) => [r.key, r.pending || ""]), [["e.1-0", "log"], ["e2", ""], ["a-0", "log"]]);
});

test("an amount is a number, a count, or both", () => {
  assert.deepEqual(MF.amount("7.67"), { quantity: 7.67, count: 1, counting: false });
  assert.deepEqual(MF.amount("x3"), { quantity: null, count: 3, counting: true });
  assert.deepEqual(MF.amount("3x"), { quantity: null, count: 3, counting: true });
  assert.deepEqual(MF.amount("7,67 ×3"), { quantity: 7.67, count: 3, counting: true });
  assert.deepEqual(MF.amount("2*"), { quantity: null, count: 2, counting: true }); // "2x": two of the usual
  assert.deepEqual(MF.amount("x"), { quantity: null, count: 1, counting: true }); // the count is still to come
  assert.deepEqual(MF.amount(""), { quantity: null, count: 1, counting: false });
  assert.equal(MF.amount("x0"), null);
  assert.equal(MF.amount("x21"), null);
  assert.equal(MF.amount("0"), null);
  assert.equal(MF.amount("12abc"), null);
  assert.equal(MF.amount("1.2.3 x2"), null);
});

test("an arrow steps an amount to the next whole unit, and never down to nothing", () => {
  assert.equal(MF.step(1, "serving", 1), 2);
  assert.equal(MF.step(1.5, "serving", 1), 2);
  assert.equal(MF.step(1.5, "serving", -1), 1);
  assert.equal(MF.step(1, "serving", -1), 0.5);
  assert.equal(MF.step(0.5, "serving", -1), 0.5);
  assert.equal(MF.step(1, "serving", 1, true), 1.5);
  assert.equal(MF.step(193, "g", 1), 194);
  assert.equal(MF.step(7.67, "g", 1), 8);
  assert.equal(MF.step(193, "g", 1, true), 200);
  assert.equal(MF.step(193, "g", -1, true), 190);
  assert.equal(MF.step(1, "g", -1), 1);
  assert.equal(MF.shiftTime("08:59", 1), "09:00");
  assert.equal(MF.shiftTime("00:05", -10), "00:00");
  assert.equal(MF.shiftTime("23:55", 10), "23:59");
});

test("what a serving weighs is known only when the unit's weight is", () => {
  assert.equal(MF.grams(yog, 2, "serving"), 250);
  assert.equal(MF.grams(yog, 250, "g"), null); // already grams
  assert.equal(MF.grams(drink, 1, "serving"), null);
});

test("before anything is typed: the hour's picks, then the latest", () => {
  const at = (name, last_at, hours) => ({ ...food(name, [], {}), last_at, hours: Object.assign(new Array(24).fill(0), hours) });
  const tea = at("Tea", "2026-11-10 08:05", { 8: 9 }), toast = at("Toast", "2026-11-14 07:50", { 7: 3 }), rice = at("Rice", "2026-11-14 12:40", { 12: 6 });
  const once = at("Cake", "2026-11-12 08:30", { 8: 1 }), soupNew = { ...soup, last_at: "", hours: null };
  const all = [rice, tea, toast, once, soupNew]; // most eaten first
  const { picks, latest } = MF.pickNow(all, 8, 30);
  assert.deepEqual(picks.map((f) => f.name), ["Tea", "Toast"]); // the hour itself counts double; once is not a habit
  assert.deepEqual(latest.map((f) => f.name), ["Rice", "Cake"]); // by recency, never a food shown above, never one not logged yet
  const after = MF.pickNow(all, 8, 30, new Map([["Winter Soup", "2026-11-15 13:00"]]));
  assert.equal(after.latest[0].name, "Winter Soup"); // logged since the catalog was read
  assert.equal(MF.pickNow(all, 8, 3).picks.length, 1); // a third of the room at most
  assert.equal(MF.pickNow(all, 20, 30).picks.length, 0);
});

test("a unit in a narrow cell", () => {
  assert.equal(MF.abbr("serving"), "srv");
  assert.equal(MF.abbr("fl oz"), "fl oz");
  assert.equal(MF.abbr("large - 3 1/4\" diameter"), "large");
});

test("an amount typed after the words of a food, with or without a space", () => {
  assert.deepEqual(MF.splitAmount("apple 156"), ["apple", "156"]);
  assert.deepEqual(MF.splitAmount("car143"), ["car", "143"]);
  assert.deepEqual(MF.splitAmount("skyr1,5"), ["skyr", "1,5"]);
  assert.deepEqual(MF.splitAmount("jello 2 150"), ["jello 2", "150"]);
  assert.deepEqual(MF.splitAmount("galette x3"), ["galette", "x3"]);
  assert.deepEqual(MF.splitAmount("galette 3x"), ["galette", "3x"]);
  assert.deepEqual(MF.splitAmount("gal x"), ["gal", "x"]); // the count is still to come
  assert.deepEqual(MF.splitAmount("galx3"), ["galx", "3"]); // a word may end in x: without a space this is 3 of "galx", not three "gal"
  assert.equal(MF.splitAmount("apple"), null);
  assert.equal(MF.splitAmount("156"), null);
  assert.equal(MF.splitAmount("apple "), null);
});

test("an entry whose minute is not known comes last of its hour", () => {
  const entries = [{ id: 3, time: "08:??", name: "c" }, { id: 1, time: "08:00", name: "a" }, { id: 2, time: "08:45", name: "b" }, { id: 4, time: "09:00", name: "d" }];
  assert.deepEqual(MF.view(entries, [], "D", () => null, null).map((row) => row.id), [1, 2, 3, 4]);
});

test("queued work for the source day cannot cover an entry after it moves to another day", () => {
  const entries = [{ id: 3, name: "Apple", time: "08:00", unit: "g", quantity: 40 }];
  for (const kind of ["remove", "edit", "retime"]) {
    const jobs = [{ key: "stale", kind, status: "queued", created: 1, args: { id: 3, on: "A", time: "11:00", quantity: 55 } }];
    assert.deepEqual(MF.view(entries, MF.ops(jobs, "B"), "B", () => null), MF.view(entries, [], "B", () => null));
  }
});

test("a row is of the food it says, whatever shares its name", () => {
  // Two recipes named Soup: each entry says which it is of (`food`), or of none when its recipe is no more.
  const thick = { name: "Soup (201 kcal)", display: "Soup (201 kcal)", shared: { name: "Soup" }, per_unit: { serving: { kcal: 201, grams: 687.5 }, g: { kcal: 201 / 687.5, grams: 1 } } };
  const thin = { name: "Soup (315 kcal)", display: "Soup (315 kcal)", shared: { name: "Soup" }, per_unit: { serving: { kcal: 315, grams: null } } };
  const foodOf = (name) => [thick, thin].find((f) => f.name === name);
  const entries = [
    { id: 1, time: "12:00", name: "Soup", food: thick.name, quantity: 1, unit: "serving", kcal: 201 },
    { id: 2, time: "12:00", name: "Soup", food: thin.name, quantity: 1, unit: "serving", kcal: 315 },
    { id: 3, time: "12:00", name: "Soup", food: null, quantity: 1, unit: "serving", kcal: 999 },
  ];
  const edit = (id, quantity, unit) => MF.view(entries, [{ key: "e", kind: "edit", state: "queued", id, quantity, unit }], "D", foodOf).find((r) => r.id === id);
  assert.equal(Math.round(edit(1, 300, "g").kcal), 88); // priced as its own recipe, in a unit only that one has
  assert.equal(edit(2, 2, "serving").kcal, 630);
  assert.equal(edit(3, 2, "serving").kcal, 1998); // of no food known: its own figures, scaled
  const asked = MF.view([], [{ key: "l", kind: "log", state: "queued", day: "D", time: "13:00", items: [{ name: thin.name, quantity: 2, unit: "serving" }] }], "D", foodOf)[0];
  assert.deepEqual([asked.food, asked.kcal], [thin.name, 630]);
});
