#!/usr/bin/env python3
"""ONE-TIME data preparation for FieldMind (needs internet). Afterwards the
app runs with NO network.

  1. data/gazetteer.json        real place names from OpenStreetMap (Overpass API)
  2. data/vendor/               Leaflet / folium JS+CSS so the map renders offline
  3. data/tiles/{z}/{x}/{y}.png OSM raster tiles for the bounding box (z10-16)
  4. data/hazards/*.geojson     hazard layers from CAL FIRE REST services (FHSZ,
                                Camp Fire perimeter) plus any configured file in data/hazards/raw/,
                                reprojected to EPSG:4326, clipped to the bounding
                                box, simplified; data/hazards/layers.json manifest

Usage:
  .venv/bin/python scripts/prepare_data.py [--skip-gazetteer] [--skip-vendor]
        [--skip-tiles] [--skip-hazards] [--only hazards] [--force]
Safe to re-run: anything already downloaded is skipped unless --force.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import config as C  # noqa: E402

HEADERS = {"User-Agent": C.HTTP_USER_AGENT, "Accept": "*/*"}
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
OSM_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# =============================================================== 1. gazetteer
def overpass_query(bbox) -> str:
    min_lon, min_lat, max_lon, max_lat = bbox
    bb = f"({min_lat},{min_lon},{max_lat},{max_lon})"
    return f"""[out:json][timeout:240];
(
  way["highway"~"^(motorway|trunk|primary|secondary|tertiary|residential|unclassified|living_street|pedestrian)$"]["name"]{bb};
  nwr["amenity"~"^(school|college|university|hospital|clinic|fire_station|police|community_centre|shelter|place_of_worship|townhall|library|marketplace)$"]["name"]{bb};
  nwr["emergency"="shelter"]{bb};
  nwr["leisure"~"^(park|pitch|golf_course|stadium|playground|garden|marina)$"]["name"]{bb};
  nwr["man_made"~"^(bridge|pier|tower|lighthouse)$"]["name"]{bb};
  way["bridge"="yes"]["name"~"Bridge"]{bb};
  nwr["shop"~"^(supermarket|mall|department_store)$"]["name"]{bb};
  nwr["tourism"~"^(attraction|museum|viewpoint)$"]["name"]{bb};
  nwr["public_transport"="station"]["name"]{bb};
  nwr["railway"="station"]["name"]{bb};
  nwr["place"~"^(neighbourhood|suburb|quarter|town|village|hamlet|island)$"]["name"]{bb};
  nwr["natural"~"^(peak|beach|bay)$"]["name"]{bb};
  nwr["waterway"~"^(river|stream|canal)$"]["name"]{bb};
  nwr["aeroway"="aerodrome"]["name"]{bb};
  nwr["building"~"^(hospital|school|church|stadium|civic|government)$"]["name"]{bb};
  nwr["office"="government"]["name"]{bb};
);
out center tags;"""


def classify(tags: dict) -> str:
    if tags.get("man_made") == "bridge" or (tags.get("bridge") == "yes" and "bridge" in tags.get("name", "").lower()):
        return "bridge"
    hw = tags.get("highway")
    if hw:
        return "major_road" if hw in ("motorway", "trunk", "primary", "secondary") else "street"
    am = tags.get("amenity")
    if am in ("school", "college", "university") or tags.get("building") == "school":
        return "school"
    if am in ("hospital", "clinic") or tags.get("building") == "hospital":
        return "hospital"
    if am == "fire_station":
        return "fire_station"
    if am == "police":
        return "police"
    if am in ("shelter", "community_centre") or tags.get("emergency") == "shelter":
        return "shelter"
    if am in ("townhall", "library", "marketplace") or tags.get("office") == "government" or tags.get("building") in ("civic", "government"):
        return "civic"
    if am == "place_of_worship" or tags.get("building") == "church":
        return "place_of_worship"
    if tags.get("leisure") in ("park", "pitch", "golf_course", "playground", "garden"):
        return "park"
    if tags.get("leisure") in ("stadium", "marina") or tags.get("building") == "stadium":
        return "landmark"
    if tags.get("public_transport") == "station" or tags.get("railway") == "station":
        return "transit_station"
    if tags.get("place"):
        return "neighborhood"
    if tags.get("natural") == "beach":
        return "beach"
    if tags.get("natural") == "peak":
        return "peak"
    if tags.get("natural") == "bay" or tags.get("waterway"):
        return "water"
    if tags.get("man_made") == "pier":
        return "pier"
    if tags.get("tourism") or tags.get("man_made") or tags.get("aeroway") or tags.get("shop"):
        return "landmark"
    return "other"


def build_gazetteer(force: bool = False) -> None:
    if C.GAZETTEER_PATH.exists() and not force:
        n = len(json.loads(C.GAZETTEER_PATH.read_text()))
        log(f"[gazetteer] exists ({n} places), skipping (use --force to rebuild)")
        return
    query = overpass_query(C.BBOX)
    data = None
    for url in OVERPASS_MIRRORS:
        try:
            log(f"[gazetteer] querying Overpass {url} ...")
            r = httpx.post(url, data={"data": query}, headers=HEADERS, timeout=300)
            r.raise_for_status()
            data = r.json()
            break
        except Exception as e:  # noqa: BLE001
            log(f"[gazetteer] mirror failed: {e}")
    if data is None:
        log("[gazetteer] ERROR: all Overpass mirrors failed; gazetteer not built")
        return
    acc: dict[tuple[str, str], list[float]] = {}
    segments: dict[str, list[list[float]]] = {}   # street name -> centres of every OSM way
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        name = (tags.get("name") or "").strip()
        if not name:
            continue
        if el["type"] == "node":
            lat, lon = el["lat"], el["lon"]
        else:
            c = el.get("center")
            if not c:
                continue
            lat, lon = c["lat"], c["lon"]
        key = (name, classify(tags))
        if key[1] in ("street", "major_road", "bridge"):
            segments.setdefault(name, []).append([round(lat, 6), round(lon, 6)])
        a = acc.setdefault(key, [0.0, 0.0, 0])
        a[0] += lat
        a[1] += lon
        a[2] += 1
    gaz = [
        {"name": k[0], "type": k[1], "lat": round(v[0] / v[2], 6), "lon": round(v[1] / v[2], 6), "segments": v[2]}
        for k, v in acc.items()
    ]
    gaz.sort(key=lambda g: (g["type"], g["name"]))
    C.GAZETTEER_PATH.write_text(json.dumps(gaz, indent=0, ensure_ascii=False))
    C.GAZETTEER_SEGMENTS_PATH.write_text(json.dumps(segments, ensure_ascii=False))
    log(f"[gazetteer] saved way centres for {len(segments)} streets -> {C.GAZETTEER_SEGMENTS_PATH.relative_to(ROOT)}")
    counts: dict[str, int] = {}
    for g in gaz:
        counts[g["type"]] = counts.get(g["type"], 0) + 1
    log(f"[gazetteer] saved {len(gaz)} places -> {C.GAZETTEER_PATH.relative_to(ROOT)}")
    log(f"[gazetteer] by type: {dict(sorted(counts.items(), key=lambda kv: -kv[1]))}")


# =============================================================== 2. vendor assets
VENDOR_EXTRA_RELATIVE = {
    # relative to the directory of the matching asset URL
    "leaflet.js": ["images/marker-icon.png", "images/marker-icon-2x.png", "images/marker-shadow.png",
                   "images/layers.png", "images/layers-2x.png"],
    "leaflet.awesome-markers.css": ["images/markers-soft.png", "images/markers-soft@2x.png",
                                    "images/markers-shadow.png", "images/markers-shadow@2x.png",
                                    "images/markers-plain.png", "images/markers-matte.png"],
    "all.min.css": ["../webfonts/fa-solid-900.woff2", "../webfonts/fa-regular-400.woff2", "../webfonts/fa-brands-400.woff2"],
}


def url_to_local(url: str) -> Path:
    p = urlparse(url)
    return C.VENDOR_DIR / p.netloc / p.path.lstrip("/")


def build_vendor(force: bool = False) -> None:
    import folium  # local import: only needed here

    m = folium.Map(location=[C.TOWN_CENTER_LAT, C.TOWN_CENTER_LON], zoom_start=C.MAP_ZOOM, tiles=None)
    folium.TileLayer(tiles=C.TILE_URL_TEMPLATE, attr=C.MAP_ATTRIBUTION, name="OSM").add_to(m)
    folium.GeoJson({"type": "FeatureCollection", "features": []}, name="h").add_to(m)
    folium.CircleMarker([C.TOWN_CENTER_LAT, C.TOWN_CENTER_LON], radius=5, popup="p").add_to(m)
    folium.Marker([C.TOWN_CENTER_LAT, C.TOWN_CENTER_LON], icon=folium.DivIcon(html="x"), tooltip="t").add_to(m)
    folium.LayerControl().add_to(m)
    html = m.get_root().render()
    urls = sorted(set(re.findall(r'(?:src|href)="(https?://[^"]+)"', html)))
    extras: list[str] = []
    for u in urls:
        base = u.rsplit("/", 1)[0]
        for rel in VENDOR_EXTRA_RELATIVE.get(u.rsplit("/", 1)[-1], []):
            extras.append(httpx.URL(base + "/" + rel).copy_with().__str__())
    manifest: dict[str, str] = {}
    ok = fail = 0
    with httpx.Client(headers=HEADERS, timeout=60, follow_redirects=True) as client:
        for u in urls + extras:
            local = url_to_local(u)
            if local.exists() and local.stat().st_size > 0 and not force:
                pass
            else:
                try:
                    r = client.get(u)
                    r.raise_for_status()
                    local.parent.mkdir(parents=True, exist_ok=True)
                    local.write_bytes(r.content)
                except Exception as e:  # noqa: BLE001
                    log(f"[vendor] failed {u}: {e}")
                    fail += 1
                    continue
            ok += 1
            if u in urls:
                manifest[u] = str(local.relative_to(C.VENDOR_DIR))
    (C.VENDOR_DIR / "manifest.json").write_text(json.dumps(manifest, indent=1))
    log(f"[vendor] {ok} assets cached ({fail} failed) -> {C.VENDOR_DIR.relative_to(ROOT)}/manifest.json")


# =============================================================== 3. tiles
def deg2num(lat_deg: float, lon_deg: float, zoom: int) -> tuple[int, int]:
    lat_rad = math.radians(lat_deg)
    n = 2 ** zoom
    x = int((lon_deg + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return x, y


def tile_list(bbox, zmin: int, zmax: int) -> list[tuple[int, int, int]]:
    min_lon, min_lat, max_lon, max_lat = bbox
    tiles = []
    for z in range(zmin, zmax + 1):
        x0, y0 = deg2num(max_lat, min_lon, z)
        x1, y1 = deg2num(min_lat, max_lon, z)
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                tiles.append((z, x, y))
    return tiles


def fetch_tile(client: httpx.Client, z: int, x: int, y: int) -> str:
    path = C.TILE_DIR / str(z) / str(x) / f"{y}.png"
    if path.exists() and path.stat().st_size > 0:
        return "cached"
    for attempt in range(4):
        try:
            r = client.get(OSM_TILE_URL.format(z=z, x=x, y=y))
            if r.status_code == 200:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(r.content)
                time.sleep(C.TILE_DELAY_S)
                return "ok"
            if r.status_code in (429, 503, 502):
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code in (403, 418):
                return "blocked"
            return f"http{r.status_code}"
        except Exception:  # noqa: BLE001
            time.sleep(2)
    return "failed"


def build_tiles(max_zoom: int, force: bool = False) -> None:
    tiles = tile_list(C.BBOX, C.TILE_MIN_ZOOM, max_zoom)
    todo = [t for t in tiles if force or not (C.TILE_DIR / str(t[0]) / str(t[1]) / f"{t[2]}.png").exists()]
    log(f"[tiles] bbox needs {len(tiles)} tiles for z{C.TILE_MIN_ZOOM}-{max_zoom}; {len(tiles) - len(todo)} cached, {len(todo)} to fetch "
        f"({C.TILE_CONCURRENCY} workers, OSM tile usage policy)")
    if not todo:
        return
    stats: dict[str, int] = {}
    t0 = time.time()
    with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as client:
        with ThreadPoolExecutor(max_workers=C.TILE_CONCURRENCY) as ex:
            futs = [ex.submit(fetch_tile, client, *t) for t in todo]
            for i, f in enumerate(as_completed(futs), 1):
                res = f.result()
                stats[res] = stats.get(res, 0) + 1
                if i % 100 == 0 or i == len(todo):
                    log(f"[tiles] {i}/{len(todo)} {stats} {time.time() - t0:.0f}s")
                if res == "blocked" and stats[res] >= 5:
                    log("[tiles] tile server is refusing requests; stopping. Re-run later.")
                    for g in futs:
                        g.cancel()
                    break
    size_mb = sum(p.stat().st_size for p in C.TILE_DIR.rglob("*.png")) / 1e6
    log(f"[tiles] done: {stats}; cache now {size_mb:.1f} MB in {C.TILE_DIR.relative_to(ROOT)}")


# =============================================================== 4. hazards
def fetch_arcgis(client: httpx.Client, layer_url: str, bbox, out_fields: str = "*", page: int = 1000, where: str = "1=1") -> dict:
    feats: list = []
    offset = 0
    while True:
        params = {
            "where": where, "geometry": ",".join(str(v) for v in bbox), "geometryType": "esriGeometryEnvelope",
            "inSR": 4326, "spatialRel": "esriSpatialRelIntersects", "outFields": out_fields, "outSR": 4326,
            "f": "geojson", "resultOffset": offset, "resultRecordCount": page,
        }
        r = client.get(layer_url.rstrip("/") + "/query", params=params)
        r.raise_for_status()
        d = r.json()
        if "error" in d:
            raise RuntimeError(d["error"])
        fs = d.get("features", [])
        feats.extend(fs)
        exceeded = d.get("properties", {}).get("exceededTransferLimit") or d.get("exceededTransferLimit")
        if not fs or (len(fs) < page and not exceeded):
            break
        offset += len(fs)
    return {"type": "FeatureCollection", "features": feats}


def download_sources(force: bool = False) -> None:
    with httpx.Client(headers=HEADERS, timeout=180, follow_redirects=True) as client:
        for src in C.HAZARD_SOURCES:
            raw = C.HAZARD_RAW_DIR / f"{src['id']}.geojson"
            if raw.exists() and not force:
                log(f"[hazards] {src['id']}: raw file exists, skipping download")
                continue
            if not src.get("url"):
                log(f"[hazards] {src['id']}: no URL configured; drop a file named {src['id']}.* into data/hazards/raw/ (see README)")
                continue
            try:
                t0 = time.time()
                if src["fetch"] == "arcgis":
                    fc = fetch_arcgis(client, src["url"], C.BBOX, src.get("out_fields", "*"), where=src.get("where", "1=1"))
                else:
                    log(f"[hazards] {src['id']}: unknown fetch type {src['fetch']}")
                    continue
                if fc is None:
                    continue
                raw.write_text(json.dumps(fc))
                log(f"[hazards] {src['id']}: downloaded {len(fc['features'])} features in {time.time() - t0:.1f}s")
            except Exception as e:  # noqa: BLE001
                log(f"[hazards] {src['id']}: DOWNLOAD FAILED ({e}); place the file in data/hazards/raw/ manually")


KEYWORD_SOURCE = [  # filename keyword -> source id, for files dropped in raw/ by hand
    ("perimeter", "camp_fire"), ("camp", "camp_fire"), ("fhsz", "fire"), ("fire", "fire"),
]
FIRE_RULES = [["*", "(?i)very\\s*high", "fire_very_high"], ["*", "(?i)^high|\\bhigh\\b", "fire_high"], ["*", "(?i)moderate", "fire_moderate"]]


def source_for_file(path: Path):
    """Source definition for a raw file, or None when the file is not a
    configured layer (such files stay on disk but are not loaded)."""
    stem = path.stem.lower()
    by_id = {s["id"]: s for s in C.HAZARD_SOURCES}
    if stem in by_id:
        return by_id[stem]
    for kw, sid in KEYWORD_SOURCE:
        if kw in stem:
            if sid in by_id:
                return dict(by_id[sid], id=sid)
            return {"id": sid, "name": sid, "kind": "polygon", "pip": True, "zone_rules": FIRE_RULES,
                    "source": f"User-supplied file {path.name}", "source_url": "", "license": "see source"}
    return None


def assign_zone(gdf, src):
    import pandas as pd

    if "zone" in src:
        return pd.Series([src["zone"]] * len(gdf), index=gdf.index)
    zones = pd.Series([None] * len(gdf), index=gdf.index, dtype=object)
    for field, pattern, zone in src.get("zone_rules", []):
        cols = [c for c in gdf.columns if c != "geometry"] if field == "*" else ([field] if field in gdf.columns else [])
        if not cols:
            continue
        mask = pd.Series(False, index=gdf.index)
        for c in cols:
            mask |= gdf[c].astype(str).str.contains(pattern, regex=True, na=False)
        zones = zones.where(zones.notna(), pd.Series([zone] * len(gdf), index=gdf.index).where(mask))
    return zones


def read_any(path: Path):
    import geopandas as gpd
    import pandas as pd

    if path.suffix.lower() == ".csv":
        df = pd.read_csv(path)
        lat = next((c for c in df.columns if c.lower() in ("latitude", "lat", "y")), None)
        lon = next((c for c in df.columns if c.lower() in ("longitude", "lon", "lng", "x")), None)
        if not lat or not lon:
            raise ValueError("CSV needs latitude/longitude columns")
        return gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df[lon], df[lat]), crs="EPSG:4326")
    return gpd.read_file(path)


def process_hazards() -> None:
    import geopandas as gpd
    from shapely.geometry import box

    clip_box = box(*C.BBOX)
    manifest: list[dict] = []
    files = sorted(p for p in C.HAZARD_RAW_DIR.iterdir()
                   if p.suffix.lower() in (".geojson", ".json", ".shp", ".gpkg", ".kml", ".kmz", ".zip", ".csv"))
    if not files:
        log("[hazards] no raw files found; the map will show no hazard layers")
    for path in files:
        src = source_for_file(path)
        if src is None:
            log(f"[hazards] {path.name}: not a configured layer, left on disk and not loaded")
            continue
        try:
            gdf = read_any(path)
            if gdf.crs is None:
                gdf = gdf.set_crs("EPSG:4326")
            gdf = gdf.to_crs("EPSG:4326")
            gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].copy()
            if gdf.empty:
                log(f"[hazards] {path.name}: no geometries, skipped")
                continue
            kind = src.get("kind") or ("point" if gdf.geom_type.iloc[0].endswith("Point") else "polygon")
            if kind != "point":
                gdf["geometry"] = gdf.geometry.make_valid()
            if src.get("clip", True):
                gdf = gdf.clip(clip_box) if kind != "point" else gdf[gdf.intersects(clip_box)].copy()
                gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].copy()
            if gdf.empty:
                log(f"[hazards] {path.name}: nothing inside the bounding box, skipped")
                continue
            gdf["zone"] = assign_zone(gdf, src)
            gdf = gdf[gdf["zone"].notna()].copy()
            if gdf.empty:
                log(f"[hazards] {path.name}: no features matched the zone rules, skipped")
                continue
            gdf["label"] = gdf["zone"].map(lambda z: C.ZONE_STYLES.get(z, (z, "#777777"))[0])
            if kind != "point":
                gdf["geometry"] = gdf.geometry.simplify(C.HAZARD_SIMPLIFY_TOLERANCE_DEG, preserve_topology=True)
            keep = ["zone", "label"] + [c for c in src.get("keep_fields", []) if c in gdf.columns]
            out = gdf[keep + ["geometry"]].copy()
            for c in keep:
                if str(out[c].dtype).startswith("datetime"):
                    out[c] = out[c].astype(str)
            write_geojson(out, C.HAZARD_DIR / f"{src['id']}.geojson")
            zones = sorted(set(out["zone"]))
            manifest.append(manifest_entry(src, src["id"], kind, out, zones, path))
            log(f"[hazards] {src['id']}: {len(out)} features, zones={zones} -> data/hazards/{src['id']}.geojson")
            # buffered polygon layer for line hazards
            if kind == "line" and src.get("buffer_m"):
                buf = out.to_crs("EPSG:3310")
                buf["geometry"] = buf.buffer(src["buffer_m"])
                buf["zone"] = src["buffer_zone"]
                buf = buf.dissolve(by="zone").reset_index()[["zone", "geometry"]].to_crs("EPSG:4326")
                buf = buf.clip(clip_box)
                buf["label"] = C.ZONE_STYLES.get(src["buffer_zone"], (src["buffer_zone"], "#777"))[0]
                bid = f"{src['id']}_buffer"
                write_geojson(buf, C.HAZARD_DIR / f"{bid}.geojson")
                bsrc = dict(src, name=f"{src['name']} ({src['buffer_m']} m buffer)", pip=True)
                manifest.append(manifest_entry(bsrc, bid, "polygon", buf, [src["buffer_zone"]], path))
                log(f"[hazards] {bid}: {len(buf)} polygons -> data/hazards/{bid}.geojson")
        except Exception as e:  # noqa: BLE001
            log(f"[hazards] {path.name}: FAILED ({type(e).__name__}: {e}); skipped")
    C.HAZARD_MANIFEST.write_text(json.dumps(manifest, indent=1))
    log(f"[hazards] manifest: {len(manifest)} layers -> {C.HAZARD_MANIFEST.relative_to(ROOT)}")


def write_geojson(gdf, path: Path) -> None:
    try:
        gdf.to_file(path, driver="GeoJSON", COORDINATE_PRECISION="6")
    except Exception:  # noqa: BLE001
        gdf.to_file(path, driver="GeoJSON")


def manifest_entry(src: dict, lid: str, kind: str, gdf, zones: list, raw_path: Path) -> dict:
    return {
        "id": lid, "name": src["name"], "file": f"{lid}.geojson", "kind": kind, "pip": bool(src.get("pip")),
        "zones": {z: {"label": C.ZONE_STYLES.get(z, (z, "#777777"))[0], "color": C.ZONE_STYLES.get(z, (z, "#777777"))[1]} for z in zones},
        "features": int(len(gdf)), "source": src.get("source", ""), "source_url": src.get("source_url", ""),
        "license": src.get("license", ""), "raw_file": raw_path.name,
        "prepared": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }


# =============================================================== main
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-gazetteer", action="store_true")
    ap.add_argument("--skip-vendor", action="store_true")
    ap.add_argument("--skip-tiles", action="store_true")
    ap.add_argument("--skip-hazards", action="store_true")
    ap.add_argument("--only", choices=["gazetteer", "vendor", "tiles", "hazards"], help="run a single step")
    ap.add_argument("--max-zoom", type=int, default=C.TILE_MAX_ZOOM)
    ap.add_argument("--force", action="store_true", help="re-download even if cached")
    a = ap.parse_args()
    steps = {"gazetteer": not a.skip_gazetteer, "vendor": not a.skip_vendor, "tiles": not a.skip_tiles, "hazards": not a.skip_hazards}
    if a.only:
        steps = {k: (k == a.only) for k in steps}
    log(f"FieldMind data prep for {C.TOWN_NAME}; bbox={C.BBOX}; steps={[k for k, v in steps.items() if v]}")
    t0 = time.time()
    if steps["gazetteer"]:
        build_gazetteer(a.force)
    if steps["vendor"]:
        build_vendor(a.force)
    if steps["tiles"]:
        build_tiles(min(a.max_zoom, 16), a.force)
    if steps["hazards"]:
        download_sources(a.force)
        process_hazards()
    log(f"done in {time.time() - t0:.0f}s. FieldMind can now run offline.")


if __name__ == "__main__":
    main()
