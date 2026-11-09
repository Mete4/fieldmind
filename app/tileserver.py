"""Tiny static file server for the offline map: OSM tiles, vendored Leaflet
assets and map previews, all from data/. Started by run.sh on
config.TILE_SERVER_PORT. Adds CORS + cache headers so the Streamlit iframe
(origin "null") can load everything with no internet."""
from __future__ import annotations

import functools
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import config as C  # noqa: E402


# 256x256 light-grey PNG served for tiles that are not in the cache (outside
# the bounding box or beyond the cached zoom), so the map never shows broken
# images and Chrome never blocks a 404 HTML body as an image response.
_BLANK_TILE = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000001000000010008060000005c72a8660000"
    "025d4944415478daedd44101003008c4b031ff464f0518c00189843e5a49fa01277d09"
    "c0000003000c003000c0000003000c003000c0000003000c003000c0000003000c0030"
    "00c0000003000c003000c0000003000c003000c0000003000c003000c0000003000c00"
    "3000c0000003000c0030003000c0000003000c003000c0000003000c003000c0000003"
    "000c003000c0000003000c003000c0000003000c003000c0000003000c003000c00000"
    "03000c003000c0000003000c003000c0000003000c0030003000c0000003000c003000"
    "c0000003000c003000c0000003000c003000c0000003000c003000c0000003000c0030"
    "00c0000003000c003000c0000003000c003000c0000003000c003000c0000003000c00"
    "30003000c0000003000c003000c0000003000c003000c0000003000c003000c0000003"
    "000c003000c0000003000c003000c0000003000c003000c0000003000c003000c00000"
    "03000c003000c0000003000c0030003000c0000003000c003000c0000003000c003000"
    "c0000003000c003000c0000003000c003000c0000003000c003000c0000003000c0030"
    "00c0000003000c003000c0000003000c003000c0000003000c0030003000c000000300"
    "0c003000c0000003000c003000c0000003000c003000c0000003000c003000c0000003"
    "000c003000c0000003000c003000c0000003000c003000c0000003000c003000c00000"
    "03000c0030003000c0000003000c003000c0000003000c003000c0000003000c003000"
    "c0000003000c003000c0000003000c003000c0000003000c003000c0000003000c0030"
    "00c0000003000c0030006033cdad05ba2cc3110b0000000049454e44ae426082")


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.startswith("/tiles/") and self.path.endswith(".png"):
            local = Path(self.directory) / self.path.lstrip("/").split("?")[0]
            if not local.exists():
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(_BLANK_TILE)))
                self.end_headers()
                self.wfile.write(_BLANK_TILE)
                return
        super().do_GET()

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "public, max-age=86400")
        super().end_headers()

    def log_message(self, fmt: str, *args) -> None:  # only log errors, tiles are noisy
        if len(args) >= 2 and str(args[1]).startswith(("4", "5")):
            super().log_message(fmt, *args)


def main(port: int = C.TILE_SERVER_PORT) -> None:
    handler = functools.partial(Handler, directory=str(C.DATA_DIR))
    httpd = ThreadingHTTPServer((C.TILE_BIND_HOST, port), handler)
    print(f"FieldMind tile server bound to {C.TILE_BIND_HOST}:{port}, browsers use http://{C.PUBLIC_HOST}:{port}/ ; serving {C.DATA_DIR}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else C.TILE_SERVER_PORT)
