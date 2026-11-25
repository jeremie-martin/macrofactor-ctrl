# Design of the page

What the page is built on, where each idea came from, and how a change to it is made. The README
says what the page does; this is meant to help decide what it should do.

**How to read it.** These are notes from building one tool for one person over a few days, not
laws of interface design. Each point says what it rests on, so it can be weighed:

- *measured*: counted in the food log or the trace;
- *seen once*: one incident or one observation, which may not generalise;
- *chosen*: a preference the user stated, or a decision made together;
- *judgment*: a view of mine that nothing here has tested.

When a point here and the evidence disagree, the evidence wins and the point should be rewritten.
Where the page does not follow a point, the exception is listed rather than hidden.

## What it is

A tool one person uses many times a day (about 22 entries in about 15 sittings, *measured*) to put a
food and an amount in the log. The user asked for a tool, not a dashboard and not a product
(*chosen*). It is judged by how little it asks for the common act and by whether it can be trusted.

## Method

1. **Look at the use before designing for it.** Over 60 days of the log (*measured*): 48 % of
   entries are a food already eaten that day, 25 % the same food and amount, 12 % the same food
   again within the same minute, 56 % an amount different from the last one. This is why the day
   became a place to log from, and why `x3` exists. It describes this user in this period; it
   should be counted again before it is leaned on for a new decision.
2. **Read the trace after a change has been used.** `mf trace` records keys, clicks and pointer
   travel with what they meant and what the phone did. So far it has been read once, for one
   afternoon (*seen once*): a log that took 28 s and 5 clicks included selecting and deleting the
   food field's text by hand, which suggested the field should select itself. That is my reading of
   the keys, not something the user said. A second reading (*measured*, on three typed logs) found
   two logged by an Enter meant to pick the food, then cancelled and redone; the user confirmed that
   reading, and Enter in the food field no longer logs. A trace shows what was done; what was
   intended has to be asked.
3. **For questions of feel, try the candidates on the real page.** Row height, typeface and the way
   protein, fat and carbs are drawn were built as switches, used, chosen by the user, and the
   others removed (*seen once*; it worked well that time). It costs more than deciding, so it is
   for choices that cannot be argued.
4. **Read every handler, asking what each key and click does that was not aimed at.** After the
   Enter slip, one reading of all the page's event handlers found nine more of the same family
   (*seen once*): Enter in the time field logged; a second Enter after saving an entry's edit logged
   it again; Backspace on an entry removed it; Esc dropped the sitting's time; a click on an entry
   left it armed for Enter while the line held something else; and others. None had been noticed in
   use yet. They are listed in the commit that fixed them.
5. **Look at the result enlarged.** Page changes are tried in `tools/sandbox.py`, driven with
   `tools/drive.mjs`, and inspected on captures at twice the size, light and dark, at two window
   sizes. Several misalignments were found this way and not otherwise (*seen once*). The captures
   come from headless Chromium on this machine; the user's own browser may draw things differently.
6. **Do not assume the environment.** The generic `system-ui` font resolved to Libertinus Sans in
   that Chromium, and names read much better once faces were named (*seen once*). Whether the
   user's browser had the same font was not checked; the font and the number of figures on screen
   both changed at about the same time, so how much each helped is not known.

## Interaction

- **Enter confirms the field you are in** (*chosen*, after the slips above). The line is two fields:
  in the food field Enter settles the food and moves to its amount; in the amount it logs. Nothing is
  logged from the food field, so an amount is only ever logged from the field that shows it. The
  first design had Enter log the usual amount straight from a typed name, on the measured ground
  that it was the shortest path; in use, the name-then-amount habit made it a trap. The shortest
  path is not the best one when a slip costs a cancel and a redo.
- **One grammar for rows, in the list and in the day** (*chosen*, from the 48 % above). A row is a
  food and an amount. Its `+` logs it as shown, and so does Enter on an entry of the day; Tab or a
  click on its name takes it to the line; its amount is what you change (type over it, step it with
  the arrows, repeat it with `x3`). Before adding a control, ask whether these already cover the need.
- **Prefer that a gesture mean one thing.** The motivating case: a click on an amount used to log in
  the list and edit in the day. Known differences today: Enter logs an entry of the day directly but only settles a food lit
  in the list (the list's lit row is the completion of what is being typed; the day has a cursor of
  its own); inside an entry's open field Enter saves that field and returns to the line; in the
  time field and the unit list it settles them and returns to the amount;
  the arrows move through a list but step an amount; a click on an amount edits in the day and takes the row to the line in the list.
  Each seemed the less surprising choice (*judgment*); none has been tested with the trace.
- **The line under the input says what Enter will do**, for the line, the time field and an entry
  of the day. It does not speak for the unit list.
- **Only the amount's Enter, a `+`, and Enter on an entry log.** Every other Enter settles
  something and moves on (*chosen*, generalising the Enter slip). The cursor in the day is the
  keyboard's (reached with ↑ from an empty line); a click never leaves an entry armed.
- **Keys that destroy are not keys used for text.** Del removes an entry; Backspace does not.
  Esc clears what is being typed and nothing that lasts longer (the sitting's time stays).
- **The common act should stay cheap**: a few letters, Enter, the amount, Enter; or letters, the
  amount, Enter (*chosen*). It costs one Enter more than it once did for the usual amount, which `+`
  does in one click.
- **Nothing should move unless you moved it** (*chosen*, from the first version's faults). Slots
  are fixed and rows have one height; the phone finishing a job changes ink, not geometry; a
  control that arrives under a resting pointer is unarmed until the pointer moves. The list at
  rest is rebuilt at moments when you are not aiming at it (after your own search, when the window
  is out of focus, on load or resize). Known limits: a `+` in the day moves from under the pointer
  when a set time places the new entry above it (it is then unarmed). Setting a time rebuilds
  the hour's picks, once the pointer is off the list. A time typed and not yet settled is the time of
  a `+` pressed then (*seen once*: it was ignored, and the entry had to be moved).
- **Sync is three readings, each named where it is asked** (*chosen*). The button reads the day shown from the
  phone's screen, the one asked several times a day (*measured*: 18 in five days). The menu beside it names that
  reading and two more, each with what it reads and what it costs: the recipes (once read with every Sync:
  11 of its 29 s, and nothing new in 18 of 18), and an export of the last seven days, which MacroFactor rations.
  Longer exports and backups stay in the terminal.
- **What was asked can usually be taken back**, and the page says what happened: nothing was
  changed, it was saved and taken back, or it was too late. Deleting a recipe cannot be undone.
- **Typing goes to the food field** from anywhere that is not itself a field. A food field holding
  a chosen food is selected when you return to it.

## Display

- **A row has the same core in the list and in the day** (*chosen*): name, weight, count, kcal, a
  bar of where the energy comes from, `+`. The day adds a time, a state mark and `×`; the index
  adds a letter in the margin.
- **Fewer figures per row, and the figures one click away** (*chosen*: the user found three grey figures a row
  too many and preferred the bar, then asked for a way to see the values). Protein, fat and carbs are one bar a
  row, showing proportions, not quantities; a click on their column heads (`P F C`, in the list or the day) turns
  every bar into the three figures in grams, under those same heads and in the columns the day's sums already
  use, and back. A zero is faint, so a row reads by what it has. The choice is kept by the browser. An earlier
  attempt at figures failed on legibility: they were grey, small and unaligned; these are ink, the row's size,
  and in columns.
- **One number down a column** (*chosen*, after weightless foods sat in a column of their own and looked
  misplaced): every amount is a number, right-aligned, then what it counts (`137 g`, `1 srv`, `16 fl oz`). The
  number is the weight whenever the food has one, followed by how it was counted (`380 g  1 srv`); a food
  without a weight shows its count in that same column. A recipe's serving shown as a weight may mean less than
  "1 serving" did; this has not been asked.
- **One set of columns at every window size** (*judgment*): the day's and the list's amount, kcal and P F C
  columns have the same widths, so a row is the same row on either side.
- **Colour for meaning** (*judgment*, kept from the first version): blue where Enter acts, orange
  for "not now, or not today", red for wrong; MacroFactor's three colours for protein, fat and
  carbs, as marks rather than text. Protein's orange-red is close to the "not now" orange; nobody
  has reported confusing them.
- **Few shapes** (*judgment*): two weights, one row height, one text button, one `+`. A reason to
  add a shape is a reason; this only asks that it be stated.
- **Compact rows and Inter** (*chosen* among the candidates tried).

## Truth

- **The phone holds the truth; the page shows a copy.** MacroFactor's export rounds every entry to
  whole numbers (*measured*: 1371 against 1375 kcal on one day), so the day's eaten and target
  figures are taken from the phone's screen whenever mf has looked at that day, and are the sum of
  the entries otherwise. Rows still show the rounded figures and may not add up to the total.
- **When the copy is known to differ** by more than rounding, the page says so until a sync. It
  cannot know about changes on the phone it has not looked at.
- **A write counts as done when the phone has confirmed it and the local result is committed.** Before that it
  is pending. A saving tap followed by failed verification is **uncertain**, not “nothing saved” (*measured*:
  three identical entries were on the phone on November 16, but the third job failed verification). Intent is persisted
  before that tap. Uncertainty survives a restart, never offers Retry, and never invents an extra food row.
  A sync of one of its days reads and reconciles all the days it touched (the page's Check asks exactly that); it
  does not infer which entry a failed attempt created.
  Dismiss only acknowledges the message. The day remains unconfirmed, and Eaten/Left stay blank until the
  phone is reconciled; an empty local list does not claim nothing was logged. This also applies to a partially
  completed move or Undo. While a save is being verified, the same slots say “confirming”.
- **A page action belongs to the entry and day under the pointer when it was asked.** In a sandbox sequence,
  a removal freed its SQLite row ID. A new log reused it, and a queued edit then changed the new serving
  (*measured*). The store now allocates each ID once. The page, queue and retry all carry the source day,
  so an action aimed at yesterday cannot follow a move into today. A verified move preserves the ID across
  both phone steps. Restoring a removed entry creates a new ID. Undo resolves all its targets before acting.
  Screen and export sync retain IDs where the observed servings, times and counts support a match.
  MacroFactor supplies observations of food rows and their order, without persistent row identifiers.
- **What was asked is done once, and soon or not at all** (*seen once*: a log asked at 18:50 waited
  75 minutes in the browser behind tabs holding every connection, and went to the phone at 20:05).
  The page's name for a request is the job's key, kept in the database; the server refuses a request
  older than 30 s; the page stops waiting for one unanswered after 20 s, and says the server may
  still have taken it.
- **The server holds what lasts; the page draws a reading of it** (*chosen*, after a day in which the
  page's own idea of what was waiting went wrong three ways). The page used to keep its own list of
  operations and reconcile it with the server's jobs; failures and Undo lived in a tab and were lost
  on reload; Undo meant "whatever was last". Now waiting rows, failures and what Undo undoes are
  all the server's, and the page keeps only what you type, the cursor, and requests not yet
  acknowledged. An independent review of the code proposed this; it is the ordinary design for
  this kind of page, and should have been the first one.
- **A page and its server are one build.** The server serves the page it was started with and
  refuses another's requests (*seen once*: files edited under a running server gave a new page
  against an old server, and two entries drawn twice). The build includes the Python sources. When disk sources
  differ, the page asks for a server restart, then a reload: reloading an old running server cannot update it.

## Safety

- **Tests are kept away from the phone, the data and the trace**: they run in temporary folders, a
  guard replaces the `Phone` the service would build, and `create_app` accepts its worker (which takes
  the service) so a test need not build the real ones. This followed a test of mine that opened a real
  entry on the phone (*seen once*; nothing was changed). The guard covers the paths that exist; a
  test that called adb directly would not be stopped by it.
- **Page work is done in the sandbox** (a copy of the data, a pretend phone; the server, service,
  journal and undo are the real ones). A saved browser scenario uses synthetic data and runs with pytest when
  Node and Chromium are available. It checks the real handlers and reload/recovery flow. It does not show how
  the real phone behaves. Changes to how the phone is driven are validated separately
  (`tools/validate_cancel.py`).

- **Exports are a scarce resource, and mf treats them as one** (*seen once*: eight exports between
  17:54 and 21:56, six of them in seventy minutes of validating, and MacroFactor refused the ninth
  "for 24 hours"). Every export is counted; beyond six in 24 hours mf refuses before touching the
  phone, and says when the next is free; MacroFactor's own refusal is recognised and remembered for a
  day. Six is a guess below the one number observed: the real limit, and whether it is per day or
  per hour, is not known. So the ordinary sync spends none: it reads the day from the phone's screen
  and reconciles the local day with it, using everything the screen tells (figures, changed
  amounts, the minutes each hour lists, and beside which row each is drawn) and nothing it does not: a minute that could be one of two
  stays unknown. One rule says whether a day adds up to the phone's total (`set_phone_totals`), and
  a sync that would leave the day not adding up is rolled back whole. Exports are left for many
  days at once and for backups.

## Making a change: questions worth asking

1. What do the log and the trace say about how often this happens and what it costs now?
2. Do the existing gestures and the existing row already cover it?
3. Has it been tried in the sandbox and looked at enlarged, light and dark, wide and narrow?
4. Is the logic tested, and does the README say what it now does?
5. After some days of use, what does the trace show?

## Open

- Ctrl+Z in an empty field undoes the last change on the phone; in a field with text it undoes
  typing. One key, two reaches; left as it is, not yet seen to cause harm.
- Undo was first "the last job, once": pressed a second time while the first was on its way it cancelled that
  undo, and after a cancelled job nothing could be undone (seen in use on 2026-11-18, two time changes then
  Undo twice). It is now a history, as in an editor. There is no Redo: a change taken back is asked again.
- Moving an entry is rare (3 of 82 jobs so far, one of them to another day), so it has no gesture of its own:
  it is the time field, with the neighbouring days on PageUp / PageDown. Dragging rows was considered and left
  out: each move is one operation of 15 to 50 s on the phone, asked with Enter like every other write.
- An entry's open field is abandoned, not saved, when you click elsewhere: a write to the phone is
  only ever asked with Enter or Tab. The typed value is lost without a word.
- A `+` on an entry of a past day logs it again on that day (in orange), not today. "The same as
  yesterday, today" has no gesture.
- A unit letter typed after a number keeps the number (`150` then `s` is 150 servings); the line
  says so, nothing stops it.

- `x3` exists in the line only; on an entry, repeating is `+` or Enter again.
- An amount typed first (`0.5 bol`) is not understood.
- Changing a recipe's ingredients is not automated.
- Times are one size smaller than names; their baselines differ slightly.
- Most points above rest on one user and a few days. The trace is there so they can be checked.
