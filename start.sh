#!/bin/sh
# 3Driver launcher (Linux/macOS). Run: bash start.sh
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install -q -r requirements.txt
python3 scripts/serve.py &
SERVER_PID=$!
sleep 2
(xdg-open http://127.0.0.1:8765 >/dev/null 2>&1 || open http://127.0.0.1:8765 >/dev/null 2>&1 || true)
wait $SERVER_PID
