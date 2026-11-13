"""
A tiny local stand-in for the parts of the Discogs API that discogs_dedupe.py uses:
  GET    /oauth/identity
  GET    /users/{user}/collection/folders/0/releases?per_page=&page=
  DELETE /users/{user}/collection/folders/{folder}/releases/{release}/instances/{instance}
It never talks to the real Discogs.
"""
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

TOKEN = "TESTTOKEN"
USERNAME = "tester"


def instance(release_id, instance_id, folder_id, date_added, artist, title):
    return {
        "id": release_id,
        "instance_id": instance_id,
        "folder_id": folder_id,
        "date_added": date_added,
        "rating": 0,
        "basic_information": {"id": release_id, "title": title, "artists": [{"name": artist}]},
    }


class FakeDiscogs:
    def __init__(self, collection):
        self.collection = [dict(c) for c in collection]
        self.deleted = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, obj=None):
                body = json.dumps(obj).encode() if obj is not None else b""
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("X-Discogs-Ratelimit-Remaining", "55")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _ok_headers(self):
                return (self.headers.get("Authorization") == f"Discogs token={TOKEN}"
                        and self.headers.get("User-Agent"))

            def do_GET(self):
                if not self._ok_headers():
                    return self._send(401, {"message": "You must authenticate to access this resource."})
                url = urlparse(self.path)
                if url.path == "/oauth/identity":
                    return self._send(200, {"id": 1, "username": USERNAME})
                if url.path == f"/users/{USERNAME}/collection/folders/0/releases":
                    q = parse_qs(url.query)
                    per_page = int(q.get("per_page", ["50"])[0])
                    page = int(q.get("page", ["1"])[0])
                    live = fake.live()
                    pages = max(1, -(-len(live) // per_page))
                    chunk = live[(page - 1) * per_page: page * per_page]
                    return self._send(200, {
                        "pagination": {"page": page, "pages": pages, "per_page": per_page, "items": len(live)},
                        "releases": chunk,
                    })
                return self._send(404, {"message": "The requested resource was not found."})

            def do_DELETE(self):
                if not self._ok_headers():
                    return self._send(401, {"message": "You must authenticate to access this resource."})
                m = re.fullmatch(rf"/users/{USERNAME}/collection/folders/(\d+)/releases/(\d+)/instances/(\d+)",
                                 urlparse(self.path).path)
                if not m:
                    return self._send(404, {"message": "The requested resource was not found."})
                folder, release, inst = map(int, m.groups())
                match = [c for c in fake.live() if c["instance_id"] == inst]
                if folder == 0 or not match or match[0]["id"] != release or match[0]["folder_id"] != folder:
                    return self._send(404, {"message": "The requested resource was not found."})
                fake.deleted.append(inst)
                return self._send(204)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def live(self):
        return [c for c in self.collection if c["instance_id"] not in self.deleted]

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
