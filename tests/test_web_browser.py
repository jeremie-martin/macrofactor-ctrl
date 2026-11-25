"""Repeatable browser checks: real page/server/service over synthetic data and a pretend phone."""

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which("chromium") or not shutil.which("node"), reason="needs Chromium and Node")
def test_page_log_cancel_retry_undo_and_uncertain_recovery(store, tmp_path):
    root = Path(__file__).parents[1]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {**os.environ, "MF_DATA_DIR": str(store.path.parent), "MF_SANDBOX_DELAY": "0.15"}
    with (tmp_path / "server.log").open("w+") as output:
        proc = subprocess.Popen([sys.executable, "tools/sandbox.py", str(port)], cwd=root, env=env, stdout=output, stderr=output)
        try:
            deadline = time.monotonic() + 15
            while True:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=1):
                        break
                except OSError:
                    assert proc.poll() is None and time.monotonic() < deadline, "sandbox did not start"
                    time.sleep(0.1)
            steps = []

            def until(js):
                steps.append(["until", js])

            def log(quantity):
                steps.extend([["click", "#food"], ["type", "apple"], ["key", "Enter"], ["type", str(quantity)], ["key", "Enter"]])

            until("typeof S !== 'undefined' && S.loaded && S.snap")
            steps.extend([["type", "apple"], ["key", "Enter"], ["assert", "S.snap.day.entries.length === 0 && S.jobs.length === 0"]])
            steps.extend([["type", "120"], ["key", "Enter"]])
            until("document.querySelectorAll('.row.saved').length === 1 && document.querySelector('#undo')")
            # protein, fat and carbs: a click on their heads shows figures instead of the bar, and back; the typing focus stays
            steps.append(["click", ".cols .lg"])
            steps.append(
                [
                    "assert",
                    "document.querySelector('.row.saved .figs') && !document.querySelector('.bar') && document.activeElement.id === 'food'",
                ]
            )
            steps.append(["click", ".cols .lg"])
            steps.append(["assert", "document.querySelector('.row.saved .bar') && !document.querySelector('.figs')"])
            steps.append(["eval", "window.oldLog = S.snap.last"])
            steps.append(["click", "#undo"])
            until("S.jobs.length === 0 && S.snap.day.entries.length === 0")
            # Replay an acknowledged log after Undo has replaced it in the server's last-result slot.
            steps.append(["eval", "var lost = { ...oldLog, local: true }; S.asked.push(lost); send(lost)"])
            until("S.asked.length === 0 && S.jobs.length === 0 && S.snap.day.entries.length === 0")
            log(150)
            until("document.querySelector('#phone [data-cancel]')")
            steps.append(["click", "#phone [data-cancel]"])
            until("S.jobs.length === 0 && S.snap.last?.status === 'cancelled'")
            steps.append(["assert", "S.snap.day.entries.length === 0"])
            log(666)
            until("document.querySelector('.row.failed') && document.querySelector('#retry:not([disabled])')")
            steps.append(["reload"])
            until("document.querySelector('.row.failed') && document.querySelector('#retry:not([disabled])')")
            steps.append(["click", "#retry"])
            until("S.jobs.some(j => j.status === 'queued' || j.status === 'running')")
            until("document.querySelector('.row.failed') && document.querySelector('#retry:not([disabled])')")
            steps.append(["click", "#dismiss"])
            until("S.jobs.length === 0")
            log(667)
            until("document.querySelector('#check')")
            steps.append(["assert", "!document.querySelector('.row.failed') && !document.querySelector('#retry:not([disabled])')"])
            steps.append(
                [
                    "assert",
                    "S.data.uncertain && !document.querySelector('#sums .eaten .k').textContent "
                    "&& !document.querySelector('#sums .left .k').textContent",
                ]
            )
            steps.append(["assert", "!document.querySelector('#ledger').textContent.includes('Nothing logged')"])
            steps.append(["reload"])
            until("document.querySelector('#check')")
            steps.append(["click", "#check"])
            until("S.jobs.length === 0 && S.snap.day.entries.length === 1")
            steps.append(["assert", "S.snap.day.entries[0].quantity === 667 && !document.querySelector('.row.failed')"])
            log(667)
            until("document.querySelector('#check')")
            steps.append(["click", "#dismiss"])
            steps.append(["reload"])
            until("S.data?.uncertain && S.jobs.length === 0")
            steps.append(["assert", "!document.querySelector('#sums .left .k').textContent"])
            steps.append(["click", "#sync"])
            until("S.jobs.length === 0 && S.snap.last?.kind === 'sync'")
            steps.append(["assert", "S.snap.day.entries.length === 2 && !S.data.uncertain"])
            # the menu beside Sync names the other readings; one is asked and the typing focus stays
            steps.extend([["click", "#syncmore"], ["click", "#syncmenu [data-sync=recipes]"]])
            until("S.jobs.length === 0 && S.snap.last?.args?.what === 'recipes' && S.snap.last.status === 'done'")
            steps.append(
                ["assert", "!document.querySelector('#syncmenu').matches(':popover-open') && document.activeElement.id === 'food'"]
            )
            # a time typed and not yet settled with Enter is the time of a + pressed then (seen: logged at "now", then retimed)
            steps.append(["eval", "window.under = document.querySelector('#grid .plus').closest('.food').textContent"])
            steps.extend([["click", "#when"], ["type", "07:15"], ["click", "#grid .plus"]])
            until("[...S.jobs, S.snap.last].some(job => job.kind === 'log' && job.args.time === '07:15')")
            steps.append(
                ["assert", "document.querySelector('#grid .plus').closest('.food').textContent === under"]
            )  # the list stayed under the pointer
            script = tmp_path / "steps.json"
            script.write_text(json.dumps(steps))
            result = subprocess.run(
                ["node", "tools/drive.mjs", str(port), str(script)], cwd=root, env=env, capture_output=True, text=True, timeout=100
            )
            assert result.returncode == 0, result.stdout + result.stderr
            output.seek(0)
            folder = Path(output.read().split("everything in ", 1)[1].split(")", 1)[0])
            events = [json.loads(line) for path in (folder / "state/trace").glob("*.jsonl") for line in path.read_text().splitlines()]
            assert not [e for e in events if e["src"] == "page" and e["ev"] == "error"]
        finally:
            proc.terminate()
            proc.wait(timeout=10)
