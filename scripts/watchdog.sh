#!/usr/bin/env bash
# Restarts Ollama (and reloads the models) and the API if they die. Started detached by run.sh.
cd "$(dirname "$0")/.."
while true; do
  sleep 30
  # generation probe: the server can answer /api/version while its runners are wedged
  PROBE=$(curl -s -m 40 http://127.0.0.1:11434/api/generate -d '{"model":"qwen2.5:14b","prompt":"Say OK.","stream":false,"keep_alive":-1,"options":{"num_predict":3}}' | grep -c '"done":true')
  if ! curl -s -m 2 http://127.0.0.1:11434/api/version >/dev/null || [ "$PROBE" != "1" ]; then
    echo "$(date '+%H:%M:%S') ollama not generating (probe=$PROBE) -> restart" >> logs/watchdog.log
    scripts/ollama_ctl.sh stop >> logs/watchdog.log 2>&1; sleep 2
    echo "$(date '+%H:%M:%S') ollama down -> restart" >> logs/watchdog.log
    scripts/ollama_ctl.sh start >> logs/watchdog.log 2>&1
    .venv/bin/python scripts/setup_models.py --preload >> logs/watchdog.log 2>&1
  fi
  if ! curl -s -m 2 http://127.0.0.1:8008/ >/dev/null; then
    echo "$(date '+%H:%M:%S') tile server down -> restart" >> logs/watchdog.log
    (FIELDMIND_TILE_BIND="${FIELDMIND_TILE_BIND:-0.0.0.0}" setsid nohup .venv/bin/python app/tileserver.py >> logs/tileserver.log 2>&1 < /dev/null &)
  fi
  if ! curl -s -m 2 http://127.0.0.1:8000/health >/dev/null; then
    echo "$(date '+%H:%M:%S') api down -> restart" >> logs/watchdog.log
    scripts/api_ctl.sh start >> logs/watchdog.log 2>&1
  fi
done
