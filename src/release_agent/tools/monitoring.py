"""The Monitoring pill: PromQL checks, and read-only questions about metrics.

A CHECK is a PromQL expression that returns series only when something is
wrong — ``up == 0``, ``increase(failed_runs[1h]) > 0`` — so "firing" is
"returned a non-zero series". That alone cannot tell healthy from UNMEASURED:
``up == 0`` is just as empty in a project with no targets at all. So a check
also names what it ``needs`` — the data it measures — and a check whose needs
returns nothing is ``no_data`` ("not measured here"), never "OK". Healthy
checks report how much they are watching ("OK · watching 17").

The built-in pack watches what this team ships, using metrics Managed Service
for Prometheus serves from Cloud Monitoring with no setup — Composer DAG runs,
Dataflow jobs, GKE container restarts — plus Prometheus scrape targets and
Google API errors. MONITOR_CHECKS adds the team's own checks to it.

This pod does not notify anyone. Periodic evaluation and notification belong
to Cloud Monitoring alerting — it runs whether or not this pod is up, dedupes,
and delivers to email / Chat / Teams / PagerDuty channels. ``alert_policy``
turns any check into that policy, ready to apply; the portal writes nothing.

Answers are capped (``_MAX_SERIES``) so one broad query cannot flood a chat
turn or the page. PromQL itself cannot write.
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


def _ns_filter() -> str:
    names = [n.strip() for n in (settings.monitor_namespaces or "").split(",") if n.strip()]
    return f',namespace_name=~"{"|".join(names)}"' if names else ""


def builtin_checks() -> list[dict[str, str]]:
    """What a release team wants watched, from metrics that need no setup.

    Metric names are Cloud Monitoring's, as Managed Prometheus exposes them
    (``composer.googleapis.com/workflow/run_count`` →
    ``composer_googleapis_com:workflow_run_count``); ``monitored_resource``
    picks the resource type where a metric has several.

    ``needs`` looks over the SAME window as the check. An instant selector only
    sees series with a sample in the last five minutes, and delta metrics (API
    requests, DAG runs) report only when something happens — so a quiet hour
    read as "not measured" while the check itself would have fired.
    """
    k8s = f'monitored_resource="k8s_container"{_ns_filter()}'
    return [
        {
            "name": "Composer DAG runs failed (1h)",
            "severity": "error",
            "query": ('sum by (environment_name, workflow_name) (increase('
                      'composer_googleapis_com:workflow_run_count{monitored_resource="cloud_composer_workflow",'
                      'state="failed"}[1h])) > 0'),
            "needs": ('count(count_over_time(composer_googleapis_com:workflow_run_count{'
                      'monitored_resource="cloud_composer_workflow"}[1h]))'),
            "description": "Airflow DAG runs that ended failed in the last hour, by environment and DAG.",
        },
        {
            "name": "Dataflow jobs failed (1h)",
            "severity": "error",
            "query": ('max by (job_name) (max_over_time('
                      'dataflow_googleapis_com:job_is_failed{monitored_resource="dataflow_job"}[1h])) > 0'),
            "needs": 'count(max_over_time(dataflow_googleapis_com:job_is_failed{monitored_resource="dataflow_job"}[1h]))',
            "description": "Dataflow jobs reported failed in the last hour.",
        },
        {
            "name": "GKE containers restarting (1h)",
            "severity": "error",
            "query": (f'sum by (namespace_name, pod_name, container_name) (increase('
                      f'kubernetes_io:container_restart_count{{{k8s}}}[1h])) > 0'),
            "needs": f'count(count_over_time(kubernetes_io:container_restart_count{{{k8s}}}[1h]))',
            "description": "Containers that restarted in the last hour — crash loops, OOM kills, failing probes.",
        },
        {
            "name": "Prometheus targets down",
            "severity": "error",
            "query": "up == 0",
            "needs": "count(up)",
            "description": "Scrape targets Prometheus can no longer reach — a crashed pod or a broken exporter.",
        },
        {
            "name": "Google API 5xx errors (1h)",
            "severity": "warn",
            "query": ('sum by (service) (increase(serviceruntime_googleapis_com:api_request_count{'
                      'monitored_resource="consumed_api",response_code_class="5xx"}[1h])) > 0'),
            "needs": ('count(count_over_time(serviceruntime_googleapis_com:api_request_count{'
                      'monitored_resource="consumed_api"}[1h]))'),
            "description": "Server errors from Google APIs this project calls (BigQuery, Vertex AI, Dataflow…).",
        },
    ]


def configured_checks() -> tuple[list[dict[str, str]], str]:
    """(built-ins + the team's checks, config error). A broken MONITOR_CHECKS is
    reported, never silently dropped."""
    checks = builtin_checks() if settings.monitor_default_checks else []
    raw = (settings.monitor_checks or "").strip()
    if not raw:
        return checks, ""
    try:
        data = json.loads(raw)
    except ValueError as e:
        return checks, f"MONITOR_CHECKS is not valid JSON: {e}"
    if not isinstance(data, list):
        return checks, "MONITOR_CHECKS must be a JSON list of {name, query}."
    problems = []
    for i, c in enumerate(data):
        if not isinstance(c, dict) or not str(c.get("name") or "").strip() \
                or not str(c.get("query") or "").strip():
            problems.append(f"entry {i + 1} needs a name and a query")
            continue
        severity = str(c.get("severity") or "error").lower()
        checks.append({
            "name": str(c["name"]).strip(),
            "query": str(c["query"]).strip(),
            "needs": str(c.get("needs") or "").strip(),
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


def run_query(promql: str, session=None, target: dict | None = None, *, _raw: bool = False) -> dict[str, Any]:
    """One instant query. Never raises; errors carry the most likely fix.

    ``_raw`` is private: promql_probe._query shares this function's HTTP call
    and Prometheus/Google error normalisation, but (per its own module
    docstring) never interprets series values — only counts and a sample
    metric name — so on success it gets the untouched result back instead of
    monitoring's float-cast, capped rows. No normal caller passes this.
    """
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
    ms = int((time.monotonic() - t0) * 1000)
    if _raw:
        return {"ok": True, "query": promql, "ms": ms,
                "result_type": data.get("resultType"), "result": data.get("result")}
    rows = _rows(str(data.get("resultType") or ""), data.get("result"))
    return {"ok": True, "query": promql, "ms": ms,
            "count": len(rows), "truncated": len(rows) > _MAX_SERIES, "series": rows[:_MAX_SERIES]}


def _firing(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if r.get("value") not in (None, 0.0)]


def _watching(res: dict[str, Any]) -> int:
    """How much a ``needs`` query found: a count() answers with its value, a
    plain selector with its series."""
    rows = res.get("series") or []
    if len(rows) == 1 and not rows[0].get("labels"):
        return int(rows[0].get("value") or 0)
    return int(res.get("count") or 0)


def _run_one(check: dict[str, str], session, target) -> dict[str, Any]:
    out: dict[str, Any] = {**check, "count": 0, "series": [], "error": "", "hint": "", "watching": None}
    if check.get("needs"):
        need = run_query(check["needs"], session=session, target=target)
        if not need.get("ok"):
            return {**out, "state": "unknown", "error": need.get("error", ""), "hint": need.get("hint", "")}
        out["watching"] = _watching(need)
        if not out["watching"]:
            return {**out, "state": "no_data"}
    res = run_query(check["query"], session=session, target=target)
    if not res.get("ok"):
        return {**out, "state": "unknown", "error": res.get("error", ""), "hint": res.get("hint", "")}
    firing = _firing(res.get("series") or [])
    return {**out, "state": "firing" if firing else "ok", "count": len(firing), "series": firing[:10]}


def run_checks(session=None) -> dict[str, Any]:
    """Every configured check, now, concurrently. ``unknown`` (could not run)
    and ``no_data`` (nothing to measure here) are never reported as OK."""
    from concurrent.futures import ThreadPoolExecutor

    checks, config_error = configured_checks()
    target = _probe.resolve_target()
    project = target.get("base_url", "").split("/projects/")[-1].split("/")[0] if target.get("managed") else ""
    out: dict[str, Any] = {
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "source": (f"Managed Service for Prometheus · {project}" if target.get("managed")
                   else target.get("base_url", "")),
        "config_error": config_error,
        "checks": [],
    }
    error = ""
    if target.get("configured") and session is None:
        try:
            session = _probe._session(target)[0]
        except Exception as e:  # noqa: BLE001
            error = f"no credentials: {type(e).__name__}: {e}"[:300]
    if error or not target.get("configured"):
        why = error or "PromQL is not configured — set PROMETHEUS_URL or GOOGLE_CLOUD_PROJECT."
        out["checks"] = [{**c, "state": "unknown", "error": why, "hint": "", "count": 0,
                          "series": [], "watching": None} for c in checks]
    else:
        with ThreadPoolExecutor(max_workers=min(6, max(1, len(checks)))) as pool:
            out["checks"] = list(pool.map(lambda c: _run_one(c, session, target), checks))
    states = [c["state"] for c in out["checks"]]
    out["counts"] = {s: states.count(s) for s in ("firing", "ok", "unknown", "no_data")}
    out["firing"], out["unknown"] = out["counts"]["firing"], out["counts"]["unknown"]
    # Healthy means something was measured and nothing is wrong.
    out["ok"] = bool(out["counts"]["ok"]) and not out["firing"] and not out["unknown"] \
        and not config_error
    return out


def alert_policy(check: dict[str, str]) -> dict[str, Any]:
    """The Cloud Monitoring alert policy that watches ``check`` every minute —
    so notification runs in Cloud Monitoring, not in this pod. Pure.

    Apply with the REST API (projects.alertPolicies.create) or
    ``gcloud alpha monitoring policies create --policy-from-file=…``, then add
    notification channels (email, Google Chat, Teams webhook, PagerDuty).
    """
    return {
        "displayName": f"Dev Portal: {check['name']}",
        "combiner": "OR",
        "severity": "ERROR" if check.get("severity", "error") == "error" else "WARNING",
        "conditions": [{
            "displayName": check["name"],
            "conditionPrometheusQueryLanguage": {
                "query": check["query"],
                "duration": "60s",
                "evaluationInterval": "60s",
            },
        }],
        "documentation": {
            "mimeType": "text/markdown",
            "content": (f"{check.get('description') or check['name']}\n\n"
                        f"Fires when this PromQL returns a series:\n\n`{check['query']}`\n\n"
                        "Open the Dev Portal's Monitoring pill and use *Ask why* to investigate."),
        },
        "notificationChannels": [],
    }
