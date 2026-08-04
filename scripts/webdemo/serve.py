"""Local preview server for the web demo. `python serve.py [port]` from site/.

Plain `python -m http.server` cannot serve this site, for two reasons found
the hard way:

1. The engine needs COOP same-origin + COEP require-corp on every response
   (pthreads -> SharedArrayBuffer needs a cross-origin-isolated page). On
   Netlify the bundled `_headers` file does this; locally we must.

2. SimpleHTTPRequestHandler ignores the Range header and answers a 1 KB
   range request with 200 and the whole file. Emscripten's loader trips over
   that on the 57 MB uzdoom.data with a looping "Unexpected error while
   handling" that looks exactly like a corrupt build.
"""

import http.server
import os
import re
import socketserver
import sys
from pathlib import Path

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
ROOT = Path(__file__).resolve().parent

MIME = {
    ".wasm": "application/wasm",
    ".js": "text/javascript",
    ".html": "text/html",
    ".json": "application/json",
    ".png": "image/png",
    ".data": "application/octet-stream",
    ".pk3": "application/octet-stream",
    ".wad": "application/octet-stream",
}

RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


class Handler(http.server.SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT), **kw)

    def end_headers(self):
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
        self.send_header("Cross-Origin-Resource-Policy", "cross-origin")
        self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def guess_type(self, path):
        for ext, mime in MIME.items():
            if path.endswith(ext):
                return mime
        return super().guess_type(path)

    def do_GET(self):
        rng = self.headers.get("Range")
        if not rng:
            return super().do_GET()
        path = self.translate_path(self.path)
        if not os.path.isfile(path):
            return super().do_GET()
        m = RANGE_RE.match(rng.strip())
        if not m:
            self.send_error(400, "Malformed Range")
            return
        size = os.path.getsize(path)
        start_s, end_s = m.group(1), m.group(2)
        if start_s == "":
            length = int(end_s or 0)
            start, end = max(0, size - length), size - 1
        else:
            start = int(start_s)
            end = min(int(end_s) if end_s else size - 1, size - 1)
        if start > end or start >= size:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        length = end - start + 1
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(length))
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def log_message(self, fmt, *args):
        if len(args) > 1 and str(args[1]).startswith(("4", "5")):
            sys.stderr.write(f"HTTP {args[1]} {args[0]}\n")


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    with Server(("127.0.0.1", PORT), Handler) as httpd:
        print(f"serving {ROOT} on http://127.0.0.1:{PORT}/", flush=True)
        httpd.serve_forever()
