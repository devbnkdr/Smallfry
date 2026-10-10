#!/usr/bin/env bash
# Smallfry installer: per-user, into ~/.local. Works from any shell, fish included:
#
#   ./install.sh               install or update
#   ./install.sh --no-deps     skip the system-package step
#   ./install.sh --uninstall   remove everything this script put down
#
# On Arch / CachyOS it installs the system packages with pacman. Elsewhere it
# checks for them and tells you what's missing.
set -euo pipefail

APP_ID="smallfry"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${SRC_DIR}/smallfry.py"
ICON_SRC="${SRC_DIR}/assets/smallfry.svg"

LIB_DIR="${HOME}/.local/share/${APP_ID}"
BIN="${HOME}/.local/bin/${APP_ID}"
DESKTOP="${HOME}/.local/share/applications/${APP_ID}.desktop"
ICON="${HOME}/.local/share/icons/hicolor/scalable/apps/${APP_ID}.svg"

say()  { printf '\033[1;34m::\033[0m %s\n' "$1"; }
ok()   { printf '\033[1;32m ok\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m  !\033[0m %s\n' "$1"; }
die()  { printf '\033[1;31m  x\033[0m %s\n' "$1" >&2; exit 1; }

refresh_caches() {
    update-desktop-database "${HOME}/.local/share/applications" &>/dev/null || true
    gtk-update-icon-cache -f -t "${HOME}/.local/share/icons/hicolor" &>/dev/null || true
}

# ------------------------------------------------------------- uninstall ----
if [[ "${1:-}" == "--uninstall" ]]; then
    for target in "${LIB_DIR}" "${BIN}" "${DESKTOP}" "${ICON}"; do
        [[ -e "${target}" ]] && rm -rf "${target}"
    done
    refresh_caches
    ok "Smallfry is gone. Your settings are in ~/.config/${APP_ID}/ if you want those removed too."
    exit 0
fi

# ------------------------------------------------------------- validate -----
[[ -f "${SRC}" ]] || die "smallfry.py not found next to this script (${SRC_DIR})."
python3 -c 'import ast, sys; ast.parse(open(sys.argv[1]).read())' "${SRC}" \
    || die "smallfry.py has a syntax error and would not start."
ok "Source is valid"

# ------------------------------------------------------------- packages -----
if [[ "${1:-}" != "--no-deps" ]] && command -v pacman &>/dev/null; then
    NEEDED=()
    for pkg in python-pyqt6 ffmpeg desktop-file-utils; do
        pacman -Qq "${pkg}" &>/dev/null || NEEDED+=("${pkg}")
    done
    if ((${#NEEDED[@]})); then
        say "Installing: ${NEEDED[*]}"
        sudo pacman -S --needed "${NEEDED[@]}"
    else
        ok "All required packages present"
    fi
fi

python3 -c 'import PyQt6.QtWidgets' &>/dev/null \
    || die "PyQt6 is missing. Install it from your distro (python-pyqt6, python3-pyqt6), or: pip install --user PyQt6"
command -v ffmpeg &>/dev/null && command -v ffprobe &>/dev/null \
    || die "ffmpeg is missing. Install it from your distro's packages."

# Optional helpers: Smallfry works without them, with fewer formats.
MISSING=()
command -v gs       &>/dev/null || MISSING+=("ghostscript (PDF compression)")
command -v qpdf     &>/dev/null || MISSING+=("qpdf (lossless PDF)")
command -v oxipng   &>/dev/null || MISSING+=("oxipng (lossless PNG)")
command -v jpegtran &>/dev/null || MISSING+=("jpegtran from libjpeg-turbo (lossless JPEG)")
command -v exiftool &>/dev/null || MISSING+=("exiftool (keeps photo dates, camera and location)")
for m in "${MISSING[@]}"; do warn "optional: ${m}"; done

# ---------------------------------------------------------------- install ---
install -Dm644 "${SRC}" "${LIB_DIR}/smallfry.py"
install -Dm644 "${ICON_SRC}" "${ICON}"

mkdir -p "$(dirname "${BIN}")"
cat > "${BIN}" <<EOF
#!/usr/bin/env bash
exec python3 "${LIB_DIR}/smallfry.py" "\$@"
EOF
chmod 755 "${BIN}"

mkdir -p "$(dirname "${DESKTOP}")"
cat > "${DESKTOP}" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=Smallfry
GenericName=File Compressor
Comment=Shrink videos, images, audio and PDFs
Exec=${BIN} %F
Icon=${APP_ID}
Terminal=false
Categories=Utility;AudioVideo;Graphics;
Keywords=compress;shrink;convert;video;image;audio;pdf;ffmpeg;
MimeType=video/mp4;video/x-matroska;video/quicktime;video/webm;video/x-msvideo;video/mpeg;image/jpeg;image/png;image/webp;image/tiff;image/bmp;image/avif;audio/mpeg;audio/x-wav;audio/flac;audio/mp4;audio/ogg;audio/opus;application/pdf;
StartupNotify=true
StartupWMClass=smallfry
EOF

refresh_caches
VERSION=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "${SRC}")
ok "Installed Smallfry ${VERSION} to ${LIB_DIR}"

if ! command -v "${APP_ID}" &>/dev/null; then
    warn "${HOME}/.local/bin is not on your PATH. Add it once:"
    printf '\n    fish:  fish_add_path ~/.local/bin\n    bash:  echo '\''export PATH="$HOME/.local/bin:$PATH"'\'' >> ~/.bashrc\n'
fi

cat <<EOF

  Launch:     ${APP_ID}
  With files: ${APP_ID} ~/Videos/clip.mp4 ~/Pictures/
  Uninstall:  ./install.sh --uninstall

EOF
