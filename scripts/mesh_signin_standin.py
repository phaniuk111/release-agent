#!/usr/bin/env python3
"""DEMO ONLY — a stand-in for Anthos Service Mesh user auth (authservice).

In the real deployment the person signs in with the company's identity
provider AT THE MESH: authservice runs the OIDC login, keeps the session, and
puts a signed RCToken on every request it lets through. Backstage (the
`rctoken` auth provider) and the release agent (identity.py) then VERIFY that
token against authservice's JWKS — neither has a login form of its own.

This script plays authservice's part for a PoC where no identity provider is
wired up, so the rest of the chain can be shown end to end:

  - it makes a throwaway RSA key at start (never written anywhere) and serves
    its public half at /_standin/jwks;
  - every other request is forwarded to UPSTREAM with a short-lived RCToken in
    the X-RCToken header, for the demo person in STANDIN_EMAIL (the step a real
    IdP login would decide); responses stream through (the chat is SSE).

It is not a security boundary: whoever reaches it is that demo person. Never
run it where the real authservice belongs.

  UPSTREAM=http://backstage:7007 STANDIN_EMAIL=alice@example.com \\
  STANDIN_ISSUER=mesh-signin-standin STANDIN_AUDIENCE=release-portal \\
  python scripts/mesh_signin_standin.py            # listens on :8080
"""
from __future__ import annotations

import base64
import http.client
import json
import os
import secrets
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

UPSTREAM = urlsplit(os.environ.get("UPSTREAM", "http://localhost:7007"))
EMAIL = os.environ.get("STANDIN_EMAIL", "alice@example.com")
NAME = os.environ.get("STANDIN_NAME", "Alice Example")
ISSUER = os.environ.get("STANDIN_ISSUER", "mesh-signin-standin")
AUDIENCE = os.environ.get("STANDIN_AUDIENCE", "release-portal")
HEADER = os.environ.get("STANDIN_HEADER", "X-RCToken")
PORT = int(os.environ.get("PORT", "8080"))
# A new key id per start: a verifier caches keys by kid, and refetches the JWKS
# only for a kid it has not seen — a restarted stand-in must not reuse one.
KID = f"standin-{secrets.token_hex(4)}"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
        "transfer-encoding", "upgrade", "content-length", "host"}


def _b64(n: int) -> str:
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def jwks() -> dict:
    pub = _KEY.public_key().public_numbers()
    return {"keys": [{"kty": "RSA", "kid": KID, "alg": "RS256", "use": "sig", "n": _b64(pub.n), "e": _b64(pub.e)}]}


def rctoken() -> str:
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": AUDIENCE, "iat": now, "exp": now + 300, "sub": EMAIL,
              "attributes": {"email": EMAIL, "name": NAME}}
    return jwt.encode(claims, _KEY, algorithm="RS256", headers={"kid": KID})


class Gateway(BaseHTTPRequestHandler):
    def _forward(self) -> None:
        if self.path == "/_standin/jwks":
            body = json.dumps(jwks()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {k: v for k, v in self.headers.items() if k.lower() not in _HOP and k.lower() != HEADER.lower()}
        headers[HEADER] = rctoken()          # whatever the client sent, the gateway decides who it is
        conn = http.client.HTTPConnection(UPSTREAM.hostname, UPSTREAM.port or 80, timeout=300)
        conn.request(self.command, self.path, body=body, headers=headers)
        resp = conn.getresponse()
        self.send_response(resp.status, resp.reason)
        for k, v in resp.getheaders():
            if k.lower() not in _HOP:
                self.send_header(k, v)
        self.send_header("Connection", "close")
        self.end_headers()
        while True:                          # stream: the chat answers as SSE
            chunk = resp.read1(8192) if hasattr(resp, "read1") else resp.read(8192)
            if not chunk:
                break
            self.wfile.write(chunk)
            self.wfile.flush()
        conn.close()

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _forward

    def log_message(self, fmt, *args):  # one short line per request, no tokens
        print(f"{self.command} {self.path.split('?')[0]} -> {args[1] if len(args) > 1 else ''}", flush=True)


if __name__ == "__main__":
    print(f"DEMO mesh sign-in stand-in on :{PORT} → {UPSTREAM.geturl()} as {EMAIL} (issuer {ISSUER})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Gateway).serve_forever()
