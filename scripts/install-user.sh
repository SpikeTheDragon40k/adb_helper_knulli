#!/usr/bin/env bash
# Install OpenADB for the current user (no root): venv in ~/.local/share/openadb-venv,
# launcher ~/.local/bin/openadb, desktop entry and icon in ~/.local/share.
set -euo pipefail
cd "$(dirname "$0")/.."
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
VENV="$DATA/openadb-venv"
python -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip
"$VENV/bin/pip" install .
mkdir -p "$HOME/.local/bin" "$DATA/applications" "$DATA/icons/hicolor/scalable/apps"
ln -sf "$VENV/bin/openadb" "$HOME/.local/bin/openadb"
sed "s|^Exec=openadb|Exec=$HOME/.local/bin/openadb|" packaging/io.github.openadb.FileManager.desktop \
  > "$DATA/applications/io.github.openadb.FileManager.desktop"
cp openadb/resources/openadb.svg "$DATA/icons/hicolor/scalable/apps/openadb.svg"
echo "Installed. Launch 'OpenADB File Manager' from the menu or run: openadb"
