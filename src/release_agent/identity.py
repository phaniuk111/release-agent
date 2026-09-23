"""Who is calling — from the mesh's signed token, never from a form field.

The portal signs nobody in itself: Cloud Service Mesh's authservice does that
at the ingress and forwards the result as an RCToken — a JWT signed RS256 by
authservice — in a header (``x-asm-rctoken``). A header is only as trustworthy
as the guarantee that nobody else can set it, and a pod is reachable without
the gateway (a port-forward, another workload in the mesh). So the token is
VERIFIED here against authservice's published keys — signature, issuer,
expiry, and audience when configured — and an unverifiable token is no
identity at all, never a best guess.

The verified email then replaces what people used to type: who queued or
withdrew a chart, whose chat session this is, whose GitHub token a thread
holds. With IDENTITY_HEADER unset the portal behaves exactly as before.

The keys are fetched from inside the cluster (``*.svc.cluster.local``), which
must never go through the corporate egress proxy — NO_PROXY in these clusters
lists only localhost, metadata and googleapis — so that fetch ignores proxy
settings for cluster-local hosts.
"""
from __future__ import annotations

import contextlib
import contextvars
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterator, Mapping

from .config import settings

_KEYS_TTL_S = 300
_KEYS_RETRY_S = 30          # an unknown kid refetches, but not more often than this
_ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384"]   # never "none" or HS*


@dataclass(frozen=True)
class Caller:
    email: str
    name: str = ""


_current: contextvars.ContextVar[Caller | None] = contextvars.ContextVar("caller", default=None)
_keys: dict[str, Any] = {"at": 0.0, "by_kid": {}, "error": ""}
_keys_lock = threading.Lock()


def enabled() -> bool:
    return bool((settings.identity_header or "").strip())


def _cluster_local(url: str) -> bool:
    host = url.split("://")[-1].split("/")[0].split(":")[0]
    return host.endswith(".cluster.local") or "." not in host


def _fetch_keys() -> dict[str, Any]:
    import jwt
    import requests

    url = settings.identity_jwks_url
    session = requests.Session()
    session.trust_env = not _cluster_local(url)
    resp = session.get(url, timeout=5)
    resp.raise_for_status()
    by_kid = {}
    for jwk in jwt.PyJWKSet.from_dict(resp.json()).keys:
        by_kid[jwk.key_id or ""] = jwk
    return by_kid


def _key_for(kid: str):
    """The signing key for ``kid``; refetches on rotation, never hammers the issuer."""
    with _keys_lock:
        now = time.monotonic()
        stale = now - _keys["at"] > _KEYS_TTL_S
        unknown = kid not in _keys["by_kid"] and now - _keys["at"] > _KEYS_RETRY_S
        if stale or unknown or not _keys["at"]:
            try:
                _keys["by_kid"], _keys["error"] = _fetch_keys(), ""
            except Exception as e:  # noqa: BLE001 — keep serving the keys we have
                _keys["error"] = f"{type(e).__name__}: {e}"[:200]
            _keys["at"] = now
        by_kid = _keys["by_kid"]
        if kid in by_kid:
            return by_kid[kid]
        if not kid and len(by_kid) == 1:
            return next(iter(by_kid.values()))
        raise LookupError(_keys["error"] or f"no key {kid!r} at the issuer")


def _claim(claims: Mapping[str, Any], dotted: str) -> str:
    value: Any = claims
    for part in dotted.split("."):
        if not isinstance(value, Mapping):
            return ""
        value = value.get(part)
    return value.strip() if isinstance(value, str) else ""


def verify(token: str) -> Caller:
    """The caller a token names — or ValueError saying why it names nobody."""
    import jwt

    token = (token or "").strip().split(" ")[-1]          # tolerate "Bearer <jwt>"
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as e:
        raise ValueError(f"not a JWT ({e})") from None
    if header.get("alg") not in _ALGORITHMS:
        raise ValueError(f"algorithm {header.get('alg')!r} is not accepted")
    try:
        key = _key_for(str(header.get("kid") or ""))
    except LookupError as e:
        raise ValueError(f"cannot check the signature: {e}") from None
    audience = (settings.identity_audience or "").strip()
    # Only the algorithm the issuer published for this key — never whichever
    # one the token's own header asks for.
    declared = getattr(key, "algorithm_name", "") or ""
    algorithms = [declared] if declared in _ALGORITHMS else _ALGORITHMS
    try:
        claims = jwt.decode(
            token, key.key, algorithms=algorithms,
            issuer=(settings.identity_issuer or None), audience=audience or None,
            options={"require": ["exp", "iss"], "verify_aud": bool(audience)},
            leeway=30,
        )
    except jwt.PyJWTError as e:
        raise ValueError(f"token rejected: {e}") from None
    email = ""
    for path in (settings.identity_email_claim or "").split(","):
        email = _claim(claims, path.strip()) if path.strip() else ""
        if email:
            break
    if "@" not in email:
        raise ValueError("verified, but the token carries no email claim — "
                         "map one in the mesh's UserAuthConfig")
    name = _claim(claims, "attributes.name") or _claim(claims, "name")
    return Caller(email=email.lower(), name=name)


def presented(token: str) -> dict:
    """What a presented token SAYS about itself — iss, aud, kid, alg — read
    without verifying anything. For /api/diagnostics only, so an operator can
    copy the aud the gateway actually sends into IDENTITY_AUDIENCE (and see an
    issuer mismatch) without kubectl. Never an input to who the caller is:
    ``verify`` reads the claims again, after the signature check. None of these
    four is a secret — they are configuration values, not the person."""
    import jwt

    token = (token or "").strip().split(" ")[-1]
    try:
        header = jwt.get_unverified_header(token)
        claims = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError as e:
        return {"error": f"not a JWT ({e})"}
    aud = claims.get("aud")
    return {
        "iss": claims.get("iss"),
        "aud": aud if isinstance(aud, str) else (list(aud) if isinstance(aud, list) else aud),
        "kid": header.get("kid"),
        "alg": header.get("alg"),
        "note": "read from the header WITHOUT verification — for setting IDENTITY_AUDIENCE / "
                "IDENTITY_ISSUER only, never who the caller is",
    }


def presented_from_headers(headers: Mapping[str, str]) -> dict | None:
    """``presented`` for the configured header, or None when no token was sent."""
    if not enabled():
        return None
    name = settings.identity_header.strip().lower()
    value = next((v for k, v in headers.items() if k.lower() == name), "")
    return presented(value) if value else None


def from_headers(headers: Mapping[str, str]) -> tuple[Caller | None, str]:
    """(caller, why-not). ``why-not`` is '' when there is a caller."""
    if not enabled():
        return None, "identity is off (IDENTITY_HEADER unset)"
    name = settings.identity_header.strip().lower()
    value = next((v for k, v in headers.items() if k.lower() == name), "")
    if not value:
        return None, f"no {name} header — not signed in through the gateway"
    try:
        return verify(value), ""
    except ValueError as e:
        return None, str(e)


@contextlib.contextmanager
def activate(caller: Caller | None) -> Iterator[Caller | None]:
    """Bind the caller for this request; tools read it via current_email()."""
    token = _current.set(caller)
    try:
        yield caller
    finally:
        _current.reset(token)


def current() -> Caller | None:
    return _current.get()


def current_email() -> str:
    c = _current.get()
    return c.email if c else ""


def actor(typed: str, caller: Caller | None) -> tuple[str, str]:
    """(who, error) for a write. The verified caller always wins over a typed
    email; without one, IDENTITY_REQUIRED refuses instead of trusting the form."""
    if caller:
        return caller.email, ""
    if enabled() and settings.identity_required:
        return "", "Sign-in required: this portal records changes against your verified identity."
    return (typed or "").strip(), ""
