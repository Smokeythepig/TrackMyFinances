#!/bin/bash
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

echo "=== TrackMyFinances Setup ==="

# Check Python
if ! command -v python3 &>/dev/null; then
  echo "ERROR: python3 not found. Install it from python.org."
  exit 1
fi

# Create venv if missing
if [ ! -d "venv" ]; then
  echo "Creating virtual environment..."
  python3 -m venv venv
fi

source venv/bin/activate

echo "Installing dependencies..."
pip install -q -r requirements.txt

echo ""
echo "=== Setup complete! ==="
echo ""
echo "Next steps:"
echo "  1. Run the app:  ./run.sh"
echo "  2. In the app, open Accounts and connect your SimpleFIN token"
echo "     (get one at https://bridge.simplefin.org — \$15/yr, links your banks)"
echo "  3. Optional — build a double-clickable .app:  ./build.sh"
