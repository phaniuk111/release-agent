"""Source-service logs: what a GKE workload logged in a window, and who was denied.

READ-ONLY, for the support-investigate agent. A pipeline fails; the first
question is whether the SOURCE service it fetches from was already unhappy —
and if so, with what. Two questions:

    read(source, start, end)  -> the source's container logs at ERROR or worse,
                                 grouped by `support_triage.signature` (one
                                 failure repeated 200 times is one group), each
                                 group tagged `security` when its text names an
                                 access/credential problem
    audit(start, end)         -> Cloud Audit Logs: PERMISSION_DENIED calls,
                                 grouped by method / service / resource, and
                                 the IAM policy changes in the same window

Both use the Cloud Logging REST API (entries:list) through Google credentials
(roles/logging.viewer on the project; audit logs also need it). Nothing here
writes, and nothing raises: a failure is {ok: False, error, hint}.

Everything that reaches a filter is checked character by character first — a
service name typed into chat must never become filter syntax.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from ...config import settings
from .config import data_project
from .report import signature

_SCOPE = "https://www.googleapis.com/auth/logging.read"
_ENTRIES_URL = "https://logging.googleapis.com/v2/entries:list"
_TIMEOUT = (5, 30)              # connect, read
_PAGE_SIZE = 200
_AUDIT_CAP = 200                # entries read, and entries listed, per audit question
_SAMPLE_CHARS = 300
_SECURITY_SCAN_CHARS = 2000     # a stack trace's tail is not where the cause is

# Severity names in order; "worst" and ">= min_severity" both use this order.
_SEVERITIES = ("DEFAULT", "DEBUG", "INFO", "NOTICE", "WARNING", "ERROR", "CRITICAL", "ALERT", "EMERGENCY")

# Matched as whole lowercase WORDS, never as substrings: "tokenizer" and
# "secretary" must not look like a credential problem, and a status code only
# counts as the word 403, not as part of 14031. The message is split on every
# character that is not a letter or digit, so PERMISSION_DENIED, permission-denied
# and "permission denied" all yield the same two words.
SECURITY_WORDS = frozenset({
    "permission", "denied", "forbidden", "unauthorized", "unauthorised", "401", "403",
    "token", "certificate", "tls", "ssl", "secret", "credential", "credentials",
    "iam", "handshake", "expired",
})
_SECURITY_PHRASES = (("access", "denied"),)

_NAME_EXTRA = "-_."
_EMAIL_EXTRA = "-_.+@"
_PROJECT_EXTRA = "-.:"


class _LogsError(Exception):
    """A Logging API failure, kept as (HTTP status, message) for the hint."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status, self.message = status, message


# ----- settings / validation ---------------------------------------------------------

def _project() -> str:
    return (settings.support_logs_project or settings.gcp_project or "").strip()


def enabled() -> bool:
    """read() needs to know WHERE the source runs; audit() only needs a project."""
    return bool(settings.support_cluster.strip() and settings.support_namespace.strip())


def _clean(value: str, what: str, extra: str = _NAME_EXTRA) -> str:
    """The value if every character is a letter, digit or one of `extra`;
    otherwise ValueError. Non-ASCII letters are refused: isalnum() would admit
    them, and the filter language has no business seeing them."""
    v = (value or "").strip()
    if not v:
        raise ValueError(f"{what} is empty")
    for ch in v:
        if not (("a" <= ch <= "z") or ("A" <= ch <= "Z") or ("0" <= ch <= "9") or ch in extra):
            raise ValueError(f"{what} may only have letters, digits and {' '.join(extra)} (got {ch!r})")
    return v


def _clean_email(value: str) -> str:
    v = _clean(value, "principal", _EMAIL_EXTRA)
    local, _, domain = v.partition("@")
    if not local or not domain or "@" in domain or "." not in domain:
        raise ValueError("principal must look like name@domain")
    return v


def _ts(moment: datetime) -> str:
    """RFC 3339 UTC, the form the filter's timestamp comparisons take. A naive
    datetime is taken as UTC, as support_triage does."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _window(start: datetime, end: datetime) -> str:
    if _ts(end) <= _ts(start):
        raise ValueError("the window ends before it starts")
    return f'timestamp>="{_ts(start)}" AND timestamp<="{_ts(end)}"'


def build_filter(source: str, start: datetime, end: datetime, min_severity: str = "ERROR") -> str:
    """The entries:list filter for one source's container logs. ValueError on
    any unsafe part."""
    src = _clean(source, "source")
    cluster = _clean(settings.support_cluster, "SUPPORT_CLUSTER")
    namespace = _clean(settings.support_namespace, "SUPPORT_NAMESPACE")
    label = _clean(settings.support_source_label or "app", "SUPPORT_SOURCE_LABEL")
    severity = (min_severity or "").strip().upper()
    if severity not in _SEVERITIES:
        raise ValueError(f"min_severity must be one of {', '.join(_SEVERITIES)}")
    return " AND ".join([
        'resource.type="k8s_container"',
        f'resource.labels.cluster_name="{cluster}"',
        f'resource.labels.namespace_name="{namespace}"',
        f'labels."k8s-pod/{label}"="{src}"',
        _window(start, end),
        f"severity>={severity}",
    ])


def denial_filter(start: datetime, end: datetime, principal: str = "") -> str:
    parts = ['logName:"cloudaudit.googleapis.com"', "protoPayload.status.code=7", _window(start, end)]
    if principal:
        parts.insert(2, f'protoPayload.authenticationInfo.principalEmail="{_clean_email(principal)}"')
    return " AND ".join(parts)


def iam_change_filter(start: datetime, end: datetime) -> str:
    # ":" is "contains": the method is "SetIamPolicy" on a project but
    # "google.iam.v1.IAMPolicy.SetIamPolicy" on most other resources.
    return " AND ".join(['logName:"cloudaudit.googleapis.com"',
                         'protoPayload.methodName:"SetIamPolicy"', _window(start, end)])


# ----- the API -----------------------------------------------------------------------

def _session():
    import google.auth
    from google.auth.transport.requests import AuthorizedSession   # honours HTTPS_PROXY / CA bundle

    creds, _ = google.auth.default(scopes=[_SCOPE])
    return AuthorizedSession(creds)


def _list_entries(session, project: str | list[str], flt: str, limit: int, order: str) -> tuple[list[dict[str, Any]], bool]:
    """(entries, truncated): up to `limit` entries, paging by token. Truncated
    means the API still had more when the limit was reached. ``project`` may be
    several: Cloud Logging reads them in one call."""
    entries: list[dict[str, Any]] = []
    token = ""
    projects = [project] if isinstance(project, str) else list(project)
    while True:
        body: dict[str, Any] = {"resourceNames": [f"projects/{_clean(p, 'project', _PROJECT_EXTRA)}" for p in projects],
                                "filter": flt, "orderBy": order,
                                "pageSize": max(1, min(_PAGE_SIZE, limit - len(entries)))}
        if token:
            body["pageToken"] = token
        resp = session.post(_ENTRIES_URL, json=body, timeout=_TIMEOUT)
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code != 200:
            err = data.get("error") if isinstance(data, dict) else None
            raise _LogsError(resp.status_code, str((err or {}).get("message") or f"HTTP {resp.status_code}")[:300])
        entries.extend(data.get("entries") or [])
        token = data.get("nextPageToken") or ""
        if len(entries) >= limit:
            return entries[:limit], bool(token) or len(entries) > limit
        if not token:
            return entries, False


def _failure(exc: Exception, project: str) -> dict[str, Any]:
    if isinstance(exc, _LogsError):
        hints = {
            403: f"needs roles/logging.viewer on {project}",
            401: "no usable Google credentials — check Workload Identity / application-default login",
            400: "Cloud Logging rejected the query — check the project, cluster, namespace and label names",
            404: f"project {project} not found, or the Cloud Logging API is not enabled there",
            429: "Cloud Logging read quota hit — try again in a minute",
        }
        return {"ok": False, "error": f"Cloud Logging said {exc.status}: {exc.message}",
                "hint": hints.get(exc.status, "")}
    if isinstance(exc, ValueError):
        return {"ok": False, "error": str(exc), "hint": "nothing was queried"}
    return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}",
            "hint": "check Google credentials and network access to logging.googleapis.com"}


# ----- pure: entries -> groups -------------------------------------------------------

def message_of(entry: dict[str, Any]) -> str:
    """The text a person would read: textPayload, else the JSON payload's
    message / msg, else the payload itself as compact JSON."""
    text = entry.get("textPayload")
    if isinstance(text, str) and text:
        return text
    payload = entry.get("jsonPayload")
    if isinstance(payload, dict) and payload:
        for key in ("message", "msg"):
            if isinstance(payload.get(key), str) and payload[key]:
                return payload[key]
        return json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)
    return ""


def _words(text: str) -> list[str]:
    words: list[str] = []
    cur: list[str] = []
    for ch in text.lower():
        if ("a" <= ch <= "z") or ("0" <= ch <= "9"):
            cur.append(ch)
        elif cur:
            words.append("".join(cur))
            cur = []
    if cur:
        words.append("".join(cur))
    return words


def is_security(message: str) -> bool:
    """Does the message talk about access, credentials or transport trust."""
    words = _words(message[:_SECURITY_SCAN_CHARS])
    if any(w in SECURITY_WORDS for w in words):
        return True
    pairs = list(zip(words, words[1:]))
    return any(p in pairs for p in _SECURITY_PHRASES)


def _rank(severity: str) -> int:
    return _SEVERITIES.index(severity) if severity in _SEVERITIES else 0


def group_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One group per signature, biggest first. `sample` is the first message
    seen (the API is asked newest-first, so the most recent one)."""
    groups: dict[str, dict[str, Any]] = {}
    for e in entries:
        msg = message_of(e)
        sig = signature(msg)
        when = str(e.get("timestamp") or "")
        sev = str(e.get("severity") or "DEFAULT").upper()
        g = groups.get(sig)
        if g is None:
            g = groups[sig] = {"signature": sig, "sample": msg[:_SAMPLE_CHARS], "count": 0,
                               "first_seen": when, "last_seen": when, "severity": sev, "security": False}
        g["count"] += 1
        if when and (not g["first_seen"] or when < g["first_seen"]):
            g["first_seen"] = when
        if when and when > g["last_seen"]:
            g["last_seen"] = when
        if _rank(sev) > _rank(g["severity"]):
            g["severity"] = sev
        g["security"] = g["security"] or is_security(msg)
    return sorted(groups.values(), key=lambda g: (-g["count"], g["signature"]))


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _first_line(text: str) -> str:
    lines = text.strip().splitlines()
    return lines[0] if lines else ""


def _read_summary(lines: int, groups: list[dict[str, Any]], min_severity: str, truncated: bool) -> str:
    kind = "error" if min_severity == "ERROR" else f"{min_severity.lower()}-or-worse"
    if not lines:
        return f"no {kind} lines in the window"
    sec = [g for g in groups if g["security"]]
    text = f"{lines}{'+' if truncated else ''} {kind} lines in {_plural(len(groups), 'group')}"
    if sec:
        # The sample, not the signature: a signature has its numbers taken out
        # ("# Forbidden from bucket #"), which reads badly in a sentence.
        top = "; ".join(f"{_first_line(g['sample'])[:90]} × {g['count']}" for g in sec[:2])
        text += f"; {_plural(len(sec), 'security group')} ({top})"
    return text


def read(source: str, start: datetime, end: datetime, *, min_severity: str = "ERROR",
         session=None) -> dict[str, Any]:
    """What `source` (a workload's pod label value) logged at `min_severity`
    or worse between start and end. Never raises."""
    project = _project()
    try:
        if not enabled():
            return {"ok": False, "disabled": True,
                    "error": "source logs are not configured — set SUPPORT_CLUSTER and SUPPORT_NAMESPACE"}
        if not project:
            return {"ok": False, "error": "no project for the logs — set SUPPORT_LOGS_PROJECT or GOOGLE_CLOUD_PROJECT",
                    "hint": ""}
        severity = (min_severity or "").strip().upper()
        flt = build_filter(source, start, end, severity)
        limit = max(1, int(settings.support_logs_max_lines))
        entries, truncated = _list_entries(session or _session(), project, flt, limit, "timestamp desc")
    except Exception as e:   # noqa: BLE001 — the tool contract is "never raises"
        return _failure(e, project)
    groups = group_entries(entries)
    sec = [g for g in groups if g["security"]]
    return {"ok": True, "source": source.strip(), "cluster": settings.support_cluster.strip(),
            "namespace": settings.support_namespace.strip(),
            "window": {"start": _ts(start), "end": _ts(end)},
            "lines_read": len(entries), "truncated": truncated, "groups": groups,
            "security_groups_count": len(sec), "security_lines": sum(g["count"] for g in sec),
            "summary": _read_summary(len(entries), groups, severity, truncated)}


# ----- audit logs --------------------------------------------------------------------

def _resource_prefix(name: str) -> str:
    """projects/p/datasets/d/tables/t -> projects/p/datasets/d: the owner of the
    thing, so denials on 50 tables of one dataset are one line. A BigQuery job
    is projects/p/jobs/<unique id> — keeping the id would make every denied
    query its own group, so those stop at the jobs collection."""
    parts = name.split("/")
    return "/".join(parts[:3] if parts[2:3] == ["jobs"] else parts[:4])


def _proto(entry: dict[str, Any]) -> dict[str, Any]:
    p = entry.get("protoPayload")
    return p if isinstance(p, dict) else {}


def group_denials(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for e in entries:
        p = _proto(e)
        key = (str(p.get("methodName") or ""), str(p.get("serviceName") or ""),
               _resource_prefix(str(p.get("resourceName") or "")))
        when = str(e.get("timestamp") or "")
        who = str((p.get("authenticationInfo") or {}).get("principalEmail") or "")
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"method": key[0], "service": key[1], "resource": key[2], "count": 0,
                               "first_seen": when, "last_seen": when, "principals": [],
                               "message": str((p.get("status") or {}).get("message") or "")[:200]}
        g["count"] += 1
        if when and (not g["first_seen"] or when < g["first_seen"]):
            g["first_seen"] = when
        if when and when > g["last_seen"]:
            g["last_seen"] = when
        if who and who not in g["principals"] and len(g["principals"]) < 5:
            g["principals"].append(who)
    return sorted(groups.values(), key=lambda g: (-g["count"], g["method"], g["resource"]))


def iam_changes(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"time": str(e.get("timestamp") or ""),
             "principal": str((_proto(e).get("authenticationInfo") or {}).get("principalEmail") or ""),
             "resource": str(_proto(e).get("resourceName") or ""),
             "method": str(_proto(e).get("methodName") or "")} for e in entries]


def audit(start: datetime, end: datetime, *, principal: str = "", session=None) -> dict[str, Any]:
    """PERMISSION_DENIED calls (optionally by one principal) and IAM policy
    changes in the window. Reads at most 200 entries of each. Never raises."""
    project = _project()
    # A denial is audited in the project that OWNS the resource: a service in
    # the services project refused a bucket or a table in the data project
    # leaves its entry there, not where the service runs. So both are read
    # (one call) when a deployment keeps them apart.
    projects = [p for p in dict.fromkeys([project, data_project()]) if p]
    try:
        if not project:
            return {"ok": False, "error": "no project for the logs — set SUPPORT_LOGS_PROJECT or GOOGLE_CLOUD_PROJECT",
                    "hint": ""}
        sess = session or _session()
        denied, denied_cut = _list_entries(sess, projects, denial_filter(start, end, principal.strip()),
                                           _AUDIT_CAP, "timestamp desc")
        changed, changed_cut = _list_entries(sess, projects, iam_change_filter(start, end), _AUDIT_CAP,
                                             "timestamp desc")
    except Exception as e:   # noqa: BLE001 — the tool contract is "never raises"
        return _failure(e, " and ".join(projects))
    denials = group_denials(denied)
    changes = iam_changes(changed)
    who = f" by {principal.strip()}" if principal.strip() else ""
    more = "+" if denied_cut else ""
    summary = f"{len(denied)}{more} permission-denied calls{who} in {_plural(len(denials), 'group')}"
    if denials:
        top = denials[0]
        summary += f" (most: {top['method'] or '?'} on {top['resource'] or '?'} × {top['count']})"
    summary += f"; {len(changes)}{'+' if changed_cut else ''} IAM policy {'change' if len(changes) == 1 else 'changes'}"
    return {"ok": True, "window": {"start": _ts(start), "end": _ts(end)}, "project": project,
            "projects": projects, "denials": denials, "denials_total": len(denied), "denials_truncated": denied_cut,
            "iam_changes": changes, "iam_changes_truncated": changed_cut, "summary": summary}
