#!/usr/bin/env python3
"""Regenerate the README screenshots in docs/ without touching your desktop.

    QT_QPA_PLATFORM=offscreen python3 scripts/screenshots.py

Fills the queue with a finished batch of made-up files and grabs the window in
both appearances. No files are read or written apart from the two PNGs.
"""

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import os

os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp()   # never read real settings

from PyQt6.QtWidgets import QApplication

import smallfry as sf

DOCS = ROOT / "docs"
MB = 1024 * 1024

# name, before, after (None = still running), note
BATCH = [
    ("beach-day.mov",           1840 * MB, 212 * MB, ""),
    ("screen-recording.mkv",     612 * MB,  48 * MB, ""),
    ("IMG_2041.jpg",             8.4 * MB, 1.1 * MB, ""),
    ("podcast-episode-12.wav",   402 * MB,  31 * MB, ""),
    ("signed-lease.pdf",          23 * MB, 2.7 * MB, ""),
    ("drone-flyover.mp4",       3100 * MB,     None, ""),
]


def fill(win: sf.Smallfry) -> None:
    for name, before, after, note in BATCH:
        path = Path("/home/you/Videos") / name
        job = sf.Job(path=path, kind=sf.kind_of(path), before=int(before))
        row = sf.QueueRow(job, win.t, win.mono)
        job.row = row
        win.queue_lay.insertWidget(win.queue_lay.count() - 1, row)
        win.jobs.append(job)
        if after is None:
            job.status = "running"
            job.frac = 0.62
            row.refresh()
            row.meter.set_progress(0.62)
        else:
            job.status = "done"
            job.after = int(after)
            job.note = note
            row.refresh()
    win.queue_changed()
    win.run_started = 1.0
    win.set_running(True)
    win.refresh_totals()
    win.status.setText("1 running   ·   drone-flyover.mp4   ·   about 40 s left")
    win.sync_progress(91)


def main() -> None:
    DOCS.mkdir(exist_ok=True)
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    for theme in ("Light", "Dark"):
        win = sf.Smallfry()
        if win.theme_name != theme:
            win.toggle_theme()
        win.dest.setText("")
        win.resize_group.button(2).setChecked(True)   # 1080p
        win.resize(760, 980)
        if "--empty" in sys.argv:
            win.show()
            for _ in range(10):
                app.processEvents()
            win.grab().save(f"/tmp/empty-{theme.lower()}.png")
            win.close()
            continue
        fill(win)
        win.show()
        for _ in range(10):
            app.processEvents()
        out = DOCS / f"screenshot-{theme.lower()}.png"
        win.grab().save(str(out))
        print("wrote", out.relative_to(ROOT))
        win.close()


if __name__ == "__main__":
    main()
