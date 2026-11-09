"""Pydantic models shared by the pipeline, the SQLite layer, the API and the UI."""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app import config as C

INCIDENT_TYPES = ("fire", "collapse", "medical", "road_blocked", "trapped", "other")
IncidentType = Literal["fire", "collapse", "medical", "road_blocked", "trapped", "other"]
InputType = Literal["audio", "text"]
Source = Literal["edge", "cloud"]
Status = Literal["local", "queued", "cloud_refined"]

_TYPE_SYNONYMS = [
    (r"wild|brush|vegetation|structure fire|house fire|smoke|burn|blaze|\bfire\b", "fire"),
    (r"collaps|cave|structural|rubble|building down", "collapse"),
    (r"medic|injur|cardiac|unconscious|burn victim|casualt|ems|patient", "medical"),
    (r"road|debris|tree down|closure|blocked|impass|traffic|bridge out", "road_blocked"),
    (r"trap|entrap|pinned|stuck|stranded|missing|search", "trapped"),
]
TYPE_SEVERITY_ORDER = {"trapped": 5, "collapse": 4, "fire": 3, "medical": 2, "road_blocked": 1, "other": 0}
LABEL_PREFIX = {"fire": "FIRE", "medical": "MED", "trapped": "TRAP", "road_blocked": "ROAD", "collapse": "COLL", "other": "OTHER"}
DEPARTMENTS = ("FD", "PD", "EMS", "other")
FIRE_SIZES = ("small", "medium", "large")
DIRECTIONS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
_DIR_WORDS = {"north": "N", "northeast": "NE", "north east": "NE", "east": "E", "southeast": "SE", "south east": "SE",
              "south": "S", "southwest": "SW", "south west": "SW", "west": "W", "northwest": "NW", "north west": "NW"}
_WORD_NUMBERS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
                 "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "dozen": 12, "fifteen": 15, "twenty": 20, "thirty": 30}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Extraction(BaseModel):
    """What the local LLM must return (Ollama structured output). Every field
    has a lenient validator so a slightly-off answer still becomes a valid
    record instead of a crash."""

    model_config = ConfigDict(extra="ignore")

    location_text: Optional[str] = None
    type: IncidentType = "other"
    severity: int = Field(3, ge=1, le=5)
    people_count: Optional[int] = Field(None, ge=0)
    needs: list[str] = Field(default_factory=list)
    confidence: float = Field(0.5, ge=0.0, le=1.0)
    summary: str = ""
    department: str = "other"                 # FD | PD | EMS | other
    unit: str = ""                            # radio call sign of the reporting unit
    structure_part: Optional[str] = None      # "east wing", "gym", "second floor"
    fire_direction: Optional[str] = None      # N NE E SE S SW W NW (where the fire comes from)
    fire_size: Optional[str] = None           # small | medium | large
    details: str = ""                         # every fact that fits no field

    @field_validator("department", mode="before")
    @classmethod
    def _dept(cls, v):
        s = str(v or "").strip().lower()
        if s in ("fd", "fire", "fire department", "cal fire", "engine", "truck", "battalion"):
            return "FD"
        if s in ("pd", "police", "sheriff", "law enforcement", "chp"):
            return "PD"
        if s in ("ems", "medic", "ambulance", "medical", "paramedic"):
            return "EMS"
        return s.upper() if s.upper() in DEPARTMENTS else "other"

    @field_validator("fire_direction", mode="before")
    @classmethod
    def _dir(cls, v):
        if v is None:
            return None
        s = str(v).strip().lower().replace("-", " ").replace("from the ", "").replace("from ", "")
        if s in ("", "null", "none", "unknown"):
            return None
        if s.upper() in DIRECTIONS:
            return s.upper()
        return _DIR_WORDS.get(s)

    @field_validator("fire_size", mode="before")
    @classmethod
    def _size(cls, v):
        if v is None:
            return None
        s = str(v).strip().lower()
        if s in FIRE_SIZES:
            return s
        if s in ("minor", "spot", "small fire", "contained"):
            return "small"
        if s in ("moderate", "medium sized", "growing"):
            return "medium"
        if s in ("big", "major", "huge", "massive", "fully involved", "large fire"):
            return "large"
        return None

    @field_validator("unit", "structure_part", "details", mode="before")
    @classmethod
    def _strs(cls, v):
        if v is None:
            return None
        s = str(v).strip()
        return None if s.lower() in ("null", "none", "unknown", "n/a") else s[:600]

    @field_validator("location_text", mode="before")
    @classmethod
    def _loc(cls, v):
        if v is None:
            return None
        v = str(v).strip()
        return None if v.lower() in ("", "null", "none", "unknown", "n/a", "unclear", "not stated") else v[:200]

    @field_validator("type", mode="before")
    @classmethod
    def _type(cls, v):
        s = str(v or "other").strip().lower().replace("-", "_").replace(" ", "_")
        if s in INCIDENT_TYPES:
            return s
        for pat, t in _TYPE_SYNONYMS:
            if re.search(pat, s.replace("_", " ")):
                return t
        return "other"

    @field_validator("severity", mode="before")
    @classmethod
    def _sev(cls, v):
        try:
            n = int(round(float(str(v).strip().split("/")[0])))
        except (TypeError, ValueError):
            n = 3
        return min(5, max(1, n))

    @field_validator("people_count", mode="before")
    @classmethod
    def _people(cls, v):
        if v is None or isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            return max(0, int(v))
        s = str(v).strip().lower()
        if s in _WORD_NUMBERS:
            return _WORD_NUMBERS[s]
        m = re.search(r"\d+", s)
        return int(m.group()) if m else None

    @field_validator("needs", mode="before")
    @classmethod
    def _needs(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            v = re.split(r"[,;/]+", v)
        out: list[str] = []
        for item in v:
            s = re.sub(r"[^a-z_ ]", "", str(item).strip().lower()).strip().replace(" ", "_")
            if s and s not in out:
                out.append(s[:40])
        return out[:8]

    @field_validator("confidence", mode="before")
    @classmethod
    def _conf(cls, v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return 0.5
        if f > 1.0:
            f = f / 100.0 if f <= 100 else 1.0
        return min(1.0, max(0.0, f))

    @field_validator("summary", mode="before")
    @classmethod
    def _summary(cls, v):
        return str(v or "").strip()[:200]


class Report(BaseModel):
    """One raw input exactly as received: a radio clip, a text message or an
    image. Saved immediately on ingest (before any model runs), then linked
    to the incident it opened or joined. Immutable afterwards."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: "r" + uuid.uuid4().hex[:11])
    ts: str = Field(default_factory=now_iso)
    channel: str = "unknown"
    input_type: InputType = "text"
    text: str = ""                          # transcript or message text
    source_file: Optional[str] = None       # audio file name, if any
    stt_backend: Optional[str] = None
    stt_latency_ms: Optional[int] = None
    stt_rtf: Optional[float] = None
    audio_s: Optional[float] = None
    # extraction result for THIS report (fire fields, section 3)
    department: str = "other"
    unit: str = ""
    type: Optional[str] = None
    severity: Optional[int] = None
    people_count: Optional[int] = None
    location_text: Optional[str] = None
    confidence: Optional[float] = None
    structure_part: Optional[str] = None
    fire_direction: Optional[str] = None
    fire_size: Optional[str] = None
    details: str = ""
    status: Literal["received", "processed", "failed"] = "received"
    incident_id: Optional[str] = None       # the link: which incident this report belongs to
    link_action: Optional[Literal["new", "merged"]] = None
    link_reason: Optional[str] = None
    processing_ms: Optional[int] = None
    error: Optional[str] = None


class Incident(BaseModel):
    """One structured incident (section 7 of the spec) aggregated from one or
    more reports: severity/people = max, needs = union, transcript = latest.
    Stored as JSON in SQLite, returned by the API."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    label: str = ""                                      # human id: FIRE-001, MED-002, ...
    timestamp: str = Field(default_factory=now_iso)      # first report
    last_ts: str = Field(default_factory=now_iso)        # latest report
    channel: str = "unknown"                             # first reporter
    channels: list[str] = Field(default_factory=list)    # every reporter
    report_ids: list[str] = Field(default_factory=list)
    report_count: int = 1
    types: list[str] = Field(default_factory=list)       # every type reported; .type is the most severe
    input_type: InputType = "text"
    transcript: str = ""                                 # latest report text
    location_text: Optional[str] = None
    lat: float = C.TOWN_CENTER_LAT
    lon: float = C.TOWN_CENTER_LON
    geocode_match: Optional[str] = None
    geocode_score: float = 0.0
    location_unknown: bool = False
    type: IncidentType = "other"
    severity: int = 3
    people_count: Optional[int] = None
    needs: list[str] = Field(default_factory=list)
    confidence: float = C.FALLBACK_CONFIDENCE
    hazard_zones: list[str] = Field(default_factory=list)
    in_hazard_zone: bool = False
    priority_score: float = 0.0
    department: list[str] = Field(default_factory=list)   # every department that reported it
    unit: list[str] = Field(default_factory=list)         # every call sign that reported it
    structure_part: Optional[str] = None                  # latest value
    fire_direction: Optional[str] = None                  # latest value
    fire_size: Optional[str] = None                       # largest reported value
    details: str = ""                                     # details of all linked reports, newest first
    source: Source = "edge"
    status: Status = "local"
    edge_latency_ms: Optional[int] = None
    cloud_latency_ms: Optional[int] = None
    summary: str = ""
    llm_model: Optional[str] = None
    tokens_per_s: Optional[float] = None
    extraction_attempts: int = 1
    fallback: bool = False
    cloud_notes: Optional[str] = None


class RequestLog(BaseModel):
    """One model/API call, for the Hardware tab and the benchmark."""

    ts: str = Field(default_factory=now_iso)
    route: str                      # edge_llm | edge_stt | cloud_refine | cloud_summary | edge_summary
    model: str = ""
    latency_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    bytes_out: int = 0              # bytes sent off-device (0 for every edge route)
    ok: bool = True
    error: Optional[str] = None
    incident_id: Optional[str] = None


class NetworkState(BaseModel):
    manual_online: bool = True      # the demo toggle
    connectivity: bool = False      # real reachability check
    online: bool = False            # manual_online AND connectivity
    checked_at: str = Field(default_factory=now_iso)
    queue_size: int = 0
