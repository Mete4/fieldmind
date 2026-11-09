"""Offline fuzzy geocoder: free-text place -> lat/lon using data/gazetteer.json
(real OpenStreetMap names) and data/gazetteer_segments.json (every OSM way
centre of each street, so "Skyway and Bille Road" resolves to the actual
intersection, not to street midpoints). ~1 ms per lookup, no network."""
from __future__ import annotations

import json
import math
import re
import threading
from typing import Optional

from rapidfuzz import fuzz, process

from app import config as C

_ABBREV = [
    (r"\brd\.?\b", "road"), (r"\bst\.?\b", "street"), (r"\bave?\.?\b", "avenue"), (r"\bdr\.?\b", "drive"),
    (r"\bln\.?\b", "lane"), (r"\bhwy\.?\b", "highway"), (r"\bblvd\.?\b", "boulevard"), (r"\bct\.?\b", "court"),
    (r"\bpl\.?\b", "place"), (r"\bcir\.?\b", "circle"), (r"\bpkwy\.?\b", "parkway"), (r"\bmt\.?\b", "mount"),
    (r"\belem\.?\b", "elementary"), (r"\bjr\.?\b", "junior"), (r"\bhosp\.?\b", "hospital"), (r"\bsta\.?\b", "station"),
]
_PREFIX = re.compile(
    r"^(?:(?:the|a|an|at|on|near|by|behind|in|of|off|around|just|about|approximately|approx|vicinity of|the area of|"
    r"area of|intersection of|corner of|in front of|across from|north of|south of|east of|west of|up|down|along|"
    r"above|below|outside|inside|next to|close to|toward|towards|heading|onto|into|from|past|beyond|"
    r"\d+(?:\.\d+)?\s*(?:blocks?|miles?|mi|yards?|yds|feet|ft|km|meters?|m)\s*(?:north|south|east|west|n|s|e|w)?\s*(?:of)?)\s+)+",
    re.I)
_SUFFIX = re.compile(r"\s*(?:area|vicinity|neighborhood|neighbourhood|district|intersection|region|zone|side)$", re.I)
_SPLIT = re.compile(r"\s+(?:and|&|at|/|@|x|crossing|cross|intersection with|near|by|next to|across from|below|above|past|before|after|between|on|along|off)\s+", re.I)
# words that describe the incident, not a place: a split part made only of these is ignored
_NONPLACE = {"spot", "fire", "fires", "smoke", "structure", "vehicle", "patient", "collision", "crash", "evacuation", "traffic",
             "people", "residents", "medical", "rescue", "brush", "vegetation", "roof", "collapse", "building", "house", "home",
             "flames", "heavy", "possible", "report", "reports", "working", "fully", "involved", "incident", "scene", "unit", "units"}
_GENERIC = {"park", "school", "church", "road", "street", "creek", "bridge", "station", "hospital", "store", "market",
            "lake", "canyon", "ridge", "highway", "house", "home", "building", "structure", "town", "city", "river",
            "parking lot", "lot", "yard", "field", "intersection", "scene", "area", "residence", "unknown"}
# local names people actually say on the radio -> gazetteer names (OpenStreetMap)
ALIASES = {
    "feather river hospital": "Adventist Health Feather River",
    "the hospital": "Adventist Health Feather River",
    "adventist hospital": "Adventist Health Feather River",
    "adventist health": "Adventist Health Feather River",
    "paradise high": "Paradise High School",
    "the high school": "Paradise High School",
    "paradise intermediate": "Paradise Intermediate School",
    "paradise pd": "Paradise Police Station",
    "police department": "Paradise Police Station",
    "station 81": "Paradise Fire Station #81",
    "fire station 81": "Paradise Fire Station #81",
    "station 82": "Paradise Fire Department #82",
    "fire station 82": "Paradise Fire Department #82",
    "station 35": "Paradise Station 35",
    "station 33": "Upper Ridge Station 33",
    "station 26": "Butte County Fire Station 26 Honey Run",
    # point aliases: landmarks that are not in the gazetteer (approximate position at Butte Creek)
    "honey run covered bridge": {"name": "Honey Run Covered Bridge", "lat": 39.733801, "lon": -121.694564},
    "honey run bridge": {"name": "Honey Run Covered Bridge", "lat": 39.733801, "lon": -121.694564},
    "the covered bridge": {"name": "Honey Run Covered Bridge", "lat": 39.733801, "lon": -121.694564},
    "covered bridge": {"name": "Honey Run Covered Bridge", "lat": 39.733801, "lon": -121.694564},
    "paradise library": "Paradise Branch Butte County Library",
    "the library": "Paradise Branch Butte County Library",
    "community park": "Paradise Community Park",
    "aquatic park": "Paul Bryne Aquatic Park",
    "the skyway": "Skyway",
    "upper skyway": "Skyway",
    "lower skyway": "Skyway",
    "old skyway": "Skyway",
    "highway 191": "Clark Road",
    "hwy 191": "Clark Road",
    "sr 191": "Clark Road",
    "feather river canyon": "West Branch Feather River",
    "west branch": "West Branch Feather River",
    "butte creek canyon": "Butte Creek",
    "paradise skypark": "Paradise Skypark",
    "the airport": "Paradise Skypark",
    "skypark": "Paradise Skypark",
    "holiday market": "Holiday Market",
    "paradise lake": "Magalia",
}


def normalize(text: str) -> str:
    s = text.lower().strip()
    s = re.sub(r"[\"'’`]", "", s)
    s = re.sub(r"[^\w\s&/#-]", " ", s)
    for pat, rep in _ABBREV:
        s = re.sub(pat, rep, s)
    return re.sub(r"\s+", " ", s).strip()


def strip_qualifiers(s: str) -> str:
    s = _PREFIX.sub("", s)
    s = _SUFFIX.sub("", s)
    s = re.sub(r"^\d{1,6}\s+(?:block of\s+)?", "", s)      # house numbers: "5800 clark road"
    s = re.sub(r"^block of\s+", "", s)
    return _PREFIX.sub("", s).strip()


class Geocoder:
    def __init__(self) -> None:
        self.entries: list[dict] = json.loads(C.GAZETTEER_PATH.read_text()) if C.GAZETTEER_PATH.exists() else []
        self.segments: dict[str, list[list[float]]] = (
            json.loads(C.GAZETTEER_SEGMENTS_PATH.read_text()) if C.GAZETTEER_SEGMENTS_PATH.exists() else {})
        # Prefer facilities over same-named streets by listing non-street entries first.
        self.entries.sort(key=lambda e: 1 if e["type"] in ("street", "major_road") else 0)
        self.choices = [normalize(e["name"]) for e in self.entries]
        self.alias_index: dict[str, int] = {}
        by_name = {e["name"].lower(): i for i, e in reversed(list(enumerate(self.entries)))}
        for alias, target in ALIASES.items():
            if isinstance(target, dict):                 # point alias -> synthetic landmark entry
                self.entries.append({"name": target["name"], "type": "landmark", "lat": target["lat"], "lon": target["lon"], "segments": 1})
                self.choices.append(normalize(target["name"]))
                self.alias_index[normalize(alias)] = len(self.entries) - 1
                continue
            idx = by_name.get(target.lower())
            if idx is not None:
                self.alias_index[normalize(alias)] = idx

    # -- single place ------------------------------------------------------
    def match_one(self, text: str) -> tuple[Optional[dict], float]:
        q = strip_qualifiers(normalize(text))
        if not q or q in _GENERIC:
            return None, 0.0
        if q in self.alias_index:
            return self.entries[self.alias_index[q]], 100.0
        if not self.choices:
            return None, 0.0
        top = process.extract(q, self.choices, scorer=fuzz.WRatio, limit=6)
        if not top:
            return None, 0.0
        best_score = top[0][1]
        # near-ties: prefer the longer, more specific name ("Paradise High School" over "Paradise")
        cands = [t for t in top if t[1] >= best_score - 2]
        name, score, idx = max(cands, key=lambda t: (len(t[0]), t[1]))
        if len(q) <= 3:
            score *= 0.6
        return self.entries[idx], float(score)

    # -- public ------------------------------------------------------------
    def geocode(self, text: Optional[str]) -> Optional[dict]:
        """Return {lat, lon, match, score, method} or None when nothing is
        good enough (score < GEOCODE_MIN_SCORE)."""
        if not text or not str(text).strip():
            return None
        q = strip_qualifiers(normalize(str(text)))
        if not q:
            return None
        if q in self.alias_index:                       # whole-string alias wins
            e = self.entries[self.alias_index[q]]
            return {"lat": e["lat"], "lon": e["lon"], "match": e["name"], "score": 100.0, "method": "alias"}
        parts = [p.strip() for p in _SPLIT.split(q) if p and p.strip()]
        parts = [p for p in parts if not all(w in _NONPLACE or w in _GENERIC or len(w) < 3 for w in p.split())] or parts
        if len(parts) >= 2:
            a, sa = self.match_one(parts[0])
            b, sb = self.match_one(parts[1])
            if a and b and min(sa, sb) >= C.GEOCODE_MIN_SCORE and a["name"] != b["name"]:
                lat, lon, method = self._meet(a, b)
                return {"lat": lat, "lon": lon, "match": f"{a['name']} & {b['name']}", "score": round(min(sa, sb), 1), "method": method}
            cand, score = max(((a, sa), (b, sb)), key=lambda t: t[1])
            if cand and score >= C.GEOCODE_MIN_SCORE:
                return {"lat": cand["lat"], "lon": cand["lon"], "match": cand["name"], "score": round(score, 1), "method": "place"}
            return None
        e, s = self.match_one(q)
        words = q.split()
        if e and e["name"].lower() == C.TOWN_NAME.split(",")[0].lower() and len(words) > 2:
            # "Unseen Paradise High School" (misheard word) -> try without the first word before accepting the town itself
            e2, s2 = self.match_one(" ".join(words[1:]))
            if e2 and s2 >= 90:
                e, s = e2, s2
        if e and s >= C.GEOCODE_MIN_SCORE:
            return {"lat": e["lat"], "lon": e["lon"], "match": e["name"], "score": round(s, 1), "method": "place"}
        return None

    def _meet(self, a: dict, b: dict) -> tuple[float, float, str]:
        """Closest pair between the way centres of a and b (or their single
        point): a good approximation of the intersection / nearest point."""
        pa = self.segments.get(a["name"]) or [[a["lat"], a["lon"]]]
        pb = self.segments.get(b["name"]) or [[b["lat"], b["lon"]]]
        best = None
        for la, lo in pa:
            k = math.cos(math.radians(la))
            for lb, lob in pb:
                d = (la - lb) ** 2 + ((lo - lob) * k) ** 2
                if best is None or d < best[0]:
                    best = (d, la, lo, lb, lob)
        _, la, lo, lb, lob = best
        a_street, b_street = a["name"] in self.segments, b["name"] in self.segments
        if a_street and b_street:
            return (la + lb) / 2, (lo + lob) / 2, "intersection"
        # street + landmark ("Pentz Road near the hospital"): the landmark is the pin
        if b_street and not a_street:
            return la, lo, "nearest"
        return lb, lob, "nearest"


_geocoder: Optional[Geocoder] = None
_lock = threading.Lock()


def get_geocoder() -> Geocoder:
    global _geocoder
    with _lock:
        if _geocoder is None:
            _geocoder = Geocoder()
        return _geocoder
