# Changelog

## 1.0.0 — 2026-10-10

First public release.

- Batch compression for video, images, audio and PDFs in one queue.
- Compress by strength (Light, Balanced, Strong, Extreme) or to a target size.
- Keep original formats, switch to modern codecs (AV1, WebP, Opus), or repack
  losslessly.
- NVIDIA NVENC when the driver is loaded, with an automatic CPU retry.
- Resize to 1440p, 1080p or 720p (video short side, photo long side), never enlarging.
- Photo date, camera and location tags carried over with exiftool, and photos
  kept upright.
- Separate lanes for GPU encodes, CPU video encodes and small jobs, so a long
  video never holds up a batch of photos.
- Each queued file previews what it will become; failed rows explain why on hover.
- Drop files anywhere on the window; double-click a finished row to open it.
- Settings are remembered, and locked while a batch runs.
- Desktop notification and total time when a batch finishes.
- 10-second video preview with a projected file size.
- Right-click a row to open, reveal, retry or cancel a single job.
- Light and dark appearance, following the system on first launch.
- Per-user installer for Arch / CachyOS, and `pipx install` for other distros.
