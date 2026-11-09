#!/usr/bin/env bash
# start|stop|restart|status for the Streamlit dashboard, detached with setsid.
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"; cd "$HERE"
PORT="${DASHBOARD_PORT:-8501}"
up() { curl -s -m 2 "http://127.0.0.1:$PORT/" >/dev/null 2>&1; }
case "${1:-status}" in
  start) up && { echo "dashboard already up on $PORT"; exit 0; }
         setsid nohup .venv/bin/streamlit run dashboard.py --server.port "$PORT" --server.headless true >> logs/dashboard.log 2>&1 < /dev/null &
         for i in $(seq 1 30); do up && break; sleep 1; done; up && echo "dashboard up on $PORT" || { echo "dashboard failed"; tail -5 logs/dashboard.log; exit 1; } ;;
  stop) pkill -f "streamlit run dashboard[.]py" && echo "dashboard stopped" || echo "dashboard was not running"; sleep 1 ;;
  restart) "$0" stop; "$0" start ;;
  status) up && echo "dashboard up" || { echo "dashboard down"; exit 1; } ;;
  *) echo "usage: $0 start|stop|restart|status"; exit 2 ;;
esac
