#!/usr/bin/env bash
# Start/stop/status for the user-space Ollama with FieldMind's settings.
# Usage: scripts/ollama_ctl.sh start|stop|restart|status
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"
OLLAMA_BIN="${OLLAMA_BIN:-$(command -v ollama || echo "$HOME/.local/share/fieldmind/ollama/bin/ollama")}"
export OLLAMA_HOST="${OLLAMA_HOST:-127.0.0.1:11434}"
export OLLAMA_KEEP_ALIVE=-1            # models stay loaded forever
export OLLAMA_NUM_PARALLEL="${OLLAMA_NUM_PARALLEL:-8}"
export OLLAMA_MAX_LOADED_MODELS="${OLLAMA_MAX_LOADED_MODELS:-4}"
export OLLAMA_FLASH_ATTENTION="${OLLAMA_FLASH_ATTENTION:-1}"
LOG="$HERE/logs/ollama.log"; mkdir -p "$HERE/logs"
status() { curl -s -m 2 "http://$OLLAMA_HOST/api/version" >/dev/null 2>&1; }
case "${1:-status}" in
  start)
    if status; then echo "ollama already running at $OLLAMA_HOST"; exit 0; fi
    [ -x "$OLLAMA_BIN" ] || { echo "ollama binary not found ($OLLAMA_BIN)"; exit 1; }
    # setsid: own session, so a killed parent shell / process group cannot take the server down
    setsid nohup "$OLLAMA_BIN" serve >> "$LOG" 2>&1 < /dev/null &
    for i in $(seq 1 30); do status && break; sleep 1; done
    status && echo "ollama started (NUM_PARALLEL=$OLLAMA_NUM_PARALLEL, KEEP_ALIVE=-1, FLASH_ATTENTION=$OLLAMA_FLASH_ATTENTION) log $LOG" || { echo "ollama failed to start, see $LOG"; exit 1; } ;;
  stop)   pkill -x ollama && echo "ollama stopped" || echo "ollama was not running" ;;   # -x: exact process name, never a shell
  restart) "$0" stop; sleep 2; "$0" start ;;
  status) if status; then echo "ollama up: $(curl -s http://$OLLAMA_HOST/api/version)"; "$OLLAMA_BIN" ps 2>/dev/null; else echo "ollama down"; exit 1; fi ;;
  *) echo "usage: $0 start|stop|restart|status"; exit 2 ;;
esac
