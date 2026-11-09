#!/usr/bin/env bash
# start|stop|restart|status for the FieldMind API (uvicorn), detached with setsid.
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"; cd "$HERE"
PORT="${API_PORT:-8000}"
up() { curl -s -m 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; }
case "${1:-status}" in
  start)
    up && { echo "api already up on $PORT"; exit 0; }
    setsid nohup .venv/bin/uvicorn app.api:app --host 127.0.0.1 --port "$PORT" >> logs/api.log 2>&1 < /dev/null &
    for i in $(seq 1 40); do up && break; sleep 1; done
    up && echo "api up on $PORT (log logs/api.log)" || { echo "api failed, see logs/api.log"; tail -5 logs/api.log; exit 1; } ;;
  stop)   pkill -f "uvicorn app[.]api:app .*--port $PORT( |$)" && echo "api stopped" || echo "api was not running"; sleep 1 ;;
  restart) "$0" stop; "$0" start ;;
  status) up && echo "api up" || { echo "api down"; exit 1; } ;;
  *) echo "usage: $0 start|stop|restart|status"; exit 2 ;;
esac
