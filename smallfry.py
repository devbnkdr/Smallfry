#!/usr/bin/env python3
"""
Smallfry: drop files in, get smaller files out.

Video, images, audio and PDFs in one queue. Compress by quality strength or
by a target file size. Runs several jobs at once and falls back to CPU
encoding when the GPU encoder refuses a file.

Backends:  ffmpeg (video, images, audio), ghostscript / qpdf (PDF),
           oxipng and jpegtran (lossless image passes)

Originals are never modified.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from PyQt6.QtCore import QProcess, QSettings, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QFontDatabase,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

# --------------------------------------------------------------- backends ---
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
GHOSTSCRIPT = shutil.which("gs")
QPDF = shutil.which("qpdf")
OXIPNG = shutil.which("oxipng")
JPEGTRAN = shutil.which("jpegtran")
EXIFTOOL = shutil.which("exiftool")
NOTIFY = shutil.which("notify-send")


def available_encoders():
    """Encoder names ffmpeg will actually accept on this machine."""
    if not FFMPEG:
        return set()
    try:
        raw = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], check=False,
                             capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    names = set()
    for line in raw.splitlines():
        parts = line.split()
        # Rows look like:  V....D av1_nvenc  NVIDIA NVENC AV1 encoder
        # The legend above them ("V..... = Video") matches the shape too, so
        # insist on something that could actually be an encoder name.
        if (len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] in "VAS"
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.\-]*", parts[1])):
            names.add(parts[1])
    return names


def nvidia_present():
    """ffmpeg lists the NVENC encoders whether or not there is a card to run
    them on, so check for the NVIDIA driver's device node as well."""
    return Path("/dev/nvidiactl").exists()


ENCODERS = available_encoders()
if not nvidia_present():
    ENCODERS -= {"av1_nvenc", "hevc_nvenc", "h264_nvenc"}

APP_ID = "smallfry"       # command, settings key, window class
APP_NAME = "Smallfry"     # what people see
VERSION = "1.0.0"

# ------------------------------------------------------------------ themes ---
THEMES = {
    "Light": {
        "paper": "#E8EAEE", "surface": "#FFFFFF", "raised": "#F4F5F7", "line": "#C9CED6",
        "text": "#181B20", "dim": "#666E7A", "accent": "#1F4FD8", "accent_hi": "#3B67E8",
        "good": "#12795A", "warn": "#B4671A", "on_accent": "#FFFFFF", "log": "#F4F5F7",
    },
    "Dark": {
        "paper": "#101216", "surface": "#171A20", "raised": "#1E222A", "line": "#2B3038",
        "text": "#E4E7EC", "dim": "#828A96", "accent": "#5B8BFF", "accent_hi": "#7BA3FF",
        "good": "#3DBE92", "warn": "#E0A458", "on_accent": "#0B0E14", "log": "#0B0D11",
    },
}

UI_FONTS = ["Inter", "IBM Plex Sans", "Noto Sans", "Cantarell",
            "DejaVu Sans", "sans-serif"]
MONO_FONTS = ["JetBrains Mono", "IBM Plex Mono", "Fira Code", "Hack",
              "Source Code Pro", "DejaVu Sans Mono", "monospace"]


def pick_font(candidates):
    installed = set(QFontDatabase.families())
    for name in candidates:
        if name in installed:
            return name
    return candidates[-1]


def system_prefers_dark():
    """True when the desktop is running a dark colour scheme."""
    app = QApplication.instance()
    if not app:
        return False
    win = app.palette().color(QPalette.ColorRole.Window)
    return win.lightness() < 128


# ------------------------------------------------------------------- media ---
VIDEO_EXT = {".mp4", ".mkv", ".mov", ".avi", ".m4v", ".wmv", ".flv", ".webm",
             ".ts", ".m2ts", ".mpg", ".mpeg", ".3gp"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp", ".avif"}
AUDIO_EXT = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma"}
PDF_EXT = {".pdf"}

KIND_LABEL = {"video": "VID", "image": "IMG", "audio": "SND", "pdf": "PDF"}

STRENGTHS = ["Light", "Balanced", "Strong", "Extreme"]

AV1_CQ    = [26, 32, 38, 44]
HEVC_CQ   = [22, 27, 32, 37]
JPEG_Q    = [3, 6, 10, 15]        # ffmpeg mjpeg -q:v, lower is better
WEBP_Q    = [92, 80, 66, 50]
OPUS_KBPS = [160, 112, 80, 56]
MP3_KBPS  = [224, 160, 128, 96]
PDF_SET   = ["/prepress", "/printer", "/ebook", "/screen"]

# Resize caps the short side of a video and the long side of an image, so a
# 1080p setting means 1920x1080 video and photos no wider than 1920.
RESIZE = ["Original", "1440p", "1080p", "720p"]
RESIZE_PX = [None, 1440, 1080, 720]

KEEP, MODERN, LOSSLESS = 0, 1, 2        # format modes
BY_STRENGTH, BY_SIZE = 0, 1             # compress modes

# Tried in order; the first one this ffmpeg build actually has wins. GPU
# encoders come first, so a card that can do the work gets it, and a machine
# without NVENC never wastes a doomed encode finding that out.
# libaom-av1 sits last on purpose: it is an AV1 encoder, but it is slow
# enough that hardware HEVC finishes a queue while libaom is still on the
# first file. Better codec, worse afternoon.
VIDEO_CHAIN = {
    MODERN: ["av1_nvenc", "libsvtav1", "hevc_nvenc", "libx265", "libaom-av1"],
    KEEP:   ["hevc_nvenc", "libx265", "h264_nvenc", "libx264"],
}

GPU_ENCODERS = frozenset({"av1_nvenc", "hevc_nvenc", "h264_nvenc"})

# Formats with a quality dial ffmpeg can search against for a size target.
SIZEABLE_IMAGE_EXT = {".webp", ".jpg", ".jpeg"}

# Shown when you hover a row that never started.
REJECT_HELP = {
    "same path": "The output would overwrite the original. Set a suffix or "
                 "another folder.",
    "cannot write": "The destination folder can't be created or written to.",
    "no lossless tool": "Install oxipng (PNG) or libjpeg-turbo (JPEG) for lossless "
                        "image passes, or qpdf for PDFs.",
    "needs ghostscript": "Install ghostscript to compress PDFs.",
    "no video encoder in ffmpeg": "This ffmpeg build has none of the encoders "
                                  "Smallfry uses (libx265, libx264, libsvtav1).",
    "no size dial for this format": "This format has no quality setting to aim "
                                    "at a size with.",
    "needs a duration": "ffprobe couldn't read how long this file is, so there's "
                        "no bitrate to aim for.",
}

PREVIEW_SECONDS = 10
MAX_SEARCH_TRIES = 6


def kind_of(path):
    ext = path.suffix.lower()
    if ext in VIDEO_EXT:
        return "video"
    if ext in IMAGE_EXT:
        return "image"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in PDF_EXT:
        return "pdf"
    return None


def human(n):
    if n is None:
        return "—"
    n = float(n)
    if n < 1024:
        return f"{int(n)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"


def duration(secs):
    secs = round(secs)
    if secs < 60:
        return f"{secs} s"
    if secs < 3600:
        return f"{secs // 60} min {secs % 60} s"
    return f"{secs // 3600} h {secs % 3600 // 60} min"


def video_scale(px):
    """ffmpeg scale filter that caps the short side at px, never upscaling."""
    return (f"scale='if(gte(iw,ih),-2,min({px},iw))'"
            f":'if(gte(iw,ih),min({px},ih),-2)'")


def image_scale(px):
    """ffmpeg scale filter that caps the long side to match a video height:
    1080 means at most 1920 wide (or tall). Never upscales."""
    edge = round(px * 16 / 9)
    return (f"scale='if(gte(iw,ih),min({edge},iw),-2)'"
            f":'if(gte(iw,ih),-2,min({edge},ih))'")


def probe_media(path):
    """Return (duration_seconds, audio_present)."""
    if not FFPROBE:
        return None, False
    try:
        raw = subprocess.run(
            [FFPROBE, "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            check=False, capture_output=True, text=True, timeout=25).stdout
        data = json.loads(raw)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None, False
    dur = None
    try:
        dur = float(data.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        pass
    has_audio = any(s.get("codec_type") == "audio" for s in data.get("streams", []))
    return dur, has_audio


@dataclass
class Job:
    path: Path
    kind: str
    before: int
    duration: float = None
    has_audio: bool = False
    after: int = None
    status: str = "waiting"     # waiting running done skipped failed
    note: str = ""
    out: Path = None
    lane: str = ""              # gpu, video (CPU encode) or light
    stage: str = ""             # "meta" while exiftool copies tags across
    error: str = ""             # last thing the tool printed, for failures
    fell_back: bool = False
    cancelled: bool = False
    frac: float = 0.0
    lo: int = None              # image target-size search state
    hi: int = None
    probe_q: int = None
    tries: int = 0
    best: Path = None
    row: object = field(default=None, repr=False)


# -------------------------------------------------------------- components ---
class Ruler(QWidget):
    """Four-stop strength control drawn as a measuring scale."""
    changed = pyqtSignal(int)

    def __init__(self, tokens, mono):
        super().__init__()
        self.t = tokens
        self.mono = mono
        self.index = 1
        self.setFixedHeight(58)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_tokens(self, tokens):
        self.t = tokens
        self.update()

    def stops(self):
        pad = 46
        span = max(1, self.width() - pad * 2)
        return [pad + span * i / (len(STRENGTHS) - 1) for i in range(len(STRENGTHS))]

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            p.setOpacity(0.45)
        xs = self.stops()
        y = 18

        p.setPen(QPen(QColor(self.t["line"]), 2))
        p.drawLine(int(xs[0]), y, int(xs[-1]), y)
        p.setPen(QPen(QColor(self.t["accent"]), 2))
        p.drawLine(int(xs[0]), y, int(xs[self.index]), y)

        for i, x in enumerate(xs):
            active = i == self.index
            colour = QColor(self.t["accent"] if i <= self.index else self.t["line"])
            p.setPen(QPen(colour, 2))
            p.drawLine(int(x), y - 7, int(x), y + 7)
            if active:
                p.setBrush(QColor(self.t["accent"]))
                p.setPen(QPen(QColor(self.t["accent"]), 1))
                p.drawEllipse(int(x) - 6, y - 6, 12, 12)
            f = QFont(self.mono, 9)
            f.setBold(active)
            p.setFont(f)
            p.setPen(QColor(self.t["text"] if active else self.t["dim"]))
            label = STRENGTHS[i]
            w = p.fontMetrics().horizontalAdvance(label)
            p.drawText(int(x - w / 2), y + 30, label)

    def pick(self, x):
        xs = self.stops()
        nearest = min(range(len(xs)), key=lambda i: abs(xs[i] - x))
        if nearest != self.index:
            self.index = nearest
            self.update()
            self.changed.emit(nearest)

    def mousePressEvent(self, e):
        self.pick(e.position().x())

    def mouseMoveEvent(self, e):
        self.pick(e.position().x())

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Left and self.index > 0:
            self.index -= 1
        elif e.key() == Qt.Key.Key_Right and self.index < len(STRENGTHS) - 1:
            self.index += 1
        else:
            return super().keyPressEvent(e)
        self.update()
        self.changed.emit(self.index)


class Meter(QWidget):
    """Thin proportional bar: full width is the original size."""

    def __init__(self, tokens):
        super().__init__()
        self.t = tokens
        self.ratio = None
        self.progress = 0.0
        self.setFixedHeight(6)

    def set_tokens(self, tokens):
        self.t = tokens
        self.update()

    def set_ratio(self, ratio):
        self.ratio = max(0.0, min(1.0, ratio))
        self.progress = 0.0
        self.update()

    def set_progress(self, frac):
        self.progress = max(0.0, min(1.0, frac))
        self.update()

    def reset(self):
        self.ratio = None
        self.progress = 0.0
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()

        def bar(width, colour):
            path = QPainterPath()
            path.addRoundedRect(0.0, 0.0, float(max(0, width)), 6.0, 3.0, 3.0)
            p.fillPath(path, QColor(colour))

        bar(w, self.t["line"])
        if self.ratio is not None:
            bar(int(w * self.ratio), self.t["good"])
        elif self.progress:
            bar(int(w * self.progress), self.t["accent"])


class QueueRow(QFrame):
    remove = pyqtSignal(object)
    act = pyqtSignal(object, str)
    plan = ""                   # what will happen, e.g. "→ .webp · 1080p"

    def __init__(self, job, tokens, mono):
        super().__init__()
        self.job = job
        self.t = tokens
        self.setObjectName("row")
        self.setFixedHeight(56)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.open_menu)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 8, 0)
        lay.setSpacing(12)

        self.badge = QLabel(KIND_LABEL[job.kind])
        self.badge.setObjectName("badge")
        self.badge.setFont(QFont(mono, 8, QFont.Weight.Bold))
        self.badge.setFixedWidth(38)
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.badge)

        centre = QVBoxLayout()
        centre.setSpacing(2)
        centre.setContentsMargins(0, 0, 0, 0)
        self.name = QLabel(job.path.name)
        self.name.setObjectName("rowName")
        self.meter = Meter(tokens)
        centre.addWidget(self.name)
        centre.addWidget(self.meter)
        lay.addLayout(centre, 1)

        self.result = QLabel(human(job.before))
        self.result.setObjectName("rowResult")
        self.result.setFont(QFont(mono, 10))
        self.result.setAlignment(Qt.AlignmentFlag.AlignRight |
                                 Qt.AlignmentFlag.AlignVCenter)
        self.result.setMinimumWidth(180)
        lay.addWidget(self.result)

        self.kill = QPushButton("×")
        self.kill.setObjectName("kill")
        self.kill.setFixedSize(24, 24)
        self.kill.setCursor(Qt.CursorShape.PointingHandCursor)
        self.kill.clicked.connect(lambda: self.remove.emit(self.job))
        lay.addWidget(self.kill)

    def mouseDoubleClickEvent(self, e):
        if self.job.out and self.job.out.exists() and self.job.status == "done":
            self.act.emit(self.job, "open")
        else:
            self.act.emit(self.job, "reveal")

    def open_menu(self, pos):
        j = self.job
        m = QMenu(self)
        if j.status == "running":
            m.addAction("Cancel this job", lambda: self.act.emit(j, "cancel"))
        else:
            if j.out and j.out.exists():
                m.addAction("Open result", lambda: self.act.emit(j, "open"))
            m.addAction("Show in folder", lambda: self.act.emit(j, "reveal"))
            if j.status in ("done", "skipped", "failed"):
                m.addAction("Compress again", lambda: self.act.emit(j, "retry"))
            m.addSeparator()
            m.addAction("Remove from queue",
                        lambda: self.act.emit(j, "remove"))
        m.exec(self.mapToGlobal(pos))

    def set_tokens(self, tokens):
        self.t = tokens
        self.meter.set_tokens(tokens)
        self.refresh()

    def paint(self, text, colour):
        return f'<span style="color:{self.t[colour]}">{text}</span>'

    def refresh(self):
        j = self.job
        tip = [str(j.path)]
        if j.status == "running":
            label = "tagging" if j.stage == "meta" else (j.note or "working")
            if j.frac and not j.note and j.stage != "meta":
                label = f"{j.frac * 100:.0f}%"
            self.result.setText(self.paint(label, "accent"))
        elif j.status == "done":
            saved = (j.before - j.after) / j.before * 100 if j.before else 0.0
            bits = [f"{human(j.before)} → {human(j.after)}",
                    self.paint(f"<b>−{saved:.0f}%</b>", "good")]
            if j.note:
                bits.append(self.paint(j.note, "warn"))
            if j.fell_back:
                bits.append("cpu")
            self.result.setText("&nbsp;&nbsp;&nbsp;".join(bits))
            self.meter.set_ratio(j.after / j.before if j.before else 1.0)
            tip.append(f"→ {j.out}")
        elif j.status == "skipped":
            self.result.setText(j.note or "no gain")
            self.meter.set_ratio(1.0)
            tip.append("The compressed copy wasn't smaller, so it was discarded.")
        elif j.status == "failed":
            self.result.setText(self.paint(j.note or "failed", "warn"))
            self.meter.reset()
            if j.error:
                tip.append(j.error)
        else:
            plan = f"&nbsp;&nbsp;{self.plan}" if self.plan else ""
            self.result.setText(f"{human(j.before)}{self.paint(plan, 'dim')}")
            self.meter.reset()
        self.setToolTip("\n".join(tip))


class DropZone(QFrame):
    files = pyqtSignal(list)

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setObjectName("drop")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setProperty("hot", False)

        self.title = QLabel()
        self.title.setObjectName("dropTitle")
        self.sub = QLabel()
        self.sub.setObjectName("dropSub")
        self.formats = QLabel("MP4 · MKV · MOV · WebM   JPEG · PNG · WebP · TIFF   "
                              "WAV · FLAC · MP3 · M4A   PDF")
        self.formats.setObjectName("dropFormats")
        for w in (self.title, self.sub, self.formats):
            w.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay = QVBoxLayout(self)
        lay.setSpacing(4)
        lay.addStretch()
        lay.addWidget(self.title)
        lay.addWidget(self.sub)
        lay.addSpacing(6)
        lay.addWidget(self.formats)
        lay.addStretch()
        self.set_compact(False)

    def set_compact(self, compact):
        """Big and inviting while the queue is empty, a slim strip after."""
        if compact:
            self.setMinimumHeight(52)
            self.setMaximumHeight(52)
            self.title.setText("Drop more files, or click to add")
            self.sub.hide()
            self.formats.hide()
        else:
            # Fill the window while there's nothing else to show.
            self.setMinimumHeight(190)
            self.setMaximumHeight(16777215)
            self.title.setText("Drop files or folders to compress")
            self.sub.setText("or click to browse")
            self.sub.show()
            self.formats.show()

    def _restyle(self, hot):
        self.setProperty("hot", hot)
        self.style().unpolish(self)
        self.style().polish(self)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._restyle(True)

    def dragLeaveEvent(self, e):
        self._restyle(False)

    def dropEvent(self, e):
        self._restyle(False)
        paths = [Path(u.toLocalFile()) for u in e.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.files.emit(paths)
        e.acceptProposedAction()

    def mousePressEvent(self, e):
        chosen, _ = QFileDialog.getOpenFileNames(self, "Add files")
        if chosen:
            self.files.emit([Path(c) for c in chosen])


class Mark(QWidget):
    """The app icon, drawn in code so the header needs no image plugin."""

    def __init__(self, tokens, size=26):
        super().__init__()
        self.t = tokens
        self.setFixedSize(size, size)

    def set_tokens(self, tokens):
        self.t = tokens
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        k = self.width() / 128
        p.scale(k, k)
        bg = QPainterPath()
        bg.addRoundedRect(6, 6, 116, 116, 26, 26)
        p.fillPath(bg, QColor(self.t["accent"]))
        fish = QPainterPath()
        fish.moveTo(22, 64)
        fish.cubicTo(38, 40, 74, 40, 90, 64)
        fish.lineTo(108, 46)
        fish.lineTo(108, 82)
        fish.lineTo(90, 64)
        fish.cubicTo(74, 88, 38, 88, 22, 64)
        p.fillPath(fish, QColor(self.t["on_accent"]))
        p.setBrush(QColor(self.t["accent"]))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(36, 57, 12, 12)


def chip(text, checked=False):
    b = QPushButton(text)
    b.setCheckable(True)
    b.setChecked(checked)
    b.setProperty("chip", True)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


# ------------------------------------------------------------- main window ---
class Smallfry(QWidget):
    def __init__(self):
        super().__init__()
        self.ui_font = pick_font(UI_FONTS)
        self.mono = pick_font(MONO_FONTS)
        self.settings = QSettings(APP_ID, APP_ID)

        # With nothing stored, follow whatever the desktop is doing.
        stored = self.settings.value("theme", "")
        if stored not in THEMES:
            stored = "Dark" if system_prefers_dark() else "Light"
        self.theme_name = stored
        self.t = THEMES[self.theme_name]

        self.jobs = []
        self.workers = {}          # QProcess -> Job
        self.stopping = False
        self.preview_proc = None
        self.preview_out = None
        self.preview_job = None
        self.preview_dir = None
        self.log_lines = []
        self._pct = 0
        self.run_started = None

        # Three lanes, so one kind of work never starves another:
        #   gpu    NVENC encodes. Recent NVIDIA cards run two side by side.
        #   video  CPU video encodes. Each already uses every core, so running
        #          many at once only adds memory pressure and context switches.
        #   light  images, audio, PDFs and remuxes: short, mostly single-threaded.
        cores = os.cpu_count() or 4
        self.slots = {"gpu": 2,
                      "video": max(1, min(3, cores // 8)),
                      "light": max(2, min(8, cores // 2))}

        self.has_ffmpeg = bool(FFMPEG)

        self.setWindowTitle(APP_NAME)
        self.setObjectName("root")
        geo = self.settings.value("geometry")
        if geo is not None:
            try:
                self.restoreGeometry(geo)
            except (TypeError, ValueError):
                self.resize(760, 980)
        else:
            self.resize(760, 980)

        root = QVBoxLayout(self)
        root.setContentsMargins(26, 20, 26, 20)
        root.setSpacing(0)

        # header ------------------------------------------------------------
        head = QHBoxLayout()
        head.setSpacing(10)
        self.mark = Mark(self.t)
        head.addWidget(self.mark)
        word = QLabel(APP_NAME)
        word.setObjectName("wordmark")
        head.addWidget(word)
        head.addStretch()

        self.theme_btn = chip("", self.theme_name == "Dark")
        self.theme_btn.clicked.connect(self.toggle_theme)
        self.sync_theme_button()
        head.addWidget(self.theme_btn)

        root.addLayout(head)
        root.addSpacing(16)

        # drop --------------------------------------------------------------
        self.drop = DropZone()
        self.drop.files.connect(self.add_files)
        root.addWidget(self.drop, 1)
        root.addSpacing(12)

        # queue -------------------------------------------------------------
        self.queue_host = QWidget()
        self.queue_lay = QVBoxLayout(self.queue_host)
        self.queue_lay.setContentsMargins(0, 0, 0, 0)
        self.queue_lay.setSpacing(6)
        self.queue_lay.addStretch()

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(self.queue_host)
        self.scroll.setObjectName("scroll")
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setMinimumHeight(140)
        root.addWidget(self.scroll, 1)


        root.addSpacing(14)
        root.addWidget(self.rule())
        root.addSpacing(14)

        # compress mode -----------------------------------------------------
        mrow = QHBoxLayout()
        mrow.addWidget(self.section("Compress"))
        mrow.addSpacing(14)
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        mode = self.stored_int("mode", BY_STRENGTH, 2)
        for i, label in enumerate(["By strength", "By size"]):
            b = chip(label, i == mode)
            self.mode_group.addButton(b, i)
            mrow.addWidget(b)
        self.mode_group.idClicked.connect(self.mode_changed)
        mrow.addStretch()
        self.settings_note = QLabel("")
        self.settings_note.setObjectName("hint")
        mrow.addWidget(self.settings_note)
        root.addLayout(mrow)
        root.addSpacing(4)

        self.ruler = Ruler(self.t, self.mono)
        self.ruler.index = self.stored_int("strength", 1, len(STRENGTHS))
        self.ruler.changed.connect(self.settings_changed)
        root.addWidget(self.ruler)

        self.size_row = QWidget()
        srow = QHBoxLayout(self.size_row)
        srow.setContentsMargins(0, 8, 0, 8)
        srow.setSpacing(8)
        self.size_field = QLineEdit(self.settings.value("target_mb", "25"))
        self.size_field.setFixedWidth(90)
        self.size_field.setFont(QFont(self.mono, 10))
        self.size_field.textChanged.connect(self.settings_changed)
        srow.addWidget(QLabel("Target"))
        srow.addWidget(self.size_field)
        srow.addWidget(QLabel("MB per file"))
        srow.addSpacing(12)
        for preset in ("8", "25", "100"):
            b = QPushButton(f"{preset} MB")
            b.setObjectName("ghost")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _, v=preset: self.size_field.setText(v))
            srow.addWidget(b)
        srow.addStretch()
        self.size_row.hide()
        root.addWidget(self.size_row)
        root.addSpacing(10)

        # format ------------------------------------------------------------
        frow = QHBoxLayout()
        frow.addWidget(self.section("Format"))
        frow.addSpacing(14)
        self.fmt_group = QButtonGroup(self)
        self.fmt_group.setExclusive(True)
        fmt = self.stored_int("format", MODERN, 3)
        for i, label in enumerate(["Keep original", "Modern codecs", "Lossless"]):
            b = chip(label, i == fmt)
            self.fmt_group.addButton(b, i)
            frow.addWidget(b)
        self.fmt_group.idClicked.connect(self.format_changed)
        frow.addSpacing(18)
        self.strip = chip("Strip metadata",
                          self.settings.value("strip", "false") == "true")
        self.strip.setToolTip("Remove titles, dates, camera and location tags")
        self.strip.toggled.connect(self.settings_changed)
        frow.addWidget(self.strip)
        frow.addStretch()
        root.addLayout(frow)
        root.addSpacing(10)

        self.resize_row = QWidget()
        rrow = QHBoxLayout(self.resize_row)
        rrow.setContentsMargins(0, 0, 0, 0)
        rrow.addWidget(self.section("Resize"))
        rrow.addSpacing(14)
        self.resize_group = QButtonGroup(self)
        self.resize_group.setExclusive(True)
        size = self.stored_int("resize", 0, len(RESIZE))
        for i, label in enumerate(RESIZE):
            b = chip(label, i == size)
            b.setToolTip("Video: cap the short side.  Photos: cap the long side "
                         "(1080p = 1920 px). Never enlarges." if i else
                         "Keep the original dimensions")
            self.resize_group.addButton(b, i)
            rrow.addWidget(b)
        self.resize_group.idClicked.connect(self.settings_changed)
        rrow.addStretch()
        root.addWidget(self.resize_row)
        root.addSpacing(12)

        # destination -------------------------------------------------------
        root.addWidget(self.section("Save to"))
        root.addSpacing(8)
        self.dest = QLineEdit(self.settings.value("dest", ""))
        self.dest.setPlaceholderText("Same folder as the original")
        self.dest.editingFinished.connect(
            lambda: self.settings.setValue("dest", self.dest.text().strip()))
        self.browse_btn = browse = QPushButton("Browse")
        browse.setObjectName("ghost")
        browse.setCursor(Qt.CursorShape.PointingHandCursor)
        browse.clicked.connect(self.pick_dest)
        self.suffix = QLineEdit(self.settings.value("suffix", "-min"))
        self.suffix.setFixedWidth(110)
        self.suffix.setFont(QFont(self.mono, 10))
        self.suffix.setToolTip("Added to each file name, e.g. clip-min.mp4")
        drow = QHBoxLayout()
        drow.setSpacing(8)
        drow.addWidget(self.dest, 1)
        drow.addWidget(self.suffix)
        drow.addWidget(browse)
        root.addLayout(drow)
        root.addSpacing(16)

        # totals ------------------------------------------------------------
        self.total = QLabel("")
        self.total.setObjectName("total")
        self.total.setFont(QFont(self.mono, 15, QFont.Weight.DemiBold))
        root.addWidget(self.total)
        self.status = QLabel("")
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        root.addSpacing(10)

        self.track = QFrame()
        self.track.setObjectName("track")
        self.track.setFixedHeight(4)
        self.fill = QFrame(self.track)
        self.fill.setObjectName("fill")
        self.fill.setGeometry(0, 0, 0, 4)
        root.addWidget(self.track)
        root.addSpacing(12)

        self.go = QPushButton("Compress")
        self.go.setObjectName("primary")
        self.go.setMinimumHeight(46)
        self.go.setEnabled(False)
        self.go.setCursor(Qt.CursorShape.PointingHandCursor)
        self.go.clicked.connect(self.start)
        root.addWidget(self.go)
        root.addSpacing(10)

        foot = QHBoxLayout()
        self.clear_btn = QPushButton("Clear")
        self.preview_btn = QPushButton(f"Preview {PREVIEW_SECONDS}s")
        self.log_btn = QPushButton("Log")
        self.open_btn = QPushButton("Open folder")
        for b in (self.clear_btn, self.preview_btn, self.log_btn, self.open_btn):
            b.setObjectName("ghost")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_btn.clicked.connect(self.clear_queue)
        self.preview_btn.clicked.connect(self.make_preview)
        self.log_btn.clicked.connect(self.flip_log)
        self.open_btn.clicked.connect(self.open_folder)
        foot.addWidget(self.clear_btn)
        foot.addWidget(self.preview_btn)
        foot.addWidget(self.log_btn)
        foot.addStretch()
        foot.addWidget(self.open_btn)
        root.addLayout(foot)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setObjectName("log")
        self.log.setFont(QFont(self.mono, 9))
        self.log.setMaximumHeight(110)
        self.log.hide()
        root.addWidget(self.log)

        self.setAcceptDrops(True)
        self.apply_theme()
        self.format_changed()
        self.queue_changed()
        self.refresh_totals()

        self.add_shortcuts()

        if not self.has_ffmpeg:
            self.status.setText("ffmpeg not found. Install it with your package manager.")
        elif not self.pick_encoder():
            self.status.setText(
                "This ffmpeg build has no usable video encoder. "
                "Images, audio and PDFs will still work.")

    def stored_int(self, key, default, limit):
        try:
            v = int(self.settings.value(key, default))
        except (TypeError, ValueError):
            return default
        return v if 0 <= v < limit else default

    def settings_changed(self, *_):
        self.settings.setValue("strength", self.ruler.index)
        self.settings.setValue("mode", self.mode())
        self.settings.setValue("format", self.fmt())
        self.settings.setValue("resize", max(0, self.resize_group.checkedId()))
        self.settings.setValue("strip", "true" if self.strip.isChecked() else "false")
        self.refresh_note()
        self.refresh_plans()

    def plan_for(self, job):
        """Short preview of what a queued file will turn into."""
        if self.fmt() == LOSSLESS:
            return "lossless"
        bits = []
        ext = self.out_ext(job)
        if ext != job.path.suffix.lower():
            bits.append(f"→ {ext.lstrip('.').upper()}")
        px = self.resize_px()
        if px and job.kind in ("video", "image"):
            bits.append(RESIZE[RESIZE_PX.index(px)])
        return " · ".join(bits)

    def refresh_plans(self):
        for job in self.jobs:
            if job.row:
                job.row.plan = self.plan_for(job)
                if job.status == "waiting":
                    job.row.refresh()

    def set_running(self, running):
        """Settings are read as each job starts, so freeze them mid-batch."""
        lossless = self.fmt() == LOSSLESS
        for group in (self.mode_group, self.fmt_group, self.resize_group):
            for b in group.buttons():
                b.setEnabled(not running)
        if not running:
            for b in self.mode_group.buttons():
                b.setEnabled(not lossless)
        for w in (self.ruler, self.size_row, self.strip, self.dest, self.suffix,
                  self.browse_btn, self.clear_btn, self.preview_btn):
            w.setEnabled(not running)
        self.go.setText("Stop" if running else "Compress")

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self.drop._restyle(True)

    def dragLeaveEvent(self, e):
        self.drop._restyle(False)

    def dropEvent(self, e):
        self.drop._restyle(False)
        paths = [Path(u.toLocalFile()) for u in e.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.add_files(paths)
        e.acceptProposedAction()

    # ------------------------------------------------------------ chrome ---
    def add_shortcuts(self):
        for keys, slot in (("Ctrl+Q", self.close),
                           ("Ctrl+W", self.close),
                           ("Ctrl+M", self.showMinimized),
                           ("Ctrl+D", self.toggle_theme),
                           ("Ctrl+O", self.browse_files),
                           ("Ctrl+L", self.flip_log),
                           ("Ctrl+Return", self.start)):
            QShortcut(QKeySequence(keys), self).activated.connect(slot)

    def browse_files(self):
        chosen, _ = QFileDialog.getOpenFileNames(self, "Add files")
        if chosen:
            self.add_files([Path(c) for c in chosen])

    def closeEvent(self, event):
        """Leave nothing running and nothing half-written behind."""
        self.settings.setValue("geometry", self.saveGeometry())
        self.stopping = True
        # Killing these fires finished() on a window that is on its way out.
        # Silence them first so no slot tries to repaint or open a file.
        for proc in list(self.workers):
            proc.blockSignals(True)
            proc.kill()
            proc.waitForFinished(2000)
        if self.preview_proc:
            self.preview_proc.blockSignals(True)
            self.preview_proc.kill()
            self.preview_proc.waitForFinished(2000)
        for job in self.jobs:
            if job.status == "running" and job.out and job.out.exists():
                job.out.unlink(missing_ok=True)
            if job.best:
                job.best.unlink(missing_ok=True)
        if self.preview_dir:
            shutil.rmtree(self.preview_dir, ignore_errors=True)
        super().closeEvent(event)

    def rule(self):
        f = QFrame()
        f.setObjectName("rule")
        f.setFixedHeight(1)
        return f

    def section(self, text):
        lbl = QLabel(text)
        lbl.setObjectName("section")
        return lbl

    def tokens_for(self, name):
        return THEMES.get(name, THEMES["Light"])

    def toggle_theme(self):
        self.theme_name = "Light" if self.theme_name == "Dark" else "Dark"
        self.settings.setValue("theme", self.theme_name)
        self.t = THEMES[self.theme_name]
        self.sync_theme_button()
        self.apply_theme()

    def sync_theme_button(self):
        dark = self.theme_name == "Dark"
        self.theme_btn.setText("☾  Dark" if dark else "☀  Light")
        self.theme_btn.setChecked(dark)
        self.theme_btn.setToolTip(
            "Switch to %s mode  (Ctrl+D)" % ("light" if dark else "dark"))

    def apply_theme(self):
        self.setStyleSheet(self.qss())
        self.ruler.set_tokens(self.t)
        self.mark.set_tokens(self.t)
        for job in self.jobs:
            if job.row:
                job.row.set_tokens(self.t)
        self.sync_progress()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.sync_progress()

    def sync_progress(self, pct=None):
        if pct is not None:
            self._pct = pct
        self.fill.setGeometry(0, 0, int(self.track.width() * self._pct / 100), 4)

    # ----------------------------------------------------------- settings ---
    def mode(self):
        return max(0, self.mode_group.checkedId())

    def fmt(self):
        return max(0, self.fmt_group.checkedId())

    def target_bytes(self):
        try:
            mb = float(self.size_field.text().strip())
            return int(mb * 1024 * 1024) if mb > 0 else None
        except (ValueError, TypeError):
            return None

    def mode_changed(self, index):
        self.ruler.setVisible(index == BY_STRENGTH)
        self.size_row.setVisible(index == BY_SIZE)
        self.settings_changed()

    def format_changed(self, _index=0):
        # Lossless has no quality dial and no size to aim at.
        lossless = self.fmt() == LOSSLESS
        if lossless and self.mode() == BY_SIZE:
            self.mode_group.button(BY_STRENGTH).setChecked(True)
        for i in (BY_STRENGTH, BY_SIZE):
            self.mode_group.button(i).setEnabled(not lossless)
        self.ruler.setVisible(not lossless and self.mode() == BY_STRENGTH)
        self.size_row.setVisible(not lossless and self.mode() == BY_SIZE)
        self.resize_row.setVisible(not lossless)
        self.settings_changed()

    def refresh_note(self, *_):
        if self.fmt() == LOSSLESS:
            tools = [
                "png " + ("oxipng" if OXIPNG else "needs oxipng"),
                "jpeg " + ("jpegtran" if JPEGTRAN else "needs jpegtran"),
                "pdf " + ("qpdf" if QPDF else "ghostscript" if GHOSTSCRIPT else "none"),
            ]
            self.settings_note.setText("Identical output   ·   " + "   ·   ".join(tools))
            return
        if self.mode() == BY_SIZE:
            tb = self.target_bytes()
            self.settings_note.setText(
                f"Aiming for {human(tb)} per file" if tb else "Enter a size in MB")
            return
        i = self.ruler.index
        modern = self.fmt() == MODERN
        vid = f"AV1 crf {AV1_CQ[i]}" if modern else f"H.265 crf {HEVC_CQ[i]}"
        img = f"WebP q{WEBP_Q[i]}" if modern else f"JPEG q{JPEG_Q[i]}"
        snd = f"Opus {OPUS_KBPS[i]}k" if modern else f"MP3 {MP3_KBPS[i]}k"
        self.settings_note.setText(
            f"{vid}   ·   {img}   ·   {snd}   ·   PDF {PDF_SET[i].lstrip('/')}")
        if self.resize_px():
            self.settings_note.setText(self.settings_note.text().replace(
                "   ·   PDF", f"   ·   {RESIZE[self.resize_group.checkedId()]}   ·   PDF"))

    def pick_dest(self):
        d = QFileDialog.getExistingDirectory(self, "Save compressed files to",
                                             self.dest.text() or str(Path.home()))
        if d:
            self.dest.setText(d)
            self.settings.setValue("dest", d)

    def open_folder(self):
        target = self.dest.text().strip()
        if not target and self.jobs:
            target = str(self.jobs[0].path.parent)
        if target:
            QDesktopServices.openUrl(QUrl.fromLocalFile(target))

    def flip_log(self):
        vis = self.log.isVisible()
        self.log.setVisible(not vis)
        self.log_btn.setText("Log" if vis else "Hide log")

    def note(self, line):
        self.log_lines.append(line)
        self.log.setPlainText("\n".join(self.log_lines[-300:]))

    # -------------------------------------------------------------- queue ---
    def add_files(self, paths):
        skipped = 0
        for p in paths:
            if p.is_dir():
                self.add_files([c for c in sorted(p.iterdir()) if c.is_file()])
                continue
            if not p.exists():
                continue
            kind = kind_of(p)
            if not kind:
                skipped += 1
                continue
            if any(j.path == p for j in self.jobs):
                continue
            job = Job(path=p, kind=kind, before=p.stat().st_size)
            if kind in ("video", "audio"):
                job.duration, job.has_audio = probe_media(p)
            row = QueueRow(job, self.t, self.mono)
            row.remove.connect(self.drop_job)
            row.act.connect(self.row_action)
            job.row = row
            row.plan = self.plan_for(job)
            row.refresh()
            self.queue_lay.insertWidget(self.queue_lay.count() - 1, row)
            self.jobs.append(job)

        self.queue_changed()
        if skipped:
            self.status.setText(f"{skipped} file(s) skipped — unsupported type.")
        self.refresh_totals()

    def cancel_job(self, job):
        for proc, running in list(self.workers.items()):
            if running is job:
                job.cancelled = True
                proc.kill()
                return

    def row_action(self, job, what):
        if what == "cancel":
            self.cancel_job(job)
        elif what == "open":
            if job.out and job.out.exists():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(job.out)))
        elif what == "reveal":
            folder = job.out.parent if job.out else job.path.parent
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
        elif what == "retry":
            if job.out and job.out.exists():
                job.out.unlink(missing_ok=True)
            job.status = "waiting"
            job.note = ""
            job.after = None
            job.frac = 0.0
            job.fell_back = False
            job.lo = job.hi = job.probe_q = None
            job.tries = 0
            job.out = None
            if job.best:
                job.best.unlink(missing_ok=True)
                job.best = None
            job.row.refresh()
            job.stage = job.error = ""
            if self.run_started is None:
                self.run_started = time.monotonic()
            self.set_running(True)
            self.pump()
        elif what == "remove":
            self.drop_job(job)

    def drop_job(self, job):
        if job in self.workers.values():
            self.cancel_job(job)
            return
        if job.row:
            job.row.setParent(None)
            job.row.deleteLater()
        if job in self.jobs:
            self.jobs.remove(job)
        self.queue_changed()
        self.refresh_totals()

    def queue_changed(self):
        has = bool(self.jobs)
        self.scroll.setVisible(has)
        self.drop.set_compact(has)
        self.go.setEnabled(has and self.has_ffmpeg)

    def clear_queue(self):
        if self.workers:
            return
        for job in list(self.jobs):
            self.drop_job(job)
        self.status.setText("")
        self.sync_progress(0)

    def refresh_totals(self):
        done = [j for j in self.jobs if j.status == "done"]
        if not done:
            pending = sum(j.before for j in self.jobs)
            self.total.setText(f"{len(self.jobs)} queued   {human(pending)}"
                               if self.jobs else "")
            return
        before = sum(j.before for j in done)
        after = sum(j.after for j in done)
        saved = before - after
        self.total.setText(f"Saved {human(saved)} of {human(before)}"
                           f"   ({saved / before * 100:.0f}%)")

    # ------------------------------------------------------------- paths ---
    def out_ext(self, job):
        f = self.fmt()
        ext = job.path.suffix.lower()
        if f == LOSSLESS:
            if job.kind == "audio" and ext == ".wav":
                return ".flac"
            if job.kind == "video" and ext in {".avi", ".wmv", ".flv"}:
                return ".mp4"
            return ext
        if job.kind == "video":
            return ".mp4" if f == MODERN else ext
        if job.kind == "image":
            if f == MODERN:
                return ".webp"
            # A size target needs a dial to turn. PNG, BMP and TIFF are
            # lossless, so aim at JPEG rather than search a knob that does
            # nothing to the file size.
            if self.mode() == BY_SIZE and ext not in SIZEABLE_IMAGE_EXT:
                return ".jpg"
            return ".jpg" if ext in {".jpg", ".jpeg"} else ext
        if job.kind == "audio":
            return ".opus" if f == MODERN else ".mp3"
        return ext

    def out_path_for(self, job):
        folder = (Path(self.dest.text().strip()) if self.dest.text().strip()
                  else job.path.parent)
        stem = f"{job.path.stem}{self.suffix.text()}"
        ext = self.out_ext(job)
        out = folder / f"{stem}{ext}"
        # Results from an earlier run, and unrelated files that happen to
        # share the name, both deserve to survive.
        n = 2
        while out.exists() and out.resolve() != job.path.resolve():
            out = folder / f"{stem}-{n}{ext}"
            n += 1
        return out

    # ------------------------------------------------------------ command ---
    def meta_args(self):
        return ["-map_metadata", "-1"] if self.strip.isChecked() else []

    def resize_px(self):
        return RESIZE_PX[max(0, self.resize_group.checkedId())]

    def scale_args(self, job):
        px = self.resize_px()
        if not px or job.kind not in ("video", "image"):
            return []
        return ["-vf", video_scale(px) if job.kind == "video" else image_scale(px)]

    def ff_head(self):
        return [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostats",
                "-progress", "pipe:1", "-y"]

    def video_bitrate(self, job, target):
        """Bits per second for the video stream to land near a target size."""
        if not job.duration:
            return None
        audio_bps = 128_000 if job.has_audio else 0
        total = target * 8 * 0.97          # 3% headroom for container overhead
        return int(max(80_000, (total / job.duration) - audio_bps))

    def pick_encoder(self, skip=()):
        """First encoder in the chain this ffmpeg build actually provides."""
        chain = VIDEO_CHAIN[KEEP if self.fmt() == KEEP else MODERN]
        for name in chain:
            if name in skip:
                continue
            # An empty probe means we could not ask; stay optimistic.
            if not ENCODERS or name in ENCODERS:
                return name
        return None

    def build_command(self, job, out, encoder=None):
        if self.fmt() == LOSSLESS:
            return self.lossless_command(job, out)
        if self.mode() == BY_SIZE:
            return self.target_command(job, out, encoder)
        return self.strength_command(job, out, encoder)

    def video_quality_args(self, enc, q):
        if enc.endswith("_nvenc"):
            pix = "yuv420p" if enc == "h264_nvenc" else "p010le"
            return ["-c:v", enc, "-preset", "p6", "-tune", "hq",
                    "-rc", "vbr", "-cq", str(q), "-b:v", "0", "-pix_fmt", pix]
        if enc == "libsvtav1":
            return ["-c:v", enc, "-crf", str(q), "-preset", "6",
                    "-pix_fmt", "yuv420p10le"]
        if enc == "libaom-av1":
            return ["-c:v", enc, "-crf", str(q), "-b:v", "0",
                    "-cpu-used", "6", "-pix_fmt", "yuv420p"]
        return ["-c:v", enc, "-crf", str(q), "-preset", "medium"]

    def image_quality_args(self, out, q):
        if out.suffix == ".webp":
            return ["-c:v", "libwebp", "-quality", str(q),
                    "-preset", "picture", "-compression_level", "6"]
        if out.suffix in {".jpg", ".jpeg"}:
            return ["-q:v", str(q)]
        # Lossless containers (PNG, BMP, TIFF) have no quality dial, so q
        # does not apply here. Ask zlib for its best effort instead.
        return ["-compression_level", "100"]

    def strength_command(self, job, out, encoder=None):
        i = self.ruler.index
        modern = self.fmt() == MODERN

        if job.kind == "pdf":
            return self.pdf_command(job, out, PDF_SET[i])

        cmd = self.ff_head()

        if job.kind == "video":
            enc = encoder or self.pick_encoder()
            if not enc:
                return None
            q = AV1_CQ[i] if "av1" in enc else HEVC_CQ[i]
            if enc in GPU_ENCODERS:
                cmd += ["-hwaccel", "cuda"]
            cmd += ["-i", str(job.path), "-map", "0:v:0", "-map", "0:a?"]
            if out.suffix == ".mkv":
                cmd += ["-map", "0:s?", "-c:s", "copy"]
            cmd += self.meta_args() + self.scale_args(job)
            cmd += self.video_quality_args(enc, q)
            cmd += ["-c:a", "libopus" if out.suffix == ".mkv" else "aac",
                    "-b:a", "128k"]
            if out.suffix == ".mp4":
                cmd += ["-movflags", "+faststart"]
                if enc in ("hevc_nvenc", "libx265"):
                    cmd += ["-tag:v", "hvc1"]
            cmd.append(str(out))
            return cmd

        if job.kind == "image":
            cmd += ["-i", str(job.path), *self.meta_args(), *self.scale_args(job)]
            cmd += self.image_quality_args(out, WEBP_Q[i] if modern else JPEG_Q[i])
            cmd += ["-frames:v", "1", str(out)]
            return cmd

        cmd += ["-i", str(job.path), "-vn", *self.meta_args()]
        if modern:
            cmd += ["-c:a", "libopus", "-b:a", f"{OPUS_KBPS[i]}k"]
        else:
            cmd += ["-c:a", "libmp3lame", "-b:a", f"{MP3_KBPS[i]}k"]
        cmd.append(str(out))
        return cmd

    def target_command(self, job, out, encoder=None):
        target = self.target_bytes()

        if job.kind == "pdf":
            # Ghostscript has no size target, so use the strongest preset and
            # report whatever it produces.
            return self.pdf_command(job, out, "/screen")

        cmd = self.ff_head()

        if job.kind == "video":
            bps = self.video_bitrate(job, target)
            if not bps:
                return None
            enc = encoder or self.pick_encoder()
            if not enc:
                return None
            if enc in GPU_ENCODERS:
                cmd += ["-hwaccel", "cuda"]
            cmd += ["-i", str(job.path), "-map", "0:v:0", "-map", "0:a?"]
            if out.suffix == ".mkv":
                cmd += ["-map", "0:s?", "-c:s", "copy"]
            cmd += self.meta_args() + self.scale_args(job)
            if enc in GPU_ENCODERS:
                pix = "yuv420p" if enc == "h264_nvenc" else "p010le"
                cmd += ["-c:v", enc, "-preset", "p6", "-rc", "vbr",
                        "-b:v", str(bps), "-maxrate", str(int(bps * 1.5)),
                        "-bufsize", str(bps * 2), "-multipass", "fullres",
                        "-pix_fmt", pix]
            elif enc == "libsvtav1":
                cmd += ["-c:v", enc, "-b:v", str(bps), "-preset", "6"]
            elif enc == "libaom-av1":
                cmd += ["-c:v", enc, "-b:v", str(bps), "-cpu-used", "6"]
            else:
                cmd += ["-c:v", enc, "-b:v", str(bps), "-preset", "medium"]
            cmd += ["-c:a", "aac", "-b:a", "128k"]
            if out.suffix == ".mp4":
                cmd += ["-movflags", "+faststart"]
                if enc in ("hevc_nvenc", "libx265"):
                    cmd += ["-tag:v", "hvc1"]
            cmd.append(str(out))
            return cmd

        if job.kind == "audio":
            if not job.duration:
                return None
            bps = int(max(32_000, min(320_000, target * 8 * 0.98 / job.duration)))
            cmd += ["-i", str(job.path), "-vn", *self.meta_args()]
            codec = "libopus" if self.fmt() == MODERN else "libmp3lame"
            cmd += ["-c:a", codec, "-b:a", str(bps), str(out)]
            return cmd

        # Images have no bitrate to solve for, so search the quality scale.
        # Only formats with a real quality dial can be searched.
        if out.suffix not in SIZEABLE_IMAGE_EXT:
            return None
        if job.lo is None:
            if out.suffix in {".jpg", ".jpeg"}:
                job.lo, job.hi = 2, 31      # lower number = better quality
            else:
                job.lo, job.hi = 1, 100     # higher number = better quality
        job.probe_q = (job.lo + job.hi) // 2
        cmd += ["-i", str(job.path), *self.meta_args(), *self.scale_args(job)]
        cmd += self.image_quality_args(out, job.probe_q)
        cmd += ["-frames:v", "1", str(out)]
        return cmd

    def lossless_command(self, job, out):
        strip = self.strip.isChecked()

        if job.kind == "image":
            ext = job.path.suffix.lower()
            if ext == ".png" and OXIPNG:
                return [OXIPNG, "-o", "4", "--quiet",
                        "--strip", "safe" if strip else "none",
                        "--out", str(out), str(job.path)]
            if ext in {".jpg", ".jpeg"} and JPEGTRAN:
                return [JPEGTRAN, "-optimize", "-progressive",
                        "-copy", "none" if strip else "all",
                        "-outfile", str(out), str(job.path)]
            return None

        if job.kind == "pdf":
            if QPDF:
                return [QPDF, "--linearize", "--object-streams=generate",
                        "--compress-streams=y", str(job.path), str(out)]
            if GHOSTSCRIPT:
                return self.pdf_command(job, out, "/prepress")
            return None

        if job.kind == "audio":
            cmd = self.ff_head() + ["-i", str(job.path), "-vn"] + self.meta_args()
            if out.suffix == ".flac":
                cmd += ["-c:a", "flac", "-compression_level", "12"]
            else:
                cmd += ["-c:a", "copy"]
            cmd.append(str(out))
            return cmd

        # Video: remux only. No re-encode, so every pixel survives intact.
        cmd = self.ff_head() + ["-i", str(job.path), "-map", "0", "-c", "copy"]
        cmd += self.meta_args()
        if out.suffix == ".mp4":
            cmd += ["-movflags", "+faststart"]
        cmd.append(str(out))
        return cmd

    def pdf_command(self, job, out, setting):
        if not GHOSTSCRIPT:
            return None
        return [GHOSTSCRIPT, "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.7",
                f"-dPDFSETTINGS={setting}", "-dNOPAUSE", "-dQUIET", "-dBATCH",
                "-dDetectDuplicateImages=true",
                f"-sOutputFile={out}", str(job.path)]

    # ----------------------------------------------------------- preview ---
    def make_preview(self):
        if self.preview_proc:
            return
        if self.fmt() == LOSSLESS:
            self.status.setText("Lossless changes nothing visually — "
                                "there is nothing to preview.")
            return
        videos = [j for j in self.jobs if j.kind == "video" and j.duration]
        if not videos:
            self.status.setText("Add a video to the queue to preview settings.")
            return

        job = videos[0]
        self.preview_job = job
        start = max(0, job.duration / 2 - PREVIEW_SECONDS / 2)
        ext = self.out_ext(job)
        if self.preview_dir is None:
            self.preview_dir = Path(tempfile.mkdtemp(prefix="smallfry-"))
        self.preview_out = self.preview_dir / f"preview{ext}"

        base = self.build_command(job, self.preview_out)
        if not base:
            self.status.setText("Cannot preview this file with these settings.")
            return
        # Splice the trim arguments in just before the input.
        idx = base.index("-i")
        cmd = (base[:idx] + ["-ss", f"{start:.2f}", "-t", str(PREVIEW_SECONDS)]
               + base[idx:])

        self.note(" ".join(cmd))
        self.preview_proc = QProcess(self)
        self.preview_proc.setProcessChannelMode(
            QProcess.ProcessChannelMode.MergedChannels)
        self.preview_proc.finished.connect(self.preview_done)
        self.preview_proc.start(cmd[0], cmd[1:])
        self.preview_btn.setText("Rendering…")
        self.status.setText(f"Encoding a {PREVIEW_SECONDS}s sample from the "
                            f"middle of {job.path.name}")

    def preview_done(self, code, _status):
        raw = bytes(self.preview_proc.readAll()).decode("utf-8", "replace")
        for line in raw.splitlines():
            if line.strip() and "=" not in line:
                self.note(line.strip())
        self.preview_proc = None
        self.preview_btn.setText(f"Preview {PREVIEW_SECONDS}s")

        out, job = self.preview_out, self.preview_job
        if code != 0 or not out or not out.exists():
            self.status.setText("Preview failed. Open the log for the reason.")
            return

        sample = out.stat().st_size
        projected = sample * (job.duration / PREVIEW_SECONDS)
        pct = (job.before - projected) / job.before * 100
        self.status.setText(
            f"Sample {human(sample)}   ·   the full file would be about "
            f"{human(projected)}, {pct:.0f}% smaller   ·   opening it now")
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(out)))

    # --------------------------------------------------------------- run ---
    def start(self):
        if self.workers:
            self.stopping = True
            self.status.setText("Stopping…")
            for proc in list(self.workers):
                proc.kill()
            return

        if self.mode() == BY_SIZE and self.fmt() != LOSSLESS and not self.target_bytes():
            self.status.setText("Enter a target size in MB first.")
            return

        for j in self.jobs:
            j.status = "waiting"
            j.after = None
            j.note = ""
            j.frac = 0.0
            j.fell_back = False
            j.stage = j.error = ""
            j.lo = j.hi = j.probe_q = None
            j.tries = 0
            j.out = None
            if j.best:
                j.best.unlink(missing_ok=True)
            j.best = None
            j.row.refresh()

        self.settings.setValue("suffix", self.suffix.text())
        self.settings.setValue("target_mb", self.size_field.text())
        self.settings.setValue("dest", self.dest.text().strip())
        self.stopping = False
        self.run_started = time.monotonic()
        self.set_running(True)
        self.status.setText("")
        self.pump()

    def lane_for(self, job):
        if job.kind != "video" or self.fmt() == LOSSLESS:
            return "light"
        return "gpu" if self.pick_encoder() in GPU_ENCODERS else "video"

    def slots_free(self, lane):
        used = sum(1 for j in self.workers.values() if j.lane == lane)
        return used < self.slots[lane]

    def pump(self):
        """Start as many waiting jobs as there are free slots."""
        if self.stopping:
            if not self.workers:
                self.finish_all()
            return

        for job in [j for j in self.jobs if j.status == "waiting"]:
            lane = self.lane_for(job)
            if self.slots_free(lane):
                self.launch(job, lane)

        if not self.workers and not any(j.status == "waiting" for j in self.jobs):
            self.finish_all()

    def launch(self, job, lane, encoder=None):
        out = job.out or self.out_path_for(job)

        if out.resolve() == job.path.resolve():
            return self.reject(job, "same path")

        target = self.target_bytes()
        if (self.mode() == BY_SIZE and self.fmt() != LOSSLESS and target
                and job.before <= target and not job.tries):
            job.status = "skipped"
            job.note = "already under target"
            job.row.refresh()
            return self.update_overall()

        try:
            out.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.note(str(e))
            return self.reject(job, "cannot write")

        cmd = self.build_command(job, out, encoder)
        if not cmd:
            if self.fmt() == LOSSLESS:
                reason = "no lossless tool"
            elif job.kind == "pdf":
                reason = "needs ghostscript"
            elif job.kind == "video" and not self.pick_encoder():
                reason = "no video encoder in ffmpeg"
            elif job.kind == "image":
                reason = "no size dial for this format"
            else:
                reason = "needs a duration"
            return self.reject(job, reason)

        job.out = out
        job.lane = lane
        job.error = ""
        job.status = "running"
        job.row.refresh()
        self.note(" ".join(cmd))

        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        proc.readyRead.connect(lambda p=proc: self.on_output(p))
        proc.finished.connect(lambda c, s, p=proc: self.on_finished(p, c))
        self.workers[proc] = job
        proc.start(cmd[0], cmd[1:])
        self.update_status()

    def reject(self, job, note):
        job.status = "failed"
        job.note = note
        job.error = REJECT_HELP.get(note, "")
        job.row.refresh()
        self.update_overall()

    def on_output(self, proc):
        job = self.workers.get(proc)
        if not job:
            return
        text = bytes(proc.readAll()).decode("utf-8", "replace")
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("out_time_us=") and job.duration:
                try:
                    secs = int(line.split("=", 1)[1]) / 1_000_000
                    job.frac = min(1.0, secs / job.duration)
                    job.row.meter.set_progress(job.frac)
                    self.update_overall()
                except (ValueError, ZeroDivisionError):
                    pass
            elif line and "=" not in line:
                job.error = line
                self.note(line)

    def on_finished(self, proc, code):
        job = self.workers.pop(proc, None)
        proc.deleteLater()
        if not job:
            return

        if job.cancelled:
            job.cancelled = False
            job.status = "failed"
            job.note = "cancelled"
            job.frac = 0.0
            if job.out and job.out.exists():
                job.out.unlink(missing_ok=True)
            job.row.refresh()
            return self.after_job()

        if self.stopping:
            job.status = "waiting"
            job.note = ""
            job.frac = 0.0
            job.row.refresh()
            if job.out and job.out.exists():
                job.out.unlink(missing_ok=True)
            self.pump()
            return

        if job.stage == "meta":
            job.stage = ""
            if code != 0:
                self.note(f"{job.path.name}: tags not copied ({job.error or 'exiftool failed'})")
            return self.settle(job)

        out = job.out
        ok = code == 0 and out and out.exists() and out.stat().st_size > 0

        # The GPU encoder refused the file — try the same job on the CPU.
        if not ok and job.lane == "gpu" and not job.fell_back:
            cpu_enc = self.pick_encoder(skip=GPU_ENCODERS)
            job.fell_back = True
            job.frac = 0.0
            if out and out.exists():
                out.unlink(missing_ok=True)
            if cpu_enc:
                self.note(f"{job.path.name}: GPU encode failed, retrying "
                          f"on CPU with {cpu_enc}")
                self.launch(job, "video", cpu_enc)
                return
            self.note(f"{job.path.name}: GPU encode failed and this ffmpeg "
                      f"build has no CPU encoder for that format")
            job.status = "failed"
            job.note = "no CPU encoder"
            job.row.refresh()
            return self.after_job()

        if not ok:
            if out and out.exists():
                out.unlink(missing_ok=True)
            job.status = "failed"
            job.note = "failed"
            job.frac = 0.0
            job.row.refresh()
            return self.after_job()

        # Image size search: narrow the quality range and go again.
        if (job.kind == "image" and self.mode() == BY_SIZE
                and self.fmt() != LOSSLESS and self.continue_search(job)):
            return

        if self.wants_tags(job):
            return self.copy_tags(job)
        self.settle(job)

    def wants_tags(self, job):
        """Photos keep their date, camera and location unless asked not to.
        ffmpeg drops EXIF, so exiftool copies it across when it's installed.
        Size targets skip this: the tags would push the file past the target."""
        return (job.kind == "image" and EXIFTOOL and not self.strip.isChecked()
                and self.fmt() != LOSSLESS and self.mode() == BY_STRENGTH
                and job.out.suffix in {".jpg", ".jpeg", ".webp"})

    def copy_tags(self, job):
        job.stage = "meta"
        job.row.refresh()
        # The pixels are already upright, so leave Orientation behind, and
        # skip embedded previews that would only bloat the result.
        cmd = [EXIFTOOL, "-q", "-q", "-overwrite_original", "-TagsFromFile",
               str(job.path), "-all:all", "--Orientation", "--ThumbnailImage",
               "--PreviewImage", "--MakerNotes", str(job.out)]
        self.note(" ".join(cmd))
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        proc.readyRead.connect(lambda p=proc: self.on_output(p))
        proc.finished.connect(lambda c, s, p=proc: self.on_finished(p, c))
        self.workers[proc] = job
        proc.start(cmd[0], cmd[1:])

    def settle(self, job):
        out = job.out
        size = out.stat().st_size
        if size >= job.before:
            out.unlink(missing_ok=True)
            job.status = "skipped"
            job.note = "already small"
        else:
            job.after = size
            job.status = "done"
        job.frac = 1.0
        job.row.refresh()
        self.after_job()

    def continue_search(self, job):
        """Binary search image quality. True if another attempt was started."""
        target = self.target_bytes()
        out = job.out
        size = out.stat().st_size
        q = job.probe_q
        job.tries += 1
        higher_is_better = out.suffix not in {".jpg", ".jpeg"}

        if size <= target:
            # It fits. Keep this result, then try for better quality.
            if job.best:
                job.best.unlink(missing_ok=True)
            job.best = out.with_name(out.name + ".best")
            shutil.copy2(out, job.best)
            if higher_is_better:
                job.lo = q + 1
            else:
                job.hi = q - 1
        else:
            if higher_is_better:
                job.hi = q - 1
            else:
                job.lo = q + 1

        if job.tries < MAX_SEARCH_TRIES and job.lo <= job.hi:
            job.status = "waiting"
            job.note = f"tuning {job.tries}/{MAX_SEARCH_TRIES}"
            job.row.refresh()
            self.launch(job, "light")
            return True

        job.note = ""
        if job.best and job.best.exists():
            job.best.replace(out)
            job.best = None
        elif size > target:
            job.note = "missed target"
        return False

    def after_job(self):
        self.refresh_totals()
        self.update_overall()
        self.update_status()
        self.pump()

    def update_overall(self):
        total = len(self.jobs) or 1
        settled = sum(1 for j in self.jobs
                      if j.status in ("done", "skipped", "failed"))
        running = sum(j.frac for j in self.jobs if j.status == "running")
        self.sync_progress(int(min(100, (settled + running) / total * 100)))
        self.update_status()

    def eta_text(self):
        """Rough time remaining, once there is enough evidence to guess."""
        if not self.run_started:
            return ""
        total = len(self.jobs) or 1
        settled = sum(1 for j in self.jobs
                      if j.status in ("done", "skipped", "failed"))
        running = sum(j.frac for j in self.jobs if j.status == "running")
        progress = (settled + running) / total
        elapsed = time.monotonic() - self.run_started
        if progress <= 0.02 or elapsed < 5:
            return ""
        left = elapsed / progress - elapsed
        return f"about {duration(left)} left"

    def update_status(self):
        if not self.workers:
            return
        names = [j.path.name for j in self.workers.values()]
        left = sum(1 for j in self.jobs if j.status == "waiting")
        head = f"{len(names)} running"
        if left:
            head += f", {left} waiting"
        tail = ", ".join(names[:2]) + ("…" if len(names) > 2 else "")
        parts = [head, tail, self.eta_text()]
        self.status.setText("   ·   ".join(x for x in parts if x))

    def finish_all(self):
        took = time.monotonic() - self.run_started if self.run_started else 0
        self.set_running(False)
        self.run_started = None
        if self.stopping:
            self.stopping = False
            self.status.setText("Stopped. Partial files were removed.")
            self.update_overall()
            return

        done = [j for j in self.jobs if j.status == "done"]
        failed = [j for j in self.jobs if j.status == "failed"]
        fell_back = [j for j in done if j.fell_back]

        self.sync_progress(100)
        bits = [f"{len(done)} compressed in {duration(took)}"]
        if failed:
            bits.append(f"{len(failed)} failed (hover for why)")
        if fell_back:
            bits.append(f"{len(fell_back)} fell back to CPU")
        self.status.setText("   ·   ".join(bits) + ".   Originals untouched.")
        self.refresh_totals()
        if not self.isActiveWindow():
            QApplication.alert(self)
            self.notify(bits[0].capitalize(), self.total.text())

    def notify(self, title, body):
        if NOTIFY:
            QProcess.startDetached(NOTIFY, ["--app-name", APP_NAME,
                                            "--icon", APP_ID, title, body])

    # ------------------------------------------------------------- style ---
    def qss(self):
        t = self.t
        return f"""
        #root {{ background: {t['paper']}; }}
        QWidget {{ color: {t['text']}; font-family: "{self.ui_font}"; font-size: 13px; }}

        #wordmark {{ font-size: 18px; font-weight: 700; letter-spacing: -0.2px; }}
        #rule {{ background: {t['line']}; border: none; }}
        #section {{ color: {t['dim']}; font-size: 12px; font-weight: 600; }}
        #hint {{ color: {t['dim']}; font-size: 11px; }}
        #status {{ color: {t['dim']}; font-size: 12px; }}
        #total {{ color: {t['text']}; }}

        #drop {{ background: {t['surface']}; border: 1px dashed {t['line']};
                 border-radius: 10px; }}
        #drop[hot="true"] {{ border: 1px solid {t['accent']}; background: {t['raised']}; }}
        #dropTitle {{ font-size: 15px; font-weight: 600; }}
        #dropSub {{ color: {t['dim']}; font-size: 12px; }}
        #dropFormats {{ color: {t['dim']}; font-size: 11px; letter-spacing: 0.3px; }}

        #scroll {{ background: transparent; }}
        #scroll > QWidget > QWidget {{ background: transparent; }}
        QScrollBar:vertical {{ background: transparent; width: 8px; margin: 0; }}
        QScrollBar::handle:vertical {{ background: {t['line']}; border-radius: 4px;
                                       min-height: 30px; }}
        QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
        QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

        #row {{ background: {t['surface']}; border: 1px solid {t['line']};
                border-radius: 8px; }}
        #rowName {{ font-size: 13px; font-weight: 500; }}
        #rowResult {{ color: {t['dim']}; }}
        #badge {{ background: {t['raised']}; color: {t['dim']};
                  border-radius: 4px; padding: 3px 0; }}
        #kill {{ background: transparent; border: none; color: {t['dim']};
                 font-size: 16px; border-radius: 12px; }}
        #kill:hover {{ background: {t['raised']}; color: {t['text']}; }}


        QLineEdit {{
            background: {t['surface']}; border: 1px solid {t['line']};
            border-radius: 7px; padding: 9px 11px;
            selection-background-color: {t['accent']};
            selection-color: {t['on_accent']};
        }}
        QLineEdit:focus {{ border: 1px solid {t['accent']}; }}
        QLineEdit:disabled {{ color: {t['dim']}; background: {t['raised']}; }}
        QToolTip {{ background: {t['surface']}; color: {t['text']};
                    border: 1px solid {t['line']}; padding: 6px; }}

        QPushButton[chip="true"] {{
            background: {t['surface']}; border: 1px solid {t['line']};
            border-radius: 14px; padding: 6px 14px; color: {t['dim']}; font-size: 12px;
        }}
        QPushButton[chip="true"]:hover {{ border-color: {t['dim']}; color: {t['text']}; }}
        QPushButton[chip="true"]:checked {{
            background: {t['accent']}; border-color: {t['accent']};
            color: {t['on_accent']}; font-weight: 600;
        }}
        QPushButton[chip="true"]:disabled {{ color: {t['line']};
                                             border-color: {t['line']}; }}

        #primary {{
            background: {t['accent']}; color: {t['on_accent']}; border: none;
            border-radius: 8px; font-size: 14px; font-weight: 600;
        }}
        #primary:hover {{ background: {t['accent_hi']}; }}
        #primary:disabled {{ background: {t['raised']}; color: {t['dim']}; }}

        #ghost {{
            background: transparent; border: 1px solid {t['line']};
            border-radius: 7px; padding: 8px 14px; color: {t['dim']};
        }}
        #ghost:hover {{ border-color: {t['dim']}; color: {t['text']}; }}
        #ghost:disabled {{ color: {t['line']}; }}

        #track {{ background: {t['line']}; border: none; border-radius: 2px; }}
        #fill {{ background: {t['accent']}; border: none; border-radius: 2px; }}

        #log {{ background: {t['log']}; border: 1px solid {t['line']};
                border-radius: 7px; color: {t['dim']}; padding: 8px; }}
        """


USAGE = f"""usage: {APP_ID} [FILE|FOLDER ...]

Drop files in, get smaller files out. Files and folders given on the
command line are added to the queue.

  --version   print the version and exit
  --help      show this help and exit"""


def main():
    if "--version" in sys.argv[1:]:
        print(f"{APP_NAME} {VERSION}")
        return
    if {"-h", "--help"} & set(sys.argv[1:]):
        print(USAGE)
        return
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setDesktopFileName(APP_ID)
    app.setStyle("Fusion")
    w = Smallfry()
    incoming = [Path(a).expanduser() for a in sys.argv[1:]
                if not a.startswith("-") and Path(a).expanduser().exists()]
    if incoming:
        w.add_files(incoming)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
