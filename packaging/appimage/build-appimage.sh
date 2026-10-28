#!/usr/bin/env bash
# Build dist/OpenADB_File_Manager-<version>-x86_64.AppImage locally (no CI involved).
#
# How it works:
#   1. builds a wheel of this project
#   2. downloads a relocatable CPython AppImage from niess/python-appimage (manylinux2014)
#   3. installs the wheel + PySide6-Essentials into it
#   4. adds AppRun, .desktop and icon, then packs it with appimagetool
#
# adb is NOT bundled: the AppImage uses the system adb (Arch: sudo pacman -S android-tools)
# or the path configured in Settings.
#
# Requirements: bash, curl, python3 (>= 3.11, with venv) and ~1.5 GB free disk space.
# Overrides: PY_VERSION=3.12  PYTHON_APPIMAGE_URL=...  APPIMAGETOOL=/path/to/appimagetool
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="$ROOT/packaging/appimage/build"
APPDIR="$WORK/AppDir"
ARCH="${ARCH:-x86_64}"
PY_VERSION="${PY_VERSION:-3.12}"
PY_TAG="cp${PY_VERSION/./}"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/openadb/__init__.py")"
OUT="$ROOT/dist/OpenADB_File_Manager-${VERSION}-${ARCH}.AppImage"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

command -v curl >/dev/null || die "curl is required"
command -v python3 >/dev/null || die "python3 is required"
[[ -n "$VERSION" ]] || die "could not read the version from openadb/__init__.py"

# Only ever delete our own build directory.
rm -rf "${WORK:?}"
mkdir -p "$WORK" "$ROOT/dist"
cd "$WORK"

log "Building wheel for openadb-file-manager $VERSION"
python3 -m venv "$WORK/buildenv"
"$WORK/buildenv/bin/pip" install --quiet --upgrade pip build
"$WORK/buildenv/bin/python" -m build --wheel --outdir "$WORK/wheel" "$ROOT" >/dev/null
WHEEL="$(ls "$WORK"/wheel/*.whl)"

if [[ -z "${PYTHON_APPIMAGE_URL:-}" ]]; then
  log "Looking up the python-appimage release for Python $PY_VERSION"
  PYTHON_APPIMAGE_URL="$(curl -fsSL "https://api.github.com/repos/niess/python-appimage/releases/tags/python${PY_VERSION}" \
    | grep -o "https://[^\"]*${PY_TAG}-${PY_TAG}-manylinux2014_${ARCH}\.AppImage" | head -n1 || true)"
  [[ -n "$PYTHON_APPIMAGE_URL" ]] || die "could not find a Python $PY_VERSION AppImage; set PYTHON_APPIMAGE_URL"
fi
log "Downloading $PYTHON_APPIMAGE_URL"
curl -fL --retry 3 -o python.AppImage "$PYTHON_APPIMAGE_URL"
chmod +x python.AppImage
./python.AppImage --appimage-extract >/dev/null
mv squashfs-root "$APPDIR"

PYBIN="$(find "$APPDIR/opt" -maxdepth 3 -type f -path "*/bin/python${PY_VERSION}" | head -n1)"
[[ -n "$PYBIN" && -x "$PYBIN" ]] || die "python binary not found inside the extracted AppImage"
PYREL="${PYBIN#"$APPDIR"/}"
log "Using embedded interpreter $PYREL"

log "Installing OpenADB and PySide6-Essentials into the AppDir"
"$PYBIN" -s -m pip install --quiet --no-warn-script-location --upgrade pip
"$PYBIN" -s -m pip install --quiet --no-warn-script-location "$WHEEL"
"$PYBIN" -s -c "import openadb, PySide6.QtWidgets; print('import check ok:', openadb.__version__)"

# Trim files that are not needed at runtime.
PYSIDE_DIR="$("$PYBIN" -s -c 'import PySide6, os; print(os.path.dirname(PySide6.__file__))')"
[[ -n "$PYSIDE_DIR" && "$PYSIDE_DIR" == "$APPDIR"/* ]] || die "unexpected PySide6 location: $PYSIDE_DIR"
for sub in examples include glue typesystems scripts; do
  rm -rf "${PYSIDE_DIR:?}/$sub"
done

log "Writing AppRun, desktop entry and icon"
for f in AppRun .DirIcon; do rm -f "${APPDIR:?}/$f"; done
find "${APPDIR:?}" -maxdepth 1 \( -name '*.desktop' -o -name '*.png' -o -name '*.svg' \) -delete
cat > "$APPDIR/AppRun" <<APPRUN
#!/bin/sh
HERE="\$(dirname "\$(readlink -f "\$0")")"
export PYTHONNOUSERSITE=1
unset PYTHONHOME PYTHONPATH
exec "\$HERE/$PYREL" -s -m openadb "\$@"
APPRUN
chmod +x "$APPDIR/AppRun"
cp "$ROOT/packaging/io.github.openadb.FileManager.desktop" "$APPDIR/io.github.openadb.FileManager.desktop"
cp "$ROOT/openadb/resources/openadb.svg" "$APPDIR/openadb.svg"
ln -sf openadb.svg "$APPDIR/.DirIcon"
mkdir -p "$APPDIR/usr/share/applications" "$APPDIR/usr/share/icons/hicolor/scalable/apps"
cp "$APPDIR/io.github.openadb.FileManager.desktop" "$APPDIR/usr/share/applications/"
cp "$APPDIR/openadb.svg" "$APPDIR/usr/share/icons/hicolor/scalable/apps/"

if [[ -z "${APPIMAGETOOL:-}" ]]; then
  log "Downloading appimagetool"
  curl -fL --retry 3 -o appimagetool \
    "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-${ARCH}.AppImage"
  chmod +x appimagetool
  APPIMAGETOOL="$WORK/appimagetool"
fi

log "Packing $OUT"
# --appimage-extract-and-run lets appimagetool work without FUSE.
ARCH="$ARCH" "$APPIMAGETOOL" --appimage-extract-and-run --no-appstream "$APPDIR" "$OUT"
chmod +x "$OUT"
log "Done: $OUT ($(du -h "$OUT" | cut -f1))"
log "Smoke test: $OUT --version"
"$OUT" --appimage-extract-and-run --version || true
