#!/usr/bin/env bash
# FieldMind one-command start: Ollama (+ resident models), tile server, API, dashboard.
set -u
cd "$(dirname "$0")"
mkdir -p logs
[ -f .env ] && set -a && . ./.env && set +a
PY=.venv/bin/python
echo "== Ollama"; scripts/ollama_ctl.sh start && $PY scripts/setup_models.py --preload | tail -1
echo "== data check"; $PY scripts/check_data.py | tail -1
echo "== tile server (8008)"; curl -s -m 1 -o /dev/null http://127.0.0.1:8008/ || (setsid nohup $PY app/tileserver.py > logs/tileserver.log 2>&1 < /dev/null &)
echo "== API (8000)"; scripts/api_ctl.sh start
echo "== dashboard (8501)"; scripts/dash_ctl.sh start
echo "open http://localhost:8501   (API http://127.0.0.1:8000/docs)"
# watchdog: restarts Ollama (+ models) and the API if they die
pgrep -f "[w]atchdog.sh" >/dev/null || (setsid nohup scripts/watchdog.sh > /dev/null 2>&1 < /dev/null &)
