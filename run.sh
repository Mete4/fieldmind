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
echo "== dashboard (8501)"; curl -s -m 1 -o /dev/null http://127.0.0.1:8501/ || (setsid nohup .venv/bin/streamlit run dashboard.py --server.port 8501 --server.headless true > logs/dashboard.log 2>&1 < /dev/null &)
echo "open http://localhost:8501   (API http://127.0.0.1:8000/docs)"
# watchdog: restart Ollama if it dies (CUDA faults happened once)
( while true; do sleep 15; curl -s -m 2 http://127.0.0.1:11434/api/version >/dev/null || { echo "$(date) ollama down, restarting" >> logs/watchdog.log; scripts/ollama_ctl.sh start >> logs/watchdog.log 2>&1; $PY scripts/setup_models.py --preload >> logs/watchdog.log 2>&1; }; done ) > /dev/null 2>&1 &
disown
