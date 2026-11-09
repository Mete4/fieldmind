#!/usr/bin/env bash
# Restarts Ollama (and reloads the models) and the API if they die. Started detached by run.sh.
cd "$(dirname "$0")/.."
while true; do
  sleep 10
  if ! curl -s -m 2 http://127.0.0.1:11434/api/version >/dev/null; then
    echo "$(date '+%H:%M:%S') ollama down -> restart" >> logs/watchdog.log
    scripts/ollama_ctl.sh start >> logs/watchdog.log 2>&1
    .venv/bin/python scripts/setup_models.py --preload >> logs/watchdog.log 2>&1
  fi
  if ! curl -s -m 2 http://127.0.0.1:8000/health >/dev/null; then
    echo "$(date '+%H:%M:%S') api down -> restart" >> logs/watchdog.log
    scripts/api_ctl.sh start >> logs/watchdog.log 2>&1
  fi
done
