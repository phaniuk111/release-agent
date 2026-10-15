"""A throwaway stand-in for an Artifactory GENERIC repo, for tests only.

It answers the few calls scripts/artifactory.sh makes, the way Artifactory does:
PUT stores a file and refuses a body whose SHA-256 differs from the
X-Checksum-Sha256 header (409); GET and HEAD return it or 404; a wrong or
missing credential is 401. Basic user:token and Bearer token are both accepted.

    python3 artifactory_stand_in.py --root DIR --port 8081 --user ci --token T

Files live under DIR exactly as their URL path after /artifactory/.
Binds to 127.0.0.1 only.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import http.server
import os
import pathlib

PREFIX = "/artifactory/"


def make_handler(root: pathlib.Path, user: str, token: str):
    good = {f"Bearer {token}", "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()}

    class Handler(http.server.BaseHTTPRequestHandler):
        def _target(self) -> pathlib.Path | None:
            if not self.path.startswith(PREFIX):
                return None
            rel = self.path[len(PREFIX):].split("?", 1)[0]
            parts = [p for p in rel.split("/") if p]
            if not parts or any(p in (".", "..") for p in parts):
                return None
            return root.joinpath(*parts)

        def _authorised(self) -> bool:
            sent = self.headers.get("Authorization", "")
            if any(hmac.compare_digest(sent, g) for g in good):
                return True
            self._reply(401, b'{"errors":[{"status":401,"message":"Bad credentials"}]}')
            return False

        def _reply(self, code: int, body: bytes = b"", head: bool = False) -> None:
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body and not head:
                self.wfile.write(body)

        def do_PUT(self) -> None:
            if not self._authorised():
                return
            target = self._target()
            if target is None:
                return self._reply(400)
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            want = self.headers.get("X-Checksum-Sha256", "")
            got = hashlib.sha256(body).hexdigest()
            if want and want != got:
                return self._reply(409, b'{"errors":[{"status":409,"message":"Checksum mismatch"}]}')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
            self._reply(201, b"{}")

        def _get(self, head: bool) -> None:
            if not self._authorised():
                return
            target = self._target()
            if target is None or not target.is_file():
                return self._reply(404, b'{"errors":[{"status":404}]}', head)
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Checksum-Sha256", hashlib.sha256(data).hexdigest())
            self.end_headers()
            if not head:
                self.wfile.write(data)

        def do_GET(self) -> None:
            self._get(head=False)

        def do_HEAD(self) -> None:
            self._get(head=True)

        def log_message(self, fmt: str, *args) -> None:  # one line per call, no headers
            print(f"stand-in: {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}", flush=True)

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--user", default="ci")
    ap.add_argument("--token", default=os.environ.get("STAND_IN_TOKEN", "stand-in-token"))
    a = ap.parse_args()
    root = pathlib.Path(a.root)
    root.mkdir(parents=True, exist_ok=True)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(root, a.user, a.token))
    print(f"stand-in Artifactory on http://127.0.0.1:{a.port}/artifactory (root {root})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
