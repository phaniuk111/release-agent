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
  - every other request is forwarded upstream with a short-lived RCToken in
    the X-RCToken header for the person signed in here; responses stream
    through (the chat is SSE).

Who that person is (the step a real IdP login decides):
  - one fixed person: STANDIN_EMAIL (+ STANDIN_NAME) and no STANDIN_USERS;
  - several: STANDIN_USERS="alice@example.com=Alice Example,bob@example.com=Bob
    Example" — /_standin/login picks one (a cookie), /_standin/logout forgets
    it, and a load client may say X-Standin-User: <email> per request.

ROUTES="/api=http://localhost:7007,/.backstage=http://localhost:7007" sends
those paths to one upstream and the rest to UPSTREAM — a Backstage dev setup
(backend :7007, frontend :3000) then looks like one origin, as in production.

It is not a security boundary: whoever reaches it is whoever they say. Never
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
from html import escape
from urllib.parse import parse_qs, quote, urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

UPSTREAM = urlsplit(os.environ.get("UPSTREAM", "http://localhost:7007"))
ROUTES = [(prefix.strip(), urlsplit(target.strip()))
          for prefix, _, target in (r.partition("=") for r in os.environ.get("ROUTES", "").split(",")) if target]
EMAIL = os.environ.get("STANDIN_EMAIL", "alice@example.com")
NAME = os.environ.get("STANDIN_NAME", "Alice Example")
USERS = {e.strip().lower(): (n.strip() or e.strip())
         for e, _, n in (u.partition("=") for u in os.environ.get("STANDIN_USERS", "").split(",")) if "@" in e}
COOKIE = "standin_user"
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


def rctoken(email: str = EMAIL, name: str = NAME) -> str:
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": AUDIENCE, "iat": now, "exp": now + 300, "sub": email,
              "attributes": {"email": email, "name": name}}
    return jwt.encode(claims, _KEY, algorithm="RS256", headers={"kid": KID})


def _upstream(path: str):
    for prefix, target in ROUTES:
        if path == prefix or path.startswith(prefix + "/") or path.startswith(prefix + "?"):
            return target
    return UPSTREAM


def _name_for(email: str) -> str:
    return USERS.get(email) or email.split("@")[0].replace(".", " ").title()


def _login_page(next_path: str) -> bytes:
    people = "".join(
        f'<button name="email" value="{escape(e)}">{escape(n)}<small>{escape(e)}</small></button>'
        for e, n in USERS.items())
    return f"""<!doctype html><meta charset="utf-8"><title>Sign in (demo)</title>
<style>body{{font-family:system-ui;background:#0b1020;color:#e2e8f0;display:grid;place-items:center;height:100vh;margin:0}}
form{{background:#111827;border:1px solid #334155;border-radius:16px;padding:28px;width:340px}}
h1{{font-size:18px;margin:0 0 4px}}p{{color:#94a3b8;font-size:13px;margin:0 0 18px}}
button{{display:block;width:100%;text-align:left;margin:8px 0;padding:10px 14px;border-radius:10px;border:1px solid #334155;
background:#0f172a;color:#e2e8f0;font-size:15px;cursor:pointer}}button:hover{{border-color:#34d399}}
small{{display:block;color:#94a3b8;font-size:12px}}</style>
<form method="post" action="/_standin/login"><h1>Sign in</h1>
<p>Demo stand-in for the mesh's identity provider — pick who you are.</p>
<input type="hidden" name="next" value="{escape(next_path)}">{people}</form>""".encode()


class Gateway(BaseHTTPRequestHandler):
    def _reply(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _who(self) -> str | None:
        """The person this request is for: the load client's header, the login
        cookie, or the one fixed person — None when nobody has signed in."""
        said = (self.headers.get("X-Standin-User") or "").strip().lower()
        if said and "@" in said:
            return said
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE and "@" in v:
                return v.strip().lower()
        return None if USERS else EMAIL

    def _forward(self) -> None:
        path = self.path.split("?")[0]
        if path == "/_standin/jwks":
            self._reply(200, json.dumps(jwks()).encode(), "application/json")
            return
        if path == "/_standin/login" and self.command == "GET":
            nxt = parse_qs(urlsplit(self.path).query).get("next", ["/"])[0]
            self._reply(200, _login_page(nxt if nxt.startswith("/") else "/"), "text/html; charset=utf-8")
            return
        if path == "/_standin/login" and self.command == "POST":
            form = parse_qs(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode())
            email = (form.get("email", [""])[0]).strip().lower()
            nxt = form.get("next", ["/"])[0]
            if "@" not in email:
                self._reply(400, b"pick a person", "text/plain")
                return
            self._reply(302, b"", "text/plain", {
                "Set-Cookie": f"{COOKIE}={email}; Path=/; HttpOnly; SameSite=Lax",
                "Location": nxt if nxt.startswith("/") else "/"})
            return
        if path == "/_standin/logout":
            self._reply(302, b"", "text/plain", {
                "Set-Cookie": f"{COOKIE}=; Path=/; Max-Age=0", "Location": "/_standin/login"})
            return
        email = self._who()
        if email is None:
            if self.command == "GET" and "text/html" in (self.headers.get("Accept") or ""):
                self._reply(302, b"", "text/plain", {"Location": "/_standin/login?next=" + quote(self.path)})
            else:
                self._reply(401, b'{"error":"not signed in at the mesh"}', "application/json")
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in _HOP and k.lower() not in (HEADER.lower(), "x-standin-user")}
        headers[HEADER] = rctoken(email, _name_for(email))   # the gateway, not the client, says who it is
        target = _upstream(path)
        conn = http.client.HTTPConnection(target.hostname, target.port or 80, timeout=300)
        try:
            conn.request(self.command, self.path, body=body, headers=headers)
            resp = conn.getresponse()
        except OSError as e:                 # upstream down: say so, do not drop the connection
            self._reply(502, json.dumps({"error": f"upstream {target.netloc} unreachable: {e}"}).encode(),
                        "application/json")
            return
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
    who = f"{len(USERS)} demo users (login at /_standin/login)" if USERS else EMAIL
    routes = ", ".join(f"{p}→{t.geturl()}" for p, t in ROUTES)
    print(f"DEMO mesh sign-in stand-in on :{PORT} → {UPSTREAM.geturl()} {routes} as {who} (issuer {ISSUER})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Gateway).serve_forever()
