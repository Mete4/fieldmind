#!/usr/bin/env python3
"""Render a static PNG of the offline map: cached OSM tiles + real hazard
layers + key facilities. Needs no network. Output: docs/map_snapshot.png"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import config as C  # noqa: E402

import geopandas as gpd  # noqa: E402
import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch, Rectangle  # noqa: E402
from PIL import Image  # noqa: E402

Z = int(sys.argv[1]) if len(sys.argv) > 1 else 14
R = 6378137.0


def deg2num(lat, lon, z):
    n = 2 ** z
    return int((lon + 180) / 360 * n), int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)


def num2deg(x, y, z):
    n = 2 ** z
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n)))), x / n * 360 - 180


def merc(lat, lon):
    return R * math.radians(lon), R * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


min_lon, min_lat, max_lon, max_lat = C.BBOX
x0, y0 = deg2num(max_lat, min_lon, Z)
x1, y1 = deg2num(min_lat, max_lon, Z)
W, H = (x1 - x0 + 1) * 256, (y1 - y0 + 1) * 256
img = Image.new("RGB", (W, H), (235, 235, 235))
missing = 0
for x in range(x0, x1 + 1):
    for y in range(y0, y1 + 1):
        p = C.TILE_DIR / str(Z) / str(x) / f"{y}.png"
        if p.exists():
            img.paste(Image.open(p).convert("RGB"), ((x - x0) * 256, (y - y0) * 256))
        else:
            missing += 1
lat_n, lon_w = num2deg(x0, y0, Z)
lat_s, lon_e = num2deg(x1 + 1, y1 + 1, Z)
xw, yn = merc(lat_n, lon_w)
xe, ys = merc(lat_s, lon_e)
bx0, by0 = merc(min_lat, min_lon)
bx1, by1 = merc(max_lat, max_lon)

fig, ax = plt.subplots(figsize=(12, 12 * (by1 - by0) / (bx1 - bx0)), dpi=150)
ax.imshow(np.asarray(img), extent=[xw, xe, ys, yn], interpolation="bilinear")
handles: dict[str, Patch] = {}
for L in json.loads(C.HAZARD_MANIFEST.read_text()):
    g = gpd.read_file(C.HAZARD_DIR / L["file"]).to_crs(3857)
    for code, z in L["zones"].items():
        sub = g[g["zone"] == code]
        if sub.empty:
            continue
        if L["kind"] == "point":
            sub.plot(ax=ax, color=z["color"], markersize=22, edgecolor="k", linewidth=0.5, zorder=5)
            handles[code] = Line2D([], [], marker="o", color=z["color"], markeredgecolor="k", ls="", ms=7, label=z["label"])
        elif L["pip"]:
            sub.plot(ax=ax, facecolor=z["color"], edgecolor=z["color"], alpha=0.40, linewidth=0.6, zorder=3)
            handles[code] = Patch(facecolor=z["color"], edgecolor=z["color"], alpha=0.6, label=z["label"])
        else:
            sub.boundary.plot(ax=ax, color="#222", linewidth=1.8, linestyle="--", zorder=4)
            handles[code] = Line2D([], [], color="#222", ls="--", lw=1.8, label=z["label"])
POI = {"hospital": ("H", "#c51b7d"), "fire_station": ("F", "#d73027"), "police": ("P", "#1a9850")}
for e in json.loads(C.GAZETTEER_PATH.read_text()):
    if e["type"] in POI:
        letter, col = POI[e["type"]]
        mx, my = merc(e["lat"], e["lon"])
        ax.text(mx, my, letter, fontsize=6.5, color="w", ha="center", va="center", zorder=7, weight="bold",
                bbox=dict(boxstyle="round,pad=0.25", fc=col, ec="w", lw=0.6))
cx, cy = merc(C.TOWN_CENTER_LAT, C.TOWN_CENTER_LON)
ax.plot(cx, cy, marker="*", color="k", ms=14, zorder=8)
ax.annotate("Paradise", (cx, cy), xytext=(8, 6), textcoords="offset points", fontsize=10, weight="bold", zorder=8)
ax.add_patch(Rectangle((bx0, by0), bx1 - bx0, by1 - by0, fill=False, ec="k", lw=1.2, ls=":", zorder=6))
ax.set_xlim(bx0, bx1)
ax.set_ylim(by0, by1)
ax.set_axis_off()
leg = list(handles.values()) + [Line2D([], [], marker="*", color="k", ls="", ms=10, label="Town center"),
                                Line2D([], [], marker="s", color="#d73027", ls="", ms=7, label="H hospital, F fire station, P police (OSM)")]
ax.legend(handles=leg, loc="lower left", fontsize=7.5, framealpha=0.92, title=C.SCENARIO_BANNER, title_fontsize=8.5)
ax.text(0.995, 0.006, "(c) OpenStreetMap contributors (ODbL) | Hazards: CAL FIRE FHSZ, FEMA NFHL, CAL FIRE FRAP, USGS | Incidents: simulated",
        transform=ax.transAxes, ha="right", va="bottom", fontsize=6.5, bbox=dict(fc="w", alpha=0.85, ec="none", pad=2))
ax.set_title(f"FieldMind - {C.TOWN_NAME}: real hazard layers on offline OSM tiles (z{Z}, {W}x{H} px, {missing} tiles missing)", fontsize=10)
out = ROOT / "docs" / "map_snapshot.png"
out.parent.mkdir(exist_ok=True)
fig.savefig(out, dpi=150, bbox_inches="tight", pad_inches=0.05)
print(f"wrote {out.relative_to(ROOT)}  ({out.stat().st_size / 1e6:.1f} MB)")
