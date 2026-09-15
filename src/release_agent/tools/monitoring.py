"""The Monitoring pill: named PromQL checks, and read-only questions about metrics.

A CHECK is a PromQL expression that returns series only when something is
wrong — ``up == 0``, ``increase(errors[10m]) > 0`` — so "firing" is simply
"returned a non-zero series". Checks are configuration (MONITOR_CHECKS), not
code, because only the team knows which of its metrics mean trouble; the
default watches scrape targets that stopped answering.

Answers are facts from Prometheus, capped so one broad query cannot flood a
chat turn or the page: at most ``_MAX_SERIES`` series come back, each with its
labels and value, plus the true count. PromQL itself cannot write, so nothing
here can change anything.

Same endpoint and auth as the /api/diagnostics PromQL probe: Managed Service
for Prometheus in PROMETHEUS_PROJECT (default GOOGLE_CLOUD_PROJECT) via the
pod's identity, or PROMETHEUS_URL. Managed Prometheus also answers PromQL over
Cloud Monitoring's own metrics (``serviceruntime_googleapis_com:…``,
``composer_googleapis_com:…``), so a check can watch GCP services too.
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime, timezone
from typing import Any

from ._common import settings
from . import promql_probe as _probe

_MAX_SERIES = 20
_MAX_QUERY_LEN = 2000

DEFAULT_CHECKS = [{
    "name": "Scrape targets down",
    "query": "up == 0",
    "severity": "error",
    "description": "Targets Prometheus can no longer scrape — a crashed pod or a broken exporter.",
}]


def configured_checks() -> tuple[list[dict[str, str]], str]:
    """(checks, config error). A broken MONITOR_CHECKS is reported, not ignored."""
    raw = (settings.monitor_checks or "").strip()
    if not raw:
        return list(DEFAULT_CHECKS), ""
    try:
        data = json.loads(raw)
    except ValueError as e:
        return [], f"MONITOR_CHECKS is not valid JSON: {e}"
    if not isinstance(data, list):
        return [], "MONITOR_CHECKS must be a JSON list of {name, query}."
    checks, problems = [], []
    for i, c in enumerate(data):
        if not isinstance(c, dict) or not str(c.get("name") or "").strip() \
                or not str(c.get("query") or "").strip():
            problems.append(f"entry {i + 1} needs a name and a query")
            continue
        severity = str(c.get("severity") or "error").lower()
        checks.append({
            "name": str(c["name"]).strip(),
            "query": str(c["query"]).strip(),
            "severity": severity if severity in ("error", "warn") else "error",
            "description": str(c.get("description") or "").strip(),
        })
    return checks, "; ".join(problems)


def _value(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _rows(result_type: str, result: Any) -> list[dict[str, Any]]:
    """Instant-query results as [{labels, value}] — vectors, and bare scalars."""
    if result_type == "scalar" and isinstance(result, list) and len(result) == 2:
        return [{"labels": {}, "value": _value(result[1])}]
    rows = []
    for item in result if isinstance(result, list) else []:
        if not isinstance(item, dict):
            continue
        pair = item.get("value") or []
        rows.append({"labels": dict(item.get("metric") or {}),
                     "value": _value(pair[1]) if len(pair) == 2 else None})
    return rows


def run_query(promql: str, session=None, target: dict | None = None) -> dict[str, Any]:
    """One instant query. Never raises; errors carry the most likely fix."""
    promql = (promql or "").strip()
    if not promql:
        return {"ok": False, "error": "empty query"}
    if len(promql) > _MAX_QUERY_LEN:
        return {"ok": False, "error": f"query longer than {_MAX_QUERY_LEN} characters"}
    target = target or _probe.resolve_target()
    if not target.get("configured"):
        return {"ok": False, "error": "PromQL is not configured — set PROMETHEUS_URL or GOOGLE_CLOUD_PROJECT."}
    try:
        http, principal = _probe._session(target, session)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"no credentials: {type(e).__name__}: {e}"[:300]}
    t0 = time.monotonic()
    try:
        r = http.get(f"{target['base_url']}/api/v1/query", params={"query": promql},
                     timeout=_probe._TIMEOUT)
        body = r.json()
    except ValueError:
        return {"ok": False, "query": promql, "error": "answer is not JSON — likely a proxy or login page"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "query": promql, "error": f"{type(e).__name__}: {e}"[:300],
                "hint": _probe.proxy_hint(target["base_url"]) or ""}
    body = body if isinstance(body, dict) else {}
    if r.status_code != 200 or body.get("status") != "success":
        err = body.get("error")
        message = err.get("message") if isinstance(err, dict) else err
        return {"ok": False, "query": promql, "status": r.status_code,
                "error": str(message or r.status_code)[:400],
                "hint": _probe._http_hint(r.status_code, target, principal, str(message or ""))}
    data = body.get("data") or {}
    rows = _rows(str(data.get("resultType") or ""), data.get("result"))
    return {"ok": True, "query": promql, "ms": int((time.monotonic() - t0) * 1000),
            "count": len(rows), "truncated": len(rows) > _MAX_SERIES, "series": rows[:_MAX_SERIES]}


def _firing(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if r.get("value") not in (None, 0.0)]


def run_checks(session=None) -> dict[str, Any]:
    """Every configured check, now. A check that cannot run is ``unknown`` —
    never "OK", because silence from a broken check is how outages hide."""
    checks, config_error = configured_checks()
    target = _probe.resolve_target()
    out: dict[str, Any] = {
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "source": ("Managed Service for Prometheus" if target.get("managed") else target.get("base_url", "")),
        "config_error": config_error,
        "checks": [],
    }
    if target.get("configured"):
        try:
            session = session or _probe._session(target)[0]
        except Exception as e:  # noqa: BLE001
            out["error"] = f"no credentials: {type(e).__name__}: {e}"[:300]
    for check in checks:
        res = run_query(check["query"], session=session, target=target) if "error" not in out else \
            {"ok": False, "error": out["error"]}
        firing = _firing(res.get("series") or []) if res.get("ok") else []
        out["checks"].append({
            **check,
            "state": "unknown" if not res.get("ok") else ("firing" if firing else "ok"),
            "count": len(firing),
            "series": firing[:10],
            "error": res.get("error", ""),
            "hint": res.get("hint", ""),
        })
    states = [c["state"] for c in out["checks"]]
    out["firing"] = states.count("firing")
    out["unknown"] = states.count("unknown")
    out["ok"] = bool(out["checks"]) and not out["firing"] and not out["unknown"] and not config_error
    return out
