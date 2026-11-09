#!/usr/bin/env bash
# Builds data/samples/skip_ahead.sqlite: radio part 1 and the 30-minute batch run through the
# real pipeline on a separate API (port 8010, own database), so the live console is not touched.
set -u
cd "$(dirname "$0")/.."
export FIELDMIND_DB="$PWD/data/skip_build.sqlite" FIELDMIND_API_PORT=8010
rm -f data/skip_build.sqlite data/skip_build.sqlite-wal data/skip_build.sqlite-shm
setsid .venv/bin/uvicorn app.api:app --host 127.0.0.1 --port 8010 >> logs/skip_build_api.log 2>&1 < /dev/null &
API_PID=$!
for i in $(seq 1 90); do curl -s -m 2 http://127.0.0.1:8010/health >/dev/null && break; sleep 2; done
sleep 20   # Whisper warm-up on the second API
.venv/bin/python scripts/play_radio.py data/samples/radio/camp_live.json --stream
.venv/bin/python scripts/play_radio.py data/samples/radio/camp_batch.json --text --one-at-a-time
sleep 60   # alerts, open-question checks and background work finish
kill "$API_PID"; sleep 2
.venv/bin/python -c "import sqlite3; c=sqlite3.connect('data/skip_build.sqlite'); c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); c.close()"
cp data/skip_build.sqlite data/samples/skip_ahead.sqlite
echo "skip-ahead state written: data/samples/skip_ahead.sqlite"
