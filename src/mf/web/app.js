"use strict";
/* mf web UI: one line to add a food, the list of what you are likely to want, the day, and the phone line.

   The line is two fields, and Enter confirms the one you are in: in the food field it settles the food and moves
   to its amount; in the amount it logs. Nothing is ever logged from the food field, so no amount is logged that
   you did not have under your eyes, in the field where you could change it.
   Rows are the same in the list and in the day:
   - A row's + logs it as shown; so does Enter on an entry of the day (once more).
   - Tab, or a click on the name, takes the row to the line, where its amount is ready to be typed over.
   - An amount is the thing you change: type over it, step it with the arrows, repeat it with x3.
   - The line under the input says what Enter will do, wherever the cursor is.

   How it stays calm:
   - The server holds everything that lasts (the day, the jobs, what failed, what can be undone) and tells it in
     one reading; the page draws that reading and keeps nothing of it but the days already seen (shown until the next reading). The ledger is always MF.view(entries,
     MF.ops(jobs)): a row waits exactly as long as the server lists a job for it, and the reading in which the
     job is gone is the one in which its entry is there, so a row changes ink in place and does not jump.
   - The list at rest (the hour's picks, then the latest; or the alphabetical index) is computed when the page
     loads or changes size, when it comes back from a search of yours, and while you are away (the window out of
     focus, the hour turning with the pointer elsewhere); never under a pointer, and never because the phone
     finished something.
   - Every region is rendered to an HTML string and written only when that string changed.
   - What changes lives in fixed slots (the phone line, a row's two action places), and a ledger just redrawn
     under a resting pointer takes no click until the pointer moves: nothing acts that you did not aim at. */

const $ = (id) => document.getElementById(id);
const foodIn = $("food"), amtIn = $("amt"), whenIn = $("when"), grid = $("grid"), ledger = $("ledger");
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const { pad, qty, gram, minutes } = MF;
const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);

const iso = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const fromIso = (s) => new Date(`${s}T12:00:00`);
const addDays = (s, n) => { const d = fromIso(s); d.setDate(d.getDate() + n); return iso(d); };
const today = () => iso(new Date());
const clock = () => MF.clockOf(Date.now() / 1000);
const dayDiff = (a, b) => Math.round((fromIso(a) - fromIso(b)) / 864e5);
const wday = (day) => fromIso(day).toLocaleDateString("en-GB", { weekday: "short" });
const dlong = (day) => fromIso(day).toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long" });
const dshort = (day) => fromIso(day).toLocaleDateString("en-GB", { weekday: "short", day: "numeric" });

const S = {
  figures: (() => { try { return !!localStorage.getItem("mf.figures"); } catch { return false; } })(), // protein, fat and carbs as figures instead of a bar
  day: today(), data: null, days: new Map(), // the day on screen, its saved entries, and days seen before (shown at once, then refreshed)
  foods: [], byName: new Map(), aliases: {}, recipes: {}, loaded: false,
  tab: "now", only: false, // the view at rest (now | usual: the index), and whether only recipes are listed
  query: "", list: [], total: 0, hi: -1, rows: 1, cols: 1, kept: null, // the list and what it shows; kept: the resting index's foods, by name
  keptNow: null, nPicks: 0, pickHour: 0, recent: new Map(), // the resting "now" list: {names, nPicks, hour}; the first nPicks of the list are the hour's picks; foods logged since the catalog was read
  peek: null, peekRow: null, // under the pointer: the food in the list, or the entry (its key) in the day
  chosen: null, unitSet: null, touched: false, typed: "", msg: "", confirm: null, // the composer; confirm: recipe name awaiting "Delete?"
  when: null, // "HH:MM" for this sitting; null = now
  snap: null, // the server's last reading: {build, phone, day, jobs, last}
  asked: [], // what this page asked that the server has not acknowledged yet, told like the server's jobs
  jobs: [], ops: [], // derived from those two: every job, and the changes they make to the day (MF.ops)
  down: false, stale: false, // the server did not answer the last reading; this page is of another build than the server
  cursor: null, edit: null, // ledger row key under the keyboard cursor; {key, field: "q" | "t", value, fresh}
  recipe: null, // {name, servings, items} while a recipe is being built in the right column
  lastKey: null,
};
let seq = 0, seenToday = today();

/** One request to mf. A POST carries when it was asked (the server refuses what is no longer fresh), this page's
 *  build, and, where it asks for a job, the name of that job (`name()`: asked again under it, nothing is done twice). mf is on this
 *  machine and answers in milliseconds: unanswered after 20 s, a request is no longer waited for and said so. The server may
 *  have taken it: the next reading that lists its name replaces the failure, and Retry sends that same name. */
async function api(path, body, key) {
  const headers = { "Content-Type": "application/json", "X-Mf-Asked": String(Date.now() / 1000), "X-Mf-Build": document.documentElement.dataset.build, ...(key ? { "X-Mf-Key": key } : {}) };
  const options = body === undefined ? {} : { method: "POST", headers, body: JSON.stringify(body) };
  const response = await fetch(path, { ...options, signal: AbortSignal.timeout(20000) }).catch((error) => {
    throw new Error(error.name === "TimeoutError" ? "mf did not answer within 20 s; it may have taken it all the same: Retry asks the same thing, never twice" : "mf could not be reached");
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : response.statusText);
  return data;
}

// ================================================================ the trace (mf/trace.py): what you did, and what it meant
const PAGE = Array.from(crypto.getRandomValues(new Uint32Array(4)), (n) => n.toString(16).padStart(8, "0")).join("");
const AREAS = { food: "food", amt: "amount", when: "time", usel: "unit", edin: "entry", rname: "recipe", rserv: "recipe", ledger: "day" };
const REGIONS = [[".composer", "line"], [".strip", "strip"], [".listhead", "tabs"], [".grid", "list"], [".dayhead", "dayhead"], [".ledger", "day"], [".phone", "phone"]];
let traced = [], travel = 0;
function T(ev, fields) { traced.push({ t: Date.now() / 1000, ev, ...fields }); }
function flushTrace(closing) {
  if (!traced.length) return;
  const batch = traced.splice(0), body = JSON.stringify({ page: PAGE, events: batch });
  if (closing === true) navigator.sendBeacon("/api/trace", body);
  else fetch("/api/trace", { method: "POST", body, keepalive: true }).then((r) => { if (!r.ok) throw 0; }).catch(() => { if (traced.length < 5000) traced.unshift(...batch); }); // kept for the next try
}
/** What a click hit, in words: the region, the control (or cell), and the food or entry it belongs to. */
function whereOf(target) {
  const region = REGIONS.find(([selector]) => target.closest(selector));
  const hit = target.closest("button, input, select, .row > span, .food > span") || target;
  const on = hit.id || hit.dataset?.tab || String(hit.className || "").split(" ").filter((c) => c && c !== "tb")[0] || hit.tagName.toLowerCase();
  const food = target.closest(".food"), row = target.closest(".row");
  const what = food ? S.list[+food.dataset.i]?.short : row ? rows().find((r) => r.key === row.dataset.key)?.name : undefined;
  return { in: region ? region[1] : "-", on, what };
}
document.addEventListener("keydown", (event) => {
  if (["Shift", "Control", "Alt", "Meta"].includes(event.key)) return;
  const mods = (event.ctrlKey ? "C-" : "") + (event.metaKey ? "M-" : "") + (event.altKey ? "A-" : "");
  T("key", { key: mods + event.key, in: AREAS[document.activeElement?.id] || "-", rep: event.repeat || undefined });
}, true);
document.addEventListener("click", (event) => { T("click", { ...whereOf(event.target), px: Math.round(travel) }); travel = 0; }, true);
addEventListener("error", (event) => T("error", { msg: event.message, at: `${String(event.filename).split("/").pop()}:${event.lineno}` }));
addEventListener("unhandledrejection", (event) => T("error", { msg: String(event.reason?.message || event.reason) }));
addEventListener("pagehide", () => flushTrace(true));
setInterval(flushTrace, 2000);

/** Write a region only when it changed, so nothing is rebuilt (or loses its scroll) for nothing. */
function put(node, html) {
  if (node._html === html) return false;
  node._html = html;
  node.innerHTML = html;
  return true;
}
const say = (message) => { S.msg = message; renderComposer(); };
/** When the line logs, if not "now, today": "at 08:30", "to Fri 13 at 21:03". Blue means now; this is what orange says. */
const whenText = () => (S.recipe ? "" : S.day !== today() ? `to ${dshort(S.day)} at ${S.when || clock()}` : S.when ? `at ${S.when}` : "");

// ================================================================ the food index
let ROW = 24; // px, the one row height: the stylesheet's --row
function measure() {
  const box = grid.getBoundingClientRect();
  ROW = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--row")) || 24;
  S.rows = Math.max(1, Math.floor((box.height - 22 - 6) / ROW)); // the last row keeps clear of the phone line
  S.cols = Math.min(2, Math.max(1, Math.floor((box.width + 20) / 400))); // two columns at most: the names get the room
  grid.classList.toggle("tight", (box.width + 20) / S.cols - 20 < 460); // no room for P, F, C: pointing at a food shows them under the input
  grid.style.gridTemplateRows = `22px repeat(${S.rows}, ${ROW}px)`;
  grid.style.gridTemplateColumns = `repeat(${S.cols}, minmax(0, 1fr))`;
  const room = document.querySelector(".right").getBoundingClientRect().bottom - ledger.getBoundingClientRect().top;
  ledger.style.maxHeight = `${Math.floor(room / ROW) * ROW}px`; // whole rows only: the edge never slices one
}

const pool = () => (S.only ? S.foods.filter((food) => food.kind === "recipe") : S.foods);
const hourNow = () => +(S.when || clock()).slice(0, 2);
function compute() {
  const room = S.rows * S.cols, lit = S.hi >= 0 ? S.list[S.hi] : null;
  S.nPicks = 0;
  if (S.query.trim()) {
    const found = MF.search(pool(), S.aliases, S.query);
    S.total = found.length;
    S.list = found.slice(0, room);
    S.hi = Math.max(S.list.length ? 0 : -1, S.list.indexOf(lit));
  } else {
    S.total = pool().filter((food) => S.only || food.count > 0).length;
    if (S.tab === "now") { // one place is kept for the "Latest" title
      if (!S.keptNow) {
        const { picks, latest } = MF.pickNow(pool(), hourNow(), room - 1, S.recent);
        const never = S.only ? pool().filter((food) => !picks.includes(food) && !latest.includes(food)) : []; // recipes not logged yet: after the others, so they can be found
        S.keptNow = { names: [...picks, ...latest, ...never].map((food) => food.name), nPicks: picks.length, hour: hourNow() };
        if (!S.loaded) S.keptNow = null;
      }
      const kept = S.keptNow || { names: [], nPicks: 0, hour: hourNow() }, found = kept.names.map((name) => S.byName.get(name));
      S.nPicks = found.slice(0, kept.nPicks).filter(Boolean).length;
      S.list = found.filter(Boolean).slice(0, room - 1);
      S.pickHour = kept.hour;
    }
    else if (S.kept) S.list = S.kept.map((name) => S.byName.get(name)).filter(Boolean).slice(0, room); // same foods, same places, whatever a sync brought
    else { // the index: the most used, in alphabetical order (recipes: those never logged too)
      S.list = S.only ? pool().slice(0, room).sort((a, b) => (a.key < b.key ? -1 : 1)) : MF.pickIndex(pool(), room);
      if (S.loaded) S.kept = S.list.map((food) => food.name);
    }
    S.hi = S.list.indexOf(lit);
  }
}

function mark(name) {
  const words = MF.norm(S.query).split(" ").filter(Boolean);
  const folded = name.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
  if (!words.length || folded.length !== name.length) return esc(name);
  const on = new Array(name.length).fill(false);
  for (const word of words) { const at = folded.indexOf(word); if (at >= 0) on.fill(true, at, at + word.length); }
  let html = "";
  for (let i = 0; i < name.length;) { let j = i; while (j < name.length && on[j] === on[i]) j++; html += on[i] ? `<u>${esc(name.slice(i, j))}</u>` : esc(name.slice(i, j)); i = j; }
  return html;
}

/** A list of rows, each a food with the amount Enter or + would log. At rest it is either the hour's picks followed
 *  by the latest, under their titles, or the alphabetical index, drawn like a book's (a letter in the margin and a
 *  hairline where each letter starts). Search results are in order of match, best first. */
function renderGrid() {
  const how = whenText(), verb = S.recipe ? "Add" : "Log", searching = !!S.query.trim();
  const lettered = !searching && S.tab !== "now", sections = !searching && S.tab === "now";
  const head = (title) => `<div class="fhead"><span class="ft">${title}</span><span class="k">kcal</span>${HEADS()}<span></span></div>`;
  const title = (index) => (!sections ? "" : index < S.nPicks ? `Around ${pad(S.pickHour)}:00` : "Latest");
  let html = "", prev = "", slot = 0;
  S.list.forEach((food, index) => {
    if (sections && index === S.nPicks && index > 0 && slot % S.rows) { html += '<div class="sect">Latest</div>'; slot++; }
    if (slot % S.rows === 0) { html += head(title(index)); prev = ""; }
    slot++;
    const use = MF.usual(food), m = MF.macros(food, use.quantity, use.unit), [name, brand] = MF.splitBrand(food.display), [w, c] = amountCells(food, use.quantity, use.unit);
    const lit = S.chosen ? S.chosen === food : index === S.hi;
    const letter = (food.key || name)[0].toUpperCase(), first = lettered && letter !== prev;
    prev = letter;
    html += `<div class="food${lit ? " hi" : ""}${first ? " first" : ""}" data-i="${index}" title="${esc(food.display)}"><span class="fl">${first ? esc(letter) : ""}</span>` +
      `<span class="fn"><span>${mark(name)}</span>${food.kind === "recipe" && !S.only ? '<i class="rc" title="Recipe">R</i>' : ""}${brand ? `<small>${esc(brand)}</small>` : ""}</span>` +
      `<span class="fw">${w}</span><span class="fc" title="${esc(use.unit)}">${c}</span>` +
      (m ? `<span class="fk">${Math.round(m.kcal)}</span>${splitBar(m)}` : '<span class="fk">–</span><span class="fb"></span>') +
      `<button class="plus" tabindex="-1" title="${verb} ${qty(use.quantity)} ${esc(use.unit)}${S.recipe ? "" : ` ${how || "now"}`}">+</button></div>`;
  });
  put(grid, html);
  grid.classList.toggle("other", !!how);
  grid.classList.toggle("plain", !lettered);
  document.querySelectorAll(".tab").forEach((tab) => tab.classList.toggle("on", tab.dataset.tab === S.tab));
  $("newrec").classList.toggle("on", !!S.recipe);
  $("only").classList.toggle("on", S.only);
  $("only").setAttribute("aria-pressed", String(S.only));
  const noun = S.only ? "recipes" : "foods", shown = S.list.length;
  $("count").textContent = !S.loaded ? "" : S.query.trim() ? `${S.total} match${S.total === 1 ? "" : "es"}${S.total > shown ? `, ${shown} shown` : ""}`
    : S.total > shown ? `${shown} of ${S.total} ${noun}` : `${S.total} ${noun}`;
}
// pointing at a food shows its figures in the line under the input: ink in a fixed place
grid.addEventListener("mouseover", (event) => { const node = event.target.closest(".food"), food = node ? S.list[+node.dataset.i] : null; if (food !== S.peek) { S.peek = food; renderComposer(); } });
grid.addEventListener("mouseleave", () => { if (S.peek) { S.peek = null; renderComposer(); } });

// ================================================================ the composer
const target = () => S.chosen || (S.hi >= 0 ? S.list[S.hi] : null);
/** The amount a unit starts with: what you last used in that unit, else 100 g or 1 of anything else. */
const startOf = (food, unit) => food.servings.find((s) => s.unit === unit)?.quantity ?? (unit === "g" ? 100 : 1);

/** Exactly what Enter would log right now, or null when the amount makes no sense. */
function draft() {
  const food = target();
  if (!food) return null;
  const base = MF.usual(food);
  if (!S.chosen) return { food, quantity: base.quantity, unit: base.unit, count: 1 };
  const typed = MF.amount(amtIn.value);
  if (!typed) return null;
  if (typed.quantity == null) { const unit = S.unitSet || base.unit; return { food, quantity: startOf(food, unit), unit, count: typed.count }; } // no number, or only "x3": the usual
  return { food, quantity: typed.quantity, unit: S.unitSet || MF.plausibleUnit(food, typed.quantity), count: typed.count };
}
/** An amount in two cells that line up down a list: its number, right-aligned, and what the number counts. The number
 *  is the weight in grams whenever the food has one, and "g" is then followed by how it was counted ("1 srv") when that
 *  was not in grams. A food without a weight has only its count: the number is that, in the same column as every other.
 *  `known` is a weight to fall back on when the food's history cannot tell (a saved entry carries its own).
 *  The third value says the number shown is a weight standing for an amount counted otherwise. */
function amountCells(food, quantity, unit, known) {
  const g = unit === "g" ? quantity : MF.grams(food, quantity, unit) ?? known;
  if (unit === "g") return [`<b>${qty(g)}</b>`, "g", false];
  if (g == null) return [`<b>${qty(quantity)}</b>`, esc(MF.abbr(unit)), false];
  return [`<b>${grams(g)}</b>`, `g<small>${qty(quantity)} ${esc(MF.abbr(unit))}</small>`, true];
}
/** A weight as read: whole grams from 10 up. */
const grams = (g) => (g >= 9.95 ? Math.round(g) : qty(g));
/** Protein, fat and carbs of a row, in one of two ways, switched from the column heads: one bar of where its energy
 *  comes from (4, 9 and 4 kcal a gram), or the three figures in grams under their heads. */
function splitBar(m) {
  if (S.figures) return `<span class="fb figs">${["protein", "fat", "carbs"].map((name) => `<i${m[name] < 0.5 ? ' class="nil"' : ""}>${Math.round(m[name])}</i>`).join("")}</span>`;
  const parts = [["p", m.protein * 4], ["f", m.fat * 9], ["c", m.carbs * 4]].filter((part) => part[1] > 0), all = parts.reduce((sum, part) => sum + part[1], 0);
  return `<span class="fb"><span class="bar">${parts.map(([name, part]) => `<i class="${name}" style="flex-grow:${(100 * part / all).toFixed(1)}"></i>`).join("")}</span></span>`;
}
const HEADS = () => `<button class="lg" tabindex="-1" aria-pressed="${S.figures}" title="Protein, fat and carbs: show ${S.figures ? "a bar of where the energy comes from" : "their figures in grams"}"><span class="p">P</span><span class="f">F</span><span class="c">C</span></button>`;
function setFigures(on) {
  S.figures = on;
  try { localStorage.setItem("mf.figures", on ? "1" : ""); } catch { /* private mode: the choice lasts as long as the page */ }
  T("act", { act: "figures", on });
  document.querySelector(".cols .lg").outerHTML = HEADS();
  renderGrid(); renderDay();
}
const weight = (g) => `${grams(g)} g`;
/** An amount as read, and what it weighs when that is not what it says: ["2 servings", "250 g"], ["7.67 g ×3", "23 g"]. */
function amountText(food, quantity, unit, count = 1) {
  const g = MF.grams(food, quantity, unit), total = g != null ? g * count : unit === "g" && count > 1 ? quantity * count : null;
  return [`${qty(quantity)} ${MF.unitLabel(unit, quantity)}${count > 1 ? ` ×${count}` : ""}`, total != null ? weight(total) : ""];
}
/** One press of an arrow in the line's amount: the count when one is being typed, else the amount (its unit then stays). */
function stepAmount(dir, other) {
  const now = draft(), typed = MF.amount(amtIn.value);
  if (!S.chosen || !now || !typed) return;
  if (typed.counting) amtIn.value = `${typed.quantity != null ? qty(typed.quantity) + " " : ""}x${Math.max(1, Math.min(20, typed.count + dir))}`;
  else { S.unitSet = now.unit; amtIn.value = qty(MF.step(now.quantity, now.unit, dir, other)); }
  S.touched = true; S.msg = "";
  renderComposer();
}

function setUnit(unit) {
  S.unitSet = unit;
  if (!S.touched) { // the number shown was only the old unit's default: 182 g must not become 182 "large"
    amtIn.value = qty(startOf(S.chosen, unit));
    if (document.activeElement === amtIn) amtIn.select();
  }
  renderComposer();
}

function renderComposer() {
  const food = target(), now = draft(), box = $("unit"), how = whenText();
  if (!food) { put(box, ""); amtIn.placeholder = "Amount"; }
  else if (S.chosen) {
    const units = MF.unitsOf(food), unit = now ? now.unit : S.unitSet || MF.usual(food).unit;
    const label = (u) => (units.filter((other) => MF.shortUnit(other) === MF.shortUnit(u)).length > 1 ? u : MF.unitLabel(u, now?.quantity ?? 1)); // short, unless two would read the same; "2 servings", as the line below says
    put(box, `<select id="usel">${units.map((u) => `<option value="${esc(u)}" title="${esc(u)}">${esc(label(u))}</option>`).join("")}</select>`);
    box.firstChild.value = unit;
    box.firstChild.title = `${unit} — to change, pick here or type the unit's first letter in the amount (g for grams)`;
    amtIn.placeholder = "";
  } else { put(box, `<span title="${esc(now.unit)}">${esc(MF.unitLabel(now.unit, now.quantity))}</span>`); amtIn.placeholder = qty(now.quantity); }
  $("composer").classList.toggle("badamt", !!food && !now);
  whenIn.placeholder = S.recipe ? "—" : S.day === today() ? `now · ${clock()}` : `at ${clock()}`;
  whenIn.disabled = !!S.recipe;
  $("whenbox").classList.toggle("set", !!S.when && !S.recipe && !S.whenBad);
  $("whenbox").classList.toggle("bad", !!S.whenBad);
  const button = $("log");
  const ready = !!S.chosen && !!now; // only a settled food with a sound amount can be logged
  button.className = !ready ? "off" : how ? "other" : ""; // grey: nothing to log yet; orange: not now, or not today
  button.tabIndex = ready ? 0 : -1;
  button.setAttribute("aria-disabled", String(!ready));
  put(button, `${S.recipe ? "Add" : how ? `Log ${esc(S.day !== today() ? `to ${dshort(S.day)}` : how)}` : "Log"} <span>↵</span>`);
  renderStrip();
}

/** The line under the input: what Enter will do, where the cursor is (the line, or an entry of the day). */
const inDay = () => !!S.cursor && (!!S.edit || document.activeElement === ledger);
function renderStrip() {
  const food = target(), now = draft(), how = whenText();
  const dot = '<span class="sep">·</span>', key = (k) => `<kbd>${k}</kbd>`;
  const figures = (m) => `<span class="sep">=</span><b>${Math.round(m.kcal)}</b><span class="fix">kcal</span>${dot}<span class="fix p">${gram(m.protein)} P</span>${dot}<span class="fix f">${gram(m.fat)} F</span>${dot}<span class="fix c">${gram(m.carbs)} C</span>`;
  const said = (f, quantity, unit, count) => { const [name, brand] = MF.splitBrand(f.display), [text, grams] = amountText(f, quantity, unit, count);
    return `<b class="nm">${esc(name)}</b>${brand ? `<span class="rest">${esc(brand)}</span>` : ""}<b>${esc(text)}</b>${grams ? `<span class="fix">${grams}</span>` : ""}`; };
  let html;
  if (S.msg) html = `<span class="bad">${esc(S.msg)}</span>`;
  else if (S.confirm) html = `<span class="rest">Delete the recipe <b>${esc(S.confirm)}</b> in MacroFactor? It cannot be undone; entries already logged stay.</span><button class="tb danger" id="delyes">Delete</button><button class="tb" id="delno">Keep</button>`;
  else if (document.activeElement === whenIn) html = `${key("↵")}<span class="fix">sets the time of this sitting: what you log next lands there</span>${dot}<span class="rest">${key("Esc")} back to now · examples: 8, 830, 20:15</span>`;
  else if (inDay()) {
    const row = rows().find((r) => r.key === S.cursor), f = row && foodOf(row.food), name = row ? MF.splitBrand(nameOf(row, f))[0] : "";
    if (!row) html = "";
    else if (S.edit) html = `${key("↵")}<span class="fix">${S.edit.field === "q" ? "sets" : "moves"}</span><b class="nm">${esc(name)}</b><span class="fix">to</span><b>${esc(S.edit.value)}</b>${dot}` +
      `<span class="rest">${key("↑")} ${key("↓")} step it${S.edit.field === "t" && !S.recipe ? ` · ${key("PgUp")} ${key("PgDn")} to ${wday(addDays(S.day, -1))} or ${wday(addDays(S.day, 1))}` : ""} · ${key("⇥")} ${S.edit.field === "q" ? "its time" : "its amount"} · ${key("Esc")} leaves it as it was</span>`;
    else html = (f && row.state !== "failed" ? `${key("↵")}<span class="fix">${S.recipe ? "adds" : "logs"}</span>${said(f, row.quantity, row.unit, 1)}<span class="fix">again</span>${how ? `<span class="at">${esc(how)}</span>` : ""}${dot}` : "") +
      `<span class="rest">a number changes its amount · ${key("@")} its time · ${key("⇥")} takes it to the line · ${key("Del")} removes it · ${key("Esc")} leaves</span>`;
  }
  else if (!food && S.peek) { const use = MF.usual(S.peek), m = MF.macros(S.peek, use.quantity, use.unit); html = said(S.peek, use.quantity, use.unit, 1) + (m ? figures(m) : ""); }
  else if (!food && S.peekRow && rows().some((r) => r.key === S.peekRow)) {
    const row = rows().find((r) => r.key === S.peekRow), f = foodOf(row.food);
    html = (f ? said(f, row.quantity, row.unit, 1) : `<b class="nm">${esc(row.name)}</b>`) + (row.kcal == null ? "" : figures({ kcal: row.kcal, protein: row.protein || 0, fat: row.fat || 0, carbs: row.carbs || 0 }));
  }
  else if (food && !now) html = `<span class="bad">“${esc(amtIn.value)}” is not an amount. Examples: 150, 2, x3, 7.67 x3</span>`;
  else if (food) {
    const m = MF.macros(food, now.quantity * now.count, now.unit), recipe = S.recipes[food.name];
    html = `${key("↵")}<span class="fix">${!S.chosen ? "chooses" : S.recipe ? "adds" : "logs"}</span>${said(food, now.quantity, now.unit, now.count)}` +
      (how && S.chosen ? `<span class="at">${esc(how)}</span>` : "") +
      (m ? figures(m) : `${dot}<span class="fix">nutrition shown once the phone confirms</span>`) +
      (S.chosen && food.kind === "recipe" ? `${dot}<span class="rest" title="${esc(recipe?.ingredients || "")}">recipe, ${qty(recipe?.servings || 1)} ${MF.unitLabel("serving", recipe?.servings || 1)}${recipe?.ingredients ? ": " + esc(recipe.ingredients.replace(/\n/g, ", ")) : ""}</span><button class="tb danger" id="delrec">Delete recipe</button>`
        : S.chosen ? `${dot}<span class="rest">${key("↑")} ${key("↓")} step it · ${key("x3")} three of them</span>`
        : `${dot}<span class="rest">then its amount, and ${key("↵")} ${S.recipe ? "adds" : "logs"} it</span>`);
  } else if (S.query.trim()) html = `<span class="rest">No food in your history matches “${esc(S.query.trim())}” · ${key("Esc")} clears</span>`;
  else if (S.recipe) html = `<span class="rest">Type an ingredient, ${key("↵")}, its amount, ${key("↵")} · nothing is logged while you build the recipe</span>`;
  else html = `<span class="rest">Type a food, ${key("↵")}, its amount, ${key("↵")} · ${key("+")} logs a row as shown · ${key("↓")} the list · ${key("↑")} the day</span>`;
  put($("strip"), html);
}

function setQuery(text) {
  Object.assign(S, { query: text, chosen: null, unitSet: null, touched: false, msg: "", confirm: null, hi: -1 }); // new text, first match; no text, nothing lit
  amtIn.value = "";
  compute(); renderGrid(); renderComposer();
}

function choose(food, digits, preset, via = "line") {
  if (!S.chosen) S.typed = foodIn.value;
  if (via !== "line") T("act", { act: "take", via, what: food.short }); // from the line it is only typing
  Object.assign(S, { chosen: food, unitSet: preset ? preset.unit : null, touched: digits != null, msg: "", confirm: null });
  foodIn.value = food.display;
  amtIn.value = digits ?? qty(preset ? preset.quantity : MF.usual(food).quantity);
  renderGrid(); renderComposer();
  amtIn.focus();
  if (digits == null) amtIn.select();
}

/** Empty the food and the amount. The time is not part of what is being typed: it is for the sitting, and stays. */
function clearComposer() {
  foodIn.value = "";
  setQuery("");
  foodIn.focus();
}

let idle;
function setWhen(time) {
  const changed = S.when !== time;
  S.whenBad = false;
  S.when = time;
  whenIn.value = time || "";
  clearTimeout(idle);
  if (time) idle = setTimeout(function expire() { // a time is for one sitting: gone 5 min after its last use, but never from under a line being written
    if (S.query.trim() || S.chosen || document.activeElement === whenIn) { idle = setTimeout(expire, 60000); return; }
    setWhen(null); renderComposer();
  }, 5 * 60000);
  if (changed) { // the insertion line, what + will do, and the picks of that hour
    if (S.keptNow && S.keptNow.hour !== hourNow() && !S.query.trim() && !S.chosen && !grid.matches(":hover")) { S.keptNow = null; compute(); } // not under the pointer: `tick` brings them once it has left
    renderLedger(); renderGrid();
  }
}
function applyWhen() {
  const text = whenIn.value.trim(), time = text ? MF.parseTime(text) : null, was = S.when;
  if (text && !time) { S.whenBad = true; return say(`“${text}” is not a time. Examples: 8, 830, 20:15`); } // it stays as typed, in red, and nothing logs meanwhile
  S.msg = "";
  setWhen(time);
  if (S.when !== was) T("act", { act: "when", what: S.when || "now" });
  renderComposer();
  if (S.when && S.when !== was) ledger.querySelector(".ins")?.scrollIntoView({ block: "center" }); // show where it will land
}

/** Enter in the food field: settle the food (the lit match) and move to its amount. It never logs. */
function pick() {
  const lit = S.hi >= 0 ? S.list[S.hi] : null;
  if (S.chosen) { amtIn.focus(); amtIn.select(); }
  else if (lit) choose(lit);
  else say(foodIn.value.trim() ? `Nothing in your history matches “${foodIn.value.trim()}”.` : "");
}
/** Enter in the amount, and the Log button: log what the line says. Only a settled food is logged. */
function commit() {
  if (!S.chosen) return amtIn.value.trim() && !target() ? say("Choose a food first.") : pick();
  const now = draft();
  if (now) return logItems([now], "line");
  say(/[a-z]/i.test(amtIn.value.replace(/x/gi, "")) ? noUnit(S.chosen, amtIn.value) : `“${amtIn.value}” is not an amount.`);
}
/** Why an amount with a unit this food does not have cannot be logged, and what it can be logged in. */
function noUnit(food, text) {
  const units = MF.unitsOf(food).map((unit) => MF.unitLabel(unit, 2)).join(", "), asked = text.replace(/[\d.,\sx*×]/gi, "");
  return asked.toLowerCase() === "g" && food.weightless ? `${food.short} has no weight in MacroFactor: it is logged in ${units}, not in grams.` : `${food.short} has no unit “${asked}”: it is logged in ${units}.`;
}

// ================================================================ jobs: how they are said
/** One sentence pattern for everything the phone does: verb (to do, doing, done) + what. The server sends the "what". */
const VERBS = {
  log: ["Log", "Logging", "Logged"], edit: ["Set", "Setting", "Set"], retime: ["Move", "Moving", "Moved"],
  remove: ["Remove", "Removing", "Removed"], undo: ["Undo", "Undoing", "Undid"], sync: ["Sync", "Syncing", "Synced"],
  recipe: ["Create", "Creating", "Created"], "recipe-delete": ["Delete", "Deleting", "Deleted"],
};
const STEPS = { going: "changing day", ingredient: "adding", opening: "opening the app" }; // the phone's progress lines, as one quiet word
const active = (job) => job.status === "queued" || job.status === "running";
const label = (job, form) => (job.kind === "undo" ? `${VERBS.undo[form]}: ${job.label}` : `${VERBS[job.kind]?.[form] || ""} ${job.label}`.trim());
const elsewhere = (change) => (change.days?.length && !change.days.includes(S.day) ? ` (on ${change.days.map(dshort).join(", ")})` : ""); // a change of a day that is not the one shown
const named = (job) => `${(VERBS[job.kind]?.[0] || "").toLowerCase()} ${job.label}`.trim(); // a change as Undo speaks of it: "move Light Cake to 17:25"
const stepOf = (job) => { const word = (job.progress || "").split(" ")[0]; return STEPS[word] || word; };

// ================================================================ what the server says, and what is asked of it
/** Take a reading of the server's state (every answer of the server is one). The server stamps them in the order
 *  it took them (`at`), and one taken before the one on screen is dropped: a slow answer must not put the page back in time. */
async function reading(path, body, key) {
  const state = await api(`${path}?day=${S.day}`, body, key);
  if (!(state.at <= S.snap?.at)) apply(state);
}
function apply(state) {
  if (state.build !== document.documentElement.dataset.build) { S.stale = true; return renderPhone(); } // mf was restarted with another page
  const before = S.snap, last = state.last;
  Object.assign(S, { snap: state, down: false });
  if (last && before?.last?.key !== last.key) { // a row that was waiting is now a saved entry: the cursor follows it
    last.ids.forEach((id, index) => { for (const name of ["cursor", "lastKey"]) if (S[name] === `${last.key}-${index}`) S[name] = `e${id}`; });
  }
  let fresh = false;
  if (state.day.date === S.day) {
    fresh = S.data?.date !== S.day;
    S.data = state.day;
    S.days.set(S.day, state.day);
  }
  const current = state.catalog === S.catalog;
  derive(current); // the amount a log asked for stays shown until the catalog that knows it arrives
  if (!current) loadCatalog().catch(() => {}); // foods, amounts last used, recipes: they changed, here or from the command line
  const keys = new Set(rows().map((row) => row.key));
  for (const name of ["cursor", "lastKey"]) if (S[name] && !keys.has(S[name])) S[name] = null;
  if (S.edit && !keys.has(S.edit.key)) S.edit = null;
  renderDay(); renderPhone();
  if (fresh) ledger.scrollTop = S.day === today() ? ledger.scrollHeight : 0;
}
/** Every job (the server's, then what it has not acknowledged yet) and the changes they make to the day. A request
 *  the server has named, as on its way or as the last one ended, is the server's from then on, whatever became of
 *  its answer. With `amounts`, logs on their way count as the catalog will count them once saved: their amount is
 *  the food's last, and the food is among the latest. */
function derive(amounts = true) {
  const told = new Set([...(S.snap?.jobs || []).map((job) => job.key), S.snap?.last?.key]);
  S.asked = S.asked.filter((job) => !told.has(job.key));
  S.jobs = [...(S.snap?.jobs || []), ...S.asked];
  S.ops = MF.ops(S.jobs, today());
  if (!amounts) return;
  for (const food of S.foods) if (food.base) food.servings = food.base;
  S.recent = new Map();
  for (const op of S.ops) {
    if (op.kind !== "log" || op.state === "cancelling" || op.state === "failed") continue;
    for (const item of op.items) {
      S.recent.set(item.name, `${op.day} ${op.time}`);
      const food = foodOf(item.name);
      if (!food) continue;
      food.base ??= food.servings;
      food.servings = [{ quantity: item.quantity, unit: item.unit }, ...food.servings.filter((s) => !(s.quantity === item.quantity && s.unit === item.unit))];
    }
  }
}
function redraw() { derive(); renderGrid(); renderComposer(); renderDay(); renderPhone(); }

/** Ask for something on the phone. It shows at once, drawn like any job, and is sent; the server's answer is a
 *  reading that lists it, and from then on it is the server's. If no answer comes it stays, failed, to be sent
 *  again under the same name (so that the server, had it received it after all, does not do it twice). */
const name = () => `${PAGE}-${++seq}`;
function ask(kind, args, what, info) {
  const key = name();
  const job = { key, kind, args, label: what, status: "queued", cancel: false, created: Date.now() / 1000, local: true };
  T("act", { act: kind, what, op: key, ...info });
  S.asked.push(job);
  send(job);
  return job;
}
function send(job) {
  Object.assign(job, { status: "queued", error: null });
  reading("/api/jobs", { kind: job.kind, args: job.args }, job.key).then(() => {
    S.asked = S.asked.filter((other) => other !== job); // even if a newer job already occupies the last-result slot
    redraw(); follow();
  }, (error) => { Object.assign(job, { status: "failed", error: error.message }); redraw(); });
  redraw();
}

/** Log foods (each may be asked `count` times: as many entries). `via` is where it was asked from: the line, a + of the
 *  list, or an entry of the day. Only the line is cleared by it: a + leaves everything where the pointer found it. */
function logItems(items, via) {
  if (document.activeElement === whenIn) applyWhen(); // a + pressed from the time field: the time it shows is the one meant
  if (S.whenBad && !S.recipe) return say(`“${whenIn.value.trim()}” is not a time. Examples: 8, 830, 20:15`);
  const each = items.flatMap((item) => Array.from({ length: item.count || 1 }, () => item));
  if (S.recipe) {
    S.recipe.error = null;
    for (const item of each) S.recipe.items.push({ key: `r${++seq}`, name: item.food.name, quantity: item.quantity, unit: item.unit });
    T("act", { act: "ingredient", via, what: items.map((item) => item.food.short).join(", ") });
    if (via === "line") clearComposer();
    return renderDay();
  }
  const wire = each.map((item) => ({ name: item.food.name, quantity: item.quantity, unit: item.unit }));
  const what = items.map((item) => `${item.food.short} ${amountText(item.food, item.quantity, item.unit, item.count)[0]}`).join(", ");
  const where = via === "day" ? {} : { rank: S.list.indexOf(items[0].food), view: S.query.trim() ? "search" : S.tab }; // for the trace: where in the list it was
  const info = via === "line" ? { q: (S.chosen ? S.typed : foodIn.value).trim() || undefined, ...where } : where;
  const job = ask("log", { items: wire, date: S.day, time: S.when }, what, { via, ...info });
  if (S.when) setWhen(S.when); // still the same sitting
  if (via === "day") return renderDay(); // the row, the scroll and the line stay as they are: Enter or + again logs one more
  if (via === "list") { // the list stays as it is under the pointer; what was typed is ready to be typed over
    if (S.query.trim() && !S.chosen) foodIn.select();
    renderGrid(); renderComposer();
  } else {
    S.keptNow = null; // the list comes back from your search: rebuilt, this food first among the latest
    S.lastKey = `${job.key}-0`;
    clearComposer();
  }
  renderDay();
  ledger.querySelector(`[data-key="${job.key}-0"]`)?.scrollIntoView({ block: "nearest" });
}
/** An entry of the day, once more: same food, same amount, now (or at the time set). */
function again(row) {
  const food = foodOf(row.food);
  if (!food || row.state === "failed") return;
  const aimed = underPointer();
  logItems([{ food, quantity: row.quantity, unit: row.unit }], "day");
  if (underPointer() !== aimed) ledger.classList.add("still"); // logged at a time above it, the row moved from under the pointer
}
/** An entry of the day, taken to the line with its amount ready to be typed over: the same food, another amount. */
function take(row) {
  const food = foodOf(row.food);
  if (!food || row.state === "failed") return false;
  if (S.edit) endEdit(false);
  if (S.cursor) { S.cursor = null; renderLedger(); }
  choose(food, null, MF.unitsOf(food).includes(row.unit) ? { quantity: row.quantity, unit: row.unit } : null, "day"); // grams of a food that has no weight any more: its usual amount instead
  return true;
}

function editAmount(row, quantity, unit) {
  if (S.recipe) { Object.assign(S.recipe.items.find((item) => item.key === row.key), { quantity, unit }); return; }
  if (quantity === row.quantity && unit === row.unit) return;
  ask("edit", { id: row.id, on: S.day, quantity, unit }, `${MF.splitBrand(row.name)[0]} to ${qty(quantity)} ${MF.shortUnit(unit)}`);
}
function retime(row, time, shift) {
  if (time === row.time && !shift) return;
  const toDay = shift ? addDays(S.day, shift) : null;
  ask("retime", { id: row.id, on: S.day, time, date: toDay }, `${MF.splitBrand(row.name)[0]} to ${toDay ? dshort(toDay) + " " : ""}${time}`);
}
function removeRow(row) {
  if (S.recipe) { S.recipe.items = S.recipe.items.filter((item) => item.key !== row.key); return renderDay(); }
  ask("remove", { id: row.id, on: S.day }, `${MF.splitBrand(row.name)[0]} ${qty(row.quantity)} ${MF.shortUnit(row.unit)}`);
}

/** Cancel a job the phone has not confirmed. Shown at once as never asked; the phone then drops it, stops before
 *  saving anything, or, when it had just saved, takes it back. If none of that is possible the next reading says so. */
function cancel(job) {
  if (!job || !active(job) || job.local || job.cancel) return;
  T("act", { act: "cancel", what: label(job, 0), op: job.key });
  job.cancel = true;
  redraw();
  reading(`/api/jobs/${job.key}/cancel`, {}).catch(follow); // it had just finished: the reading says how
}
/** Undo, as in an editor: each press takes back one more change made from the page, the latest first. A change still
 *  on its way to the phone is cancelled instead. While an undo is on its way a press does nothing: the change before
 *  it can only be taken back once this one is, and a press kept for later could reach for something else by then. */
function undo() {
  if (S.jobs.some((job) => active(job) && job.kind === "undo")) return;
  const waiting = S.jobs.filter((job) => active(job) && !job.cancel).pop();
  if (waiting) return cancel(waiting); // what was asked last has not happened yet: undoing it is cancelling it
  const next = S.snap?.undo;
  if (next && S.snap.phone !== null) ask("undo", { op: next.op }, named(next)); // by the name the server gave it: that change, whatever was done since
}
const failures = () => S.jobs.filter((job) => job.status === "failed" || job.status === "uncertain");
function check(job = failures().pop()) {
  if (job?.check) ask("sync", { day: job.check }, "the phone; review the saved entries before making another change");
}
function retry(job = failures().pop()) {
  if (!job || !(job.local || job.retry)) return;
  const key = job.local ? job.key : name(); // the trace names the job this starts
  T("act", { act: "retry", what: label(job, 0), op: key });
  if (job.local) return send(job); // it never reached the server: the same request, under the same name
  reading(`/api/jobs/${job.key}/retry`, {}, key).then(follow, (error) => { say(cap(error.message)); follow(); }); // the server asks it again as a new job, once
}
function dismiss(job = failures().pop()) {
  if (!job) return;
  T("act", { act: "dismiss", what: label(job, 0), op: job.key });
  if (job.local) { S.asked = S.asked.filter((other) => other !== job); return redraw(); }
  reading(`/api/jobs/${job.key}/dismiss`, {}).catch(follow);
}

// ================================================================ loading
let loading = null;
function loadCatalog() { return (loading ??= fetchCatalog().finally(() => { loading = null; })); } // one at a time; a reading asks again if it is still old
async function fetchCatalog() {
  const data = await api("/api/catalog");
  MF.prepare(data.foods);
  Object.assign(S, { catalog: data.version, foods: data.foods, aliases: data.aliases, recipes: data.recipes, byName: new Map(data.foods.map((food) => [food.name, food])), loaded: true });
  if (S.chosen) S.chosen = S.byName.get(S.chosen.name) || null;
  derive();
  const lit = S.hi >= 0 ? S.list[S.hi]?.name : null; // Enter keeps meaning the same food
  S.hi = -1;
  compute();
  if (lit) { const at = S.list.findIndex((food) => food.name === lit); if (at >= 0) S.hi = at; }
  renderGrid(); renderComposer(); renderDay();
}

/** The server's state is read, not listened to: every 0.4 s while something is on its way, every 2 s otherwise, and
 *  not at all by a page nobody is looking at. A reading is the whole truth, so nothing is missed by having been
 *  away, and no page holds a connection (a browser allows a site six: held ones ran out once, with tabs left open). */
let busy = false, nextRead = 0;
async function follow() {
  if (busy || document.hidden || S.stale) return;
  busy = true;
  clearTimeout(nextRead);
  try { await reading("/api/state"); } catch { S.down = true; renderPhone(); }
  busy = false;
  if (!S.stale) nextRead = setTimeout(follow, S.jobs.some(active) ? 400 : 2000);
}

function go(day) {
  if (S.recipe || day === S.day) return;
  T("act", { act: "day", what: day });
  Object.assign(S, { day, cursor: null, edit: null, data: S.days.get(day) || null }); // a day seen before shows at once
  setWhen(null);
  renderComposer(); renderGrid(); renderDay();
  ledger.scrollTop = day === today() ? ledger.scrollHeight : 0;
  follow();
}

// ================================================================ the day (or the recipe being built)
/** A food of the catalogue by its name there. A row of the day says its food (`row.food`): for a name several recipes
 *  share, the one that entry is of, or none; the row itself keeps the name MacroFactor has for it. */
const foodOf = (name) => S.byName.get(name);
const nameOf = (row, food) => food?.shared?.name || food?.display || row.name;
function rows() {
  if (S.recipe) return S.recipe.items.map((item) => ({ ...item, food: item.name, time: "", ...(MF.macros(foodOf(item.name), item.quantity, item.unit) || {}) }));
  return MF.view(S.data?.entries || [], S.ops, S.day, foodOf, S.day === today() ? clock() : null);
}

function renderDayHead() {
  const head = $("dayhead");
  if (S.recipe) {
    if (!head.querySelector(".rname")) {
      head._html = null;
      head.innerHTML = '<span class="rtitle">New recipe</span><input class="rname" id="rname" placeholder="Name" autocomplete="off" aria-label="Recipe name"><span class="lbl">servings</span><input class="rserv" id="rserv" inputmode="decimal" aria-label="Servings"><button class="tb box" id="rdiscard">Discard</button><button class="tb box pri" id="rsave">Save recipe</button>';
      $("rname").value = S.recipe.name;
      $("rserv").value = S.recipe.servings;
    }
    return;
  }
  const diff = dayDiff(S.day, today());
  const rel = diff === 0 ? "today" : diff === -1 ? "yesterday" : diff === 1 ? "tomorrow" : `${Math.abs(diff)} days ${diff < 0 ? "ago" : "ahead"}`;
  put(head, '<button class="tb box nav" id="prev" title="Previous day (PageUp)">‹</button><button class="tb box nav" id="next" title="Next day (PageDown)">›</button>' +
    `<button class="date${diff ? " other" : ""}" id="date" title="Pick a date">${esc(dlong(S.day))}<small>${rel}</small></button>` +
    `<button class="tb box last" id="today"${diff ? "" : " disabled"}>Back to today</button>`);
}

function renderSums() {
  const all = rows(), total = MF.totals(all);
  const line = (cls, name, values, signed) => `<div class="sum ${cls}"><span></span><span class="n">${name}</span><span></span>` + // red is for one thing: more energy than the target. What is left is never less than nothing: past the target it is 0
    MF.MACROS.map((m, i) => `<span class="${i ? "m" : "k"}${!i && signed && values && values[m] < -0.5 ? " over" : ""}">${values ? Math.round(signed ? Math.max(0, values[m]) : values[m]) : ""}</span>`).join("") + "<span></span></div>";
  if (S.recipe) {
    const n = MF.number(S.recipe.servings);
    const each = n > 0 ? Object.fromEntries(MF.MACROS.map((m) => [m, total[m] / n])) : null; // no number of servings, no per-serving figures
    return put($("sums"), line("tgt", S.recipe.error ? `<span class="bad">${esc(S.recipe.error)}</span>` : `${all.length} ingredient${all.length === 1 ? "" : "s"}`, null) + line("eaten", "Whole recipe", total) + line("left", "Per serving", each));
  }
  // MacroFactor's export rounds every entry to whole numbers, the app adds the exact ones: the rows rarely add up to
  // the app's figure. `phone.offset` is the difference when mf last looked at the phone; with it, Eaten is the app's.
  const goal = S.data?.targets, phone = S.data?.phone, off = phone && !phone.apart ? phone.offset : null;
  if (S.data?.uncertain) {
    const busy = S.jobs.some(active), note = busy ? "confirming" : "Sync to confirm";
    return put($("sums"), line("tgt", "Target", goal) + line("eaten", `Eaten <span class="${busy ? "rest" : "bad"}">${note}</span>`, null) + line("left", "Left", null));
  }
  const eaten = phone?.apart ? phone.totals : off ? Object.fromEntries(MF.MACROS.map((m) => [m, total[m] + off[m]])) : total;
  const left = goal && Object.fromEntries(MF.MACROS.map((m) => [m, goal[m] - eaten[m]]));
  const name = phone?.apart ? `Eaten <span class="bad" title="Showing the phone's totals read at ${esc(phone.read_at.slice(11, 16))}. The local entries differ; Sync checks the phone and reconciles them.">phone totals · Sync entries</span>`
    : `<span title="${off ? (all.some((row) => row.pending) ? "MacroFactor's figure and what is on its way." : "As MacroFactor shows it.") + " Its export rounds each entry, so the rows below can add up to a few units more or less." : "The sum of the entries below; MacroFactor's own figure can differ by a few units (its export rounds each entry)."}">Eaten</span>`;
  put($("sums"), line("tgt", "Target", goal) + line("eaten", name, S.data ? eaten : null) + line("left", "Left", left, true));
}

function renderLedger() {
  const all = rows();
  let html = "", prev = null;
  for (const row of all) {
    const ed = S.edit && S.edit.key === row.key ? S.edit : null;
    const rep = prev && prev.time === row.time, gap = prev && row.time && minutes(row.time) - minutes(prev.time) >= 30;
    const saved = !row.pending && row.state !== "failed" && (row.id != null || S.recipe);
    const [name, brand] = MF.splitBrand(nameOf(row, foodOf(row.food)));
    const state = row.state === "failed" ? "failed" : row.pending ? row.state : "";
    const acts = ed && ed.field === "t" ? `<button class="tb" data-shift="-1" title="Move to ${dlong(addDays(S.day, -1))}, at this time">‹ ${wday(addDays(S.day, -1))}</button><button class="tb" data-shift="1" title="Move to ${dlong(addDays(S.day, 1))}, at this time">${wday(addDays(S.day, 1))} ›</button>`
      : row.state === "failed" ? `<button class="tb" data-retry${row.op.job.local || row.op.job.retry ? "" : " disabled"} title="Send it to the phone again">Retry</button><button class="tb x" data-dismiss title="Drop it">×</button>`
      : row.pending ? (!row.op.job.local ? '<button class="tb" data-cancel title="Cancel: nothing is changed, or it is taken back">Cancel</button>' : "")
      : `<button class="tb plus" title="${S.recipe ? "Add" : "Log"} it once more (↵)">+</button><button class="tb x" title="Remove (Del)">×</button>`;
    const [weighs, counts, counted] = amountCells(foodOf(row.food), row.quantity, row.unit, row.pending ? null : row.grams);
    const editing = ed && ed.field === "q"; // an amount is edited where its own number is: in the number column, or after the weight that stands for it
    const input = (value) => `<input id="edin"${ed.error ? ' class="bad"' : ""} value="${esc(value)}" autocomplete="off" title="${ed.field === "q" ? "180, 180 g, 2 servings" : "8, 830, 20:15"}">`;
    html += `<div class="row${gap ? " gap" : ""}${row.pending && state !== "failed" ? " pend" : ""}${state === "failed" ? " failed" : ""}${saved ? " saved" : ""}${S.cursor === row.key ? " cur" : ""}" data-key="${esc(row.key)}">` +
      `<span>${state ? `<i class="mk ${state}"></i>` : ""}</span>` +
      `<span class="t${rep && !ed ? " rep" : ""}${ed && ed.field === "t" ? " ed" : ""}">${ed && ed.field === "t" ? input(ed.value) : esc(row.time)}</span>` +
      (ed?.error ? `<span class="n err"><b>${esc(name)}</b><span class="bad">${esc(ed.error)}</span></span>` // said where you are looking
        : `<span class="n" title="${esc(row.name)}${row.state === "failed" ? " — " + esc(row.op.job.error || "failed") : ""}"><b>${esc(name)}</b>${brand ? `<small>${esc(brand)}</small>` : ""}</span>`) +
      `<span class="q">${editing && !counted ? input(ed.value) : weighs}</span>` +
      `<span class="u${editing && counted ? " ed" : ""}" title="${esc(row.unit)}">${editing && counted ? `g${input(ed.value)}<i>${esc(MF.abbr(row.unit))}</i>` : counts}</span>` +
      `<span class="k">${row.kcal == null ? "" : Math.round(row.kcal)}</span>${row.kcal == null ? '<span class="fb"></span>' : splitBar({ protein: row.protein || 0, fat: row.fat || 0, carbs: row.carbs || 0 })}<span class="a${ed && ed.field === "t" ? " days" : ""}">${acts}</span></div>`;
    prev = row;
  }
  if (!all.length) html = `<div class="empty">${S.recipe ? "No ingredients yet." : S.data?.uncertain ? "Check the phone to confirm this day." : S.data ? "Nothing logged this day." : ""}</div>`;
  if (S.when && !S.recipe) { // where the next one lands; a row logged "now" counts at the present, so the phone confirming it moves nothing
    const present = minutes(clock()) + 2.5, at = all.filter((row) => (row.rank ? present : minutes(row.time)) <= minutes(S.when)).length; html += `<div class="ins${at ? "" : " top"}" style="top:${at * ROW}px" title="Logged at ${S.when}, it lands here"></div>`; }
  $("timecol").textContent = S.recipe ? "" : "Time";
  const old = $("edin"), caret = old && document.activeElement === old ? [old.selectionStart, old.selectionEnd] : null, scroll = ledger.scrollTop;
  const aimed = underPointer();
  renderStrip();
  ledger.classList.toggle("other", !!whenText());
  if (!put(ledger, html)) return;
  ledger.scrollTop = scroll;
  if (!byHand && underPointer() !== aimed) ledger.classList.add("still"); // another action came under a resting pointer: see the pointermove handler
  const field = $("edin");
  if (field) {
    field.focus();
    if (S.edit.fresh === "end") field.setSelectionRange(field.value.length, field.value.length); // a digit typed on the row started it, or an arrow stepped it
    else if (S.edit.fresh) field.select();
    else if (caret) field.setSelectionRange(caret[0], caret[1]);
    S.edit.fresh = false;
  }
}
function renderDay() { renderDayHead(); renderSums(); renderLedger(); }
let byHand = false; // true while a click in the ledger is being handled: what it redraws, you asked for
let pointer = null; // where the pointer last really was
/** The row action under the pointer, as text: which row, and what its action cell offers. */
function underPointer() {
  const cell = pointer && document.elementFromPoint(pointer[0], pointer[1])?.closest(".ledger .a");
  return cell ? cell.parentNode.dataset.key + cell.innerHTML : "";
}

function startEdit(key, field, text) {
  const row = rows().find((r) => r.key === key);
  const busy = row && row.pending && !(row.id != null && ["edit", "retime"].includes(row.pending)); // an entry waiting for one change can take another
  if (!row || busy || row.state === "failed" || (field === "t" && S.recipe)) return false;
  S.cursor = key;
  S.edit = { key, field, value: text ?? (field === "q" ? qty(row.quantity) : row.time.replace("??", "00")), fresh: text == null ? true : "end" };
  renderLedger();
  return true;
}
/** Leave the field. False when the value cannot be saved: the field stays open and the row says why. */
function endEdit(save, shift = 0) {
  const ed = S.edit;
  if (!ed) return true;
  const row = rows().find((r) => r.key === ed.key);
  if (save && row) {
    if (ed.field === "q") {
      const match = ed.value.trim().match(/^([\d.,]+)\s*(.*)$/), food = foodOf(row.food), amount = match && MF.number(match[1]);
      const unit = match && (match[2].trim() ? (food ? MF.unitByPrefix(food, match[2]) : match[2].trim()) : row.unit);
      if (!(amount > 0 && unit)) ed.error = "Not an amount"; // the field's tooltip has examples
      else { S.edit = null; editAmount(row, amount, unit); }
    } else {
      const time = MF.parseTime(ed.value);
      if (!time) ed.error = "Not a time";
      else { S.edit = null; retime(row, time, shift); }
    }
  } else S.edit = null;
  renderDay();
  return !S.edit;
}

/** One press of an arrow in an entry's open field: the amount by a unit (Shift: 10 g, or half a unit), the time by a minute (Shift: ten). */
function stepEdit(dir, other) {
  const ed = S.edit, row = rows().find((r) => r.key === ed.key);
  if (!row) return;
  if (ed.field === "q") {
    const match = ed.value.trim().match(/^([\d.,]+)\s*(.*)$/), amount = match && MF.number(match[1]), food = foodOf(row.food);
    if (!(amount > 0)) return;
    const unit = (match[2].trim() && food && MF.unitByPrefix(food, match[2])) || row.unit;
    ed.value = `${qty(MF.step(amount, unit, dir, other))}${match[2].trim() ? " " + match[2].trim() : ""}`;
  } else {
    const time = MF.parseTime(ed.value);
    if (!time) return;
    ed.value = MF.shiftTime(time, dir * (other ? 10 : 1));
  }
  ed.error = null; ed.fresh = "end";
  renderLedger();
}

// ================================================================ the phone line
/** Six fixed slots: name, the one action on what the phone is doing (or did), that in words, the queue, a failure, sync.
 *  Each action comes before its text, so no control moves because a sentence or a number got longer. */
const SLOTS = ["act", "queue", "fail"];
let aim = {}, phoneHand = false; // what the line's buttons act on; true while a click on the line is being handled
function renderPhone() {
  const jobs = S.jobs.filter(active);
  const current = jobs.find((job) => job.status === "running") || jobs[0], waiting = jobs.filter((job) => job !== current), lastAsked = waiting[waiting.length - 1];
  const failed = failures(), failure = failed[failed.length - 1], snap = S.snap, last = snap?.last;
  let now = "", act = "", target = "";
  if (S.stale) now = '<span class="txt err">mf has been updated — reload this page</span>';
  else if (S.down) now = '<span class="txt err">mf is not answering — is `mf serve` still running?</span>';
  else if (!snap) now = "";
  else if (current) {
    const running = current.status === "running", stopping = current.cancel;
    const text = stopping ? `Cancelling: ${label(current, 0)}` : label(current, running ? 1 : 0);
    now = `<i class="mk ${running ? "running" : "queued"}"></i><span class="txt" title="${esc(text)}">${esc(text)}</span><span class="step">${running && !stopping ? esc(stepOf(current)) : ""}</span>`;
    if (!stopping && !current.local) { target = `c${current.key}`; act = `<button class="tb" data-cancel="${esc(current.key)}" title="Stop it: nothing is changed, or it is taken back">Cancel</button>`; }
  } else if (snap.phone === null) now = '<span class="txt err">Not connected — showing saved data; changes will fail</span>';
  else if (last) {
    const text = last.status === "cancelled" ? `Cancelled: ${label(last, 0)} — ${last.note}` : `${label(last, 2)}${last.note ? ` — ${last.note}` : ""}`;
    const further = snap.undo && (snap.undo.op !== last.undo || elsewhere(snap.undo)) ? `Undo: ${named(snap.undo)}${elsewhere(snap.undo)}` : ""; // not what the line speaks of: say which
    now = `<span class="ok">✓</span><span class="txt" title="${esc(text)}">${esc(text)}</span><span class="next" title="${esc(further)}">${esc(further)}</span>`;
  } else now = '<span class="txt">Ready</span>';
  if (!current && snap?.undo && snap.phone !== null && !S.stale && !S.down) { // the latest change that still stands, whatever the line last spoke of
    target = `u${snap.undo.op}`; act = `<button class="tb" id="undo" title="Undo: ${esc(named(snap.undo) + elsewhere(snap.undo))} (Ctrl+Z; once it is done, again for the change before)">Undo</button>`; }
  if (snap?.update_available && !current && !S.stale) now = '<span class="txt err">Update ready — restart mf serve, then reload</span>';
  const queue = lastAsked && !lastAsked.local && !lastAsked.cancel ? `<button class="tb" data-cancel="${esc(lastAsked.key)}" title="Cancel the last one asked: ${esc(label(lastAsked, 0))}">Cancel last</button><span>${waiting.length} waiting</span>`
    : waiting.length ? `<span>${waiting.length} waiting</span>` : "";
  const at = failure && MF.clockOf(failure.created); // a failure is of its moment: it may have been put right since
  const said = failure && `${at} · ${label(failure, 0)} ${failure.status === "uncertain" ? "may be saved, check first" : "failed"}: ${failure.error || failure.note || ""}`;
  const recover = failure?.check ? '<button class="tb" id="check">Check</button>' : `<button class="tb" id="retry"${failure?.local || failure?.retry ? "" : " disabled"}>Retry</button>`;
  const fail = failure ? `<button class="tb" id="dismiss">Dismiss</button>${recover}<i class="mk failed"></i>` +
    `${failed.length > 1 ? `<span class="more">${failed.length} failures · latest:</span>` : ""}<span class="txt" title="${esc(said)}">${esc(said)}</span>` : "";
  const seen = S.data?.phone?.read_at; // when mf last looked at this day on the phone
  const stamp = !snap || S.down || !seen ? "" : `<span title="MacroFactor's own totals for this day were read then">phone seen ${seen.slice(0, 10) === today() ? seen.slice(11, 16) : dshort(seen.slice(0, 10))}</span>`;
  put($("phone"), `<span class="who">Phone</span><span class="act">${act}</span><span class="now${current?.status === "running" ? " run" : ""}">${now}</span><span class="queue">${queue}</span>` +
    `<span class="fail">${fail}</span><span class="rt">${stamp}<span class="sync"><button class="tb" id="sync" title="Read this day again from the phone's screen: picks up what you changed on the phone itself">Sync</button>` +
    `<button class="tb" id="syncmore" popovertarget="syncmenu" aria-label="What to sync" title="What to sync: this day, the recipes, or the last 7 days">▾</button></span></span>`);
  const left = snap?.exports_left ?? 0;
  put($("exports"), `One export from MacroFactor, exact to the minute. ${left ? `${left} left today.` : "None left today."}`);
  $("syncmenu").querySelector("[data-sync=week]").disabled = !left;
  const aims = { act: target, queue: lastAsked?.key ?? "", fail: failure?.key ?? "" };
  if (!phoneHand) for (const slot of SLOTS) if (aims[slot] !== aim[slot]) $("phone").classList.add(`still-${slot}`); // its buttons now act on something else, and not by your click: armed once the pointer moves
  aim = aims;
}

// ================================================================ events: composer
foodIn.addEventListener("input", () => {
  const text = foodIn.value, split = MF.splitAmount(text);
  setQuery(text);
  // "apple 18", "apple18", "galette x3": no food is called that, so the number is the amount (or the count) of the best match for "apple"
  if (!S.list.length && split) { const found = MF.search(pool(), S.aliases, split[0]); if (found.length) { S.query = split[0]; compute(); choose(found[0], split[1]); S.typed = text.slice(0, text.length - split[1].length); } }
});
for (const field of [foodIn, amtIn]) field.addEventListener("focus", () => { if (S.cursor && !S.edit) { S.cursor = null; renderLedger(); } });
let reselect = false; // the click that brought the focus would otherwise drop the selection where it landed
foodIn.addEventListener("focus", () => { reselect = !!S.chosen; if (reselect) foodIn.select(); });
foodIn.addEventListener("mouseup", (event) => { if (reselect) { event.preventDefault(); reselect = false; } });
foodIn.addEventListener("blur", () => { reselect = false; });
foodIn.addEventListener("keydown", (event) => {
  const key = event.key, lit = S.hi >= 0 ? S.list[S.hi] : null;
  if (key === "@") { event.preventDefault(); if (S.recipe) return; if (lit && !S.chosen) choose(lit); whenIn.focus(); whenIn.select(); return; }
  if (event.ctrlKey || event.metaKey || event.altKey) return;
  // a digit after a space starts the amount, unless a food's name goes on that way ("Jello 2", "Galettes 4 Céréales")
  if (/^\d$/.test(key) && lit && !S.chosen && /\s$/.test(foodIn.value) && foodIn.value.trim() && !MF.search(pool(), S.aliases, foodIn.value + key).length) { event.preventDefault(); choose(lit, key); }
  else if (key === "Tab" && !event.shiftKey && lit && !S.chosen) { event.preventDefault(); pick(); }
  else if (key === "ArrowDown") { event.preventDefault(); if (!S.chosen && S.hi < S.list.length - 1) { S.hi++; renderGrid(); renderComposer(); } }
  else if (key === "ArrowUp") {
    event.preventDefault();
    if (S.chosen) return;
    if (S.hi > 0 || (S.hi === 0 && !S.query.trim())) { S.hi--; renderGrid(); renderComposer(); } else if (S.hi < 0) enterLedger();
  } else if ((key === "ArrowLeft" || key === "ArrowRight") && !foodIn.value && S.hi >= 0) { // nothing typed: the arrows are the list's, across its columns too
    event.preventDefault();
    const at = S.hi + (key === "ArrowLeft" ? -S.rows : S.rows);
    if (at >= 0 && at < S.list.length) { S.hi = at; renderGrid(); renderComposer(); }
  } else if (key === "Enter") { event.preventDefault(); if (!event.repeat) pick(); } // confirms this field: the food. The amount's Enter logs
  else if (key === "Escape") clearComposer();
});
amtIn.addEventListener("input", () => { S.touched = true; S.msg = ""; renderComposer(); });
amtIn.addEventListener("keydown", (event) => {
  const key = event.key;
  if (key === "@") { event.preventDefault(); if (!S.recipe) { whenIn.focus(); whenIn.select(); } return; }
  if (event.ctrlKey || event.metaKey || event.altKey) return;
  if (key === "Enter") { event.preventDefault(); if (!event.repeat) commit(); }
  else if (key === "Backspace" && !amtIn.value && S.chosen) { event.preventDefault(); const text = S.typed.trimEnd(); foodIn.value = text; setQuery(text); foodIn.focus(); }
  else if (key === "Escape") clearComposer();
  else if (key === "ArrowUp" || key === "ArrowDown") { event.preventDefault(); stepAmount(key === "ArrowUp" ? 1 : -1, event.shiftKey); }
  else if (key.length === 1 && !/[\d.,x*× ]/i.test(key)) { // a letter names the unit: g, s(erving), f(l oz)…; x is "times"
    event.preventDefault();
    const unit = S.chosen && /\S/.test(key) && MF.unitByPrefix(S.chosen, key, S.unitSet);
    if (unit) setUnit(unit);
    else if (S.chosen && /\S/.test(key)) { // it names no unit of this food: it stays in the field, which is then not an amount. 50 then g must not become 50 servings
      amtIn.setRangeText(key, amtIn.selectionStart, amtIn.selectionEnd, "end");
      S.touched = true; say(noUnit(S.chosen, amtIn.value));
    }
  }
});
let unitByKeys = false; // choosing in the unit list with the keyboard keeps the focus there; with the mouse it returns to the amount
$("unit").addEventListener("keydown", (event) => {
  unitByKeys = true;
  if (event.key === "Enter") { event.preventDefault(); amtIn.focus(); amtIn.select(); } // the unit is settled: back to the amount, whose Enter logs
  else if (event.key === "Escape") clearComposer();
});
$("unit").addEventListener("mousedown", () => { unitByKeys = false; });
$("unit").addEventListener("change", (event) => { setUnit(event.target.value); if (!unitByKeys) { amtIn.focus(); amtIn.select(); } else $("usel")?.focus(); });
whenIn.addEventListener("keydown", (event) => {
  if (event.key === "Enter") { event.preventDefault(); if (event.repeat) return; applyWhen(); if (S.msg) return; if (S.chosen) { amtIn.focus(); amtIn.select(); } else foodIn.focus(); } // the time is settled: back to the line, never a log
  else if (event.key === "Escape") { setWhen(null); S.msg = ""; renderComposer(); renderLedger(); foodIn.focus(); }
});
whenIn.addEventListener("blur", applyWhen);
for (const type of ["focus", "blur"]) whenIn.addEventListener(type, () => setTimeout(renderStrip, 0)); // the line under the input speaks for this field too
whenIn.addEventListener("input", () => { if (S.whenBad) { S.whenBad = false; S.msg = ""; renderComposer(); } });
$("nowbtn").addEventListener("mousedown", () => { setWhen(null); S.msg = ""; renderComposer(); renderLedger(); });
$("log").addEventListener("click", (event) => { if (event.detail <= 1 && S.chosen) commit(); });
grid.addEventListener("click", (event) => {
  if (event.target.closest(".lg")) return setFigures(!S.figures);
  const node = event.target.closest(".food");
  if (!node) return;
  const food = S.list[+node.dataset.i], use = MF.usual(food);
  if (event.target.closest(".plus")) logItems([{ food, quantity: use.quantity, unit: use.unit }], "list"); // each click is one more: the list does not move under it
  else if (event.detail <= 1) choose(food, null, null, "list");
});
document.querySelector(".listhead").addEventListener("click", (event) => {
  const tab = event.target.closest(".tab");
  if (tab) { S.tab = tab.dataset.tab; T("act", { act: "tab", what: S.tab }); foodIn.value = ""; setQuery(""); foodIn.focus(); }
  else if (event.target.id === "only") { // the same views, of recipes only; what is typed stays and is searched among them
    Object.assign(S, { only: !S.only, kept: null, keptNow: null });
    T("act", { act: "only", what: S.only ? "recipes" : "all" });
    if (S.chosen) { compute(); renderGrid(); renderComposer(); } else { setQuery(foodIn.value); foodIn.focus(); } // a food settled in the line stays there
  }
  else if (event.target.id === "newrec" && !S.recipe) {
    Object.assign(S, { recipe: { name: "", servings: "1", items: [] }, cursor: null, edit: null });
    setWhen(null); clearComposer(); renderGrid(); renderDay(); // a recipe has no time
    $("rname").focus();
  }
});
$("strip").addEventListener("click", (event) => { // deleting a recipe is asked twice, in place: the second button is not where the first was
  const id = event.target.id, name = S.confirm;
  if (id === "delrec" && S.chosen) { S.confirm = S.chosen.name; renderComposer(); }
  else if (id === "delno") { S.confirm = null; renderComposer(); }
  else if (id === "delyes" && name && event.detail <= 1) {
    ask("recipe-delete", { name }, `recipe ${name}`);
    clearComposer();
  }
});

// ================================================================ events: day
function enterLedger() {
  const all = rows();
  if (!all.length) return;
  S.cursor = (all.find((row) => row.key === S.lastKey) || all[all.length - 1]).key;
  renderLedger();
  ledger.focus();
  ledger.querySelector(".cur")?.scrollIntoView({ block: "nearest" });
}
ledger.addEventListener("mouseover", (event) => { const key = event.target.closest(".row")?.dataset.key || null; if (key !== S.peekRow) { S.peekRow = key; renderStrip(); } });
ledger.addEventListener("mouseleave", () => { if (S.peekRow) { S.peekRow = null; renderStrip(); } });
document.querySelector(".cols").addEventListener("click", (event) => { if (event.target.closest(".lg")) setFigures(!S.figures); });
ledger.addEventListener("click", (event) => { byHand = true; try { ledgerClick(event); } finally { byHand = false; } });
function ledgerClick(event) {
  const node = event.target.closest(".row");
  if (!node || event.target.tagName === "INPUT") return;
  if (ledger.classList.contains("still") && event.target.closest(".a")) return; // it came under the pointer by itself: not aimed at
  const row = rows().find((r) => r.key === node.dataset.key), button = event.target.closest("button");
  if (!row) return;
  if (button) {
    if (button.classList.contains("plus")) return again(row); // each click is one more: the row stays under the pointer
    if (event.detail > 1) return; // a double-click must not also hit the row that slid under the pointer
    if (button.dataset.shift) { if (endEdit(true, +button.dataset.shift)) ledger.focus(); return; }
    if ("cancel" in button.dataset) return cancel(row.op.job);
    if ("retry" in button.dataset) return retry(row.op.job);
    if ("dismiss" in button.dataset) return dismiss(row.op.job);
    return removeRow(row);
  }
  if (event.target.closest(".q, .u") && startEdit(row.key, "q")) return;
  if (event.target.closest(".t") && startEdit(row.key, "t")) return;
  if (event.detail <= 1) take(row); // the cursor in the day is the keyboard's (↑ from an empty line): a click never leaves an entry armed for Enter
}
ledger.addEventListener("input", (event) => {
  if (!S.edit || event.target.id !== "edin") return;
  S.edit.value = event.target.value;
  if (S.edit.error) { S.edit.error = null; renderLedger(); }
  renderStrip();
});
ledger.addEventListener("focusout", (event) => {
  if (event.target.id === "edin" && S.edit) setTimeout(() => { if (S.edit && document.activeElement?.id !== "edin") endEdit(false); }, 0);
});
for (const type of ["focusin", "focusout"]) ledger.addEventListener(type, () => setTimeout(renderStrip, 0)); // the line under the input follows the cursor into the day and back
ledger.addEventListener("keydown", (event) => {
  const key = event.key, all = rows(), at = all.findIndex((row) => row.key === S.cursor);
  if (S.edit) {
    if (key === "Enter") { event.preventDefault(); if (!event.repeat && endEdit(true)) { S.cursor = null; renderLedger(); foodIn.focus(); } } // saved, and back to the line: a second Enter finds nothing armed
    else if (key === "Escape") { event.stopPropagation(); endEdit(false); ledger.focus(); }
    else if (key === "Tab" && !S.recipe) { event.preventDefault(); const { key: rowKey, field } = S.edit; if (endEdit(true) && !startEdit(rowKey, field === "q" ? "t" : "q")) ledger.focus(); }
    else if (key === "Tab") { event.preventDefault(); if (endEdit(true)) ledger.focus(); }
    else if (key === "ArrowUp" || key === "ArrowDown") { event.preventDefault(); stepEdit(key === "ArrowUp" ? 1 : -1, event.shiftKey); }
    else if ((key === "PageUp" || key === "PageDown") && S.edit.field === "t" && !S.recipe) { // the keys that change the day shown: here they send the entry there
      event.preventDefault(); event.stopPropagation(); if (!event.repeat && endEdit(true, key === "PageUp" ? -1 : 1)) ledger.focus(); }
    return;
  }
  if (event.ctrlKey || event.metaKey || event.altKey) return;
  if (key === "ArrowUp" || key === "ArrowDown") {
    event.preventDefault();
    const next = all[Math.max(0, Math.min(all.length - 1, at + (key === "ArrowUp" ? -1 : 1)))];
    if (next) { S.cursor = next.key; renderLedger(); ledger.querySelector(".cur")?.scrollIntoView({ block: "nearest" }); }
  } else if (key === "Enter" && at >= 0) { event.preventDefault(); if (!event.repeat) again(all[at]); } // Enter logs, here as everywhere: this entry, once more
  else if (key === "Tab" && at >= 0 && !event.shiftKey) { event.preventDefault(); take(all[at]); }
  else if (/^[\d.,]$/.test(key) && at >= 0) { event.preventDefault(); event.stopPropagation(); startEdit(S.cursor, "q", key); } // a number typed on an entry is its new amount
  else if (key === "@" && at >= 0) { event.preventDefault(); startEdit(S.cursor, "t"); }
  else if (key === "Delete" && at >= 0) { // not Backspace, which belongs to text: a stray one must not remove an entry
    event.preventDefault();
    const row = all[at], next = all[at + 1] || all[at - 1];
    if (row.pending || row.state === "failed" || event.repeat) return;
    S.cursor = next ? next.key : null;
    removeRow(row);
    if (!next) foodIn.focus();
  } else if (key === "Escape") { S.cursor = null; renderLedger(); foodIn.focus(); }
});

$("dayhead").addEventListener("click", (event) => {
  const button = event.target.closest("button"), id = button?.id;
  if (id === "prev") go(addDays(S.day, -1));
  else if (id === "next") go(addDays(S.day, 1));
  else if (id === "today") go(today());
  else if (id === "date") {
    const pick = $("datepick"), box = button.getBoundingClientRect();
    Object.assign(pick.style, { left: `${box.left}px`, top: `${box.bottom}px` });
    pick.value = S.day;
    try { pick.showPicker(); } catch { /* no picker here: the arrows remain */ }
  } else if (id === "rdiscard") {
    if (event.detail > 1) return;
    if (S.recipe.items.length && button.textContent !== "Sure?") { // asked once more, in the same place; leaving the button takes the question back
      button.textContent = "Sure?";
      button.title = `Discard the ${S.recipe.items.length} ingredient${S.recipe.items.length === 1 ? "" : "s"} of this draft`;
      button.addEventListener("mouseleave", () => { button.textContent = "Discard"; button.title = ""; }, { once: true });
      return;
    }
    S.recipe = null; renderGrid(); renderComposer(); renderDay(); foodIn.focus();
  }
  else if (id === "rsave") {
    const recipe = S.recipe, servings = MF.number(recipe.servings);
    if (event.detail > 1) return;
    const missing = !recipe.name.trim() ? "Give the recipe a name" : !(servings > 0) ? "Say how many servings it makes" : !recipe.items.length ? "Add at least one ingredient" : "";
    $("rname").classList.toggle("bad", !recipe.name.trim()); $("rserv").classList.toggle("bad", !(servings > 0)); // the field it is about
    if (missing) { recipe.error = missing; return renderSums(); }
    S.recipe = null; // from here on it is a job like any other: if it fails, the phone line says so and offers to ask again
    ask("recipe", { name: recipe.name.trim(), items: recipe.items.map(({ name, quantity, unit }) => ({ name, quantity, unit })), servings }, `recipe ${recipe.name.trim()}`);
    renderGrid(); renderComposer(); renderDay(); foodIn.focus();
  }
});
$("dayhead").addEventListener("input", (event) => {
  if (!S.recipe) return;
  S.recipe.error = null;
  event.target.classList.remove("bad");
  if (event.target.id === "rname") S.recipe.name = event.target.value;
  if (event.target.id === "rserv") S.recipe.servings = event.target.value;
  renderSums();
});
const NO_FOCUS = "#nowbtn, #log, #grid .plus, .lg, #strip button, #ledger .a button, #dayhead button, #phone button, #syncmenu button"; // pressed, these leave the typing focus where it is
document.addEventListener("mousedown", (event) => { if (event.target.closest(NO_FOCUS)) event.preventDefault(); });
$("datepick").addEventListener("change", (event) => { if (event.target.value) go(event.target.value); });
$("phone").addEventListener("click", (event) => { phoneHand = true; try { phoneClick(event); } finally { phoneHand = false; } });
function phoneClick(event) {
  const button = event.target.closest("button");
  const slot = button?.parentNode.className.split(" ")[0];
  if (!button || event.detail > 1 || $("phone").classList.contains(`still-${slot}`)) return;
  if (button.dataset.cancel) cancel(S.jobs.find((job) => job.key === button.dataset.cancel));
  else if (button.id === "undo") undo();
  else if (button.id === "retry") retry();
  else if (button.id === "check") check();
  else if (button.id === "dismiss") dismiss();
  else if (button.id === "sync") sync("day");
}
/** The three readings of the phone, from the quick one to the one MacroFactor rations. The server words them the same. */
function sync(what) {
  if (what === "day") ask("sync", { day: S.day }, `${dshort(S.day)} with the phone's screen`);
  else ask("sync", { what }, what === "recipes" ? "recipes with the phone's list" : "the last 7 days from an export");
}
$("syncmenu").addEventListener("click", (event) => {
  const item = event.target.closest("button");
  if (!item || item.disabled) return;
  $("syncmenu").hidePopover();
  sync(item.dataset.sync);
});

// ================================================================ global keys, clock, start
document.addEventListener("keydown", (event) => {
  const key = event.key, field = ["INPUT", "SELECT"].includes(document.activeElement?.tagName);
  if (key === "PageUp") { event.preventDefault(); if (!event.repeat) go(addDays(S.day, -1)); } // one day a press: a key held after it moved an entry must not run through the days
  else if (key === "PageDown") { event.preventDefault(); if (!event.repeat) go(addDays(S.day, 1)); }
  else if ((event.ctrlKey || event.metaKey) && key.toLowerCase() === "z" && !(field && document.activeElement.value)) { event.preventDefault(); if (!event.repeat) undo(); }
  else if (!field && key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) foodIn.focus(); // letters always mean "food"
});
let resizing;
addEventListener("resize", () => { clearTimeout(resizing); resizing = setTimeout(() => { measure(); S.kept = S.keptNow = null; compute(); renderGrid(); renderComposer(); if (S.day === today()) ledger.scrollTop = ledger.scrollHeight; }, 60); });
/* A control that came under a resting pointer (the phone finished something, a row left) is not armed until the
   pointer really moves: a click always goes to what was there when you aimed. */
document.addEventListener("pointermove", (event) => {
  if (pointer && pointer[0] === event.clientX && pointer[1] === event.clientY) return; // a browser may re-send the same position after a layout change
  if (pointer) travel += Math.hypot(event.clientX - pointer[0], event.clientY - pointer[1]);
  pointer = [event.clientX, event.clientY];
  ledger.classList.remove("still"); $("phone").classList.remove(...SLOTS.map((slot) => `still-${slot}`));
});
addEventListener("focus", () => { T("focus"); tick(); follow(); });
/* While you are away the list at rest is brought up to date (what you logged is now among the latest), so that it
   never has to change under your eyes. */
const resting = () => !S.query.trim() && !S.chosen;
function freshen() { if (S.keptNow && resting()) { S.keptNow = null; compute(); renderGrid(); renderComposer(); } }
addEventListener("blur", () => { T("blur"); freshen(); });

/** Once in a while: keep "now · 14:32" honest, and follow the calendar past midnight. */
function tick() {
  const now = today();
  if (now !== seenToday) {
    const busy = S.recipe || S.chosen || S.query || S.edit; // in the middle of something: follow as soon as it is done
    if (S.day !== seenToday) seenToday = now; // you were looking at another day anyway
    else if (!busy) { seenToday = now; return go(now); }
    renderDayHead();
  }
  if (S.keptNow && S.keptNow.hour !== hourNow() && !grid.matches(":hover")) freshen(); // another hour, other picks; not under the pointer
  renderComposer();
  if (S.day !== now) renderGrid(); // its tooltips say the time a + would log at
}

async function start() {
  measure(); renderGrid(); renderComposer(); renderDay(); renderPhone();
  document.querySelector(".cols .lg").outerHTML = HEADS();
  T("load", { w: innerWidth, h: innerHeight, rows: S.rows, cols: S.cols });
  foodIn.focus();
  follow(); // the first reading brings the day, and tells that the catalog is to be fetched
  setInterval(tick, 20000);
}
document.addEventListener("visibilitychange", follow);
start();
