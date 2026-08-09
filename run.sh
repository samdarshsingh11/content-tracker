#!/usr/bin/env bash
# Creates the venv on first run, installs PyMySQL, starts the server.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "Creating virtualenv..."
  python3 -m venv .venv
  ./.venv/bin/pip install --quiet --upgrade pip
  ./.venv/bin/pip install --quiet -r requirements.txt
fi

if [ ! -f .env ]; then
  echo
  echo "  No .env yet. Copying .env.example -> .env"
  echo "  Fill it in, then run this again."
  echo
  cp .env.example .env
fi

exec ./.venv/bin/python server.py
