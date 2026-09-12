"""Can this pod run PromQL — and as whom?

Two kinds of endpoint, told apart by host:

    Managed Service for Prometheus   monitoring.googleapis.com/v1/projects/<p>/
                                     location/global/prometheus  (Google auth:
                                     the pod's Workload Identity, which needs
                                     roles/monitoring.viewer on <p>)
    any other Prometheus-compatible  e.g. an in-cluster http://prometheus…:9090
    HTTP API                         (no auth unless PROMETHEUS_AUTH=google)

With no PROMETHEUS_URL set, the managed service in PROMETHEUS_PROJECT (default
GOOGLE_CLOUD_PROJECT) is probed — on GKE that is the one that usually exists.

Three instant queries, each answering a different question:
    vector(1)            reachable, authenticated, permitted — needs no data
    count(up)            is there metric data this identity can see
    PROMQL_PROBE_QUERY   (optional) does the metric you actually plan to use exist
Only counts and a sample metric name come back, never series values.
Never raises; errors are reported with the most likely fix.
"""
from __future__ import annotations

import os
import time
from typing import Any

from ._common import settings

_TIMEOUT = (5, 15)          # connect, read
_GMP_HOST = "monitoring.googleapis.com"
_SCOPE = "https://www.googleapis.com/auth/monitoring.read"


def _gmp_base(project: str) -> str:
    return f"https://{_GMP_HOST}/v1/projects/{project}/location/global/prometheus"


def _host(url: str) -> str:
    return url.split("://")[-1].split("/")[0].split(":")[0].lower()


def resolve_target() -> dict[str, Any]:
    """Which endpoint to probe and how to authenticate. Pure (settings only)."""
    url = (settings.prometheus_url or "").strip().rstrip("/")
    project = (settings.prometheus_project or settings.gcp_project or "").strip()
    if not url:
        if not project:
            return {"configured": False}
        url = _gmp_base(project)
    # Accept a base given with or without the /api/v1 suffix.
    if url.endswith("/api/v1"):
        url = url[: -len("/api/v1")]
    mode = (settings.prometheus_auth or "auto").strip().lower()
    google = mode == "google" or (mode == "auto" and _host(url) == _GMP_HOST)
    return {"configured": True, "base_url": url, "managed": _host(url) == _GMP_HOST,
            "auth": "google" if google else "none"}


def _bypasses_proxy(host: str) -> bool:
    no_proxy = os.getenv("NO_PROXY") or os.getenv("no_proxy") or ""
    for entry in (e.strip().lower() for e in no_proxy.split(",")):
        if entry and (host == entry.lstrip(".") or host.endswith("." + entry.lstrip("."))
                      or entry == "*"):
            return True
    return False


def proxy_hint(url: str) -> str | None:
    """An in-cluster address sent through a corporate proxy never arrives."""
    host = _host(url)
    cluster_local = ("." not in host or host.endswith(".svc") or ".svc." in host
                     or host.endswith(".cluster.local"))
    proxied = bool(os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
                   or os.getenv("HTTP_PROXY") or os.getenv("http_proxy"))
    if cluster_local and proxied and not _bypasses_proxy(host):
        return (f"{host} is a cluster-local address but a proxy is set and NO_PROXY "
                f"does not cover it — add it (or .svc,.cluster.local) to NO_PROXY.")
    return None


def _http_hint(status: int, target: dict, principal: str) -> str:
    who = principal or "the pod's service account"
    if target.get("managed"):
        project = target["base_url"].split("/projects/")[-1].split("/")[0]
        if status == 403:
            return (f"{who} is not allowed to read metrics in {project}: grant it "
                    f"roles/monitoring.viewer there (for a metrics scope, on the scoping project).")
        if status == 401:
            return ("no usable Google credentials — check Workload Identity is bound "
                    "for this pod's Kubernetes service account.")
        if status in (400, 404):
            # An unknown project answers 400 "invalid argument", not 404 (seen live).
            return (f"the managed service rejected project '{project}' — check the id in "
                    f"PROMETHEUS_PROJECT and that the Cloud Monitoring API is enabled there.")
    elif status in (401, 403):
        return ("the endpoint wants credentials — set PROMETHEUS_AUTH=google if it "
                "fronts the managed service, or give it an in-cluster address that does not.")
    elif status == 404:
        return "no Prometheus HTTP API at this address — PROMETHEUS_URL should be the base, without /api/v1."
    return ""


def _session(target: dict, session=None, credentials=None) -> tuple[Any, str]:
    """(HTTP session, principal). Google auth uses the pod's identity."""
    if session is not None:
        return session, ""
    import requests  # honours HTTPS_PROXY / NO_PROXY / REQUESTS_CA_BUNDLE

    if target["auth"] != "google":
        return requests.Session(), ""
    import google.auth
    from google.auth.transport.requests import AuthorizedSession, Request

    creds = credentials
    if creds is None:
        creds, _ = google.auth.default(scopes=[_SCOPE])
    try:
        creds.refresh(Request())
    except Exception:
        pass    # the query below reports the auth failure with its own status
    # Metadata-server credentials say "default" until a refresh succeeds.
    email = str(getattr(creds, "service_account_email", "") or "")
    if email and email != "default":
        principal = email
    else:
        principal = "" if hasattr(creds, "service_account_email") else "(user credentials)"
    return AuthorizedSession(creds), principal


def _query(session, base: str, promql: str) -> dict[str, Any]:
    t0 = time.monotonic()
    try:
        r = session.get(f"{base}/api/v1/query", params={"query": promql}, timeout=_TIMEOUT)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "query": promql, "error": f"{type(e).__name__}: {e}"[:300]}
    ms = int((time.monotonic() - t0) * 1000)
    out: dict[str, Any] = {"query": promql, "status": r.status_code, "ms": ms}
    try:
        body = r.json()
    except ValueError:
        out.update(ok=False, error="answer is not JSON — likely a proxy or login page")
        return out
    body = body if isinstance(body, dict) else {}
    if r.status_code != 200 or body.get("status") != "success":
        err = body.get("error")        # a string from Prometheus, an object from Google APIs
        message = err.get("message") if isinstance(err, dict) else err
        out.update(ok=False, error=str(message or body or getattr(r, "reason", ""))[:300])
        return out
    result = (body.get("data") or {}).get("result")
    rows = result if isinstance(result, list) else []
    out.update(ok=True, series=len(rows))
    if rows and isinstance(rows[0], dict):
        name = (rows[0].get("metric") or {}).get("__name__")
        if name:
            out["sample_metric"] = name
        value = rows[0].get("value")
        if promql.startswith("count(") and isinstance(value, list) and len(value) == 2:
            out["count"] = value[1]
    return out


def probe_promql(session=None, credentials=None) -> dict[str, Any]:
    """Run the three probe queries. Never raises."""
    target = resolve_target()
    if not target.get("configured"):
        return {"ok": None, "configured": False,
                "note": "No PROMETHEUS_URL and no GOOGLE_CLOUD_PROJECT — nothing to probe."}
    report: dict[str, Any] = {"target": target["base_url"], "managed": target["managed"],
                              "auth": target["auth"]}
    hint = proxy_hint(target["base_url"])
    try:
        http, principal = _session(target, session, credentials)
    except Exception as e:  # noqa: BLE001
        report.update(ok=False, error=f"no credentials: {type(e).__name__}: {e}"[:300],
                      hint="Workload Identity is not set up for this pod, or ADC is missing locally.")
        return report
    if principal:
        report["principal"] = principal

    access = _query(http, target["base_url"], "vector(1)")
    report["access"] = access
    if not access.get("ok"):
        report["ok"] = False
        status = access.get("status")
        report["hint"] = (_http_hint(status or 0, target, principal) or hint
                          or (f"the endpoint answered HTTP {status} — see access.error." if status
                              else "could not reach the endpoint — check egress / proxy for this host."))
        return report

    data = _query(http, target["base_url"], "count(up)")
    report["data"] = data
    if data.get("ok") and not data.get("series"):
        report["note"] = ("access works but no `up` series are visible — nothing is scraped "
                          "into this project yet, or the metrics live in another project "
                          "(set PROMETHEUS_PROJECT to the metrics scope).")
    custom = (settings.promql_probe_query or "").strip()
    if custom:
        report["custom"] = _query(http, target["base_url"], custom)
        if report["custom"].get("ok") and not report["custom"].get("series"):
            report["custom"]["note"] = "the query ran but matched no series"
    report["ok"] = True
    if hint:
        report["hint"] = hint
    return report
