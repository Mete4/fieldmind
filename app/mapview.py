"""Offline folium map for FieldMind: local OSM tiles, real hazard layers,
incident pins, legend. The HTML it returns references only
http://localhost:<TILE_SERVER_PORT>/ (tiles + vendored Leaflet), so it renders
with no internet inside the Streamlit dashboard."""
from __future__ import annotations

import html as _html
import json
import re
from urllib.parse import urlparse
from typing import Iterable

import folium

from app import config as C

SEVERITY_COLORS = {1: "#2b83ba", 2: "#74add1", 3: "#fdae61", 4: "#f46d43", 5: "#d7191c"}
TYPE_LETTERS = {"fire": "F", "collapse": "C", "medical": "M",
                "road_blocked": "R", "trapped": "T", "other": "O"}
POI_TYPES = {"hospital": ("H", "#c51b7d"), "fire_station": ("F", "#d73027"),
             "police": ("P", "#1a9850"), "school": ("S", "#4575b4")}


def load_layer_manifest() -> list[dict]:
    from app.hazards import active_layers

    return active_layers()


def _vendor_base() -> str:
    return f"http://{C.PUBLIC_HOST}:{C.TILE_SERVER_PORT}/vendor/"


_ASSET_RE = re.compile(r'(?P<attr>src|href)="(?P<url>https?://[^"]+)"')


def make_offline(html: str) -> str:
    """Rewrite every CDN script/link URL to the locally cached copy under
    data/vendor/<host>/<path> (served by app/tileserver.py). URLs with no
    cached copy are left alone and logged once."""
    base = _vendor_base()
    manifest_path = C.VENDOR_DIR / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    missing: list[str] = []

    def repl(m: re.Match) -> str:
        url = m.group("url")
        if url in manifest:
            return f'{m.group("attr")}="{base}{manifest[url]}"'
        p = urlparse(url)
        rel = f"{p.netloc}{p.path}"
        if (C.VENDOR_DIR / p.netloc / p.path.lstrip("/")).exists():
            return f'{m.group("attr")}="{base}{rel}"'
        missing.append(url)
        return m.group(0)

    out = _ASSET_RE.sub(repl, html)
    if missing:
        print(f"[mapview] {len(missing)} assets not cached offline: {missing}")
    return out


def _legend_html(layers: list[dict]) -> str:
    rows = []
    seen = set()
    for L in layers:
        for code, z in L["zones"].items():
            if code in seen:
                continue
            seen.add(code)
            shape = "border-radius:50%;" if L["kind"] == "point" else ""
            rows.append(f"<div style='margin:2px 0'><span style='display:inline-block;width:14px;height:14px;{shape}"
                        f"background:{z['color']};opacity:.75;border:1px solid #444;vertical-align:middle;margin-right:6px'></span>"
                        f"<span style='vertical-align:middle'>{_html.escape(z['label'])}</span></div>")
    sev = "".join(f"<span style='display:inline-block;width:12px;height:12px;border-radius:50%;background:{c};"
                  f"border:1px solid #333;margin:0 2px 0 6px;vertical-align:middle'></span>{s}" for s, c in SEVERITY_COLORS.items())
    return f"""
<details style="position:fixed; bottom:28px; left:12px; z-index:9999; background:rgba(255,255,255,.93);
     padding:6px 10px; border:1px solid #999; border-radius:6px; font:12px/1.35 sans-serif; max-width:330px">
  <summary style="font-weight:700;cursor:pointer">{_html.escape(C.SCENARIO_BANNER)} - legend</summary>
  <div style="color:#555;margin-bottom:6px">{_html.escape(C.TOWN_NAME)} - {_html.escape(C.SCENARIO_NAME)}</div>
  {''.join(rows)}
  <div style="margin-top:6px;border-top:1px solid #ddd;padding-top:4px">Incident severity: {sev}</div>
</details>"""


def add_incident(m: folium.Map, inc: dict) -> None:
    lat, lon = inc.get("lat"), inc.get("lon")
    if lat is None or lon is None:
        return
    sev = int(inc.get("severity") or 3)
    color = SEVERITY_COLORS.get(sev, "#999")
    letter = TYPE_LETTERS.get(inc.get("type", "other"), "O")
    zones = inc.get("hazard_zones") or []
    units = ", ".join(inc.get("unit") or []) or "-"
    depts = "/".join(inc.get("department") or []) or "-"
    n = int(inc.get("report_count") or 1)
    details = _html.escape(str(inc.get("details") or "")).replace("\n", "<br>")
    label = inc.get("label") or inc.get("id", "")[:6]
    popup = (f"<b>{_html.escape(label)}</b> &nbsp; <b>{_html.escape(str(inc.get('type', '?')).upper())}</b> severity {sev} | people {inc.get('people_count', '?')} | "
             f"{n} report{'s' if n != 1 else ''} | {_html.escape(depts)}: {_html.escape(units)}<br>"
             f"<b>Where:</b> {_html.escape(str(inc.get('geocode_match') or inc.get('location_text') or 'unknown'))}"
             f"{' (location unknown, pinned at town center)' if inc.get('location_unknown') else ''}<br>"
             f"<b>Fire:</b> from {inc.get('fire_direction') or '?'}, size {inc.get('fire_size') or '?'}, part {_html.escape(str(inc.get('structure_part') or '-'))}<br>"
             f"<b>Needs:</b> {_html.escape(', '.join(inc.get('needs') or []) or '-')} | <b>hazard zones:</b> {_html.escape(', '.join(zones) or 'none')}<br>"
             f"<i>{_html.escape(str(inc.get('summary') or inc.get('transcript') or '')[:300])}</i><br>"
             f"<b>Details:</b> {details[:700]}<br>"
             + (f"<b>Needs review:</b> {_html.escape(', '.join(inc.get('review_reasons') or []))}<br>" if inc.get("needs_review") else "")
             + f"<small>status {_html.escape(str(inc.get('status', '')))} | id {inc.get('id', '')}</small>")
    review = inc.get("needs_review")
    folium.CircleMarker([lat, lon], radius=10, color="#fff" if review else "#222", weight=2.5 if review else 1, dash_array="4 3" if review else None,
                        fill=True, fill_color=color, fill_opacity=0.95, popup=folium.Popup(popup, max_width=420),
                        tooltip=f"{label} | {inc.get('type', '?')} sev {sev} | {n} report(s) | {units}").add_to(m)
    badge = (f"<div style='position:absolute;left:8px;top:-12px;background:#fff;border:1.5px solid #222;border-radius:9px;"
             f"min-width:16px;height:16px;font:bold 10px/16px sans-serif;text-align:center;padding:0 3px'>{n}</div>" if n > 1 else "")
    folium.Marker([lat, lon], icon=folium.DivIcon(html=f"<div style='position:relative;width:14px;height:14px;margin:-7px 0 0 -7px;text-align:center;font:bold 11px/14px sans-serif;color:#fff'>{letter}{badge}</div>",
                                                  icon_size=(14, 14), icon_anchor=(0, 0)),
                  tooltip=f"{label} | {inc.get('type', '?')} sev {sev} | {n} report(s) | {units}").add_to(m)


def build_map(incidents: Iterable[dict] = (), enabled_layers: set[str] | None = None,
              show_pois: bool = True, center: tuple[float, float] | None = None, zoom: int | None = None) -> str:
    """Return self-contained HTML for the offline map (open in an iframe)."""
    return make_offline(make_map(incidents, enabled_layers, show_pois, center, zoom).get_root().render())


def make_map(incidents: Iterable[dict] = (), enabled_layers: set[str] | None = None,
             show_pois: bool = True, center: tuple[float, float] | None = None, zoom: int | None = None) -> folium.Map:
    """The folium Map object (for st_folium click handling)."""
    m = folium.Map(location=list(center) if center else [C.TOWN_CENTER_LAT, C.TOWN_CENTER_LON], zoom_start=zoom or C.MAP_ZOOM, tiles=None,
                   control_scale=True, prefer_canvas=True)
    folium.TileLayer(tiles=C.TILE_URL_TEMPLATE, attr=C.MAP_ATTRIBUTION, name="OpenStreetMap (offline cache z10-16)",
                     min_zoom=C.TILE_MIN_ZOOM, max_zoom=18, max_native_zoom=C.TILE_MAX_ZOOM,
                     bounds=[[C.BBOX[1], C.BBOX[0]], [C.BBOX[3], C.BBOX[2]]]).add_to(m)
    layers = load_layer_manifest()
    for L in layers:
        if enabled_layers is not None and L["id"] not in enabled_layers:
            continue
        data = json.loads((C.HAZARD_DIR / L["file"]).read_text())
        if L["kind"] == "point":
            fg = folium.FeatureGroup(name=L["name"], show=True)
            for f in data["features"]:
                lon, lat = f["geometry"]["coordinates"][:2]
                p = f["properties"]
                detail = ", ".join(f"{k}={v}" for k, v in p.items() if k not in ("zone", "label") and v not in (None, ""))
                folium.CircleMarker([lat, lon], radius=4, color=L["zones"][p["zone"]]["color"], fill=True, fill_opacity=0.85,
                                    tooltip=f"{p.get('label')}: {detail}").add_to(fg)
            fg.add_to(m)
        else:
            zones = L["zones"]
            pip = L["pip"]

            def style(feat, zones=zones, pip=pip):
                z = feat["properties"].get("zone")
                col = zones.get(z, {}).get("color", "#777")
                if not pip:                                   # context layer (Camp Fire perimeter): outline only
                    return {"fillColor": col, "color": "#333", "weight": 2.5, "fillOpacity": 0.0, "dashArray": "8 6"}
                if z == "fire_high":                          # outline only, so streets stay readable
                    return {"fillColor": col, "color": col, "weight": 1.5, "fillOpacity": 0.05}
                return {"fillColor": col, "color": col, "weight": 0, "fillOpacity": 0.12}

            folium.GeoJson(data, name=L["name"], style_function=style, show=False,
                           tooltip=folium.GeoJsonTooltip(fields=["label"], labels=False)).add_to(m)
    if show_pois and C.GAZETTEER_PATH.exists():
        fg = folium.FeatureGroup(name="Key facilities (OpenStreetMap)", show=True)
        for e in json.loads(C.GAZETTEER_PATH.read_text()):
            if e["type"] in POI_TYPES:
                letter, col = POI_TYPES[e["type"]]
                folium.Marker([e["lat"], e["lon"]], tooltip=f"{e['name']} ({e['type'].replace('_', ' ')})",
                              icon=folium.DivIcon(html=f"<div style='background:{col};color:#fff;font:bold 10px/16px sans-serif;"
                                                       f"width:16px;height:16px;text-align:center;border-radius:3px;border:1px solid #fff;"
                                                       f"margin:-8px 0 0 -8px'>{letter}</div>", icon_size=(16, 16), icon_anchor=(0, 0))).add_to(fg)
        fg.add_to(m)
    for inc in incidents:
        try:
            add_incident(m, inc)
        except Exception:  # noqa: BLE001  never let one bad record kill the map
            continue
    folium.LayerControl(collapsed=True).add_to(m)
    m.get_root().html.add_child(folium.Element(_legend_html(layers)))
    return m
