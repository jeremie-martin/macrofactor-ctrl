"""A sandbox mf server for working on the page: a copy of your data in a temporary folder, and a pretend phone that
takes 3 s per write and only ever changes that copy. The real phone, data and trace cannot be reached from it.
Everything but the phone is the real thing: the server, the service, the journal, undo. Asking for 666 of anything
is refused before saving; 667 saves but fails verification, to exercise recovery.

    uv run python tools/sandbox.py [PORT]      # http://127.0.0.1:8399 by default
    node tools/drive.mjs PORT steps.json       # drive that page in headless Chromium (keys, clicks, screenshots)

Its trace is in the folder it prints: MF_STATE_DIR=<folder>/state uv run mf trace
"""

import os
import sqlite3
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path

here = Path(tempfile.mkdtemp(prefix="mf-sandbox-"))
(here / "data").mkdir()
real = Path(os.environ.get("MF_DATA_DIR", Path.home() / ".local/share/macrofactor")) / "mf.db"
if real.exists():
    with sqlite3.connect(f"file:{real}?mode=ro", uri=True) as source, sqlite3.connect(here / "data" / "mf.db") as copy:
        source.backup(copy)
os.environ["MF_DATA_DIR"] = str(here / "data")
os.environ["MF_STATE_DIR"] = str(here / "state")
os.environ["ANDROID_SERIAL"] = "sandbox-no-phone"
sys.path.insert(0, str(Path(__file__).parent.parent / "tests"))
import uvicorn  # noqa: E402
from fakes import FakeApp, fake_service  # noqa: E402

from mf import server  # noqa: E402
from mf import service as svc  # noqa: E402
from mf.app import AppError, Entry  # noqa: E402
from mf.store import MACROS, Store  # noqa: E402


class NoPhone:
    def __init__(self, *a, **k):
        raise AppError("sandbox: no phone")


svc.Phone = NoPhone
svc.phone_serial = lambda: "sandbox"


class PretendPhone(FakeApp):
    """Holds the last days of the copy as MacroFactor would, and takes its time over each write."""

    def __init__(self, store: Store):
        super().__init__()
        self.store, self.service = store, None
        for back in range(8):
            for e in store.day(date.today() - timedelta(days=back))["entries"]:
                hour, minute = e["time"][:2], e["time"][3:]
                serving = f"{e['quantity']:g} {e['unit']}"
                macros = [e[m] or 0 for m in MACROS]
                self.rows.append(
                    Entry(date.fromisoformat(e["date"]), int(hour), e["name"], serving, *macros, int(minute) if minute.isdigit() else 0)
                )

    def _work(self):
        for _ in range(30):
            time.sleep(float(os.environ.get("MF_SANDBOX_DELAY", "3")) / 30)
            if self.service.stop and self.service.stop():
                raise svc.Cancelled

    def price(self, item):
        if item.shown:
            return super().price(item)
        food = self.store.food(item.name)
        per = (food.per_unit.get(item.unit or "g") or {}) if food else {}
        return tuple(round((per.get(m) or 0) * (item.quantity or 0), 1) for m in MACROS)

    def log(self, items, day=None, hour=None, minute=None):
        self._work()
        if any(item.quantity == 666 for item in items):
            raise AppError("sandbox: the phone refused 666")
        self.fail_after_save = any(item.quantity == 667 for item in items)
        now = datetime.now()
        return super().log(items, day, now.hour if hour is None else hour, now.minute if hour is None else minute)

    def remove(self, *args, **kwargs):
        self._work()
        return super().remove(*args, **kwargs)

    def edit(self, *args, **kwargs):
        self._work()
        return super().edit(*args, **kwargs)

    def retime(self, *args, **kwargs):
        self._work()
        return super().retime(*args, **kwargs)


store = Store()
phone = PretendPhone(store)
phone.service = fake_service(store, phone)
app = server.create_app(server.Worker(phone.service))
port = int(sys.argv[1]) if len(sys.argv) > 1 else 8399
print(f"mf sandbox: http://127.0.0.1:{port}   (copy of the data, pretend phone; everything in {here})", flush=True)
uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
