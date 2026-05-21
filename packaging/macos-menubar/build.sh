#!/usr/bin/env bash
set -euo pipefail

APP_NAME="Hermes Menu"
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SRC_DIR="$REPO_ROOT/packaging/macos-menubar"
APP_DIR="$HOME/Applications/${APP_NAME}.app"
CONTENTS="$APP_DIR/Contents"
MACOS="$CONTENTS/MacOS"
RESOURCES="$CONTENTS/Resources"

mkdir -p "$MACOS" "$RESOURCES"
cp "$SRC_DIR/Info.plist" "$CONTENTS/Info.plist"

swiftc "$SRC_DIR/HermesMenu.swift" \
  -O \
  -framework Cocoa \
  -o "$MACOS/HermesMenu"

chmod +x "$MACOS/HermesMenu"
codesign --force --deep --sign - "$APP_DIR" >/dev/null

echo "$APP_DIR"
