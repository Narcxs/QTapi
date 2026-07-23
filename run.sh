#!/usr/bin/env bash
# ---- One-click launcher for Linux / VPS ----
cd "$(dirname "$0")"

if [ ! -d venv ]; then
  echo "Creating virtual environment..."
  python3 -m venv venv
fi

source venv/bin/activate
echo "Installing / updating dependencies..."
pip install -q -r requirements.txt

echo "Starting GexBot cache server (poller + API)..."
python -m app.main
