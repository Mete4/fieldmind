#!/usr/bin/env python3
"""Write the offline map (hazards + key facilities, no incidents) to
data/preview/map_preview.html and print its local URL. The tile server
(app/tileserver.py) must be running."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import config as C  # noqa: E402
from app.mapview import build_map  # noqa: E402

out = C.DATA_DIR / "preview" / "map_preview.html"
out.parent.mkdir(parents=True, exist_ok=True)
html = build_map()
out.write_text(html)
import re
cdn_left = sorted(set(re.findall(r'(?:src|href)="(https://[^"]+)"', html)))
print(f"wrote {out.relative_to(ROOT)} ({len(html) / 1e6:.2f} MB); external https refs left: {cdn_left or 'none'}")
print(f"open http://127.0.0.1:{C.TILE_SERVER_PORT}/preview/map_preview.html")
