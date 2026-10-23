#!/usr/bin/env sh
# Installs LeanCut for the current user (no root needed).
#   ./install.sh            install the app to ~/.local/share/LeanCut/app and add it to the app menu
#   ./install.sh --remove   uninstall (your projects and settings in ~/.local/share/LeanCut are kept)
# The app lives in its own app/ subfolder because ~/.local/share/LeanCut is also LeanCut's data folder.
set -e
SHARE="${XDG_DATA_HOME:-$HOME/.local/share}"
APP_DIR="$SHARE/LeanCut/app"
BIN="$HOME/.local/bin/leancut"
DESKTOP="$SHARE/applications/leancut.desktop"
ICON="$SHARE/icons/hicolor/512x512/apps/leancut.png"

if [ "$1" = "--remove" ]; then
  rm -rf "$APP_DIR" "$BIN" "$DESKTOP" "$ICON"
  echo "LeanCut removed. Your projects and settings are still in $SHARE/LeanCut."
  exit 0
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
if [ "$HERE" != "$APP_DIR" ]; then
  rm -rf "$APP_DIR"  # a clean copy, so files from an older version don't linger
  mkdir -p "$APP_DIR"
  cp -R "$HERE"/. "$APP_DIR"/
fi
mkdir -p "$(dirname "$BIN")" "$(dirname "$DESKTOP")" "$(dirname "$ICON")"
ln -sf "$APP_DIR/LeanCut" "$BIN"
cp "$HERE/leancut.png" "$ICON"
sed "s|^Exec=.*|Exec=\"$APP_DIR/LeanCut\"|" "$HERE/leancut.desktop" > "$DESKTOP"
command -v update-desktop-database >/dev/null && update-desktop-database "$(dirname "$DESKTOP")" || true
echo "Installed. Launch LeanCut from your app menu, or run: $BIN"
