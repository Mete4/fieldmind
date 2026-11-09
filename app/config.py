"""FieldMind central configuration. Every model name, threshold, path and
limit lives here. Nothing in this file is secret: the Anthropic key is read
from the ANTHROPIC_API_KEY environment variable only."""
from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------- paths
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
GAZETTEER_PATH = DATA_DIR / "gazetteer.json"
GAZETTEER_SEGMENTS_PATH = DATA_DIR / "gazetteer_segments.json"   # street name -> OSM way centres (for intersections)
HAZARD_DIR = DATA_DIR / "hazards"
HAZARD_RAW_DIR = HAZARD_DIR / "raw"
HAZARD_MANIFEST = HAZARD_DIR / "layers.json"
TILE_DIR = DATA_DIR / "tiles"
VENDOR_DIR = DATA_DIR / "vendor"          # cached Leaflet/folium JS+CSS for offline use
SAMPLE_DIR = DATA_DIR / "samples"
DB_PATH = DATA_DIR / "fieldmind.sqlite"
BENCHMARK_MD = ROOT / "benchmark.md"
LOG_DIR = ROOT / "logs"

# ---------------------------------------------------------------- scenario
# Paradise, Butte County, California (Camp Fire, November 2018).
# The town sits inside CAL FIRE's Very High Fire Hazard Severity Zone on a
# ridge between Butte Creek canyon (west) and the West Branch Feather River.
TOWN_NAME = "Paradise, California"
SCENARIO_NAME = "Simulated wildfire"
TOWN_CENTER_LAT = 39.7596
TOWN_CENTER_LON = -121.6219
MAP_ZOOM = 13
# min_lon, min_lat, max_lon, max_lat. OSM town boundary is 39.7254-39.8015 N,
# -121.6557 to -121.5645 E; padded west to Butte Creek canyon and east to the
# West Branch Feather River so the whole ridge and its canyons are on the map.
BBOX = (-121.72, 39.69, -121.52, 39.83)
SCENARIO_BANNER = "Simulated scenario on real geography"

# ---------------------------------------------------------------- map tiles
TILE_MIN_ZOOM = 10
TILE_MAX_ZOOM = 16          # OSM tile usage policy forbids bulk download at z17+
TILE_SERVER_PORT = 8008     # local static server: tiles + vendored JS/CSS
TILE_URL_TEMPLATE = f"http://localhost:{TILE_SERVER_PORT}/tiles/{{z}}/{{x}}/{{y}}.png"
MAP_ATTRIBUTION = (
    "&copy; <a href='https://www.openstreetmap.org/copyright'>OpenStreetMap</a> "
    "contributors (ODbL) | Hazards: CAL FIRE FHSZ, CAL FIRE FRAP | Incidents: simulated"
)
# Overpass / tile fetch politeness
HTTP_USER_AGENT = "FieldMind-hackathon/0.1 (disaster-response demo; contact via GitHub)"
TILE_CONCURRENCY = 2        # OSM tile usage policy: max 2 download threads
TILE_DELAY_S = 0.15         # per-request delay per worker

# ---------------------------------------------------------------- models (Ollama)
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
# Order of preference. LLM_MODEL "auto" = the first candidate that is pulled.
LLM_CANDIDATES = ["llama3.3:70b", "qwen2.5:72b", "qwen2.5:32b", "qwen2.5:14b"]
LLM_MODEL = os.environ.get("FIELDMIND_LLM", "auto")
# The ORGANIZER does extraction, SQL writing and the edge situation. qwen3.6:35b-a3b takes over when pulled
# (measured ~64 tok/s on GB10, "think": false); until then qwen2.5:32b. The 14B is the fallback when the organizer is too slow.
ORGANIZER_CANDIDATES = ["qwen3.6:35b-a3b", "qwen2.5:32b"]
FALLBACK_MODEL = "qwen2.5:14b"
ORGANIZER_TIMEOUT_S = 45       # per extraction call before stepping down to the fallback
LLM_THINK = False              # Qwen 3 models think by default; always off
LLM_MIN_TOKENS_PER_S = 10.0    # below this, step down one size for live extraction
LLM_TEMPERATURE = 0.1
LLM_NUM_CTX = 4096
LLM_TIMEOUT_S = 120
OLLAMA_KEEP_ALIVE = -1          # int: Ollama API wants a number or a unit string ("-1" alone is rejected)
OLLAMA_NUM_PARALLEL = 8        # 128 GB unified memory allows 8 parallel streams (run.sh)
OLLAMA_MAX_LOADED_MODELS = 4
OLLAMA_FLASH_ATTENTION = 1
MODELS_JSON = DATA_DIR / "models.json"   # written by scripts/setup_models.py (live/summary model choice + tok/s)

# ---------------------------------------------------------------- speech-to-text
WHISPER_MODEL = "large-v3"
WHISPER_DEVICE = "cuda"
WHISPER_COMPUTE_TYPE = "float16"
WHISPER_BACKEND = os.environ.get("FIELDMIND_STT_BACKEND", "auto")  # auto|faster|transformers|whispercpp|faster_cpu
WHISPER_HF_MODEL = "openai/whisper-large-v3"            # transformers fallback (the PGX path: CTranslate2 aarch64 has no CUDA)
WHISPER_CPP_DIR = Path.home() / ".local/share/fieldmind/whisper.cpp"   # optional whisper.cpp CUDA build
WHISPER_CPP_MODEL = WHISPER_CPP_DIR / "models/ggml-large-v3.bin"
WHISPER_LANGUAGE = "en"
STT_MAX_CONCURRENT = 2

# ---------------------------------------------------------------- cloud
ANTHROPIC_MODEL = "claude-sonnet-5-5"
ANTHROPIC_SUMMARY_MODEL = "claude-sonnet-5-5"
ANTHROPIC_MAX_TOKENS = 1024
ANTHROPIC_TIMEOUT_S = 60
# USD per million tokens, used only for the cost estimate on the Hardware tab
ANTHROPIC_PRICE_IN_PER_M = 3.0
ANTHROPIC_PRICE_OUT_PER_M = 15.0
CONNECTIVITY_CHECK_URL = "https://api.anthropic.com/"
CONNECTIVITY_TIMEOUT_S = 2.0
CONNECTIVITY_CACHE_S = 5.0

# ---------------------------------------------------------------- router / triage
CONFIDENCE_THRESHOLD = 0.7
UNCLEAR_CONFIDENCE = 0.6       # below this a call becomes an open question for the operator instead of a pin     # below this -> cloud refine (online) or queue (offline)
FALLBACK_CONFIDENCE = 0.3      # used when the LLM returns invalid JSON twice
GEOCODE_MIN_SCORE = 70         # rapidfuzz score 0-100
GEOCODE_FAIL_CONFIDENCE_PENALTY = 0.25
HAZARD_PRIORITY_BONUS = 0      # all of Paradise is a hazard zone: the bonus carried no information
# people factor added to priority_score: people_count >= threshold -> factor
PEOPLE_FACTOR_BANDS = [(1, 1), (3, 2), (10, 3)]
NEEDS_VOCAB = ["fire_suppression", "technical_rescue", "medical", "ambulance", "evacuation",
               "shelter", "law_enforcement", "traffic_control", "heavy_equipment", "utilities", "air_support", "search"]
LLM_NUM_PREDICT = 300
QUEUE_DRAIN_INTERVAL_S = 3.0
SITUATION_INTERVAL_S = 60          # cloud situation + suggestions refresh when online

# ---------------------------------------------------------------- report -> incident linking rule
# Every input is stored as a REPORT first. A new report JOINS an existing
# incident when: same type (LINK_SAME_TYPE), within LINK_RADIUS_M metres of it,
# within LINK_WINDOW_MIN minutes of that incident's last report, and both have
# a known location. Otherwise it opens a NEW incident.
# LINK_RADIUS_M = 0 disables merging: one report = one incident.
LINK_RADIUS_M = 250
LINK_WINDOW_MIN = 30
LINK_SAME_TYPE = False      # link by place and time for any type (review defect 1)
LINK_MAX_REPORTS = 20

# ---------------------------------------------------------------- API / UI
API_HOST = "127.0.0.1"
API_PORT = 8000
API_URL = f"http://{API_HOST}:{API_PORT}"
DASHBOARD_PORT = 8501
DASHBOARD_REFRESH_MS = 2000
DEVICE_LABEL = "Lenovo ThinkStation PGX (NVIDIA GB10 Grace Blackwell)"

# ---------------------------------------------------------------- hazard zones
# zone code -> (legend label, colour). Shared by prepare_data.py, hazards.py
# and the dashboard legend. Only layers with "pip": True count for
# in_hazard_zone; the rest are display-only context.
ZONE_STYLES = {
    "fire_very_high":      ("Very High fire hazard severity (CAL FIRE FHSZ)", "#99000d"),
    "fire_high":           ("High fire hazard severity (CAL FIRE FHSZ)", "#ef3b2c"),
    "fire_moderate":       ("Moderate fire hazard severity (CAL FIRE FHSZ)", "#fc9272"),
    "fire_perimeter_2018": ("2018 Camp Fire perimeter (CAL FIRE FRAP)", "#636363"),
}
HAZARD_SIMPLIFY_TOLERANCE_DEG = 0.00003   # ~3 m

# Public REST sources queried by scripts/prepare_data.py (needs internet once).
# If a download fails, drop the file into data/hazards/raw/<id>.<ext> instead
# (shapefile/.zip, GeoJSON, KML, GPKG or CSV) and re-run the script. See
# data/hazards/README.md for download links. Each entry: fetch = arcgis |
# (public ArcGIS REST). zone = fixed zone code, or zone_rules = list of
# [field, regex, zone] (first match wins, unmatched features are dropped).
_CALFIRE = "https://services1.arcgis.com/jUJYIo9tSA7EHvfZ/arcgis/rest/services"
FHSZ_RULES = [["FHSZ_Description", "(?i)very\\s*high", "fire_very_high"],
              ["FHSZ_Description", "(?i)\\bhigh\\b", "fire_high"],
              ["FHSZ_Description", "(?i)moderate", "fire_moderate"]]
HAZARD_SOURCES = [
    {"id": "fire", "name": "Fire Hazard Severity Zones (CAL FIRE)", "kind": "polygon", "pip": True,
     "fetch": "arcgis", "out_fields": "SRA,FHSZ,FHSZ_Description,FHSZ_6Class", "zone_rules": FHSZ_RULES,
     "keep_fields": ["SRA", "FHSZ_Description", "FHSZ_6Class"],
     "url": f"{_CALFIRE}/fhsz24_1/FeatureServer/0",
     "source": "CAL FIRE / Office of the State Fire Marshal, Fire Hazard Severity Zones (SRA 2022-23 + LRA 2024-25 combined, 6-class; CAL FIRE hosted layer fhsz24_1)",
     "source_url": "https://osfm.fire.ca.gov/what-we-do/community-wildfire-preparedness-and-mitigation/fire-hazard-severity-zones",
     "license": "Public data from the State of California; attribution to CAL FIRE"},
    {"id": "camp_fire", "name": "2018 Camp Fire perimeter (context)", "kind": "polygon", "pip": False,
     "fetch": "arcgis", "url": f"{_CALFIRE}/2018_California_Fire_Perimeters_View/FeatureServer/0",
     "where": "FIRE_NAME = 'CAMP' AND YEAR_ = 2018", "out_fields": "FIRE_NAME,YEAR_,GIS_ACRES,ALARM_DATE",
     "zone": "fire_perimeter_2018", "keep_fields": ["FIRE_NAME", "YEAR_", "GIS_ACRES"],
     "source": "CAL FIRE FRAP, California Fire Perimeters (all)",
     "source_url": "https://www.fire.ca.gov/what-we-do/fire-resource-assessment-program/fire-perimeters",
     "license": "Public data from the State of California; attribution to CAL FIRE FRAP"},
]

for _d in (DATA_DIR, HAZARD_DIR, HAZARD_RAW_DIR, TILE_DIR, VENDOR_DIR, SAMPLE_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)
