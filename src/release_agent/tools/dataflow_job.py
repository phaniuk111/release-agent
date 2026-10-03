"""What a Dataflow job did — READ-ONLY, for the support-investigate agent.

A support ticket usually names a job id (the control table's job_id role). This
answers what a person opens the console for: the job's state and timing, the
errors Dataflow itself recorded, and the worker error logs grouped by what they
say, with one plain-word `kind` for the failure.

    inspect(job_id, region="") -> {ok, job_id, region, project, name, state, …,
                                   errors, log_groups, kind, summary, console_url}

Three reads, all GET/list (nothing here can start, cancel or change a job):
the job (Dataflow REST v1b3), its job messages (same API), and the worker
error logs (Cloud Logging entries:list). They go through google-auth's
AuthorizedSession by REST — no client library. The job read decides ok; the
other two degrade to a note, so a missing logging role still leaves the
job-level answer.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..config import settings
from .support_triage import signature

_DATAFLOW = "https://dataflow.googleapis.com/v1b3"
_LOGGING = "https://logging.googleapis.com/v2/entries:list"
_CONSOLE = "https://console.cloud.google.com/dataflow/jobs"
_TIMEOUT = 30
_MAX_ERRORS = 100
_MAX_WARNINGS = 50
_MESSAGE_PAGE = 100
_TEXT_CHARS = 300
_SUMMARY_TEXT_CHARS = 120
_MAX_PAGES = 20          # a runaway page token must not loop forever
_JOB_ID_MAX = 100

_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
_REGION_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-")
# ':' and '.' for domain-scoped project ids
_PROJECT_CHARS = _REGION_CHARS | frozenset(":.")
_TERMINAL = frozenset({"JOB_STATE_DONE", "JOB_STATE_FAILED", "JOB_STATE_CANCELLED",
                       "JOB_STATE_UPDATED", "JOB_STATE_DRAINED"})

# kind -> (single words, word sequences). The failure text is lowercased and cut
# into word tokens (anything not a letter or digit separates them); the FIRST row
# with a hit wins, so the order is the precedence: a permission error that also
# says "not found" (a hidden bucket reads as missing) is a permission problem.
# Words are matched whole — "type" does not match "prototype".
KIND_RULES: tuple[tuple[str, frozenset[str], tuple[tuple[str, ...], ...]], ...] = (
    ("out_of_memory", frozenset({"oom", "memory", "killed"}), ()),
    ("quota", frozenset({"quota", "exhausted"}), (("resources", "exhausted"),)),
    ("permission", frozenset({"permission", "denied", "forbidden", "401", "403", "unauthorized"}), ()),
    ("schema", frozenset({"schema", "cast", "mismatch", "numeric", "type"}), (("cannot", "cast"),)),
    # Java and Beam word a missing input as FileNotFoundException / "No files matched spec"
    ("not_found", frozenset({"404", "nosuchkey", "filenotfoundexception", "nosuchfileexception"}),
     (("not", "found"), ("no", "files", "matched"), ("no", "such", "file"))),
    ("worker_lost", frozenset(), (("lost", "contact"), ("worker", "lost"), ("timed", "out"))),
)


# ----- pure helpers -----------------------------------------------------------

def _tokens(text: str) -> list[str]:
    """Lowercased runs of letters and digits."""
    out: list[str] = []
    word: list[str] = []
    for c in text.lower():
        if c.isalnum():
            word.append(c)
        elif word:
            out.append("".join(word))
            word = []
    if word:
        out.append("".join(word))
    return out


def _has_sequence(tokens: list[str], seq: tuple[str, ...]) -> bool:
    n = len(seq)
    return any(tuple(tokens[i:i + n]) == seq for i in range(len(tokens) - n + 1))


def classify(texts: list[str]) -> str:
    """The failure's kind from its text (job messages, then worker log
    groups); "unknown" when nothing in KIND_RULES matches."""
    tokens = _tokens(" ".join(texts))
    present = set(tokens)
    for kind, words, phrases in KIND_RULES:
        if words & present or any(_has_sequence(tokens, p) for p in phrases):
            return kind
    return "unknown"


def _valid(value: str, allowed: frozenset[str], limit: int = 100) -> bool:
    return bool(value) and len(value) <= limit and all(c in allowed for c in value)


def _when(value: Any) -> datetime | None:
    """An RFC 3339 timestamp (Google writes up to nanoseconds) → aware datetime."""
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    head, dot, rest = text.partition(".")
    if dot:   # fromisoformat takes at most microseconds
        n = len(rest) - len(rest.lstrip("0123456789"))
        text = f"{head}.{rest[:min(n, 6)]}{rest[n:]}"
    try:
        d = datetime.fromisoformat(text)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _iso(d: datetime | None) -> str | None:
    return d.isoformat() if d else None


def group_logs(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Worker log entries → groups by signature, biggest first:
    {signature, sample, count, first_seen, last_seen, step}."""
    groups: dict[str, dict[str, Any]] = {}
    for e in entries:
        message = _entry_text(e)
        if not message:
            continue
        sig = signature(message)
        at = _when(e.get("timestamp"))
        labels = (e.get("resource") or {}).get("labels") or {}
        g = groups.setdefault(sig, {"signature": sig, "sample": message[:_TEXT_CHARS], "count": 0,
                                    "first_seen": None, "last_seen": None,
                                    "step": labels.get("step") or labels.get("step_id") or None})
        g["count"] += 1
        if at and (g["first_seen"] is None or at < g["first_seen"]):
            g["first_seen"] = at
        if at and (g["last_seen"] is None or at > g["last_seen"]):
            g["last_seen"] = at
    out = sorted(groups.values(), key=lambda g: -g["count"])
    for g in out:
        g["first_seen"], g["last_seen"] = _iso(g["first_seen"]), _iso(g["last_seen"])
    return out


def _entry_text(entry: dict[str, Any]) -> str:
    payload = entry.get("jsonPayload")
    if isinstance(payload, dict):
        text = payload.get("message") or payload.get("exception") or ""
        if text:
            return str(text).strip()
    text = entry.get("textPayload")
    if text:
        return str(text).strip()
    status = (entry.get("protoPayload") or {}).get("status")
    return str(status.get("message") or "").strip() if isinstance(status, dict) else ""


def _options(job: dict[str, Any]) -> dict[str, Any]:
    """Worker machine type, worker counts and SDK from a JOB_VIEW_DESCRIPTION
    job — whichever of them the job carries."""
    env = job.get("environment") or {}
    pool = next(iter(env.get("workerPools") or []), None) or {}
    opts = (env.get("sdkPipelineOptions") or {}).get("options") or {}
    sdk = (env.get("sdkPipelineOptions") or {}).get("sdkPipelineOptions") or {}
    found = {
        "machine_type": pool.get("machineType") or opts.get("workerMachineType") or None,
        "max_workers": opts.get("maxNumWorkers") or pool.get("autoscalingSettings", {}).get("maxNumWorkers") or None,
        "num_workers": pool.get("numWorkers") or opts.get("numWorkers") or None,
        "sdk": (env.get("userAgent") or {}).get("name") or sdk.get("sdk_version") or None,
        "sdk_version": (env.get("userAgent") or {}).get("version") or None,
    }
    return {k: v for k, v in found.items() if v}


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _headline(text: str) -> str:
    """The first line, without the Java exception class chain in front of the
    message ("a.b.UserCodeException: java.io.FileNotFoundException: No files…")."""
    line = text.splitlines()[0] if text.strip() else ""
    return (line.rpartition("Exception: ")[2] or line)[:_SUMMARY_TEXT_CHARS]


def _distinct(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The job's error messages with repeats folded: Dataflow logs the same
    failure once per retry of the work item. First occurrence's time, plus a count."""
    seen: dict[str, dict[str, Any]] = {}
    for m in messages:
        text = _text(m.get("messageText"))[:_TEXT_CHARS]
        if not text:
            continue
        sig = signature(text)
        if sig in seen:
            seen[sig]["count"] += 1
        else:
            seen[sig] = {"text": text, "time": m.get("time"), "count": 1}
    return list(seen.values())


def build_result(*, job_id: str, region: str, project: str, job: dict[str, Any],
                 options: dict[str, Any], errors: list[dict[str, Any]], warnings_count: int,
                 log_groups: list[dict[str, Any]], notes: list[str], now: datetime) -> dict[str, Any]:
    """PURE. The three reads → the answer."""
    state = _text(job.get("currentState")) or "JOB_STATE_UNKNOWN"
    created = _when(job.get("createTime"))
    started = _when(job.get("startTime"))
    changed = _when(job.get("currentStateTime"))
    ended = changed if state in _TERMINAL else None
    last = ended or now
    minutes = round(max(0.0, (last - created).total_seconds()) / 60, 1) if created else None

    failing = state in ("JOB_STATE_FAILED", "JOB_STATE_CANCELLED") or bool(errors) or bool(log_groups)
    # the job's own messages name the cause; worker logs are noisier (kubelet and
    # SDK chatter at ERROR), so they only decide when the messages say nothing
    kind = "none"
    if failing:
        kind = classify([e["text"] for e in errors])
        if kind == "unknown":
            kind = classify([g["sample"] for g in log_groups])

    when = ""
    if minutes is not None:
        span = "<1" if minutes < 1 else f"{minutes:.0f}"
        when = f" after {span} min" if ended else f" for {span} min"
    parts = [f"{state}{when}"]
    if failing:
        parts.append(f"kind: {kind}")
        if errors:
            parts.append(f"'{_headline(errors[0]['text'])}' (job message)")
        elif log_groups:
            parts.append(f"'{_headline(log_groups[0]['sample'])}' (worker log)")
    summary = " — ".join(parts)
    if log_groups:
        summary += f"; {len(log_groups)} worker error group{'s' if len(log_groups) != 1 else ''}"

    out: dict[str, Any] = {
        "ok": True, "job_id": job_id, "region": region, "project": project,
        "name": job.get("name") or None, "state": state, "type": job.get("type") or None,
        "created": _iso(created), "started": _iso(started), "ended": _iso(ended),
        "duration_minutes": minutes, "errors": errors, "warnings_count": warnings_count,
        "log_groups": log_groups, "kind": kind, "summary": summary,
        "console_url": f"{_CONSOLE}/{region}/{job_id}?project={project}",
    }
    out.update(options)
    if notes:
        out["notes"] = notes
    return out


# ----- the reads (the seams tests monkeypatch) ------------------------------------

class _ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _session():
    """An authorised HTTP session on the application-default credentials."""
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    return AuthorizedSession(credentials)


def _call(http, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    r = http.request(method, url, timeout=_TIMEOUT, **kwargs)
    try:
        body = r.json()
    except ValueError:
        body = {}
    body = body if isinstance(body, dict) else {}
    if r.status_code != 200:
        err = body.get("error")
        message = err.get("message") if isinstance(err, dict) else err
        raise _ApiError(r.status_code, _text(message) or f"HTTP {r.status_code}")
    return body


def _job_url(project: str, region: str, job_id: str) -> str:
    return f"{_DATAFLOW}/projects/{project}/locations/{region}/jobs/{job_id}"


def _read_job(http, project: str, region: str, job_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """(the job summary, its options). The description view is best effort —
    it is the larger read, and only the options come from it."""
    url = _job_url(project, region, job_id)
    job = _call(http, "GET", url, params={"view": "JOB_VIEW_SUMMARY"})
    try:
        full = _call(http, "GET", url, params={"view": "JOB_VIEW_DESCRIPTION"})
        options = _options(full)
        for key in ("name", "type", "createTime", "startTime", "currentState", "currentStateTime"):
            job.setdefault(key, full.get(key))
    except _ApiError:
        options = {}
    return job, options


def _read_messages(http, project: str, region: str, job_id: str, importance: str,
                   cap: int) -> list[dict[str, Any]]:
    url = f"{_job_url(project, region, job_id)}/messages"
    out: list[dict[str, Any]] = []
    token = ""
    for _ in range(_MAX_PAGES):
        params = {"minimumImportance": importance, "pageSize": min(_MESSAGE_PAGE, cap - len(out))}
        if token:
            params["pageToken"] = token
        body = _call(http, "GET", url, params=params)
        out.extend(m for m in body.get("jobMessages") or [] if isinstance(m, dict)
                   and (importance != "JOB_MESSAGE_WARNING" or m.get("messageImportance") == importance))
        token = _text(body.get("nextPageToken"))
        if not token or len(out) >= cap:
            break
    return out[:cap]


def _log_floor(created: str) -> str:
    """The earliest time a job's worker logs can carry: a minute before the job
    was created, as RFC 3339 — or "" when the job has no usable create time."""
    from datetime import datetime, timedelta, timezone

    text = _text(created)
    if not text:
        return ""
    try:
        at = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return (at - timedelta(minutes=1)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_logs(http, project: str, job_id: str, cap: int, created: str = "") -> list[dict[str, Any]]:
    # job_id was validated character by character, so it is safe inside the filter
    flt = (f'resource.type="dataflow_step" AND resource.labels.job_id="{job_id}" '
           f'AND severity>=ERROR')
    # Without a time bound Cloud Logging scans its whole default range for the
    # filter — measured 13.5 s for one job against 0.6 s bounded. A job cannot
    # have logged before it existed, so its create time is a safe floor.
    floor = _log_floor(created)
    if floor:
        flt += f' AND timestamp>="{floor}"'
    out: list[dict[str, Any]] = []
    token = ""
    for _ in range(_MAX_PAGES):
        body: dict[str, Any] = {"resourceNames": [f"projects/{project}"], "filter": flt,
                                "orderBy": "timestamp desc", "pageSize": min(1000, cap - len(out))}
        if token:
            body["pageToken"] = token
        page = _call(http, "POST", _LOGGING, json=body)
        out.extend(e for e in page.get("entries") or [] if isinstance(e, dict))
        token = _text(page.get("nextPageToken"))
        if not token or len(out) >= cap:
            break
    return out[:cap]


# ----- entry point -------------------------------------------------------------------

def _failure(error: str, hint: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {"ok": False, "error": error}
    if hint:
        out["hint"] = hint
    return out


def _hint(e: _ApiError, project: str, region: str, job_id: str) -> str:
    if e.status in (401, 403):
        return f"needs roles/dataflow.viewer and roles/logging.viewer on {project}"
    if e.status == 404:
        return f"no job {job_id} in region {region} — check SUPPORT_DATAFLOW_REGION"
    return ""


def inspect(job_id: str, *, region: str = "") -> dict[str, Any]:
    """What a Dataflow job did: state, timing, the job's own errors and its
    worker error logs grouped by signature, with a `kind` for the failure.
    Never raises — a problem comes back as {"ok": False, "error", "hint"}."""
    job_id = (job_id or "").strip()
    region = (region or settings.support_dataflow_region or "").strip()
    project = (settings.support_dataflow_project or settings.gcp_project or "").strip()
    if not _valid(job_id, _ID_CHARS, _JOB_ID_MAX):
        return _failure("job id must be letters, digits, '-' and '_' only.")
    if not region:
        return _failure("no region given.", "set SUPPORT_DATAFLOW_REGION, or pass the job's region")
    if not _valid(region, _REGION_CHARS):
        return _failure(f"region {region!r} has characters a region name does not.")
    if not project:
        return _failure("no project.", "set SUPPORT_DATAFLOW_PROJECT (or GCP_PROJECT)")
    if not _valid(project, _PROJECT_CHARS):
        return _failure("the project id has characters a project id does not.")

    try:
        http = _session()
    except Exception as e:  # noqa: BLE001 — google-auth raises its own family
        return _failure(f"no credentials: {type(e).__name__}: {e}"[:300],
                        "run with application-default credentials that can read Dataflow")
    try:
        job, options = _read_job(http, project, region, job_id)
    except _ApiError as e:
        return _failure(f"Dataflow {e.status}: {e}"[:300], _hint(e, project, region, job_id))
    except Exception as e:  # noqa: BLE001 — network and decoding errors
        return _failure(f"{type(e).__name__}: {e}"[:300])

    notes: list[str] = []
    errors: list[dict[str, Any]] = []
    warnings_count = 0
    try:
        found = _read_messages(http, project, region, job_id, "JOB_MESSAGE_ERROR", _MAX_ERRORS)
        errors = _distinct(found)
        warnings_count = len(_read_messages(http, project, region, job_id, "JOB_MESSAGE_WARNING", _MAX_WARNINGS))
    except _ApiError as e:
        notes.append(f"job messages could not be read ({e.status}).")
    except Exception as e:  # noqa: BLE001
        notes.append(f"job messages could not be read ({type(e).__name__}).")
    log_groups: list[dict[str, Any]] = []
    try:
        cap = max(1, int(getattr(settings, "support_logs_max_lines", 500)))
        log_groups = group_logs(_read_logs(http, project, job_id, cap, _text(job.get("createTime"))))
    except _ApiError as e:
        notes.append(f"worker logs could not be read ({e.status}) — "
                     f"{_hint(e, project, region, job_id) or _text(e)[:100]}.")
    except Exception as e:  # noqa: BLE001
        notes.append(f"worker logs could not be read ({type(e).__name__}).")
    return build_result(job_id=job_id, region=region, project=project, job=job, options=options,
                        errors=errors, warnings_count=warnings_count, log_groups=log_groups,
                        notes=notes, now=datetime.now(timezone.utc))
