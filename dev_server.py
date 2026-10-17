"""
Run the whole demo on a laptop:  python dev_server.py   then open http://localhost:8000

- With SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY set (or in a .env file), it uses Supabase.
- Without them it runs fully OFFLINE from data/local_db.json (created from data/seed.json).
  Delete data/local_db.json to start fresh.
"""

import importlib.util
import json
import os
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))

env_file = os.path.join(ROOT, ".env")
if os.path.exists(env_file):
    for line in open(env_file):
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.strip().split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"'))
if os.environ.get("OFFLINE") == "1":
    os.environ.pop("SUPABASE_URL", None)

spec = importlib.util.spec_from_file_location("api_core", os.path.join(ROOT, "api", "index.py"))
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def _api(self, method):
        parsed = urllib.parse.urlparse(self.path)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        body = None
        if method == "POST":
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        status, payload = core.dispatch(method, parsed.path, query, body)
        out = json.dumps(payload, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        if self.path.startswith("/api/"):
            return self._api("GET")
        return super().do_GET()

    def do_POST(self):
        if self.path.startswith("/api/"):
            return self._api("POST")
        self.send_error(404)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"Store: {type(core.get_store()).__name__}  ->  http://localhost:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
