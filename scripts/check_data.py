#!/usr/bin/env python3
"""Offline self-test for the data prepared by scripts/prepare_data.py.
Needs no network. Exit code 0 = everything FieldMind needs is present."""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import config as C  # noqa: E402


def deg2num(lat, lon, z):
    n = 2 ** z
    return int((lon + 180) / 360 * n), int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)


def main() -> int:
    ok = True
    print(f"FieldMind data check: {C.TOWN_NAME}  bbox={C.BBOX}")
    # gazetteer
    if C.GAZETTEER_PATH.exists():
        gaz = json.loads(C.GAZETTEER_PATH.read_text())
        by: dict[str, int] = {}
        for e in gaz:
            by[e["type"]] = by.get(e["type"], 0) + 1
        print(f"[gazetteer] OK  {len(gaz)} places {dict(sorted(by.items(), key=lambda kv: -kv[1]))}")
    else:
        gaz = []
        print("[gazetteer] MISSING data/gazetteer.json")
        ok = False
    # vendored map assets
    man = C.VENDOR_DIR / "manifest.json"
    if man.exists():
        m = json.loads(man.read_text())
        missing = [u for u, p in m.items() if not (C.VENDOR_DIR / p).exists()]
        print(f"[vendor]    {'OK ' if not missing else 'BAD'} {len(m)} Leaflet/folium assets cached offline" + (f"; missing {missing}" if missing else ""))
        ok &= not missing
    else:
        print("[vendor]    MISSING data/vendor/manifest.json (map will need internet)")
        ok = False
    # tiles
    min_lon, min_lat, max_lon, max_lat = C.BBOX
    total_have = total_need = 0
    rows = []
    for z in range(C.TILE_MIN_ZOOM, C.TILE_MAX_ZOOM + 1):
        x0, y0 = deg2num(max_lat, min_lon, z)
        x1, y1 = deg2num(min_lat, max_lon, z)
        need = (x1 - x0 + 1) * (y1 - y0 + 1)
        have = sum(1 for x in range(x0, x1 + 1) for y in range(y0, y1 + 1) if (C.TILE_DIR / str(z) / str(x) / f"{y}.png").exists())
        rows.append(f"z{z}:{have}/{need}")
        total_have += have
        total_need += need
    size_mb = sum(p.stat().st_size for p in C.TILE_DIR.rglob("*.png")) / 1e6
    print(f"[tiles]     {'OK ' if total_have == total_need else 'PARTIAL'} {total_have}/{total_need} OSM tiles ({size_mb:.1f} MB)  {' '.join(rows)}")
    ok &= total_have >= 0.98 * total_need
    # hazards
    if not C.HAZARD_MANIFEST.exists():
        print("[hazards]   MISSING data/hazards/layers.json")
        return 1
    layers = json.loads(C.HAZARD_MANIFEST.read_text())
    for L in layers:
        f = C.HAZARD_DIR / L["file"]
        print(f"[hazards]   {'OK ' if f.exists() else 'BAD'} {L['id']:<10} {L['kind']:<7} pip={str(L['pip']):<5} {L['features']:>4} features  zones={list(L['zones'])}")
        ok &= f.exists()
    try:
        import geopandas as gpd
        from shapely.geometry import Point
        from shapely.strtree import STRtree

        geoms, zones = [], []
        for L in layers:
            if not L["pip"]:
                continue
            g = gpd.read_file(C.HAZARD_DIR / L["file"])
            geoms += list(g.geometry)
            zones += list(g["zone"])
        tree = STRtree(geoms)
        places = {e["name"]: e for e in gaz}
        tests = [("Town center (Skyway / Pearson Rd)", C.TOWN_CENTER_LAT, C.TOWN_CENTER_LON)]
        for name in ("Adventist Health Feather River", "Paradise High School", "Bille Park", "Centerville", "Magalia", "Paradise Fire Station #81"):
            if name in places:
                tests.append((name, places[name]["lat"], places[name]["lon"]))
        print(f"[pip]       {len(geoms)} hazard polygons in STRtree; sample point-in-polygon checks:")
        for name, lat, lon in tests:
            pt = Point(lon, lat)
            hits = sorted({zones[i] for i in tree.query(pt, predicate="intersects")})
            print(f"            {name:<36} -> {hits or 'none'}")
        centre_hits = {zones[i] for i in tree.query(Point(C.TOWN_CENTER_LON, C.TOWN_CENTER_LAT), predicate="intersects")}
        if not centre_hits:
            print("[pip]       WARN: town center is in no hazard zone; check the fire layer")
    except Exception as e:  # noqa: BLE001
        print(f"[pip]       check failed: {e}")
        ok = False
    print("RESULT:", "ALL GOOD, FieldMind can run offline" if ok else "INCOMPLETE, see lines above")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
