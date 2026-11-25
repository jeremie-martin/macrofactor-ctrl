"""Shared fixtures: a store filled from a small synthetic export (no personal data)."""

import csv
import os
import tempfile
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

# Before mf is imported: threads a test leaves running must not find the real folders once its patches are undone.
os.environ["MF_STATE_DIR"] = os.environ["MF_DATA_DIR"] = tempfile.mkdtemp(prefix="mf-tests-")

from mf import device, service, trace
from mf import store as store_module
from mf.store import Store

HEADER = ["Date", "Time", "Food Name", "Serving Size", "Serving Qty", "Serving Weight (g)"]
HEADER += ["Calories (kcal)", "Fat (g)", "Carbs (g)", "Protein (g)"]
TODAY = date.today()


def days_ago(n: int) -> str:
    return (TODAY - timedelta(days=n)).isoformat()


def food_log_rows() -> list[list[str]]:
    """Twenty days of apples at 08:00, some skyr, two kinds of galette, one stew recipe serving."""
    rows = []
    for back in range(1, 21):
        day = (TODAY - timedelta(days=back)).isoformat()
        rows.append([day, "08:00", "Apple, Fresh", "g", "150.0", "1.0", "78", "0.3", "20.7", "0.4"])
        if back % 2:
            rows.append([day, "12:30", "Skyr Nature 0%", "g", "200.0", "1.0", "114", "0.4", "8", "20"])
        if back % 5 == 0:
            rows.append([day, "16:00", "Galettes 4 Céréales", "galette", "2.0", "7.5", "58", "0.4", "12", "1.4"])
    rows.append([days_ago(3), "19:15", "Galettes De Riz", "g", "30.0", "1.0", "115", "1", "24", "2"])
    rows.append([days_ago(2), "20:00", "Lentil Stew", "serving", "1.0", "300.0", "330", "9", "40", "21"])
    return rows


def no_adb(*args, **kwargs):
    raise AssertionError("a test tried to run adb")


class NoPhone:
    def __init__(self, *args, **kwargs):
        raise AssertionError("a test tried to drive the real phone")


@pytest.fixture(autouse=True)
def trace_dir(tmp_path: Path, monkeypatch) -> Path:
    """Tests never reach the real phone, the real data or the real trace, whatever they forget to fake."""
    monkeypatch.setattr(service, "Phone", NoPhone)
    monkeypatch.setattr(service, "phone_environment", lambda serial, package: dict(service.S.VERIFIED))
    monkeypatch.setattr(service, "phone_serial", lambda: "synthetic")  # the server's state asks which phone is there
    monkeypatch.setattr(device, "subprocess", SimpleNamespace(run=no_adb))  # the only way the package reaches adb
    monkeypatch.setattr(store_module, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(trace, "TRACE_DIR", tmp_path / "trace")
    return tmp_path / "trace"


@pytest.fixture
def export_csv(tmp_path: Path) -> Path:
    path = tmp_path / "MacroFactor-synthetic.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle, quoting=csv.QUOTE_ALL)
        writer.writerow(HEADER)
        writer.writerows(food_log_rows())
    return path


@pytest.fixture
def store(tmp_path: Path, export_csv: Path) -> Store:
    store = Store(tmp_path / "mf.db")
    store.import_food_log(export_csv)
    return store
