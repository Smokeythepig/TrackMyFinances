#!/bin/bash
# Builds TrackMyFinances.app — double-clickable macOS app
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
source venv/bin/activate

echo "=== Building TrackMyFinances.app ==="

pyinstaller \
  --name "TrackMyFinances" \
  --windowed \
  --onedir \
  --add-data "frontend:frontend" \

  
  --hidden-import "webview" \
  --hidden-import "flask" \
  --hidden-import "requests" \
  main.py

echo ""
echo "=== Done! ==="
echo "App is at: dist/TrackMyFinances.app"
echo "Drag it to /Applications to install."
