#!/usr/bin/env bash
# Phase 3 curl test: online -> offline (queue grows) -> online (queue drains).
# Needs the API running (scripts/ollama_ctl.sh start; .venv/bin/uvicorn app.api:app --port 8000).
set -u
API="${API:-http://127.0.0.1:8000}"
J() { python3 -c "import sys,json; d=json.load(sys.stdin); print($1)"; }
post_text() { curl -s -X POST "$API/ingest/text" -H 'content-type: application/json' -d "{\"text\": $(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$1"), \"channel\": \"$2\"}"; }
echo "== 1. health";   curl -s "$API/health" | J "'ok', d['ok'], '| cloud_ready', d['cloud_ready'], '| models', [m.get('model') for m in d['models_loaded']]"
echo "== 2. reset + ONLINE"; curl -s -X POST "$API/reset" >/dev/null; curl -s -X POST "$API/network" -H 'content-type: application/json' -d '{"online": true}' | J "'manual', d['manual_online'], '| connectivity', d['connectivity'], '| online', d['online'], '| cloud_ready', d['cloud_ready'], d['cloud_reason']"
echo "== 3. ONLINE: a clear report (stays local) and an ambiguous one (cloud refine if ready, else queued)"
post_text "Engine 81 to Command. Fully involved structure fire at Skyway and Bille Road, two residents unaccounted for, requesting a second alarm." "Engine 81" | J "'  clear ->', d['incident']['type'], 'sev', d['incident']['severity'], 'conf', d['incident']['confidence'], '| routing:', d['routing'], '| zones', d['incident']['hazard_zones']"
post_text "Command, be advised, social media post says several people, maybe a dozen, unaccounted for somewhere in the Magalia area. Unconfirmed." "Unknown unit" | J "'  ambiguous ->', d['incident']['type'], 'conf', d['incident']['confidence'], '| status', d['incident']['status'], '| routing:', d['routing'], '| source', d['incident']['source']"
echo "== 4. OFFLINE toggle"; curl -s -X POST "$API/network" -H 'content-type: application/json' -d '{"online": false}' | J "'online', d['online'], '| cloud_ready', d['cloud_ready'], d['cloud_reason']"
echo "== 5. OFFLINE: three ambiguous reports -> queue grows (edge keeps working)"
post_text "[static] ...heavy smoke, somewhere off the Skyway above town, might be a structure, might be brush... [static]" "Unknown unit" | J "'  ->', d['incident']['type'], 'conf', d['incident']['confidence'], '| routing:', d['routing']"
post_text "Caller says there is smoke coming out of the canyon somewhere below Centerville, or maybe toward Honey Run, nobody hurt as far as she knows." "Citizen hotline" | J "'  ->', d['incident']['type'], 'conf', d['incident']['confidence'], '| routing:', d['routing']"
post_text "Engine 12 to Command, possible medical, maybe a fall, somewhere around the park, no further information." "Engine 12" | J "'  ->', d['incident']['type'], 'conf', d['incident']['confidence'], '| routing:', d['routing']"
curl -s "$API/queue" | J "'  queue size:', d['size']"
echo "== 6. OFFLINE summary (edge LLM, labelled)"; curl -s -X POST "$API/summary" | J "'  ', d['label'], '|', d['model'], d['latency_ms'], 'ms |', d['text'][:160].replace(chr(10),' ')"
echo "== 7. ONLINE again -> background drain"; curl -s -X POST "$API/network" -H 'content-type: application/json' -d '{"online": true}' | J "'online', d['online'], '| cloud_ready', d['cloud_ready'], d['cloud_reason']"
for i in 1 2 3 4 5 6; do sleep 4; Q=$(curl -s "$API/queue" | J "d['size']"); echo "  t+$((i*4))s queue size: $Q"; [ "$Q" = "0" ] && break; done
curl -s "$API/incidents" | J "'  statuses:', {i['status']: sum(1 for j in d if j['status']==i['status']) for i in d}, '| sources:', {i['source']: sum(1 for j in d if j['source']==i['source']) for i in d}"
echo "== 8. ONLINE summary"; curl -s -X POST "$API/summary" | J "'  ', d['label'], '|', d['model'], d['latency_ms'], 'ms |', d['text'][:160].replace(chr(10),' ')"
echo "== 9. metrics"; curl -s "$API/metrics" | J "'  edge avg ms', d['pipeline']['edge_avg_latency_ms'], '| cloud calls', d['pipeline']['cloud_calls'], '| cloud avg ms', d['pipeline']['cloud_avg_latency_ms'], '| bytes off-device', d['pipeline']['bytes_sent_off_device'], '| est cost USD', d['pipeline']['cloud_cost_usd_est'], '| gpu util', d['gpu'].get('utilization_pct'), '| mem used GiB', d['memory']['used_gib'], '| whisper rtf', d['pipeline']['whisper_rtf_avg']"
echo "== 10. audio ingest (Whisper on GPU)"; curl -s -X POST "$API/ingest/audio" -F "file=@data/samples/audio/5.mp3" -F "channel=Engine 83" | J "'  stt', d['stt']['backend'], d['stt']['latency_ms'], 'ms rtf', d['stt']['rtf'], '->', d['incident']['type'], 'sev', d['incident']['severity'], '| zones', d['incident']['hazard_zones'], '| link', d['link'], '| routing', d['routing']"
