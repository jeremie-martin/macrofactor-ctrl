"use strict";
/* Pure logic of the mf web UI: no DOM, no network. Loaded by the page as `MF`, and by `node --test`. */
(function (exports) {
  const MACROS = ["kcal", "protein", "fat", "carbs"];
  const pad = (n) => String(n).padStart(2, "0");
  const norm = (s) => String(s).normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase().replace(/\s+/g, " ").trim();
  const minutes = (time) => { const [hh, mm] = String(time).split(":"); return +hh * 60 + (Number.isNaN(+mm) ? 0 : +mm); };
  /** A typed number ("12", "12,5", ".5"), or null: "12abc" and "1.2.3" are mistakes, not 12 and 1.2. */
  const number = (text) => { const t = String(text).trim().replace(",", "."); return /^(\d+(\.\d*)?|\.\d+)$/.test(t) ? parseFloat(t) : null; };
  /** Quantities as typed: at most two decimals, no trailing zeros. */
  const qty = (v) => String(Math.round(v * 100) / 100);
  /** Macros in grams: whole numbers, one decimal only where it carries information (below 10). */
  const gram = (v) => (v == null ? "" : v >= 9.95 || Math.abs(v - Math.round(v)) < 0.05 ? String(Math.round(v)) : v.toFixed(1));
  const splitBrand = (display) => { const at = display.indexOf(" By "); return at < 0 ? [display, ""] : [display.slice(0, at), display.slice(at + 4)]; };
  /** "large - 3 1/4\" diameter" -> "large": enough to recognise the unit in a narrow column. */
  const shortUnit = (unit) => unit.split(/ - | \(|, /)[0];
  /** A unit in a narrow cell: "srv" for the long word that is also the commonest; others as `shortUnit` has them. */
  const abbr = (unit) => { const short = shortUnit(unit); return short === "serving" ? "srv" : short; };
  /** A unit as read next to its quantity: "1 serving", "2 servings". Other units are names, left as they are. */
  const unitLabel = (unit, quantity) => { const short = shortUnit(unit); return short === "serving" && quantity !== 1 ? "servings" : short; };

  /** What an amount field holds: a number, a count ("x3", "3x", "*3"), or both ("7.67 x3"). The count is how many
   *  entries to log. Returns {quantity (null when not given), count, counting (a count is being typed)}, or null. */
  function amount(text) {
    const t = String(text).trim().toLowerCase().replace(/[*×]/g, "x");
    const only = t.match(/^(\d+)\s*x$/), m = only ? [t, "", only[1]] : t.match(/^([\d.,]*)\s*(?:x\s*(\d*))?$/);
    if (!m) return null;
    const quantity = m[1] ? number(m[1]) : null, counting = t.includes("x"), count = m[2] ? +m[2] : 1;
    if ((m[1] && !(Number.isFinite(quantity) && quantity > 0)) || (counting && m[2] && !(count >= 1 && count <= 20))) return null;
    return { quantity, count, counting };
  }
  /** What was typed in the food field, as the words of a food and an amount typed after them: "apple 156" and
   *  "apple156" -> ["apple", "156"], "galette x3" -> ["galette", "x3"]. A number needs no space; a count does, as a
   *  word may end in x. Null when the text does not end in an amount. Only meaningful when no food is called that. */
  function splitAmount(text) {
    const spaced = text.match(/^(.*\S)\s+(\d+(?:[.,]\d*)?|[x*×]\d*|\d+[x*×])$/i);
    if (spaced) return [spaced[1], spaced[2]];
    const glued = text.match(/^(.*[^\d\s.,])(\d+(?:[.,]\d*)?)$/);
    return glued ? [glued[1], glued[2]] : null;
  }
  /** One press of an arrow on an amount: the next whole unit (or gram); with `other`, the other step: 10 g, or half a unit.
   *  Never down to nothing: a unit can still be halved once, a gram stays. */
  function step(value, unit, dir, other) {
    const by = other ? (unit === "g" ? 10 : 0.5) : 1;
    const next = dir > 0 ? Math.floor(value / by + 1e-9) * by + by : Math.ceil(value / by - 1e-9) * by - by;
    if (next > 0) return Math.round(next * 100) / 100;
    return unit !== "g" && value > 0.5 ? 0.5 : value;
  }
  /** A time of day moved by minutes, staying within the day. */
  function shiftTime(time, by) {
    const at = Math.max(0, Math.min(1439, minutes(time) + by));
    return `${pad(Math.floor(at / 60))}:${pad(at % 60)}`;
  }

  /** "8" -> 08:00, "830" -> 08:30, "8:30", "8h30", "20" -> 20:00; null when it is not a time of day. */
  function parseTime(text) {
    const match = String(text).trim().match(/^(\d{1,2})(?:\s*[:h.]\s*(\d{2})|(\d{2}))?$/i);
    if (!match) return null;
    const hh = +match[1], mm = +(match[2] || match[3] || 0);
    return hh > 23 || mm > 59 ? null : `${pad(hh)}:${pad(mm)}`;
  }

  // ---- foods
  const usual = (food) => food.servings[0] || { quantity: 1, unit: Object.keys(food.per_unit)[0] || "serving" };
  /** Units a food can be logged in: those it has been logged with, and grams (anything can be weighed) unless
   *  MacroFactor shows no weight for it: then it has no grams to be logged in. */
  const unitsOf = (food) => { const units = Object.keys(food.per_unit); return units.includes("g") || food.weightless ? units : [...units, "g"]; };

  /** Nutrition of one `unit`; grams are derived from any unit whose weight is known. Null when unknown. */
  function perUnit(food, unit) {
    if (food.per_unit[unit]) return food.per_unit[unit];
    if (unit !== "g") return null;
    const weighed = Object.values(food.per_unit).find((u) => u.grams);
    return weighed ? Object.fromEntries(MACROS.map((m) => [m, weighed[m] / weighed.grams])) : null;
  }
  function macros(food, quantity, unit) {
    const per = food && perUnit(food, unit);
    return per ? Object.fromEntries(MACROS.map((m) => [m, (per[m] || 0) * quantity])) : null;
  }

  /** What an amount weighs when it is not given in grams and the unit's weight is known; else null. */
  function grams(food, quantity, unit) {
    const g = unit !== "g" && food && food.per_unit[unit] && food.per_unit[unit].grams;
    return g ? g * quantity : null;
  }

  /** The unit in which a bare number is a plausible amount: 150 of skyr is grams, 2 of yoghurt is servings. */
  function plausibleUnit(food, quantity) {
    const options = food.servings.filter((s) => s.quantity > 0).map((s) => [s.unit, s.quantity]);
    if (perUnit(food, "g") && !options.some(([unit]) => unit === "g")) options.push(["g", 100]);
    if (!options.length || !(quantity > 0)) return usual(food).unit;
    const off = ([, typical]) => Math.abs(Math.log(quantity / typical));
    return options.reduce((best, option) => (off(option) < off(best) ? option : best))[0];
  }

  /** The food's unit a typed word means ("g", "serv", "fl"); `after` cycles to the next match on a repeated key. */
  function unitByPrefix(food, text, after) {
    const want = norm(text), units = unitsOf(food);
    const exact = units.find((u) => norm(u) === want);
    if (exact && after == null) return exact;
    const hits = units.filter((u) => norm(u).startsWith(want));
    if (!hits.length) return null;
    return hits[(hits.indexOf(after) + 1) % hits.length];
  }

  function prepare(foods) {
    for (const food of foods) {
      food.key = norm(food.display);
      food.short = splitBrand(food.display)[0];
    }
    return foods;
  }

  /** Foods containing every typed word; word starts first, then the most eaten. `foods` must be in most-eaten order. */
  function search(foods, aliases, query) {
    const words = norm(query).split(" ").filter(Boolean);
    if (!words.length) return [];
    const alias = aliases[norm(query)];
    const found = [];
    foods.forEach((food, rank) => {
      let score = alias && food.name === alias ? 1000 : 0;
      for (const word of words) {
        const at = food.key.indexOf(word);
        if (at < 0) { if (!score) return; continue; }
        score += at === 0 ? 3 : /[ ,(-]/.test(food.key[at - 1]) ? 2 : 0;
      }
      if (score < 1000 && !words.every((word) => food.key.includes(word))) return;
      found.push({ food, score, rank });
    });
    return found.sort((a, b) => b.score - a.score || a.rank - b.rank).map((hit) => hit.food);
  }

  /** The resting index: the `cap` most eaten foods, in alphabetical order so each keeps its place. */
  function pickIndex(foods, cap) {
    return foods.filter((food) => food.count > 0).slice(0, cap).sort((a, b) => (a.key < b.key ? -1 : a.key > b.key ? 1 : 0));
  }

  /** What to offer before anything is typed, as MacroFactor's own search does: the foods you usually log around this
   *  hour (at least twice in the last 30 days within an hour of it; the hour itself counts double), then the rest by
   *  how recently they were logged. `recent` maps a food's name to "YYYY-MM-DD HH:MM" for logs newer than the catalog. */
  function pickNow(foods, hour, cap, recent = new Map()) {
    const around = (food) => { const h = food.hours || []; return (h[hour - 1] || 0) + 2 * (h[hour] || 0) + (h[hour + 1] || 0); };
    const near = (food) => { const h = food.hours || []; return (h[hour - 1] || 0) + (h[hour] || 0) + (h[hour + 1] || 0); };
    const last = (food) => { const a = food.last_at || food.last || "", b = recent.get(food.name) || ""; return a > b ? a : b; };
    const picks = foods.map((food, rank) => ({ food, rank })).filter((x) => near(x.food) >= 2)
      .sort((a, b) => around(b.food) - around(a.food) || a.rank - b.rank).slice(0, Math.min(8, Math.floor(cap / 3))).map((x) => x.food);
    const latest = foods.filter((food) => last(food) && !picks.includes(food)).sort((a, b) => (last(a) < last(b) ? 1 : last(a) > last(b) ? -1 : 0)).slice(0, Math.max(0, cap - picks.length));
    return { picks, latest };
  }

  // ---- the day as shown: saved entries plus what the jobs on their way will do to them
  /** The server's jobs (and requests still on their way to it, told the same way) as the changes `view` draws:
   *  what each will add to the day, change in it or take from it. A job asked to stop reads "cancelling". */
  function ops(jobs, today) {
    const part = (kind, a, created) => (kind === "log" ? { kind, day: a.date, time: a.time || clockOf(created), now: !a.time && a.date === today, items: a.items }
      : kind === "retime" ? { kind, id: a.id, on: a.on, time: a.time, toDay: a.date || null } : { kind, ...a });
    return jobs.filter((job) => ["queued", "running", "failed"].includes(job.status)).map((job) => {
      const base = { key: job.key, seq: job.created, state: job.cancel && job.status !== "failed" ? "cancelling" : job.status, job };
      return job.kind === "undo" ? { ...base, kind: "undo", parts: (job.args.parts || []).map((p) => part(p.kind, p, job.created)) } : { ...base, ...part(job.kind, job.args, job.created) };
    });
  }
  const clockOf = (seconds) => { const d = new Date(seconds * 1000); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; };

  /** The day as it is drawn: the saved entries with what is on its way laid over them (`ops`, above). A log that is
   *  waiting is a row of its own, at its time (or at the present, for one asked "now"); a change to an entry shows
   *  on that entry; a removal takes it out. A failed log stays, at the time it was asked for, until retried or dismissed;
   *  a failed undo draws nothing (the day is as it was, and the phone line says so). */
  function view(entries, ops, day, foodOf, now) {
    let rows = entries.map((entry) => ({ ...entry, key: `e${entry.id}` }));
    const at = (id) => rows.findIndex((row) => row.id === id);
    const flat = ops.filter((op) => op.state !== "cancelling" && !(op.parts && op.state === "failed")).flatMap((op) => (op.parts ? op.parts.map((part, i) => ({ ...part, key: `${op.key}.${i}`, state: op.state, owner: op })) : [op]));
    for (const op of flat) {
      const owner = op.owner || op, base = { op: owner, pending: op.kind, state: op.state };
      if (op.kind === "log") {
        if (op.day !== day) continue;
        const rank = owner.now && op.state !== "failed" ? 1 : 0;
        op.items.forEach((item, index) => {
          const m = macros(foodOf(item.name), item.quantity, item.unit) || {};
          rows.push({ ...base, key: `${op.key}-${index}`, added: true, rank, seq: owner.seq || 0, time: op.time, name: item.name, food: item.name, quantity: item.quantity, unit: item.unit, ...m });
        });
        continue;
      }
      if (op.on && op.on !== day) continue;
      const index = at(op.id);
      if (index < 0) continue;
      const row = rows[index];
      if (op.state === "failed") continue; // the entry is as it was; the phone line says what failed
      if (op.kind === "remove" || (op.kind === "retime" && op.toDay && op.toDay !== day)) rows.splice(index, 1);
      else if (op.kind === "retime") rows[index] = { ...row, ...base, time: op.time };
      else if (op.kind === "edit") {
        const unit = op.unit ?? row.unit; // no unit asked: the entry keeps its own
        const m = macros(foodOf(row.food), op.quantity, unit) || (unit === row.unit && row.quantity ? Object.fromEntries(MACROS.map((k) => [k, (row[k] || 0) * op.quantity / row.quantity])) : {});
        rows[index] = { ...row, ...base, quantity: op.quantity, unit, ...m };
      }
    }
    rows = rows.map((row, index) => ({ row, index }));
    const present = now ? minutes(now) + 2.5 : 1e9; // the phone's minute may be a little ahead of this clock
    const when = (row) => (row.rank === 1 ? present : minutes(row.time) + (String(row.time).endsWith("??") ? 59.9 : 0)); // a minute not known: last of its hour, as the store lists it
    const birth = (row) => (row.added ? Infinity : row.id ?? 0); // saved entries by id; what is not saved yet, after them
    rows.sort((a, b) => when(a.row) - when(b.row) || birth(a.row) - birth(b.row) || (a.row.seq || 0) - (b.row.seq || 0) || a.index - b.index);
    return rows.map((item) => item.row);
  }

  function totals(rows) {
    const sum = { kcal: 0, protein: 0, fat: 0, carbs: 0 };
    for (const row of rows) if (row.state !== "failed") for (const m of MACROS) sum[m] += row[m] || 0;
    return sum;
  }

  Object.assign(exports, { MACROS, pad, norm, minutes, number, qty, gram, splitBrand, shortUnit, abbr, unitLabel, parseTime, amount, splitAmount, step, shiftTime, usual, unitsOf, macros, grams, pickNow, plausibleUnit, unitByPrefix, prepare, search, pickIndex, clockOf, ops, view, totals });
})(typeof module !== "undefined" ? module.exports : (window.MF = {}));
