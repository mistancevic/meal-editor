"""Meal editor — Python stdlib only, nothing to install.

    python app.py            # then open http://localhost:8000

GET  /               -> the editor page (web/index.html)
GET  /api/library    -> ingredients, effects, tracks, transition kinds, demo days
POST /api/render     -> macro math + tolerance check + timing notes + plan export
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from mealeditor import engine  # noqa: E402

PORT = int(os.environ.get("PORT", "8000"))
LIB = engine.Library.load()
DEMOS = engine.load_demo_days()
INDEX_HTML = ROOT / "web" / "index.html"


def library_payload() -> dict:
    payload = LIB.payload()
    payload["demos"] = DEMOS
    payload["tolerance"] = dict(engine.DEFAULT_TOLERANCE)
    return payload


def handle_render(body: dict) -> dict:
    profile = engine.profile_from_dict(body.get("profile"))
    project = engine.project_from_dict(body.get("project") or {}, LIB)
    rendered = engine.render(project, LIB, profile)
    rendered["export_markdown"] = engine.export_plan_markdown(rendered, profile.name, profile.goal)
    return rendered


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, content: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _json(self, code: int, data: dict) -> None:
        self._send(code, json.dumps(data).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html", "/editor"):
            self._send(200, INDEX_HTML.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/library":
            self._json(200, library_payload())
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if urlparse(self.path).path != "/api/render":
            self._json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            self._json(200, handle_render(body))
        except Exception as exc:  # surface the error to the UI instead of a blank 500
            self._json(400, {"error": str(exc)})

    def log_message(self, fmt, *args):  # quieter console
        pass


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Meal editor running: http://localhost:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBye.")


if __name__ == "__main__":
    main()
