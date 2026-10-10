import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtWidgets import QApplication

import smallfry as sf

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")


@pytest.fixture(scope="session")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(app, tmp_path, monkeypatch):
    # Keep settings out of the real ~/.config.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    w = sf.Smallfry()
    w.dest.setText("")
    w.suffix.setText("-min")
    yield w
    w.close()


def make_job(path, kind=None, **kw):
    path = Path(path)
    return sf.Job(path=path, kind=kind or sf.kind_of(path),
                  before=path.stat().st_size if path.exists() else 1000, **kw)


def set_mode(w, mode, fmt):
    w.fmt_group.button(fmt).setChecked(True)
    w.format_changed()
    w.mode_group.button(mode).setChecked(True)
    w.mode_changed(mode)


# ----------------------------------------------------------------- helpers ---
def test_kind_of():
    assert sf.kind_of(Path("a.MP4")) == "video"
    assert sf.kind_of(Path("a.jpeg")) == "image"
    assert sf.kind_of(Path("a.flac")) == "audio"
    assert sf.kind_of(Path("a.pdf")) == "pdf"
    assert sf.kind_of(Path("a.txt")) is None


def test_human():
    assert sf.human(None) == "—"
    assert sf.human(512) == "512 B"
    assert sf.human(1536) == "1.5 KB"
    assert sf.human(5 * 1024 ** 3) == "5.0 GB"


def test_encoder_list_parsing(monkeypatch):
    sample = """Encoders:
 V..... = Video
 A..... = Audio
 ------
 V....D av1_nvenc            NVIDIA NVENC av1 encoder (codec av1)
 V..... libsvtav1            SVT-AV1 encoder (codec av1)
 A....D libopus              libopus Opus (codec opus)
"""

    class Done:
        stdout = sample

    monkeypatch.setattr(sf, "FFMPEG", "ffmpeg")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Done())
    assert sf.available_encoders() == {"av1_nvenc", "libsvtav1", "libopus"}


def test_encoder_probe_failure_is_harmless(monkeypatch):
    def boom(*a, **k):
        raise OSError("no ffmpeg")

    monkeypatch.setattr(sf, "FFMPEG", "ffmpeg")
    monkeypatch.setattr(subprocess, "run", boom)
    assert sf.available_encoders() == set()


# --------------------------------------------------------- encoder choice ---
def test_encoder_chain(win, monkeypatch):
    monkeypatch.setattr(sf, "ENCODERS", {"libx265", "libx264", "libsvtav1"})
    set_mode(win, sf.BY_STRENGTH, sf.MODERN)
    assert win.pick_encoder() == "libsvtav1"
    set_mode(win, sf.BY_STRENGTH, sf.KEEP)
    assert win.pick_encoder() == "libx265"
    assert win.pick_encoder(skip={"libx265"}) == "libx264"


def test_gpu_preferred_when_present(win, monkeypatch):
    monkeypatch.setattr(sf, "ENCODERS", {"av1_nvenc", "libsvtav1"})
    set_mode(win, sf.BY_STRENGTH, sf.MODERN)
    assert win.pick_encoder() == "av1_nvenc"
    assert win.pick_encoder(skip=sf.GPU_ENCODERS) == "libsvtav1"


def test_no_encoder(win, monkeypatch):
    monkeypatch.setattr(sf, "ENCODERS", {"libopus"})
    set_mode(win, sf.BY_STRENGTH, sf.MODERN)
    assert win.pick_encoder() is None


# ------------------------------------------------------------ output names ---
def test_output_extensions(win, tmp_path):
    jobs = {n: make_job(tmp_path / n) for n in
            ("a.png", "b.jpg", "c.wav", "d.mkv", "e.avi", "f.pdf")}

    set_mode(win, sf.BY_STRENGTH, sf.KEEP)
    assert [win.out_ext(j) for j in jobs.values()] == \
        [".png", ".jpg", ".mp3", ".mkv", ".avi", ".pdf"]

    set_mode(win, sf.BY_STRENGTH, sf.MODERN)
    assert [win.out_ext(j) for j in jobs.values()] == \
        [".webp", ".webp", ".opus", ".mp4", ".mp4", ".pdf"]

    set_mode(win, sf.BY_STRENGTH, sf.LOSSLESS)
    assert [win.out_ext(j) for j in jobs.values()] == \
        [".png", ".jpg", ".flac", ".mkv", ".mp4", ".pdf"]

    # A size target needs a quality dial, so PNG aims at JPEG.
    set_mode(win, sf.BY_SIZE, sf.KEEP)
    assert win.out_ext(jobs["a.png"]) == ".jpg"


def test_outputs_never_clobber(win, tmp_path):
    src = tmp_path / "photo.jpg"
    src.write_bytes(b"x" * 100)
    (tmp_path / "photo-min.jpg").write_bytes(b"old result")
    (tmp_path / "photo-min-2.jpg").write_bytes(b"older result")
    set_mode(win, sf.BY_STRENGTH, sf.KEEP)
    assert win.out_path_for(make_job(src)).name == "photo-min-3.jpg"


def test_destination_folder(win, tmp_path):
    src = tmp_path / "clip.wav"
    src.write_bytes(b"x")
    win.dest.setText(str(tmp_path / "out"))
    set_mode(win, sf.BY_STRENGTH, sf.MODERN)
    assert win.out_path_for(make_job(src)) == tmp_path / "out" / "clip-min.opus"


def test_refuses_to_write_over_the_original(win, tmp_path):
    src = tmp_path / "photo.jpg"
    src.write_bytes(b"original")
    win.suffix.setText("")
    set_mode(win, sf.BY_STRENGTH, sf.KEEP)
    job = make_job(src)
    win.jobs.append(job)
    job.row = sf.QueueRow(job, win.t, win.mono)
    win.launch(job, "light")
    assert job.status == "failed"
    assert job.note == "same path"
    assert src.read_bytes() == b"original"


# ---------------------------------------------------------------- commands ---
def test_video_strength_command(win, tmp_path, monkeypatch):
    monkeypatch.setattr(sf, "ENCODERS", {"libx265"})
    set_mode(win, sf.BY_STRENGTH, sf.KEEP)
    job = make_job(tmp_path / "a.mp4", duration=10.0, has_audio=True)
    cmd = win.build_command(job, tmp_path / "a-min.mp4")
    assert cmd[cmd.index("-c:v") + 1] == "libx265"
    assert cmd[cmd.index("-crf") + 1] == str(sf.HEVC_CQ[win.ruler.index])
    assert "-hwaccel" not in cmd
    assert ["-tag:v", "hvc1"] == cmd[cmd.index("-tag:v"):cmd.index("-tag:v") + 2]
    assert cmd[-1].endswith("a-min.mp4")


def test_mkv_keeps_subtitles(win, tmp_path, monkeypatch):
    monkeypatch.setattr(sf, "ENCODERS", {"libx265"})
    set_mode(win, sf.BY_STRENGTH, sf.KEEP)
    job = make_job(tmp_path / "a.mkv", duration=10.0)
    cmd = win.build_command(job, tmp_path / "a-min.mkv")
    assert "0:s?" in cmd


def test_gpu_command_uses_cuda(win, tmp_path, monkeypatch):
    monkeypatch.setattr(sf, "ENCODERS", {"hevc_nvenc", "libx265"})
    set_mode(win, sf.BY_STRENGTH, sf.KEEP)
    job = make_job(tmp_path / "a.mp4", duration=10.0)
    cmd = win.build_command(job, tmp_path / "a-min.mp4")
    assert "-hwaccel" in cmd and "hevc_nvenc" in cmd


def test_video_bitrate_hits_target(win):
    job = make_job(Path("x.mp4"), duration=100.0, has_audio=True)
    target = 10 * 1024 * 1024
    bps = win.video_bitrate(job, target)
    total = (bps + 128_000) * 100 / 8
    assert 0.95 * target < total <= target


def test_png_size_target_has_no_dial(win, tmp_path):
    set_mode(win, sf.BY_SIZE, sf.KEEP)
    win.size_field.setText("1")
    job = make_job(tmp_path / "a.png")
    assert win.target_command(job, tmp_path / "a-min.png") is None


def test_lossless_needs_tools(win, tmp_path, monkeypatch):
    monkeypatch.setattr(sf, "OXIPNG", None)
    set_mode(win, sf.BY_STRENGTH, sf.LOSSLESS)
    assert win.build_command(make_job(tmp_path / "a.png"), tmp_path / "b.png") is None
    monkeypatch.setattr(sf, "OXIPNG", "/usr/bin/oxipng")
    cmd = win.build_command(make_job(tmp_path / "a.png"), tmp_path / "b.png")
    assert cmd[0] == "/usr/bin/oxipng"


def test_lossless_video_is_a_remux(win, tmp_path):
    set_mode(win, sf.BY_STRENGTH, sf.LOSSLESS)
    cmd = win.build_command(make_job(tmp_path / "a.mkv"), tmp_path / "b.mkv")
    assert cmd[cmd.index("-c") + 1] == "copy"


def test_strip_metadata(win, tmp_path):
    set_mode(win, sf.BY_STRENGTH, sf.MODERN)
    win.strip.setChecked(True)
    cmd = win.build_command(make_job(tmp_path / "a.jpg"), tmp_path / "b.webp")
    assert "-map_metadata" in cmd
    win.strip.setChecked(False)
    cmd = win.build_command(make_job(tmp_path / "a.jpg"), tmp_path / "b.webp")
    assert "-map_metadata" not in cmd


# --------------------------------------------------------- size search ---
def test_size_search_clears_progress_note(win, tmp_path):
    set_mode(win, sf.BY_SIZE, sf.KEEP)
    win.size_field.setText("1")
    out = tmp_path / "a-min.jpg"
    out.write_bytes(b"x" * 1000)
    job = make_job(tmp_path / "a.jpg", out=out, lo=10, hi=10, probe_q=10,
                   tries=sf.MAX_SEARCH_TRIES - 1, note="tuning 5/6")
    assert win.continue_search(job) is False
    assert job.note == ""
    assert out.exists()
    assert not list(tmp_path.glob("*.best"))


# ------------------------------------------------------------ end to end ---
def run_queue(win, app, files, timeout=120):
    win.add_files(files)
    win.start()
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        app.processEvents()
        time.sleep(0.01)
        if not win.workers and all(j.status not in ("waiting", "running")
                                   for j in win.jobs):
            break
    app.processEvents()


@needs_ffmpeg
def test_end_to_end(win, app, tmp_path):
    src = tmp_path / "in"
    src.mkdir()
    ff = ["ffmpeg", "-v", "error", "-y"]
    subprocess.run(ff + ["-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25",
                         "-f", "lavfi", "-i", "sine=f=440", "-t", "2",
                         "-c:v", "libx264", "-crf", "5", "-c:a", "aac",
                         str(src / "clip.mp4")], check=True)
    subprocess.run(ff + ["-f", "lavfi", "-i", "testsrc2=size=800x600", "-frames:v", "1",
                         "-q:v", "1", str(src / "photo.jpg")], check=True)
    subprocess.run(ff + ["-f", "lavfi", "-i", "sine=f=300", "-t", "3",
                         str(src / "tone.wav")], check=True)
    originals = {p.name: p.read_bytes() for p in src.iterdir()}

    win.dest.setText(str(tmp_path / "out"))
    set_mode(win, sf.BY_STRENGTH, sf.MODERN)
    run_queue(win, app, sorted(src.iterdir()))

    by_name = {j.path.name: j for j in win.jobs}
    for name in ("photo.jpg", "tone.wav"):
        job = by_name[name]
        assert job.status == "done", (name, job.note)
        assert job.out.exists() and job.after < job.before
    assert by_name["photo.jpg"].out.suffix == ".webp"
    assert by_name["tone.wav"].out.suffix == ".opus"
    # The video depends on which encoders this ffmpeg has.
    if win.pick_encoder():
        assert by_name["clip.mp4"].status in ("done", "skipped")

    # Nothing was touched in place.
    assert {p.name: p.read_bytes() for p in src.iterdir()} == originals


# ------------------------------------------------------------- resizing ---
def test_scale_filters():
    assert sf.video_scale(1080) == ("scale='if(gte(iw,ih),-2,min(1080,iw))'"
                                    ":'if(gte(iw,ih),min(1080,ih),-2)'")
    # Photos cap the long side: 1080p means 1920 px.
    assert "min(1920,iw)" in sf.image_scale(1080)
    assert "min(1280,ih)" in sf.image_scale(720)


def test_resize_only_touches_pictures(win, tmp_path):
    set_mode(win, sf.BY_STRENGTH, sf.MODERN)
    win.resize_group.button(2).setChecked(True)
    img = win.build_command(make_job(tmp_path / "a.jpg"), tmp_path / "b.webp")
    snd = win.build_command(make_job(tmp_path / "a.wav", duration=3.0), tmp_path / "b.opus")
    assert img[img.index("-vf") + 1] == sf.image_scale(1080)
    assert "-vf" not in snd
    win.resize_group.button(0).setChecked(True)
    img = win.build_command(make_job(tmp_path / "a.jpg"), tmp_path / "b.webp")
    assert "-vf" not in img


@needs_ffmpeg
def test_resize_end_to_end(tmp_path):
    src, out = tmp_path / "tall.png", tmp_path / "small.png"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=1080x1920", "-frames:v", "1", str(src)], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-vf",
                    sf.video_scale(720), "-frames:v", "1", str(out)], check=True)
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=width,height", "-of", "csv=p=0",
                            str(out)], capture_output=True, text=True, check=True)
    assert probe.stdout.strip() == "720,1280"


# --------------------------------------------------------------- the UI ---
def test_plan_preview(win, tmp_path):
    set_mode(win, sf.BY_STRENGTH, sf.MODERN)
    win.resize_group.button(3).setChecked(True)
    assert win.plan_for(make_job(tmp_path / "a.png")) == "→ WEBP · 720p"
    assert win.plan_for(make_job(tmp_path / "a.wav")) == "→ OPUS"
    set_mode(win, sf.BY_STRENGTH, sf.LOSSLESS)
    assert win.plan_for(make_job(tmp_path / "a.png")) == "lossless"


def test_settings_lock_while_running(win):
    win.set_running(True)
    assert not any(b.isEnabled() for b in win.fmt_group.buttons())
    assert not win.dest.isEnabled() and not win.ruler.isEnabled()
    assert win.go.text() == "Stop"
    win.set_running(False)
    assert all(b.isEnabled() for b in win.fmt_group.buttons())
    assert win.go.text() == "Compress"


def test_settings_are_remembered(app, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    a = sf.Smallfry()
    a.fmt_group.button(sf.KEEP).setChecked(True)
    a.format_changed()
    a.resize_group.button(2).setChecked(True)
    a.ruler.index = 3
    a.settings_changed()
    a.settings.sync()
    a.close()
    b = sf.Smallfry()
    assert (b.fmt(), b.resize_group.checkedId(), b.ruler.index) == (sf.KEEP, 2, 3)
    b.close()


def test_drop_zone_shrinks_once_files_arrive(win, tmp_path):
    assert win.drop.minimumHeight() >= 190
    f = tmp_path / "a.jpg"
    f.write_bytes(b"x")
    win.add_files([f])
    assert win.drop.maximumHeight() == 52
    win.clear_queue()
    assert win.drop.minimumHeight() >= 190


def test_lanes(win, tmp_path, monkeypatch):
    set_mode(win, sf.BY_STRENGTH, sf.MODERN)
    monkeypatch.setattr(sf, "ENCODERS", {"av1_nvenc", "libsvtav1"})
    assert win.lane_for(make_job(tmp_path / "a.mp4")) == "gpu"
    monkeypatch.setattr(sf, "ENCODERS", {"libsvtav1"})
    assert win.lane_for(make_job(tmp_path / "a.mp4")) == "video"
    assert win.lane_for(make_job(tmp_path / "a.jpg")) == "light"
    set_mode(win, sf.BY_STRENGTH, sf.LOSSLESS)
    assert win.lane_for(make_job(tmp_path / "a.mp4")) == "light"
    assert win.slots["video"] <= 3 and win.slots["light"] >= 2


def test_tags_are_copied_only_when_wanted(win, tmp_path, monkeypatch):
    monkeypatch.setattr(sf, "EXIFTOOL", "/usr/bin/exiftool")
    set_mode(win, sf.BY_STRENGTH, sf.MODERN)
    job = make_job(tmp_path / "a.jpg", out=tmp_path / "a-min.webp")
    assert win.wants_tags(job)
    win.strip.setChecked(True)
    assert not win.wants_tags(job)
    win.strip.setChecked(False)
    set_mode(win, sf.BY_SIZE, sf.MODERN)
    assert not win.wants_tags(job)
    monkeypatch.setattr(sf, "EXIFTOOL", None)
    set_mode(win, sf.BY_STRENGTH, sf.MODERN)
    assert not win.wants_tags(job)


def test_duration():
    assert sf.duration(42) == "42 s"
    assert sf.duration(72) == "1 min 12 s"
    assert sf.duration(3720) == "1 h 2 min"


def test_reject_explains_itself(win, tmp_path):
    job = make_job(tmp_path / "a.pdf")
    job.row = sf.QueueRow(job, win.t, win.mono)
    win.reject(job, "needs ghostscript")
    assert "ghostscript" in job.row.toolTip()


@needs_ffmpeg
@pytest.mark.skipif(not sf.EXIFTOOL, reason="exiftool not installed")
def test_photo_tags_survive(win, app, tmp_path):
    src = tmp_path / "in"
    src.mkdir()
    photo = src / "photo.jpg"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=1600x1200", "-frames:v", "1", "-q:v", "1",
                    str(photo)], check=True)
    subprocess.run([sf.EXIFTOOL, "-q", "-overwrite_original",
                    "-DateTimeOriginal=2025:07:04 18:30:00", "-Make=TestCam",
                    str(photo)], check=True)
    win.dest.setText(str(tmp_path / "out"))
    set_mode(win, sf.BY_STRENGTH, sf.MODERN)
    run_queue(win, app, [photo])
    job = win.jobs[0]
    assert job.status == "done", (job.note, job.error)
    tags = subprocess.run([sf.EXIFTOOL, "-s3", "-DateTimeOriginal", "-Make", str(job.out)],
                          capture_output=True, text=True, check=True).stdout.split()
    assert tags[:3] == ["2025:07:04", "18:30:00", "TestCam"]


def test_files_already_under_target_are_left_alone(win, tmp_path):
    f = tmp_path / "small.jpg"
    f.write_bytes(b"x" * 1000)
    set_mode(win, sf.BY_SIZE, sf.KEEP)
    win.size_field.setText("1")
    win.add_files([f])
    win.launch(win.jobs[0], "light")
    assert win.jobs[0].status == "skipped"
    assert win.jobs[0].note == "already under target"
    assert not win.workers
