"""Re-run one job across the queued images' build runs — design/RERUN_JOB.md.

A person names ANY job (or step) — "sdlc-peer-review", "RCTLDEF0001691",
"1691" — and ticks queued images. `preview` reads each distinct build run once
and answers every image (will re-run / already passed / still running / too
old / refused / ambiguous / no such job / unreadable) with the GitHub calls it
will cost; `start` spends the single-use plan and hands the batch to a thread
that sends one re-run per run, a second apart, follows them with conditional
reads, records passed / failed again, tells the queue what now passes, and
rings the bell once for the whole batch.

Constraints that shaped it:
  * GitHub writes are never retried here: a refused POST is reported with WHY
    (a limit, a permission, too old, already running) and the result card
    offers to start those again — a person's click, after a new yes.
  * The rate limit is read before the first write (/rate_limit is free) and
    obeyed from the headers of every answer; a limit stops the starts, it does
    not burn the rest of the batch on refusals.
  * A job that builds or publishes (RERUN_PROTECTED_JOBS), or that such a job
    depends on — GitHub re-runs a job's dependents with it — is never re-run.
  * The REST calls are made by hand (requests), not PyGithub: the headers ARE
    the rate-limit facts, ETags make polling free, and PyGithub's own retry
    would sleep through a limit with the person waiting.

Nothing here raises to a caller; every failure is an answer.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import settings

logger = logging.getLogger("release_copilot")

# GitHub refuses to re-run a run created more than this long ago.
MAX_RUN_AGE_DAYS = 30
# GitHub asks for at least a second between content-creating requests.
START_GAP_SECONDS = 1.0
POLL_SECONDS = 30.0
# Calls kept back from the token's budget: the follow and the person's other work.
RESERVE_CALLS = 10
# What one started run costs after its POST: a completed read and its jobs.
FOLLOW_CALLS_PER_RUN = 2
PLAN_TTL_SECONDS = 600
_READ_WORKERS = 4
_JOB_PAGES = 3
_MAX_READ_FAILURES = 5
DOMAIN = "rerun"

_PASS = "success"
_FAIL = {"failure", "timed_out", "cancelled", "startup_failure", "action_required"}


# ---------------------------------------------------------------- GitHub REST

@dataclass
class Resp:
    status: int                    # 0 = never reached GitHub
    data: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    error: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class GitHubApi:
    """The few REST calls this feature makes. ``call`` never raises."""

    def __init__(self, token: str | None, base_url: str = "", timeout: float = 20.0):
        import requests

        self.base = (base_url or "https://api.github.com").rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/vnd.github+json",
                                     "X-GitHub-Api-Version": "2022-11-28"})
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        self.timeout = timeout
        self.calls = 0                 # requests sent (304s included — they cost nothing)

    def call(self, method: str, path: str, params: dict | None = None, etag: str = "",
             accept: str = "") -> Resp:
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if accept:
            headers["Accept"] = accept
        self.calls += 1
        try:
            r = self.session.request(method, self.base + path, params=params, headers=headers,
                                     timeout=self.timeout)
        except Exception as e:  # noqa: BLE001
            return Resp(0, error=f"{type(e).__name__}: {e}"[:300])
        low = {k.lower(): v for k, v in r.headers.items()}
        data: Any = None
        if r.content and r.status_code != 304:
            try:
                data = r.json()
            except ValueError:
                data = r.text
        return Resp(r.status_code, data, low)

    def rate(self) -> dict[str, Any] | None:
        """{"remaining", "limit", "reset"} of the core budget, or None. Free."""
        r = self.call("GET", "/rate_limit")
        try:
            core = r.data["resources"]["core"]
            return {"remaining": int(core["remaining"]), "limit": int(core["limit"]),
                    "reset": int(core["reset"])}
        except Exception:  # noqa: BLE001
            return None


def _default_api() -> GitHubApi:
    from ._common import _resolve_github_token

    return GitHubApi(_resolve_github_token(), settings.github_base_url)


# ---------------------------------------------------------------- PURE rules

def words(name: str) -> list[str]:
    """Lowercase alphanumeric words: 'build-and-publish (x)' → build, and, publish, x."""
    out, cur = [], ""
    for ch in str(name or "").lower() + " ":
        if ch.isalnum():
            cur += ch
        elif cur:
            out.append(cur)
            cur = ""
    return out


def last_segment(name: str) -> str:
    """A reusable workflow's job is '<caller> / <job>'; the caller prefix is not
    what the job does."""
    return str(name or "").rsplit(" / ", 1)[-1].strip()


def protected_words(raw: str | None = None) -> set[str]:
    raw = settings.rerun_protected_jobs if raw is None else raw
    return {w for part in str(raw or "").split(",") for w in words(part)}


def is_protected(job_name: str, protect: set[str]) -> bool:
    return bool(protect & set(words(last_segment(job_name))))


def _norm(s: str) -> str:
    return str(s or "").strip().lower()


def match_jobs(jobs: list[dict], wanted: str) -> tuple[list[dict], str]:
    """The jobs ``wanted`` names, and how ("job" | "step"). Exact job name (or
    its last segment) first; then a pattern over job names (controls.control_
    matches: '1691' finds RCTLDEF0001691); then the job that HOLDS a step so
    named — a step cannot be re-run on its own, its job can."""
    from .controls import control_matches

    w = _norm(wanted)
    if not w:
        return [], ""
    exact = [j for j in jobs if _norm(j.get("name")) == w or _norm(last_segment(j.get("name", ""))) == w]
    if exact:
        return exact, "job"
    pattern = [j for j in jobs if control_matches(j.get("name", ""), wanted)]
    if pattern:
        return pattern, "job"
    holders = [j for j in jobs if any(_norm(s.get("name")) == w or control_matches(s.get("name", ""), wanted)
                                      for s in j.get("steps") or [])]
    return holders, ("step" if holders else "")


def workflow_graph(text: Any) -> dict[str, dict] | None:
    """{job key: {"name": display name, "needs": [keys]}} from a workflow file,
    or None when it cannot be read as one."""
    import yaml

    try:
        doc = yaml.safe_load(text) if isinstance(text, str) else None
        jobs = doc.get("jobs") if isinstance(doc, dict) else None
        if not isinstance(jobs, dict):
            return None
        graph = {}
        for key, body in jobs.items():
            body = body if isinstance(body, dict) else {}
            needs = body.get("needs") or []
            needs = [needs] if isinstance(needs, str) else [str(n) for n in needs if n]
            graph[str(key)] = {"name": str(body.get("name") or key), "needs": needs}
        return graph
    except Exception:  # noqa: BLE001
        return None


def _key_for(graph: dict[str, dict], api_name: str) -> str | None:
    """Which job key an API job name comes from: the key, its `name:`, a matrix
    leg 'name (x, y)', or a reusable call's caller 'caller / inner'."""
    n = _norm(api_name)
    caller = _norm(str(api_name or "").split(" / ", 1)[0])
    for key, body in graph.items():
        for label in (_norm(key), _norm(body["name"])):
            if n == label or n.startswith(label + " (") or caller == label:
                return key
    return None


def dependents(graph: dict[str, dict] | None, api_name: str) -> list[str] | None:
    """Display names of every job that (transitively) needs ``api_name``'s job —
    GitHub re-runs them with it. None when that cannot be told."""
    if not graph:
        return None
    key = _key_for(graph, api_name)
    if key is None:
        return None
    found, frontier = set(), {key}
    while frontier:
        nxt = {k for k, b in graph.items() if k not in found and k != key and set(b["needs"]) & frontier}
        found |= nxt
        frontier = nxt
    return sorted(graph[k]["name"] for k in found)


def _parse_ts(value: Any) -> _dt.datetime | None:
    try:
        return _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def answer_for_run(run: dict, jobs: list[dict], wanted: str, *, include_passed: bool,
                   graph: dict[str, dict] | None, now: _dt.datetime, protect: set[str]) -> dict[str, Any]:
    """What happens to ONE run. {"state", "detail", ...} — state is rerun |
    passed | running | too_old | protected | ambiguous | no_job."""
    created = _parse_ts(run.get("created_at"))
    attempt = int(run.get("run_attempt") or 1)
    base = {"attempt": attempt, "run_url": run.get("html_url") or "", "workflow": run.get("name") or ""}
    if run.get("status") != "completed":
        return {**base, "state": "running", "detail": "the run is still going — GitHub re-runs only a finished run"}
    if created and (now - created).days >= MAX_RUN_AGE_DAYS:
        return {**base, "state": "too_old", "detail": f"run is {(now - created).days} days old "
                f"(GitHub allows {MAX_RUN_AGE_DAYS}) — it needs a new build"}
    matched, via = match_jobs(jobs, wanted)
    if not matched:
        return {**base, "state": "no_job", "detail": f"no job or step in this run is named like '{wanted}'"}
    names = sorted({j.get("name", "") for j in matched})
    if len(matched) > 1:
        return {**base, "state": "ambiguous", "detail": "matches " + ", ".join(names[:6]) +
                (" …" if len(names) > 6 else "") + " — type the full job name", "matches": names}
    job = matched[0]
    name = job.get("name", "")
    out = {**base, "job_id": job.get("id"), "job": name, "via": via, "conclusion": job.get("conclusion")}
    if via == "step":
        out["step"] = next((s.get("name") for s in job.get("steps") or []
                            if _norm(s.get("name")) == _norm(wanted)), wanted)
    if is_protected(name, protect):
        return {**out, "state": "protected", "detail": f"'{name}' builds or publishes — re-running it would "
                "rebuild and republish the image" + (f" ('{wanted}' is a step inside it)" if via == "step" else "")}
    deps = dependents(graph, name)
    out["dependents"] = deps
    hit = [d for d in deps or [] if is_protected(d, protect)]
    if hit:
        return {**out, "state": "protected", "detail": f"re-running '{name}' also re-runs {', '.join(hit)}, "
                "which depends on it and builds or publishes the image"}
    if job.get("conclusion") == _PASS and not include_passed:
        return {**out, "state": "passed", "detail": f"'{name}' already passed on attempt {attempt}"}
    return {**out, "state": "rerun", "detail": f"will re-run job {name}"}


def budget(rate: dict | None, runs: int, reserve: int = RESERVE_CALLS) -> dict[str, Any]:
    """How many of ``runs`` re-runs the token can afford: each costs its POST
    plus what following it takes. Unknown budget = all (GitHub still answers
    every call with its limit, which the start obeys)."""
    need = runs * (1 + FOLLOW_CALLS_PER_RUN)
    if not rate:
        return {"known": False, "needed": need, "fits": runs}
    spare = max(0, int(rate["remaining"]) - reserve)
    fits = min(runs, spare // (1 + FOLLOW_CALLS_PER_RUN))
    return {"known": True, "needed": need, "fits": fits, "remaining": int(rate["remaining"]),
            "limit": int(rate.get("limit") or 0), "reset": int(rate.get("reset") or 0)}


def _hhmm(epoch: Any) -> str:
    try:
        return _dt.datetime.fromtimestamp(int(epoch), _dt.timezone.utc).strftime("%H:%M UTC")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _message(data: Any) -> str:
    if isinstance(data, dict):
        return str(data.get("message") or "")
    return str(data or "")[:300]


def classify(resp: Resp, now: float | None = None) -> dict[str, Any]:
    """What a GitHub answer means here. {"kind", "text", "until"} — kind is ok |
    primary_limit | secondary_limit | permission | too_old | already_running |
    not_found | network | server | refused. Pure apart from the clock."""
    now = time.time() if now is None else now
    status, h, msg = resp.status, resp.headers or {}, _message(resp.data)
    low = msg.lower()
    if resp.ok:
        return {"kind": "ok", "text": "", "until": None}
    if status == 0:
        return {"kind": "network", "text": f"GitHub could not be reached: {resp.error}", "until": None}
    if status in (403, 429):
        retry_after = h.get("retry-after")
        if "secondary rate limit" in low or (retry_after and h.get("x-ratelimit-remaining") != "0"):
            try:
                until = now + float(retry_after or 60)
            except ValueError:
                until = now + 60
            return {"kind": "secondary_limit", "until": until,
                    "text": f"GitHub asked to slow down (secondary limit) — try again after {_hhmm(until)}"}
        if h.get("x-ratelimit-remaining") == "0" or "rate limit" in low:
            reset = h.get("x-ratelimit-reset")
            until = float(reset) if reset and reset.isdigit() else now + 3600
            return {"kind": "primary_limit", "until": until,
                    "text": f"GitHub API limit reached — it resets at {_hhmm(until)}"}
    if "month ago" in low or "older than" in low:
        return {"kind": "too_old", "text": f"run older than {MAX_RUN_AGE_DAYS} days — GitHub refused: {msg}", "until": None}
    if "already running" in low or "currently running" in low or "in progress" in low:
        return {"kind": "already_running", "text": "already re-running", "until": None}
    if status in (401, 403) or "not accessible" in low or "must have admin" in low:
        return {"kind": "permission", "until": None,
                "text": f"this token cannot re-run workflows here (needs Actions: write) — GitHub said: {msg or status}"}
    if status == 404:
        return {"kind": "not_found", "until": None,
                "text": "GitHub answered 404 — the run is gone, or this token cannot see or write the repo"}
    if status >= 500:
        return {"kind": "server", "text": f"GitHub error {status}: {msg}"[:300], "until": None}
    return {"kind": "refused", "text": f"GitHub refused ({status}): {msg}"[:300], "until": None}


STOPS_BATCH = {"primary_limit", "secondary_limit"}
STOPS_REPO = {"permission"}


def poll_delay(unfinished: int, rate: dict | None, now: float | None = None,
               base: float = POLL_SECONDS) -> float:
    """Seconds until the next look at the started runs. 304s are free, but a
    changed run is a counted read: when the budget left would not cover a few
    more passes, wait longer — up to the reset, never past ten minutes."""
    if not rate or unfinished <= 0:
        return base
    now = time.time() if now is None else now
    left = int(rate.get("remaining") or 0) - RESERVE_CALLS
    if left >= unfinished * 10:
        return base
    to_reset = max(base, float(rate.get("reset") or 0) - now)
    if left <= unfinished:
        return min(to_reset + 1, 600.0)
    return min(max(base, to_reset / max(1, left // unfinished)), 600.0)


RESULT_STATES = ("passed", "failed_again", "not_started", "running", "skipped", "unfollowed")


def summary(batch: dict) -> dict[str, Any]:
    """Counts per image and the one line the bell says. Pure."""
    counts = {s: 0 for s in RESULT_STATES}
    for row in batch.get("rows") or []:
        counts[row.get("result") or "skipped"] = counts.get(row.get("result") or "skipped", 0) + 1
    parts = []
    for key, label in (("pending", "waiting to start"), ("passed", "passed"), ("failed_again", "failed again"), ("not_started", "not started"),
                       ("running", "still running"), ("unfollowed", "not followed to the end"),
                       ("skipped", "not re-run")):
        if counts.get(key):
            parts.append(f"{counts[key]} {label}")
    # The job the runs resolved to ("sdlc-peer-review"), not what was typed ("1691").
    names = sorted({r.get("job") for r in batch.get("runs") or [] if r.get("job")})
    job = names[0] if len(names) == 1 else (batch.get("job") or "job")
    done = batch.get("status") in ("finished", "stopped")
    title = f"{job} re-run {'finished' if done else 'running'}: " + (", ".join(parts) or "nothing to re-run")
    return {"counts": counts, "title": title}


# ---------------------------------------------------------------- queue facts

def _split(artifact: str) -> tuple[str, str]:
    name, _, version = str(artifact or "").strip().partition(":")
    return name.strip(), version.strip()


def candidates() -> dict[str, Any]:
    """The queued charts this can act on: each with its build run's repo and id."""
    from . import release_queue
    from .controls import _parse_run_url

    q = release_queue.current_queue()
    if not q.get("ok"):
        return {"ok": False, "error": q.get("error") or "the release queue is unavailable",
                "disabled": bool(q.get("disabled"))}
    out = []
    for item in q.get("queue") or []:
        url = item.get("build_run_url") or ""
        repo, run_id = _parse_run_url(url) if url else (None, None)
        out.append({"artifact": f"{item.get('artifact_name')}:{item.get('artifact_version')}",
                    "requested_by": item.get("requested_by") or "",
                    "allowed_failures": item.get("allowed_failures") or "",
                    "build_verified": item.get("build_verified"),
                    "run_url": url, "repo": repo or "", "run_id": run_id})
    return {"ok": True, "images": out}


# ---------------------------------------------------------------- reading runs

def _read_jobs(api: GitHubApi, repo: str, run_id: int) -> tuple[list[dict] | None, Resp | None]:
    jobs: list[dict] = []
    for page in range(1, _JOB_PAGES + 1):
        r = api.call("GET", f"/repos/{repo}/actions/runs/{run_id}/jobs",
                     params={"filter": "latest", "per_page": 100, "page": page})
        if not r.ok:
            return None, r
        batch = (r.data or {}).get("jobs") or []
        jobs += [{"id": j.get("id"), "name": j.get("name") or "", "status": j.get("status"),
                  "conclusion": j.get("conclusion"), "run_attempt": j.get("run_attempt"),
                  "html_url": j.get("html_url") or "",
                  "steps": [{"name": s.get("name") or "", "conclusion": s.get("conclusion"),
                             "number": s.get("number")} for s in j.get("steps") or []]}
                 for j in batch]
        if len(batch) < 100:
            break
    return jobs, None


def _read_run(api: GitHubApi, repo: str, run_id: int, wanted: str, include_passed: bool,
              now: _dt.datetime, protect: set[str], files: dict, lock: threading.Lock) -> dict[str, Any]:
    r = api.call("GET", f"/repos/{repo}/actions/runs/{run_id}")
    if not r.ok:
        c = classify(r)
        return {"state": "unreadable", "detail": f"run {run_id} could not be read — {c['text']}", "why": c["kind"]}
    run = r.data or {}
    jobs, bad = _read_jobs(api, repo, run_id)
    if jobs is None:
        c = classify(bad)
        return {"state": "unreadable", "detail": f"the jobs of run {run_id} could not be read — {c['text']}",
                "why": c["kind"], "run_url": run.get("html_url") or ""}
    graph = None
    path, sha = run.get("path") or "", run.get("head_sha") or ""
    if path and sha:
        key = (repo, path, sha)
        with lock:
            cached = files.get(key, ...)
        if cached is ...:
            fr = api.call("GET", f"/repos/{repo}/contents/{path}", params={"ref": sha},
                          accept="application/vnd.github.raw+json")
            cached = workflow_graph(fr.data) if fr.ok else None
            with lock:
                files[key] = cached
        graph = cached
    ans = answer_for_run(run, jobs, wanted, include_passed=include_passed, graph=graph, now=now, protect=protect)
    if ans.get("state") == "rerun" and ans.get("dependents") is None:
        ans["caution"] = "which jobs depend on it could not be read from the workflow file — GitHub re-runs those too"
    return ans


# ---------------------------------------------------------------- plans

_plans: dict[str, dict] = {}
_batches: dict[str, dict] = {}
_lock = threading.Lock()


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def preview(job: str, images: list[str], *, include_passed: bool = False, owner: str | None = None,
            thread_id: str = "", api: GitHubApi | None = None) -> dict[str, Any]:
    """Read-only: what re-running ``job`` for ``images`` would do. Returns the
    plan (with a single-use ``plan_id`` when anything would start). Never raises."""
    try:
        wanted = str(job or "").strip()
        if not wanted:
            return {"ok": False, "error": "Type the job (or step) to re-run."}
        picked = [str(i).strip() for i in images or [] if str(i).strip()]
        if not picked:
            return {"ok": False, "error": "Tick at least one image."}
        cand = candidates()
        if not cand.get("ok"):
            return cand
        by_artifact = {c["artifact"]: c for c in cand["images"]}
        api = api or _default_api()
        protect = protected_words()
        now = _now()
        rows: list[dict] = []
        runs: dict[tuple[str, int], dict] = {}
        order: list[tuple[str, int]] = []
        for art in picked:
            c = by_artifact.get(art)
            if c is None:
                rows.append({"artifact": art, "state": "unreadable", "detail": "not in the release queue any more"})
                continue
            if not c["run_id"] or not c["repo"]:
                rows.append({"artifact": art, "state": "unreadable", "detail": "no build run URL on its queue entry"})
                continue
            key = (c["repo"], int(c["run_id"]))
            if key not in runs:
                runs[key] = {"repo": key[0], "run_id": key[1], "images": []}
                order.append(key)
            runs[key]["images"].append(art)
            rows.append({"artifact": art, "repo": key[0], "run_id": key[1], "run_url": c["run_url"]})
        cap = max(1, int(settings.rerun_max_runs or 50))
        over = set(order[cap:])
        files: dict = {}
        flock = threading.Lock()

        def read(key):
            return key, _read_run(api, key[0], key[1], wanted, include_passed, now, protect, files, flock)

        with ThreadPoolExecutor(max_workers=_READ_WORKERS) as pool:
            answers = dict(pool.map(read, [k for k in order if k not in over]))
        for key in over:
            answers[key] = {"state": "over_cap", "detail": f"not in this batch — at most {cap} runs at once "
                            "(RERUN_MAX_RUNS); preview the rest after"}
        for key, info in runs.items():
            info.update(answers[key])
        for row in rows:
            key = (row.get("repo"), row.get("run_id"))
            if key in runs:
                info = runs[key]
                row.update({k: info.get(k) for k in ("state", "detail", "job", "via", "attempt", "caution", "step")
                            if info.get(k) is not None})
                if info.get("run_url"):
                    row["run_url"] = info["run_url"]
                row["shared"] = len(info["images"]) > 1
        to_run = [runs[k] for k in order if runs[k].get("state") == "rerun"]
        rate = api.rate()
        plan = {"plan_id": uuid.uuid4().hex[:12], "job": wanted, "include_passed": bool(include_passed),
                "owner": owner, "thread_id": thread_id or "", "created": time.time(),
                "rows": rows, "runs": [{k: r.get(k) for k in ("repo", "run_id", "job_id", "job", "attempt",
                                                              "images", "run_url")} for r in to_run],
                "budget": budget(rate, len(to_run)), "calls": api.calls}
        out = {"ok": True, **{k: v for k, v in plan.items() if k not in ("owner", "thread_id", "created")}}
        if to_run:
            with _lock:
                _expire_plans()
                _plans[plan["plan_id"]] = plan
        else:
            out.pop("plan_id")
        return out
    except Exception as e:  # noqa: BLE001
        logger.exception("rerun preview failed")
        return {"ok": False, "error": f"The preview failed: {type(e).__name__}: {e}"[:300]}


def _expire_plans() -> None:
    cut = time.time() - PLAN_TTL_SECONDS
    for pid in [p for p, plan in _plans.items() if plan["created"] < cut]:
        _plans.pop(pid, None)


def _take_plan(plan_id: str, owner: str | None, thread_id: str) -> tuple[dict | None, str]:
    with _lock:
        _expire_plans()
        plan = _plans.get(str(plan_id or ""))
        if plan is None:
            return None, "That preview has expired or was already used — preview again."
        if plan["owner"] != owner or plan["thread_id"] != (thread_id or ""):
            return None, "That preview belongs to someone else — preview it yourself."
        _plans.pop(plan["plan_id"], None)   # single use: spent before anything is sent
        return plan, ""


# ---------------------------------------------------------------- the batch

def _public(batch: dict) -> dict:
    out = {k: v for k, v in batch.items() if not k.startswith("_")}
    out.update(summary(batch))
    return out


def _persist(batch: dict, event_type: str) -> None:
    from . import events_store

    s = summary(batch)
    events_store.record({"domain": DOMAIN, "event_type": event_type, "subject": f"rerun:{batch['id']}",
                         "actor": batch.get("requested_by") or "portal", "status": batch.get("status"),
                         "title": s["title"], "workflow": batch.get("job"),
                         "count": len(batch.get("runs") or []),
                         "details": json.dumps(_public(batch), default=str)})


def _line(batch: dict, text: str) -> None:
    batch["lines"].append({"at": _now().isoformat(timespec="seconds"), "text": text})
    batch["lines"] = batch["lines"][-400:]


def _set_rows(batch: dict, run: dict, **values: Any) -> None:
    for row in batch["rows"]:
        if row.get("repo") == run["repo"] and row.get("run_id") == run["run_id"]:
            row.update(values)


def start(plan_id: str, *, owner: str | None, thread_id: str = "", requested_by: str = "",
          api: GitHubApi | None = None, background: bool = True,
          pat_gone: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Spend the plan and begin. {"ok", "batch_id", "batch"} — the starts and the
    follow run on a thread (``background=False`` runs them here, for tests)."""
    plan, why = _take_plan(plan_id, owner, thread_id)
    if plan is None:
        return {"ok": False, "error": why}
    api = api or _default_api()
    batch = {"id": uuid.uuid4().hex[:12], "job": plan["job"], "requested_by": requested_by or "",
             "status": "starting", "started_at": _now().isoformat(timespec="seconds"), "finished_at": None,
             "runs": [dict(r, state="pending") for r in plan["runs"]],
             "rows": [dict(r, result=("pending" if r.get("state") == "rerun" else "skipped")) for r in plan["rows"]],
             "lines": [], "stopped": "", "_thread_id": thread_id or ""}
    with _lock:
        _batches[batch["id"]] = batch
    _persist(batch, "started")
    gone = pat_gone or (lambda: False)
    if background:
        threading.Thread(target=run_batch, args=(batch, api, gone), name=f"rerun-{batch['id']}",
                         daemon=True).start()
    else:
        run_batch(batch, api, gone)
    return {"ok": True, "batch_id": batch["id"], "batch": _public(batch)}


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _clock() -> float:
    return time.time()


def run_batch(batch: dict, api: GitHubApi, pat_gone: Callable[[], bool],
              sleep: Callable[[float], None] | None = None, clock: Callable[[], float] | None = None) -> None:
    """Start every run, follow them, tell the person. Never raises."""
    sleep = sleep or _sleep
    clock = clock or _clock
    try:
        _start_all(batch, api, sleep)
        _persist(batch, "starts_done")
        _follow(batch, api, pat_gone, sleep, clock)
    except Exception as e:  # noqa: BLE001
        logger.exception("rerun batch %s failed", batch.get("id"))
        batch["stopped"] = f"the portal hit an error: {type(e).__name__}: {e}"[:300]
        for run in batch["runs"]:
            if run["state"] in ("pending", "started"):
                run["state"] = "unfollowed"
                _set_rows(batch, run, result="unfollowed" if run.get("started") else "not_started",
                          detail=batch["stopped"])
    batch["status"] = "finished" if not batch["stopped"] else "stopped"
    batch["finished_at"] = _now().isoformat(timespec="seconds")
    _persist(batch, "finished")
    _notify(batch)


def _start_all(batch: dict, api: GitHubApi, sleep: Callable[[float], None]) -> None:
    runs = batch["runs"]
    n = len(runs)
    rate = api.rate()
    plan = budget(rate, n)
    _line(batch, f"Re-running {batch['job']} in {n} run{'s' if n != 1 else ''}…" +
          (f" (GitHub budget: {plan['remaining']} calls left)" if plan.get("known") else ""))
    stop: dict | None = None
    blocked: dict[str, str] = {}
    sent = 0
    for i, run in enumerate(runs, 1):
        label = f"{i}/{n} run {run['run_id']} ({', '.join(run['images'])})"
        why = ""
        if stop:
            why = stop["text"]
        elif run["repo"] in blocked:
            why = blocked[run["repo"]]
        elif i > plan["fits"]:
            why = (f"not enough GitHub API calls left ({plan.get('remaining')} left, "
                   f"resets at {_hhmm(plan.get('reset'))})")
        if why:
            run.update(state="not_started", why=why)
            _set_rows(batch, run, result="not_started", detail=why)
            _line(batch, f"{label} — not started: {why}")
            continue
        if sent:
            sleep(START_GAP_SECONDS)
        r = api.call("POST", f"/repos/{run['repo']}/actions/jobs/{run['job_id']}/rerun")
        sent += 1
        c = classify(r)
        if c["kind"] == "ok":
            run.update(state="started", started=True, target_attempt=int(run.get("attempt") or 1) + 1)
            # The attempt number is its own field: rows that share an outcome share one reason.
            _set_rows(batch, run, result="running", detail="started a new attempt", new_attempt=run["target_attempt"])
            _line(batch, f"{label} — started, attempt {run['target_attempt']}")
            continue
        run.update(state="not_started", why=c["text"], why_kind=c["kind"])
        _set_rows(batch, run, result="not_started", detail=c["text"], why_kind=c["kind"])
        _line(batch, f"{label} — not started: {c['text']}")
        if c["kind"] in STOPS_BATCH:
            stop = c
        elif c["kind"] in STOPS_REPO:
            blocked[run["repo"]] = c["text"]
    if stop:
        batch["stopped_starts"] = stop["text"]
    batch["status"] = "following"


def _job_result(jobs: list[dict], job_name: str) -> tuple[str, str, str]:
    """(result, detail, job url) for the job of that name on the new attempt."""
    job = next((j for j in jobs if j.get("name") == job_name), None)
    if job is None:
        return "failed_again", f"'{job_name}' is not in the new attempt", ""
    concl = job.get("conclusion")
    if concl == _PASS:
        return "passed", "passed on the new attempt", job.get("html_url") or ""
    step = next((s.get("name") for s in job.get("steps") or [] if s.get("conclusion") in _FAIL), "")
    detail = (concl or "unknown") + (f" · step “{step}”" if step else "")
    return "failed_again", detail, job.get("html_url") or ""


def _follow(batch: dict, api: GitHubApi, pat_gone: Callable[[], bool], sleep: Callable[[float], None],
            clock: Callable[[], float]) -> None:
    started = [r for r in batch["runs"] if r["state"] == "started"]
    if not started:
        return
    minutes = float(min(90, settings.session_idle_minutes or 90))
    deadline = clock() + minutes * 60
    etags: dict[int, str] = {}
    failures: dict[int, int] = {}
    rate = api.rate()
    while True:
        open_runs = [r for r in started if r["state"] == "started"]
        if not open_runs:
            return
        if pat_gone():
            # The session token is gone: no further call is made with it (the
            # rule df_run.py follows). The bell still says what is known — that
            # takes no token.
            batch["stopped"] = "the session's GitHub token was dropped — the runs carry on; open them to see"
            for run in open_runs:
                run["state"] = "unfollowed"
                _set_rows(batch, run, result="unfollowed", detail="not followed to the end — the token was dropped")
            return
        if clock() >= deadline:
            left = int((rate or {}).get("remaining", RESERVE_CALLS + len(open_runs) * 10))
            if rate and left - RESERVE_CALLS < len(open_runs) * 10:      # poll_delay was stretching
                # The waits stretched because the budget ran low — say that, not "slow".
                batch["stopped"] = (f"stopped following — the GitHub API budget ran low ({left} left, resets at "
                                    f"{_hhmm(rate.get('reset'))}); the runs carry on")
                result = "unfollowed"
            else:
                batch["stopped"] = f"still running after {int(minutes)} min — the portal stopped following"
                result = "running"
            for run in open_runs:
                run["state"] = "unfollowed"
                _set_rows(batch, run, result=result, detail=batch["stopped"])
            _line(batch, batch["stopped"])
            return
        limited: dict | None = None
        for run in open_runs:
            rid = int(run["run_id"])
            r = api.call("GET", f"/repos/{run['repo']}/actions/runs/{rid}", etag=etags.get(rid, ""))
            if r.status == 304:
                continue
            c = classify(r)
            if c["kind"] in STOPS_BATCH:
                limited = c
                break
            if not r.ok:
                failures[rid] = failures.get(rid, 0) + 1
                if failures[rid] >= _MAX_READ_FAILURES:
                    run["state"] = "unfollowed"
                    _set_rows(batch, run, result="unfollowed", detail=f"could not be read: {c['text']}")
                    _line(batch, f"run {rid} — no longer followed: {c['text']}")
                continue
            failures[rid] = 0
            if r.headers.get("etag"):
                etags[rid] = r.headers["etag"]
            data = r.data or {}
            if int(data.get("run_attempt") or 0) < int(run["target_attempt"]) or data.get("status") != "completed":
                continue
            jobs, bad = _read_jobs(api, run["repo"], rid)
            if jobs is None:
                c = classify(bad)
                if c["kind"] in STOPS_BATCH:
                    limited = c
                    break
                continue
            result, detail, job_url = _job_result(jobs, run["job"])
            run.update(state=result, job_url=job_url)
            _set_rows(batch, run, result=result, detail=detail, job_url=job_url,
                      run_url=data.get("html_url") or run.get("run_url"))
            _line(batch, f"run {rid} ({', '.join(run['images'])}) — attempt {run['target_attempt']} "
                  f"{'passed' if result == 'passed' else 'failed again'}: {detail}")
            if result == "passed":
                _reverify(batch, run, jobs)
        if not limited and not [r for r in started if r["state"] == "started"]:
            return                       # the last one just finished: no wait for the next pass
        if limited:
            wait_to = float(limited["until"] or clock() + 60)
            if wait_to >= deadline:
                batch["stopped"] = f"stopped following — {limited['text']}; the runs carry on"
                for run in [r for r in started if r["state"] == "started"]:
                    run["state"] = "unfollowed"
                    _set_rows(batch, run, result="unfollowed", detail=batch["stopped"])
                _line(batch, batch["stopped"])
                return
            _line(batch, f"waiting — {limited['text']}")
            sleep(max(1.0, wait_to - clock()))
            rate = api.rate()
            continue
        # Never sleep past the deadline: the person is told when following stops.
        wait = poll_delay(len([r for r in started if r["state"] == "started"]), rate, now=clock())
        sleep(max(1.0, min(wait, deadline - clock() + 1)))
        rate = api.rate()


def _reverify(batch: dict, run: dict, jobs: list[dict]) -> None:
    """Tell the queue which allowed controls still fail on this run's images.
    Best-effort: the result card says what happened either way."""
    from . import release_queue
    from .controls import _is_control_step, allowed_to_fail

    try:
        failing = []
        for job in jobs:
            steps = [s for s in job.get("steps") or [] if _is_control_step(s.get("name", ""))]
            if steps:
                failing += [s["name"] for s in steps if s.get("conclusion") in _FAIL]
            elif _is_control_step(job.get("name", "")) and job.get("conclusion") in _FAIL:
                failing.append(job["name"])
        if any(not allowed_to_fail(f) for f in failing):
            _set_rows(batch, run, queue="a control that is not allowed to fail fails on this run — queue unchanged")
            return
        if not release_queue.queue_enabled():
            return
        for art in run["images"]:
            res = release_queue.reverify(art, batch.get("requested_by") or "portal", failing,
                                         build_run_url=run.get("run_url") or "",
                                         note=f"re-run of {run['job']} (attempt {run.get('target_attempt')})")
            for row in batch["rows"]:
                if row.get("artifact") == art:
                    row["queue"] = ("controls: " + (", ".join(failing) + " still open" if failing else "all passed")
                                    if res.get("ok") else f"queue not updated: {res.get('error')}")
    except Exception as e:  # noqa: BLE001
        logger.warning("rerun %s: queue not updated: %s", batch.get("id"), e)


def _notify(batch: dict) -> None:
    from . import notifications

    who = batch.get("requested_by") or ""
    if "@" not in who:
        return
    s = summary(batch)
    counts = s["counts"]
    bad = counts.get("failed_again") or counts.get("not_started") or counts.get("unfollowed")
    notifications.notify(who, kind="rerun", scope=f"rerun:{batch['id']}", title=s["title"],
                         body=batch.get("stopped") or batch.get("stopped_starts") or "",
                         link=f"portal:rerun:{batch['id']}", severity="warning" if bad else "info")


def get_batch(batch_id: str) -> dict[str, Any]:
    """The batch for its card: this process's live copy, else its last row."""
    with _lock:
        live = _batches.get(str(batch_id or ""))
    if live is not None:
        return {"ok": True, "batch": _public(live), "live": True}
    from . import events_store

    res = events_store.latest(DOMAIN, subject=f"rerun:{batch_id}", since_days=30, limit=1)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error") or "the re-run could not be read"}
    rows = res.get("rows") or []
    if not rows:
        return {"ok": False, "error": "No such re-run — it is older than 30 days or never started."}
    try:
        batch = json.loads(rows[0].get("details") or "{}")
    except ValueError:
        return {"ok": False, "error": "The re-run's record could not be read."}
    if batch.get("status") not in ("finished", "stopped"):
        batch["status"] = "stopped"
        batch["stopped"] = "no longer followed — the portal restarted while it ran; open the runs to see"
    batch.update(summary(batch))
    return {"ok": True, "batch": batch, "live": False}
