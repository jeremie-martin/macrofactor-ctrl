# MacroFactor control

Read README.md first, and DESIGN.md before touching the page (`src/mf/web`): it holds the rules the page is built on and how a change to it is made. This is a standalone Python/ADB project (`mf`, under `src/mf`); the
ancestor Bolt instructions (pipelines, `tf.expect` test framework) do not describe this repo.
Tests are plain pytest in `tests/`; fixtures are synthetic, never real exports or UI captures.

Operating the phone

- Use `mf` (`--json` for structured output) rather than raw taps. `mf log -n` previews,
  `mf day --live` reads the phone, `mf ui` prints the current screen, `mf home` resets the app.
- The user's food log is real data. Log only what was asked; a request to explore or preview
  does not authorize logging. Clean up anything logged for testing and verify with `mf backup`
  against an earlier snapshot.
- A failed write is never retried blindly: read the error, look at `mf day --live` and the
  evidence in `~/.local/state/mf/failures/`, then decide.
- mf refuses to touch a draft plate it did not create; its own, left by an interrupted write, it knows by what it
  wrote down as it filled it (`staging`), and discards. Never tap "Clear Day".
- A food is asked for by its catalogue name. Several recipes of one name each have their own ("Soup (201 kcal)");
  the bare name is refused with the choices. Never pick one for the user.
- MacroFactor limits exports. After about nine in one day (2026-11-15) it answered "Unusual Activity: please try
  again in 24 hours" and refused them. A backup is two exports, `mf sync week|month|all` is one; `mf sync [DAY]`
  and the page's Sync read the screen and are not exports. mf counts exports and refuses beyond six in 24 hours
  (`mf status` shows what is left; `--force` is the user's decision, never an agent's). Take one backup before a
  validation and check the result with `mf day --live` or `mf sync`, neither of which is an export.
- Snapshots in `~/.local/share/macrofactor/snapshots/` are originals; do not edit them.

Changing the code

- A write's saving taps are sent with `_commit` (one, or two for a log at a set minute: the save, then its time); everything before the first sits inside
  `_before_saving` so it can be stopped and backed out. After changing a write, run `tools/validate_cancel.py` (real phone, future day).
- What was tried by hand is a few foods; the log holds hundreds, drawn in more ways than that (no weight, other
  units, accents, long result lists). After changing how a food is searched, opened, set or checked, run
  `tools/probe.py` (real phone, saves nothing, no export) and read what it recorded before believing the change.
- Screen knowledge belongs in `screens.py` (pure, testable); sequences in `app.py`; anything
  both front ends need in `service.py`. Every write must end by reading back what was saved.
- Flutter's accessibility tree lies in known ways (stale panels, labels joined by newlines,
  controls hidden under the keyboard). Wait on labels, never on sleeps or blind coordinates.
- Run `uv run pytest -q` and `uv run ruff check .`. Logic changes need tests.
- Never build the real `Service`/`Worker` in a test (`create_app` takes a `Worker`, which takes the `Service`): a job would drive the phone.
  `tests/conftest.py` guards against it; keep that guard. Try page changes in `tools/sandbox.py` (copy of the
  data, pretend phone), driven with `tools/drive.mjs`; `mf trace` shows how the page is really used.
- Keep health data, credentials, exports and UI captures out of Git. Do not commit or push
  unless asked.
