#!/bin/bash
set -e

echo ""
echo "========================================="
echo "  VORTEX Video Downloader"
echo "========================================="
echo ""

# Check Python
if ! command -v python3 &>/dev/null; then
  echo "ERROR: Python 3 is required. Install from https://python.org"
  exit 1
fi

# Check ffmpeg
if ! command -v ffmpeg &>/dev/null; then
  echo "WARN: ffmpeg not found - some format conversions may fail."
  echo "      Install: https://ffmpeg.org/download.html"
  echo "      (macOS: brew install ffmpeg | Ubuntu: sudo apt install ffmpeg)"
  echo ""
fi

# Create venv if not exists
if [ ! -d ".venv" ]; then
  echo "-> Creating virtual environment..."
  python3 -m venv .venv
fi

source .venv/bin/activate

echo "-> Installing / updating dependencies..."
pip install -q --upgrade pip
pip install -q -r requirements.txt

echo ""
echo "[OK] Starting VORTEX at http://localhost:5000"
echo "     Press Ctrl+C to stop."
echo ""

python app.py
