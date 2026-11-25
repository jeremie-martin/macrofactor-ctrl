"""The trace: one line for everything that was done, by you on the page, by the server, and by mf on the phone.

It exists to learn how the tool is really used and where it is slow or odd, not to drive anything: writing to it
never fails an operation, and nothing reads it but `mf trace` and whoever is curious.

One JSON object per line in `~/.local/state/mf/trace/YYYY-MM-DD.jsonl` (the day is the event's own):

    t     seconds since the epoch, to the millisecond
    src   page | server | phone | cli
    ev    what happened (below)
    run   the process that wrote it (a server start, a command)

    page    (every line also carries `page`, the name of the page that wrote it)
            key {key, in, rep}       a key press and the field it went to (rep: held down)
            click {in, on, what, px} a click, what it hit, and how far the pointer travelled to get there
            act {act, via, op, ...}  what it meant: log, edit, retime, remove, cancel, undo, day, when, tab, ...; `via` is where
                                     a log was asked from (line, list, day); take: a row was brought to the line
            load, focus, blur, error
    server  job {job, kind, label, status, wait_ms, run_ms, error, note}
    phone   step {text}              a progress line of the driver
            input {cmd, action, ms}  a tap, key or text sent with adb (action: input sequence on this phone)
            screen {file, action, ms}   the XML actually read, compressed under TRACE_DIR; action is the last input
            wait {what, ms, reads, matched, ok}   a wait on the screen; matched counts frames satisfying the condition
    cli     run {argv}, end {code, ms}

`op` is the key of the job an act is about: the one it starts (the page names it "<page>-<n>" and the job takes
that name), or the one it cancels or dismisses. Phone lines carry that key as `job`. So an act, its job and
every tap it cost can be read as one story: `timeline` does that.
"""

from __future__ import annotations

import contextlib
import contextvars
import gzip
import hashlib
import json
import os
import shutil
import statistics
import threading
import time
from collections.abc import Iterable, Iterator
from datetime import date, datetime
from pathlib import Path

STATE_DIR = Path(os.environ.get("MF_STATE_DIR", Path.home() / ".local/state/mf"))  # lock, failures, trace
TRACE_DIR = STATE_DIR / "trace"
RUN = f"{os.getpid():x}-{int(time.time()) % 0xFFFF:04x}"
context: contextvars.ContextVar[dict | None] = contextvars.ContextVar("mf_trace", default=None)  # fields added to every line: the job
_lock = threading.Lock()
IDLE = 20.0  # seconds without input after which what follows is a new gesture


def emit(src: str, ev: str, t: float | None = None, **fields) -> None:
    """Append one line. Never raises: the trace must not be able to break what it observes."""
    with contextlib.suppress(Exception):
        t = round(time.time() if t is None else float(t), 3)
        line = {"t": t, "src": src, "ev": ev, "run": RUN, **(context.get() or {}), **{k: v for k, v in fields.items() if v is not None}}
        text = json.dumps(line, ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
        with _lock:
            TRACE_DIR.mkdir(parents=True, exist_ok=True)
            with (TRACE_DIR / f"{date.fromtimestamp(t).isoformat()}.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(text)


SCREEN_DAYS = 7  # days of screens kept: enough to replay a failure noticed late; they hold what the food log showed


def screen(xml: str, *, action: int, ms: int) -> None:
    """Keep an already-read hierarchy for offline replay, once per distinct XML per day, for `SCREEN_DAYS` days.

    No new phone reads or screenshots. Like the text trace, failure to record never affects an operation.
    Every read still gets an event, so repeated frames and their positions between inputs remain visible.
    """
    with contextlib.suppress(Exception):
        data = xml.encode("utf-8")
        relative = Path("screens") / date.today().isoformat() / f"{hashlib.sha256(data).hexdigest()}.xml.gz"
        path = TRACE_DIR / relative
        with _lock:
            if not path.exists():
                if not path.parent.exists():  # the first screen of a day: the days beyond the last few go
                    for old in sorted(p for p in path.parent.parent.glob("*") if p.is_dir())[: 1 - SCREEN_DAYS]:
                        shutil.rmtree(old, ignore_errors=True)
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix(f".{RUN}.tmp")
                try:
                    temporary.write_bytes(gzip.compress(data, mtime=0))
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
        emit("phone", "screen", file=relative.as_posix(), action=action, ms=ms)


@contextlib.contextmanager
def within(**fields) -> Iterator[None]:
    """Lines emitted inside also carry `fields` (in this thread or task)."""
    token = context.set({**(context.get() or {}), **fields})
    try:
        yield
    finally:
        context.reset(token)


def read(day: date | None = None) -> list[dict]:
    """A day's lines in time order; lines cut short by a crash are skipped."""
    path = TRACE_DIR / f"{(day or date.today()).isoformat()}.jsonl"
    events = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            with contextlib.suppress(ValueError):
                event = json.loads(line)
                if isinstance(event, dict) and "t" in event:
                    events.append(event)
    return sorted(events, key=lambda e: e["t"])


# ---------------------------------------------------------------- reading it as a story


def timeline(events: Iterable[dict]) -> list[dict]:
    """Acts in order, each with what it cost you (keys, clicks, pointer travel, seconds) and what it cost the phone.

    The inputs of an act are the page's key presses and clicks since the previous act of that page, or since the
    last pause longer than `IDLE`. An act that started a job carries it: wait, run time, status, inputs and waits.
    """
    events = list(events)
    jobs: dict[str, dict] = {}  # job key (the page's name for the act that asked for it) -> job story
    for e in events:
        if e["src"] == "server" and e["ev"] == "job":
            job = jobs.setdefault(
                e["job"],
                {"job": e["job"], "kind": e.get("kind"), "label": e.get("label"), "inputs": 0, "waits": [], "steps": []},
            )
            job.update({k: e[k] for k in ("status", "wait_ms", "run_ms", "error", "note") if k in e})
        elif e["src"] == "phone" and "job" in e:
            job = jobs.get(e["job"])
            if job is None:
                continue
            if e["ev"] == "input":
                job["inputs"] += 1
            elif e["ev"] == "wait":
                job["waits"].append({"what": e.get("what"), "ms": e.get("ms"), "ok": e.get("ok", True)})
            elif e["ev"] == "step":
                job["steps"].append(e.get("text"))
    acts: list[dict] = []
    pending: dict[str, list[dict]] = {}  # page -> inputs since its last act
    for e in events:
        if e["src"] != "page":
            if e["src"] == "cli" and e["ev"] == "run":
                acts.append({"t": e["t"], "src": "cli", "act": "mf " + " ".join(e.get("argv", []))})
            continue
        inputs = pending.setdefault(e.get("page", ""), [])
        if e["ev"] in ("key", "click"):
            if inputs and e["t"] - inputs[-1]["t"] > IDLE:
                inputs.clear()
            inputs.append(e)
        elif e["ev"] == "act":
            keys = [i["key"] for i in inputs if i["ev"] == "key"]
            clicks = [i for i in inputs if i["ev"] == "click"]
            act = {k: v for k, v in e.items() if k not in ("src", "ev", "run", "page")}
            act.update(
                src="page",
                keys=keys,
                clicks=len(clicks),
                px=round(sum(c.get("px") or 0 for c in clicks)),
                seconds=round(e["t"] - inputs[0]["t"], 2) if inputs else 0.0,
            )
            if e.get("op") in jobs:
                act["job"] = jobs[e["op"]]
            acts.append(act)
            if e.get("act") != "take":  # a food taken to the line is half a gesture: what is typed next belongs with it
                inputs.clear()
    known = {id(a["job"]) for a in acts if "job" in a}
    for job in jobs.values():  # jobs whose asking is not in this trace (it was traced on another day, or not from a page)
        if id(job) not in known and job.get("status") in ("done", "failed", "uncertain", "cancelled"):
            acts.append({"t": 0.0, "src": "server", "act": job["kind"], "what": job["label"], "job": job})
    return acts


_KEYS = {
    " ": "␣",
    "Enter": "↵",
    "ArrowUp": "↑",
    "ArrowDown": "↓",
    "ArrowLeft": "←",
    "ArrowRight": "→",
    "Backspace": "⌫",
    "Escape": "esc",
    "Tab": "⇥",
    "Delete": "del",
}


def _cost(act: dict) -> str:
    parts = []
    if act.get("keys"):
        parts.append("".join(_KEYS.get(k, k) if len(_KEYS.get(k, k)) == 1 else f" {_KEYS.get(k, k)} " for k in act["keys"]).strip())
    if act.get("clicks"):
        parts.append(f"{act['clicks']} click{'s' if act['clicks'] > 1 else ''} ({act['px']} px)")
    if act.get("seconds"):
        parts.append(f"{act['seconds']:.1f} s")
    return " · ".join(parts)


def _phone(job: dict) -> str:
    if "status" not in job or job["status"] in ("queued", "running"):
        return f"job {job['job']} {job.get('status', '?')}"
    text = f"job {job['job']} {job['status']} in {(job.get('run_ms') or 0) / 1000:.1f} s"
    if job["inputs"] or job["waits"]:
        text += f" ({job['inputs']} inputs, {len(job['waits'])} waits"
        slow = max(job["waits"], key=lambda w: w["ms"] or 0, default=None)
        if slow and (slow["ms"] or 0) >= 1500:
            text += f"; slowest: {slow['what']} {slow['ms'] / 1000:.1f} s"
        text += ")"
    if job.get("error"):
        text += f" — {job['error']}"
    elif job.get("note"):
        text += f" — {job['note']}"
    return text


def render(acts: list[dict]) -> str:
    """The timeline as text: one line an act, a second one for what the phone did about it."""
    lines = []
    for act in acts:
        when = datetime.fromtimestamp(act["t"]).strftime("%H:%M:%S") if act["t"] else "        "
        what = act.get("what") or ""
        via = f" [{act['via']}]" if act.get("via") else ""
        lines.append(f"{when}  {act['act']:<8} {what}{via}".rstrip() + (f"   ← {_cost(act)}" if _cost(act) else ""))
        if "job" in act:
            lines.append(f"{'':10}{'':<8} {_phone(act['job'])}")
    return "\n".join(lines)


def stats(acts: list[dict]) -> dict:
    """Per kind of act: how many, the median keys, clicks and seconds they cost, and the phone's median and worst time.
    Also the screen waits that took longest in total, and everything that failed."""
    kinds: dict[str, list[dict]] = {}
    for act in acts:
        kinds.setdefault(f"{act['act']}{'/' + act['via'] if act.get('via') else ''}", []).append(act)
    table = {}
    for kind, group in sorted(kinds.items(), key=lambda item: -len(item[1])):
        mine = [a for a in group if a["src"] == "page"]
        runs = [a["job"]["run_ms"] / 1000 for a in group if a.get("job", {}).get("status") == "done" and a["job"].get("run_ms")]
        table[kind] = {
            "n": len(group),
            "keys": statistics.median(len(a["keys"]) for a in mine) if mine else None,
            "clicks": statistics.median(a["clicks"] for a in mine) if mine else None,
            "seconds": round(statistics.median(a["seconds"] for a in mine), 1) if mine else None,
            "phone_s": round(statistics.median(runs), 1) if runs else None,
            "phone_max_s": round(max(runs), 1) if runs else None,
        }
    waits: dict[str, list[float]] = {}
    for act in acts:
        for wait in act.get("job", {}).get("waits", []):
            waits.setdefault(wait["what"] or "?", []).append((wait["ms"] or 0) / 1000)
    slow = sorted(
        ({"what": w, "n": len(v), "total_s": round(sum(v), 1), "max_s": round(max(v), 1)} for w, v in waits.items()),
        key=lambda r: -r["total_s"],
    )
    failed = [
        {"act": a["act"], "what": a.get("what") or a["job"].get("label"), "error": a["job"].get("error")}
        for a in acts
        if a.get("job", {}).get("status") in ("failed", "uncertain")
    ]
    return {"acts": table, "waits": slow[:12], "failed": failed}
