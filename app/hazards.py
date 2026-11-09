"""Real hazard layers -> per-incident hazard_zones, in_hazard_zone and
priority_score. Polygons from data/hazards/*.geojson (layers.json manifest,
only layers with "pip": true) go into one shapely STRtree."""
from __future__ import annotations

import json
import threading
from typing import Optional

from shapely.geometry import Point, shape
from shapely.strtree import STRtree

from app import config as C
from app.schemas import Incident


def active_layers() -> list[dict]:
    """Manifest layers that are configured in HAZARD_SOURCES (other layer files
    may exist on disk but are not loaded)."""
    if not C.HAZARD_MANIFEST.exists():
        return []
    known = {s["id"] for s in C.HAZARD_SOURCES} | {f"{s['id']}_buffer" for s in C.HAZARD_SOURCES}
    return [L for L in json.loads(C.HAZARD_MANIFEST.read_text()) if L["id"] in known]


class HazardIndex:
    def __init__(self) -> None:
        self.layers: list[dict] = active_layers()
        geoms, zones, layer_ids = [], [], []
        for L in self.layers:
            if not L.get("pip"):
                continue
            path = C.HAZARD_DIR / L["file"]
            if not path.exists():
                continue
            for f in json.loads(path.read_text()).get("features", []):
                try:
                    g = shape(f["geometry"])
                except Exception:  # noqa: BLE001
                    continue
                if g.is_empty:
                    continue
                geoms.append(g)
                zones.append(f["properties"].get("zone", L["id"]))
                layer_ids.append(L["id"])
        self._zones = zones
        self._layer_ids = layer_ids
        self._tree: Optional[STRtree] = STRtree(geoms) if geoms else None
        self.polygon_count = len(geoms)

    def zones_at(self, lat: float, lon: float) -> list[str]:
        if self._tree is None:
            return []
        idx = self._tree.query(Point(lon, lat), predicate="intersects")
        return sorted({self._zones[i] for i in idx})

    def layer_summary(self) -> list[dict]:
        return [{k: L.get(k) for k in ("id", "name", "kind", "pip", "features", "zones", "source", "source_url", "license", "prepared")}
                for L in self.layers]


_index: Optional[HazardIndex] = None
_lock = threading.Lock()


def get_index() -> HazardIndex:
    global _index
    with _lock:
        if _index is None:
            _index = HazardIndex()
        return _index


def people_factor(people: Optional[int]) -> int:
    factor = 0
    for threshold, value in C.PEOPLE_FACTOR_BANDS:
        if people is not None and people >= threshold:
            factor = value
    return factor


def priority_score(severity: int, in_hazard_zone: bool, people: Optional[int]) -> float:
    return float(severity * 2 + (C.HAZARD_PRIORITY_BONUS if in_hazard_zone else 0) + people_factor(people))


def zone_label(code: str) -> str:
    return C.ZONE_STYLES.get(code, (code, "#777"))[0]


def annotate(inc: Incident) -> Incident:
    """Fill hazard_zones / in_hazard_zone / priority_score in place. Never raises."""
    try:
        zones = get_index().zones_at(inc.lat, inc.lon) if not inc.location_unknown else []
    except Exception:  # noqa: BLE001
        zones = []
    inc.hazard_zones = zones
    inc.in_hazard_zone = bool(zones)
    inc.priority_score = priority_score(inc.severity, inc.in_hazard_zone, inc.people_count)
    return inc
