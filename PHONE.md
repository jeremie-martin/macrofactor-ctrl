# Phone observations and driver contract

Observed on MacroFactor 5.8.11, on the connected Android phone (720 × 1600), on 2026-11-17. This records what was established during exploration,
including limits of the current implementation; it is not a claim that every MacroFactor screen is covered.
Personal screenshots, XML and before/after comparisons are kept under `~/.local/state/mf/investigations/`, not
in the repository. The exploration used temporary drafts, disabled the driver's saving callback, discarded its
own drafts, and compared the saved day and totals before and after. It used no exports.

## What the transport tells us

`Phone` sends input through ADB and reads the accessibility hierarchy through uiautomator2. A successful ADB
command says that the input was sent, not that MacroFactor accepted the intended action. A hierarchy is a
description of UI widgets, not an application data API. It can include covered panels, omit off-screen rows,
and expose several nodes for one visual object. A screenshot is useful independent evidence, but some
inconsistencies exist in the visible application itself.

Two identical frames help avoid using moving coordinates. They do not establish that an action succeeded:
an unchanged wrong screen is stable too. Every transition needs its own observed destination condition.

## Reading across midnight

At local midnight the Food Log can keep the title `Today` while its rows, totals and scroll position switch
to the new day. Matching titles or even matching hour chips cannot establish that both screens belong to
the same date. During collection, every settled frame must still name the requested day and the host date must
remain stable. Whole-day reads also compare the displayed eaten totals across frames. A positive date or totals
change discards the partial result and starts one fresh read of the original date. The progress trace names the
reason. A draft refusal, cancellation, stall or unexplained loss of overlap propagates to the caller.

Before a positional write the service resolves the stored entry against the observed hour. The driver uses
that same reading as its before-state and checks the row's displayed name and serving again at selection.
An interrupted write follows the ordinary save-boundary rules. Automatic restart applies only to whole-day reads.

## Search results

Observed with `tools/probe.py` and by scrolling the list on 2026-11-18.

| Observation | Consequence for the driver |
| --- | --- |
| The search matches fragments of words (`Brass` finds `Brassé`, `teau` finds `Gâteau`) but not a plain letter for an accented one, and a comma in the query hides every history result. | The query is every word of the name, each cut to its longest ASCII run. The food then comes first, or close. |
| An hour of the Food Log lists its distinct times (`21:00`, `21:26`, `21:28`) in one label, and its rows as siblings with nothing between them: no reader is told which row has which time. On the screen each time is drawn in the left margin, beside the top of the first row logged at it (2026-11-25: four rows, three times). | A whole-day reading takes a picture of each frame showing such an hour and looks for ink in the margin beside each row (`screens.stamp_beside`). Each row has the time above it only when the rows so marked are as many as the times; otherwise the minute stays unknown. |
| Text sent as key presses (`adb shell input text`) goes through the phone's keyboard, which corrects it: `Fleurs en Fleurettes` reached the search field as `Fleur's en Fleurettes` (2026-11-21; the food was still listed, after a 13 s wait), `borwn` as `brown`. Text set on the field through the accessibility service arrives as given. More than one node can be `focused` at once (the window and the field); set on "the focused node", a query once became `g`. | Words are set on the focused `EditText` (`Phone.fill`); only figures are sent as key presses. What a field holds is read back before its results are believed. |
| History results list five rows, then a `See N More` link at the right end of the section header; the rest of the header does nothing. Expanded (`See Less`), all history rows are listed, followed by `Common` and `Branded`; headers scroll away with their rows. | Expand, then scan downwards until the food or the next section. A row with no header above it continues the section the list is cut in. |
| The search field and `Log Foods` float over the end of the list: above the keyboard when it is up, at the bottom otherwise. A tap on a row just above the bar (16 px was seen) is taken by it and only closes the keyboard. While the list is scrolled down the bar is absent from the tree. | A row is tapped only when it is whole between the top of the list and whatever covers its end: the bar less a margin, the keyboard, Android's navigation bar. |
| After the keyboard has gone without a tap of ours (closing the time picker does it), settled frames can still give the search field its place above the keyboard, over the fifth row; a tap there opened that food (caught before saving, 2026-11-18). In 2,000 recorded frames the field's bottom is 21 px above the keyboard or 41 px above the navigation bar, or the frame is one of these. | The field counts as the search field only where the screen can have it; otherwise the driver waits. |
| A row cut by the top edge keeps its full label in a node a few pixels high. | It is a food, not a section header; it is not tapped. |
| A food logged long ago (one from December 2025 was tried) may be in no history result at all. | That is reported as not found; nothing else is opened in its place. |
| Opening a food puts it on the plate provisionally, at its last amount; leaving its details with the back arrow removes it. | The plate is only read with the details closed. |

A food used only inside recipes is not in "From History": the search lists it under "Common" or "Branded", like a
food never used. The same name can stand in two sections ("Olive Oil" in history and in Common, "Tomato Paste" in
Common and in Branded) with other servings. A fourth section, "Custom", was seen between history and Common
(query `a`: two of the user's own foods). A food named exactly is therefore taken from the first section that
lists it, in the list's own order: history (expanded first), Custom, Common, Branded.

**Names are not unique.** On 2026-11-19 the Library held 268 recipes under 246 names: seventeen names had two to
four recipes each ("Soup" four). Each such recipe that was logged has its own history row, of the same name, with
its own serving and figures; opening the first one named "Roasted Chickpeas" staged 967 kcal where the other is
266. Rows of one name in one section are therefore never picked among: the write is refused before anything is
staged, saying what each row shows. A recipe is not deleted when its name is shared, nor created under a name in
use. The recipe list keeps every recipe of a name; only a name listed once says what a serving is.

The logger's Library tab, searched for such a name, lists its recipes one under the other, each as "1 serving" with
its weight if it has one and its own figures ("Soup": 201 kcal 687.5 g, 232 kcal 525 g, 315 kcal, 365 kcal 500 g),
and each opens as one serving of exactly that. So one of them is asked for by that serving: the row that shows it
is tapped, and the details, once the amount is set, must show that amount of it or nothing is added.

Two rows of one name are not always two recipes. History held "Galettes 4 Céréales By Carrefour Bio" twice, at
"7.67 g" and at "3 portion (23.01 g)": the same energy to the gram, yet two foods, the first offering only g, oz,
serving and lb. Which stands first changes with use. Rows alike to the gram are not refused; the one whose last
amount is in the unit asked for is taken (the first refusal rule had stopped a log of galettes asked from the page).

Read frame after frame, the list's frames overlap by one to several rows, and the rows at the top of a frame have
lost their header: they are placed by the overlap with the frame before (`continue_list`), not by their figures
(the first attempt labelled a Custom row "Common"). Some foods are not listed by any text (a "Pulpe Fine De Tomates" that is an ingredient of an existing
recipe: probably scanned). The recipe wizard's ingredient picker is the same list as the logger's.

## Creating a recipe

| Observation | Consequence for the driver |
| --- | --- |
| Opening a food's details in the ingredient picker already lists it among the recipe's ingredients, at its default serving; Add confirms the amount set. | The staged rows are not counted while a food is open. |
| The recipe form stays in the tree behind the picker and behind a food's details, and scrolls. With three ingredients its "servings" field sat at the height of the details' title bar, and the title was read as "1": every recipe of four ingredients or more failed with "details did not settle". | The title is a row that is not a text field. |
| With many ingredients the form's three fields scroll away; the "Ingredients" header stays, with "Weight of ingredients is 3674.1 g" under it. Only eight or so ingredients are on screen. | The form is known by that header. The ingredients are not counted on screen: the weight shown must equal the sum of what each ingredient weighed when added (the details show what one unit weighs to 0.1 g, which bounds the slack). |
| The last step shows the recipe as a food row with a pencil: name, kcal and P/F/C a serving. | Its kcal must equal the ingredients' sum divided by the servings, within the rounding of each. Then only is Create tapped. |
| A database food's units are whatever its author entered: "cube" of a Maggi stock cube weighs 2000 g (2 cubes: 10840 kcal); a cream has no ml; "cup, cubes" in a search row is the chip "cup". | Nothing is assumed about a unit. A unit the food lacks is refused with all it offers; `mf recipe try` shows each ingredient's energy at its amount, for whoever asks to read. |
| The same food can be added twice (olive oil in ml and in g) and stays two ingredients. | None. |
| Just launched, the Food Log is drawn before its row of shortcuts. | The Library waits for them. |

## Logger and plate

| Observation | Consequence for the driver |
| --- | --- |
| The logger contains the plate, a search sheet, and food details. Covered panels can remain in the tree. | Recognize the active panel before using its controls or interpreting its rows. |
| Tapping the header exposes the plate. Tapping it again does not reopen search; the Search tab does. | Model these as different transitions, not a toggle. |
| A normal food can have one header image; a recipe can have several small ingredient images. The header shows only recent thumbnails plus an overflow number. | Image-node count is not food count. The image-counting implementation has been removed. |
| The expanded plate has food summaries and quantity controls. A long mixed plate omits rows after they scroll off screen; a row can be clipped at either edge. Short plates may omit scroll containers and row wrappers entirely. | Read the semantic food summaries through transparent layout containers. A whole-plate read needs proven start/end and measured overlap, preserving duplicate entries. |
| A plate quantity box can show only the first digit while its row summary and nutrition reflect the full amount. This discrepancy appears in both XML and screenshot. | Verify the food summary against the active details used to stage it, not the passive quantity box. |
| The details show grams beside the selected unit only for foods MacroFactor has a weight for (`serving • 417.9 g`); a recipe without a total weight shows `serving`, a drink `fl oz`. Those grams are per unit, rounded to one decimal. | Not a figure to compute with. |
| A plate row shows name, energy and macros, then a weight in grams when the food has one (`417.86 g`, two decimals) and nothing otherwise. It never shows the selected unit. | Compare what both screens show: name and nutrition always, grams when grams were entered. A serving converted through the details' grams per unit differs from the plate's weight by rounding alone. |
| A Food Log row shows the serving in the selected unit (`1 serving`, `16 fl oz`, `0.33 serving` for 0.333) and never a weight. | A saved row is matched on the quantity to two decimals and the unit. |
| The header pill reads `78 / 2636`, but `34681/2666` without spaces once a figure has five digits. That happens in ordinary use: changing a food from grams to servings leaves the old amount in the field for a moment (113 servings). | The header is recognised with or without the spaces. |
| The quantity field is a clickable node whose label is its content. Holding `1` (any food last logged as 1 serving), it looks like the keypad's `1` key. | The key is the one that is not the field; the unit chips sit between the field and that key. |
| Closing the logger can leave one settled frame with no labelled node at all. | Wait; Back from there leaves the app. |
| The selection bar offers Move, then "To Today", "To Tomorrow", "To Date & Time": a date wheel and an hour wheel with "Move to Date and Time". The entry itself moves, with the figures it was logged with. The app stays on the day it left. | A move to another day uses it; logging again would give the figures of the recipe as it is now. |
| The Move date wheel goes from 29 days before the entry's day to 7 days after it (seen from Nov 21 and from Nov 28), not further. | A longer way is gone in steps, each one verified; the days passed are named in the journal. |
| Moved to the same hour of another day, an entry keeps its minute (13:30 stayed 13:30); moved to another hour it gets :00. | The minute is read after the move (the one minute the hour did not list before) and set only when it is not the one asked. |
| While rows are selected, a group whose hour header has scrolled off no longer lists its times, so the frame alone does not say which hour its rows are in. An hour of seven rows is taller than the screen. | The hour is taken from the frame before, where the same rows were under their header. |
| Days of another year are titled without the year ("Sat, Jan 2", "Wed, Dec 31"), before and after this one. | Titles are compared as dates; the year is the one where the weekday fits, closest to today. |
| Calendar cells say only "31 / Su": the same run of weeks recurs months apart (August 2025 and May 2026 read alike). | Each calendar frame is dated from the one before, not from the day the calendar opened on. |
| The Library lists recipes in one of four orders, chosen from the control at the right of the "Recipes" header: A-Z, Z-A, Created, Modified ("starts with most recently modified"). In Modified order a recipe just created came first and recipes cooked again recently came before older creations. The control scrolls away with the list. Each row shows one serving: `1 serving (380 g)` when the recipe has a total weight, `1 serving` when it has none (140 of 245 on 2026-11-18). | `mf sync recipes` reads the list in Modified order until a screen of rows is all known, and puts the order back (opening the Library again when the control is off screen). About 12 s; the whole list about 70 s. |
| After the time picker closed, the search field was once reported above a keyboard that had gone for good (ten seconds and more), not for a few frames. | Waiting is not a remedy: a log at a given hour opens the logger from that hour's "+", already on the day and hour, and does not use the picker. The picker remains only for an hour whose "+" cannot be shown. |
| The logger's time chip names a day of another year without the year ("12:00 / Jan 2"). Its date wheel went no further than a week after today. | The chip is read as the date closest to today. A far day is logged through that day's own hour `+`, which opens the logger already on it: the wheel is not needed. |
| Beside a food's amount the detail shows its weight ("serving • 417.9 g") or nothing ("serving", "fl oz"). | Recorded at each log: a food shown without a weight has none here either, whatever the export says. |
| Seen once: the totals drawn in the header were absent from what the app exposes, read after read, until another day had been shown. | A whole-day reading that finds no totals shows the day before and comes back, once (written from that one case, not seen again since). |
| Android Back can collapse the quantity keypad while leaving food details open. The detail header has its own back control. | After returning, wait for details to disappear before selecting another plate row; otherwise the old underlying rows are still discoverable. |

In the focused quantity check, three draft entries were set to different gram amounts. Reopening each entry
through its active details returned the requested amount and corresponding nutrition, despite a passive plate
box displaying only its first digit. This establishes an inconsistency between presentations; no foods were
saved in this exploration, so it does not by itself establish what a later save would persist.

The single-recipe failure was reproduced by parsing its original XML: the driver expected one staged item,
counted three image nodes, and timed out before the saving boundary. The plate reader was changed previously
because hidden rows were incomplete. Both implementations inferred complete application state from a partial
presentation; adding an exception for a particular recipe thumbnail would repeat that mistake.

## Verification implemented by the driver

The pre-save check is **the actual staged foods agree with the request**, including multiplicity, amounts and
nutrition. After staging, the driver exposes the plate, establishes its start from the heading above the first
row, and scans through the Nutrition boundary. Only fully visible food summaries are collected. The plate
and Food Log share the measured-overlap algorithm and short drags for repeated rows. A missing boundary,
unknown row, ambiguous overlap or stalled scan refuses the save.

The expected contents come from the active food details: exact name and nutrition for the amount set there, where
the quantity and unit were confirmed in the field itself. Every plate row must agree with one of them on name,
energy and macros; when the amount was entered in grams the row's weight must be those grams. Passive quantity
boxes, thumbnails and converted weights play no part. Matching consumes each expected entry once, preserving
duplicates without depending on row order. A whole count with the wrong food or amount is a mismatch.

Post-save verification also matches the complete new multiset and checks that existing entries remain.
Precise macros are assigned from the matching details, not from the requested order. The driver returns entries
in request order, with their actual phone positions, so the service associates each with the correct requested food.
A subsequent time change
must preserve those contents. For a "now" log, an unknown minute stays unknown rather than being filled with
the clock from before staging, which can be a minute or more before the saving tap.

Keep screen interpretation pure in `screens.py` and navigation in `app.py`. The service's durable save marker,
phone lease and post-save readback remain necessary regardless of how staging is read. A pre-save count check
does not replace checking the saved entries, and a failed post-save check must remain uncertain rather than
retryable. These checks do not add retries or change the saving boundary.

## Evidence and verification

The trace now records each already-read hierarchy, its last input number and the read duration. Identical XML
shares a compressed file within the day. A wait records both the number of reads and the number satisfying its
condition; a timeout after a transient match is distinguished from a condition that never matched. Full input
commands are retained, including complete drag sequences. No additional phone requests are made for this.

Replay observations through the pure readers to diagnose failures. Regression fixtures must remain synthetic:
represent the discovered structure and transitions without copying private captures into tests. The fake service
tests establish job, transaction and recovery behaviour; they cannot validate assumptions about Flutter widgets.
Driver changes still require real-phone validation under the procedure in `AGENTS.md` and `README.md`.
`tools/validate_cancel.py --only plate --recipe "EXACT RECIPE NAME"` exercises fractional recipes, mixed and
long plates, reversal, and cancellation at both sides of plate verification. Reuse the session's backup with
`--baseline` when running the separate cancellation sweep; no additional exports are needed.

Verification on 2026-11-17: fractional recipes, mixed foods/amounts and an eight-entry plate containing a recipe
were saved, read back and undone on an initially empty future day; cancellation was swept over navigation, staging,
plate reading and the saving tap. All of it used one plain food in grams and one recipe with a round serving weight.

Verification on 2026-11-18, with `tools/probe.py` (no save) over 64 foods and servings sampled by kind from the
history: 52 staged and verified. Of the 12 that did not, 8 were the two defects above (header, quantity 1), fixed
and probed again; 4 are foods MacroFactor's search no longer lists in history, reported as not found. Then, saved
for real on an empty future day and removed: a weightless recipe, a recipe at 0.333 serving, a drink in fl oz, half
a cube, an accented branded food, 7.67 g, a fruit by the piece and a custom unit; each edited, one retimed, one
copied and undone, the day synced with nothing to change. Read from the screen and compared with the export: seven
past days, among them one of 45 entries, one with calories entered by hand, and units such as `each - 2" diameter`.
Four plates were also logged one after another into an hour that already held entries (the path through the
time picker), with results found by scrolling and units changed. Then MacroFactor's own Move, on future days: about twenty
moves between Nov 21 and Nov 22 of a weightless recipe, a drink in fl oz, accented branded foods, a recipe at half
a serving, 7.67 g and one of two identical rows; into empty hours and hours already holding entries, to 00:00 and
23:59, with and without a minute; three weeks ahead in three steps and back in one; undone; cancelled before the
saving tap and during selection (nothing changed); through `mf mv` and `mf undo`. Seven rows were given a time
at once. Navigation was read (not written) from today to August 2025, to January 2027 and back.
The recipe list was read whole twice (245 recipes) and incrementally four times, the order found back on
Created after each. A throwaway recipe was created through mf, found by the next recipe reading (1 new),
staged from the Library without saving, deleted, and absent from a whole reading. Logging 45 days ahead was
staged (two foods). The page's own path was driven against the real server and phone on the test days: a log of
two foods at a set minute, a time change, a move to the next day, three Undos in a row, a Sync.
Last run of the day on the final code: 41 of 44 foods and servings staged and verified (the other three are
foods the phone's search no longer lists); then sixteen of them saved into one hour in two plates, each edited,
the day synced with nothing to change, all removed. The first attempt at that second plate had failed before
saving: after the time picker the search field stayed reported where the keyboard had been. The logger is now
opened from the hour's own "+" (brought back into view after a long hour is read), which needs no time picker.
Recipes, 2026-11-19: the search was read for 30 queries (history, Common, Branded, nothing found). Rehearsals
(`mf recipe try`, nothing created): 21 ingredients from the three sections in g and ml, twice; nine ingredients;
a recipe of four servings with a total weight, made of another recipe at half a serving, a drink in fl oz, half a
tablespoon and a cup and a half; a unit the food lacks, refused with the wizard left. Then "Creamy Chickpea" was
created with 21 ingredients (3674.1 g, 3731 kcal) and found by the next recipe reading with the same figures.
Not exercised: a recipe whose weight or energy check fails on the phone (only in the tests' terms); a creation
stopped between the creating tap and the Library; an ingredient that is a recipe never logged.

Not exercised on the phone: a recipe of the user's actually edited between two readings (that an edit brings it
to the top of Modified order is the phone's own description, and what the order showed); that a moved entry keeps
its figures when its recipe was edited in between (the phone's Move is relied on for that); a move into or out
of today; the recovery from unreadable totals (the state did not come back); a history list whose last row
cannot be scrolled clear of the search bar.

2026-11-19, around what the reviews touched (future days, cleaned up, both read empty afterwards): three foods of
different kinds logged in one plate, one edited, one moved to the next day at a set minute and the move undone,
one removed and the removal undone, both days synced with nothing to change. The live server was killed as the
driver said "saving" for a log asked through its API: after its restart the job was "uncertain" with its day, the
plate was still open on the phone (the kill had come before the tap) and every mf operation refused it as a draft
mf did not create, as designed; once that draft was discarded on the phone, `mf sync` of the day (another
process than the server) settled the journal entry and the server's job with it. `tools/probe.py --per-kind 3`:
50 of 56 staged; the six were four foods the search no longer lists and the two rows named "Roasted Chickpeas".

Later on 2026-11-19, recipes of shared names and interrupted plates (future days, cleaned up, read empty afterwards).
All 39 recipes of the 17 shared names were staged from the Library without saving: 33 at once; six had been added
and then waited for a search field that was no longer there. The Library's field heads its list and scrolls away
with it, the wizard's Done with the field, and with Library selected the strip of tabs no longer shows Search:
the list is now brought back to its head and the strip swiped before the next food. Then: two recipes named
"Soup" and one "Roasted Chickpeas" logged in one plate, each as the recipe asked (315, 101 and 358 kcal); one of
the two soups removed and put back; one copied to the next day; both days synced with nothing to change; a recipe
rehearsed from two soups, an apple and the chickpeas, through the Library and the Search tabs in turn. The details
show what one unit weighs whatever the amount (a first check compared it to the amount's weight and refused half
a serving).

A log was killed while filling its plate (second food open, the first on the plate), and again as it said
"saving": each time the next `mf sync` backed the open food out, read the plate, found what had been written down
of it, discarded it and went on. With what was written down altered by a gram, the same sync refused the plate
as not its own and left its foods on it.

Two foods named "Galettes 4 Céréales By Carrefour Bio" are in the history, alike to the gram: the database's
(units g, oz, serving, lb; "To Custom") and a custom copy made on 2026-11-19 to have a "portion" (g, oz, portion,
lb; "Edit"). Which row is listed first follows the last use. With the row lacking "portion" made to be opened
first, the driver read its units, left its details (the food then leaves the plate), opened the other and staged
3 portion (87 kcal); nothing was saved.

The two galettes are not alike to the gram after all: logged at 20 g the database's shows 77 kcal (385 for 100 g),
the copy 87 kcal for 23.01 g (378). At 7.67 g both rounded to 29 kcal, which is why they had passed for one; a test
log of 20 g then made mf refuse them again (and left "20 g" as that row's last amount). Rows of one name within 5%
of each other are now one food. Then, without saving: 25 foods of every kind staged, 24 at once and the galettes
after this change, in portions and in grams. With saving, on a future day since emptied: galettes in portions and
in grams with half a "Soup (365 kcal)" in one plate, an edit to 2 portion, a sync with nothing to change.

Plates left open on purpose, each by killing a log with its second food open: with the record of it altered so
that the open food was not one mf was adding, and again with no record, `mf sync` refused and the food was still
open afterwards; with the record back, a sync killed as it began to discard left the record in place, and the
next one discarded the plate and forgot it.
