"""Build-pipeline verification + RLFT/RFTL release-control tools."""

import base64
import itertools
import json
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from ._common import (
    settings,
    tool,
    _resolve_github_token,
    _get_github_client,
    active_build_repo,
    CONFIG_PATH,
)


def _image_build_workflow(repo_obj, image: str) -> str | None:
    """Look up an image's build workflow from image-workflows.json. Accepts either
    the 'build_workflow' or 'workflow' key (different repos use different names)."""
    try:
        cfg = json.loads(base64.b64decode(repo_obj.get_contents(CONFIG_PATH).content).decode())
        entry = cfg.get("images", {}).get(image)
        if isinstance(entry, dict):
            return entry.get("build_workflow") or entry.get("workflow")
        return None
    except Exception:
        return None


def _resolve_tag_commit(repo_obj, tag: str) -> str | None:
    """Resolve a git tag to the commit SHA it points to (handles annotated tags)."""
    try:
        ref = repo_obj.get_git_ref(f"tags/{tag}")
    except Exception:
        return None
    obj = ref.object
    if obj.type == "tag":  # annotated tag -> dereference to the underlying commit
        try:
            return repo_obj.get_git_tag(obj.sha).object.sha
        except Exception:
            return obj.sha
    return obj.sha


def _fetch_job_log(repo_full: str, job_id: int) -> str:
    """Download a job's full log text via the REST API (PyGithub has no helper for this)."""
    token = _resolve_github_token()
    if not token:
        return ""
    try:
        import requests

        # GitHub Enterprise serves the API from GITHUB_BASE_URL, not api.github.com.
        api = (settings.github_base_url or "https://api.github.com").rstrip("/")
        r = requests.get(
            f"{api}/repos/{repo_full}/actions/jobs/{job_id}/logs",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            allow_redirects=True,
            timeout=30,
        )
        return r.text if r.status_code == 200 else ""
    except Exception:
        return ""


# Step conclusions that count as a failed control gate.
_FAIL_CONCLUSIONS = {"failure", "timed_out", "cancelled", "startup_failure", "action_required"}


def _is_control_step(name: str) -> bool:
    """Case-insensitive prefix match against CONTROL_PREFIXES — the live gate is
    RCTLD, covering RCTLDEF0001691-style SDLC controls; RLFT/RFTL are demo gates.

    Stripped before matching: a step named " RCTLDEF0001691" would otherwise stop
    being a control, and an unmatched control is indistinguishable from a run
    with no controls at all — the run would read as clean."""
    low = name.strip().lower()
    return any(low.startswith(p.strip().lower()) for p in settings.control_prefixes)


def _digit_runs(text: str) -> list[int]:
    """Every run of digits in ``text``, as numbers: 'RCTLDEF0001691v2' → [1691, 2]."""
    runs, digits = [], ""
    for ch in text + " ":
        if ch.isdigit():
            digits += ch
        elif digits:
            runs.append(int(digits))
            digits = ""
    return runs


def control_matches(control_name: str, token: str) -> bool:
    """Does ``token`` name this control? Pure. A PATTERN, not the exact name:

      "1691"          the name CONTAINS the number 1691 — any run of digits equal
                      to it, leading zeros ignored: RCTLDEF0001691, RCTL-1691,
                      "Control 1691: …", RCTLDEF0001691v2. A bigger number that
                      merely contains the digits is a different control
                      (RCTLDEF0016910, RCTLDEF0021691) and does not match.
      "*1691*"        a wildcard pattern (* and ?) over the whole name,
                      case-insensitive — for a plain "contains" match.
      "RCTLDEF0001691" any other text: the name contains it, case-insensitive.
    """
    from fnmatch import fnmatchcase

    token = (token or "").strip().lower()
    name = (control_name or "").lower()
    if not token:
        return False
    if "*" in token or "?" in token:
        return fnmatchcase(name, token)
    if token.isdigit():
        return int(token) in _digit_runs(name)
    return token in name


def allowed_to_fail(control_name: str) -> bool:
    """Is this control's failure allowed at queue time (QUEUE_ALLOWED_FAILING_CONTROLS)?"""
    tokens = [t for t in (settings.queue_allowed_failing_controls or "").split(",") if t.strip()]
    return any(control_matches(control_name, t) for t in tokens)


def _collect_controls(run) -> list[dict]:
    """Enumerate a build run's release controls with pass/fail. Controls may be
    STEPS inside a job (RCTLDEF… in build-deploy-publish) or entire JOBS named
    like a control — both are collected; skipped controls
    are neither passed nor failed (they make the gate UNKNOWN, not PASS)."""
    controls = []
    for job in run.jobs():
        job_name = getattr(job, "name", "") or ""
        steps = getattr(job, "steps", None) or []
        matched_step = False
        for step in steps:
            name = getattr(step, "name", "") or ""
            if not _is_control_step(name):
                continue
            matched_step = True
            concl = getattr(step, "conclusion", None)
            controls.append(
                {
                    "control": name,
                    "job": job_name,
                    "status": getattr(step, "status", None),
                    "conclusion": concl,
                    "passed": concl == "success",
                    "failed": concl in _FAIL_CONCLUSIONS,
                }
            )
        # A whole job can BE the control (e.g. a security-scan job at top level).
        if not matched_step and _is_control_step(job_name):
            concl = getattr(job, "conclusion", None)
            controls.append(
                {
                    "control": job_name,
                    "job": job_name,
                    "status": getattr(job, "status", None),
                    "conclusion": concl,
                    "passed": concl == "success",
                    "failed": concl in _FAIL_CONCLUSIONS,
                }
            )
    return controls


def _build_repo_full(repo: str = "") -> str:
    return repo or active_build_repo()


# ============ Which image:tag a run built — one run, one artifact ============
# Queueing takes a chart:version AND the run URL that built it. The controls of
# that run only mean something if the run really built THAT chart:version —
# otherwise a passing run of payments-api could vouch for orders-api:2.0. So the
# run has to say what it built, in one of the two places a build records it:
#   * the git tag that TRIGGERED it (tag-push pipelines: head_branch is the tag);
#   * the "<BUILD_TAG_MARKER><tag>" line its tag-generation step logs
#     (pipelines that create the tag inside the run, triggered from a branch).
# A tag names the version; it names the image too when it carries the image
# name ("orders-api-1.2.3", "orders-api/1.2.3", "orders-api:1.2.3"). A bare
# version ("1.2.3", "v1.2.3") ties the run to the image only through the build
# workflow image-workflows.json assigns to that image.
_TAG_SEPARATORS = "-_/:@"


def split_built_tag(built: str, image: str) -> tuple[bool, str]:
    """(tag names the image, the version part). Pure."""
    tag = str(built or "").strip().strip("\"'")
    if tag.startswith("refs/tags/"):
        tag = tag[len("refs/tags/"):]
    name = str(image or "").strip()
    if name and tag.lower().startswith(name.lower()) and tag[len(name):len(name) + 1] in tuple(_TAG_SEPARATORS):
        return True, tag[len(name) + 1:]
    return False, tag


def version_matches(built_version: str, version: str) -> bool:
    """Exact, bar a leading "v" — 1.0.1 must never match 1.0.10. Pure."""
    b, v = str(built_version or "").strip(), str(version or "").strip()
    return bool(v) and (b == v or b == "v" + v or "v" + b == v)


_TAG_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-_/:+@")


def _strip_ansi(line: str) -> str:
    """Drop terminal colour codes (ESC [ … letter) that GitHub keeps in logs."""
    out, i = [], 0
    while i < len(line):
        if line[i] == "\x1b" and line[i + 1:i + 2] == "[":
            i += 2
            while i < len(line) and not line[i].isalpha():
                i += 1
            i += 1
            continue
        out.append(line[i])
        i += 1
    return "".join(out)


def _tags_from_log(text: str, marker: str) -> list[str]:
    """Every tag logged after ``marker`` (one per built image in a matrix).

    GitHub's log also echoes the step's own script — `echo "New tag is:
    ${GITHUB_REF_NAME}"` — so only a value made of tag characters counts; an
    unexpanded variable or a quoted command fragment is not a tag.
    """
    found = []
    for line in str(text or "").splitlines():
        line = _strip_ansi(line)
        at = line.find(marker)
        if at < 0:
            continue
        value = line[at + len(marker):].strip().split()[0:1]
        if not value:
            continue
        tag = value[0].strip("\"'")
        if tag and set(tag) <= _TAG_CHARS and tag not in found:
            found.append(tag)
    return found


def tag_name_matches(configured: str, name: str) -> bool:
    """Does a job or step name answer to BUILD_TAG_STEP? Pure.

    A job that comes from a REUSABLE workflow is named "<caller job> / <job>" in
    the API ("build-deploy-publish / Create new tag"), while the run page shows
    only the last part — so the segment after the final " / " counts too.
    Compared case-insensitively and stripped: a trailing space in the workflow
    YAML must not silently unverify every build.
    """
    want = (configured or "").strip().lower()
    got = (name or "").strip().lower()
    if not want or not got:
        return False
    return got == want or got.rsplit(" / ", 1)[-1].strip() == want


def run_built_tags(repo_obj, repo_full: str, run) -> dict[str, Any]:
    """What ``run`` says it built: {"tags": [...], "source": "trigger"|"log"|None,
    "detail": why nothing was found}.

    BUILD_TAG_STEP may name either a STEP inside a job or a whole JOB — the same
    two shapes a control takes (_collect_controls). A pipeline that generates the
    tag in a reusable-workflow job ("Create new tag") names that job for the tag
    and its steps for the actions they run ("Tag this new commit"), so matching
    step names alone finds nothing and every build reads as unverifiable.
    """
    head = str(getattr(run, "head_branch", "") or "")
    if head and getattr(run, "event", "") == "push":
        try:
            repo_obj.get_git_ref(f"tags/{head}")          # a tag push, not a branch push
            return {"tags": [head], "source": "trigger", "detail": ""}
        except Exception:
            pass
    step_name = settings.build_tag_step
    marker = settings.build_tag_marker
    tags: list[str] = []
    unfinished: list[str] = []
    found = unreadable = False
    try:
        for job in run.jobs():
            job_name = str(getattr(job, "name", "") or "")
            hits = [st for st in (getattr(job, "steps", None) or [])
                    if tag_name_matches(step_name, str(getattr(st, "name", "") or ""))]
            as_job = not hits and tag_name_matches(step_name, job_name)
            if not hits and not as_job:
                continue
            found = True
            succeeded = (any(getattr(st, "conclusion", None) == "success" for st in hits) if hits
                         else getattr(job, "conclusion", None) == "success")
            if not succeeded:
                unfinished.append(job_name or step_name)
                continue
            log = _fetch_job_log(repo_full, job.id)
            if not log:
                # Distinct from "no marker in the log": a token without
                # actions:read, or a blocked log download, reads the same as a
                # pipeline that never logged a tag unless we say so.
                unreadable = True
                continue
            for tag in _tags_from_log(log, marker):
                if tag not in tags:
                    tags.append(tag)
    except Exception as e:
        return {"tags": [], "source": None,
                "detail": f"the run's jobs could not be read: {e}"[:200]}
    if tags:
        return {"tags": tags, "source": "log", "detail": ""}
    if not found:
        detail = (f"no step or job in that run is named '{step_name}' (BUILD_TAG_STEP) — "
                  "check the name on the run page, including the job it sits in")
    elif unfinished:
        detail = f"'{unfinished[0]}' did not succeed, so it vouches for nothing"
    elif unreadable:
        detail = (f"'{step_name}' succeeded but its log could not be read — the token needs "
                  "actions:read and the log download has to be reachable from here")
    else:
        detail = f"'{step_name}' succeeded but its log has no '{marker}<tag>' line"
    return {"tags": [], "source": None, "detail": detail}


def match_run_to_artifact(repo_full: str, run_id: int, image: str, version: str) -> dict[str, Any]:
    """Did run ``run_id`` build exactly ``image:version``? Never raises.

    {"ok": True, "built": tag} or {"ok": False, "reason": ..., "built": [...]}.
    """
    label = f"{image}:{version}"
    try:
        repo_obj = _get_github_client().get_repo(repo_full)
        run = repo_obj.get_workflow_run(int(run_id))
    except Exception as e:
        return {"ok": False, "built": [], "reason": f"Could not read run {run_id} in {repo_full}: {e}"[:300]}
    built = run_built_tags(repo_obj, repo_full, run)
    tags = built["tags"]
    if not tags:
        # Say WHICH of the ways it fell short (built["detail"]) — "the step
        # logged nothing" sends people to the log of a step that never ran.
        return {"ok": False, "built": [], "reason": (
            f"Can't tell which image and tag that run built: it was not started by a tag push, "
            f"and {built.get('detail') or 'no tag was found in its logs'}. "
            f"Use the run that built {label}.")}

    run_workflow = str(getattr(run, "path", "") or "").split("/")[-1]
    image_workflow = _image_build_workflow(repo_obj, image)
    for tag in tags:
        names_image, tag_version = split_built_tag(tag, image)
        if not version_matches(tag_version, version):
            continue
        if names_image:
            return {"ok": True, "built": tag, "source": built["source"]}
        # A BARE version ("5.0.445") names no image, so image-workflows.json is
        # the only thing that can tie it to one. Three cases:
        if image_workflow and run_workflow == image_workflow:
            return {"ok": True, "built": tag, "source": built["source"], "via_workflow": run_workflow}
        if image_workflow:
            # The catalogue CONTRADICTS the run — positive evidence that this is
            # another service's build. Refused whatever the setting says.
            return {"ok": False, "built": tags, "reason": (
                f"That run built tag {tag}, but it is not {image}'s build: the tag carries no "
                f"image name and {CONFIG_PATH} says {image} is built by {image_workflow}, "
                f"while this run is {run_workflow}. Use the run of {image}'s own build.")}
        # Not in the catalogue at all. A monorepo tags per service and listing
        # every one goes stale, so by default the matching version is enough;
        # QUEUE_REQUIRE_IMAGE_WORKFLOW=true demands the mapping instead.
        if not settings.queue_require_image_workflow:
            return {"ok": True, "built": tag, "source": built["source"], "via_version_only": True}
        return {"ok": False, "built": tags, "reason": (
            f"That run built tag {tag}, but nothing ties it to {image}: the tag carries no image "
            f"name and {image} has no build workflow in {CONFIG_PATH}. "
            f"Use the run of {image}'s own build.")}
    shown = ", ".join(tags[:5]) + (f" (+{len(tags) - 5} more)" if len(tags) > 5 else "")
    return {"ok": False, "built": tags, "reason": (
        f"That run built {shown}, not {label}. Use the run that built {label} — "
        "each chart:version needs the run that built it.")}


def _find_build_run(repo_obj, image: str, tag: str):
    """Return (newest build run for image at tag's commit, None) or (None, reason)."""
    workflow = _image_build_workflow(repo_obj, image)
    if not workflow:
        return None, f"image '{image}' has no build workflow in {CONFIG_PATH}"
    commit = _resolve_tag_commit(repo_obj, tag)
    if not commit:
        return None, f"tag '{tag}' not found"
    try:
        wf = repo_obj.get_workflow(workflow)
        # Filter by head_sha server-side (GitHub) instead of scanning 100 runs client-side.
        runs = list(itertools.islice(wf.get_runs(head_sha=commit), 20))
    except Exception as e:
        return None, str(e)
    if not runs:
        return None, f"no '{workflow}' run found at commit {commit[:7]}"
    # Multiple tags can point at the same commit, so head_sha alone is ambiguous.
    # Tag-triggered runs carry the tag name in head_branch — prefer an exact match.
    exact = [r for r in runs if (getattr(r, "head_branch", "") or "") == tag]
    return sorted(exact or runs, key=lambda r: r.created_at, reverse=True)[0], None


# ============ Build report: step failures + built-from-main (live, PyGithub) ============


def _parse_run_url(url: str):
    """Extract (owner/repo, run_id) from a GitHub Actions run URL, e.g.
    https://github.com/<owner>/<repo>/actions/runs/<run_id>[/job/<id>|/attempts/<n>].
    Returns (repo_full_or_None, run_id_or_None). No regex — plain path split."""
    try:
        parts = [p for p in urlparse(url).path.split("/") if p]
        if "runs" in parts:
            i = parts.index("runs")
            run_id = int(parts[i + 1])
            repo_full = f"{parts[0]}/{parts[1]}" if i >= 2 else ""
            return (repo_full or None, run_id)
    except Exception:
        pass
    return (None, None)


def _check_built_from_main(repo_obj, commit: str) -> dict:
    """Is `commit` reachable from the repo's default branch (i.e. built from main)?
    Uses the compare API: comparing default_branch -> commit, status 'identical'/'behind'
    means the commit is an ancestor of the default branch (built from main); 'ahead'/
    'diverged' means it carries commits not on the default branch (not built from main)."""
    default = repo_obj.default_branch
    try:
        status = repo_obj.compare(default, commit).status  # identical|behind|ahead|diverged
        return {"result": status in ("identical", "behind"), "default_branch": default, "status": status}
    except Exception as e:
        return {"result": None, "default_branch": default, "reason": str(e)}


def _failed_steps(run) -> list[dict]:
    """Failed NON-control steps across the run's jobs. Control steps (RLFT/RFTL...) are
    excluded here because they're reported separately in the `controls` array — this keeps
    a failed control from showing up twice in the rendered table."""
    out: list[dict] = []
    try:
        for job in run.jobs():
            for step in getattr(job, "steps", None) or []:
                name = getattr(step, "name", "") or ""
                concl = getattr(step, "conclusion", None)
                if concl in _FAIL_CONCLUSIONS and not _is_control_step(name):
                    out.append(
                        {
                            "job": job.name,
                            "name": name,
                            "number": getattr(step, "number", None),
                            "conclusion": concl,
                        }
                    )
    except Exception:
        pass
    return out


class BuildReportInput(BaseModel):
    image: str = Field(
        default="", description="Image name (resolves the tag's build run). Provide image+tag, OR workflow_url."
    )
    tag: str = Field(
        default="", description="Git tag that was built, e.g. v1.2.3. Provide image+tag, OR workflow_url."
    )
    workflow_url: str = Field(
        default="",
        description="A GitHub Actions run URL (…/actions/runs/<id>) to inspect directly, instead of image+tag.",
    )
    repo: str = Field(
        default="",
        description="owner/repo where the build ran. Defaults to the build repo; auto-derived from workflow_url.",
    )
    run_id: int = Field(
        default=0,
        description="GitHub Actions run id that generated the tag. Pass it (with repo, if not the "
        "default build repo) to inspect a run directly by id, instead of workflow_url or image+tag.",
    )


@tool(args_schema=BuildReportInput)
def get_build_report(
    image: str = "", tag: str = "", workflow_url: str = "", repo: str = "", run_id: int = 0
) -> str:
    """Report a build's outcome for an image:tag, a bare run_id, or a GitHub Actions run URL: which
    STEPS failed, which RLFT/RFTL controls passed/failed, and whether the tag was built from the
    build repo's main/default branch. Read-only, resolved live from GitHub (tag -> commit -> run ->
    steps, or the run id directly).

    PRESENT THE RESULT TO THE USER AS A MARKDOWN TABLE — a summary line with the clickable run URL
    + conclusion + built-from-main verdict, then a table of Step/Control | Job | Result rows with
    ✅/❌ markers. Do NOT show the raw JSON to the user."""
    repo_full = _build_repo_full(repo)

    # 1) Resolve the run — directly from a URL, from a bare run id, or by image+tag -> commit -> run.
    if workflow_url.strip():
        url_repo, url_run_id = _parse_run_url(workflow_url)
        repo_full = repo or url_repo or repo_full
        if not url_run_id:
            return json.dumps(
                {"found": False, "reason": f"could not parse a run id from '{workflow_url}'."}, indent=2
            )
        try:
            repo_obj = _get_github_client().get_repo(repo_full)
            run = repo_obj.get_workflow_run(int(url_run_id))
        except Exception as e:
            return json.dumps(
                {"found": False, "repo": repo_full, "reason": f"run {url_run_id} not found in {repo_full}: {e}"},
                indent=2,
            )
    elif run_id:
        try:
            repo_obj = _get_github_client().get_repo(repo_full)
            run = repo_obj.get_workflow_run(int(run_id))
        except Exception as e:
            return json.dumps(
                {"found": False, "repo": repo_full, "reason": f"run {run_id} not found in {repo_full}: {e}"},
                indent=2,
            )
    else:
        if not (image and tag):
            return json.dumps(
                {"found": False, "reason": "provide a workflow_url, a run_id, or both image and tag."},
                indent=2,
            )
        try:
            repo_obj = _get_github_client().get_repo(repo_full)
        except Exception as e:
            return f"ERROR building report: {e}"
        run, err = _find_build_run(repo_obj, image, tag)
        if run is None:
            return json.dumps(
                {
                    "found": False,
                    "image": image,
                    "tag": tag,
                    "repo": repo_full,
                    "reason": err,
                    "hint": "If you have the build's workflow run URL or id, pass it as workflow_url or run_id.",
                },
                indent=2,
            )

    # 2) Assemble the report from permanent run/step metadata (no log downloads).
    commit = getattr(run, "head_sha", None)
    controls = _collect_controls(run)
    failed_controls = [c["control"] for c in controls if c["failed"]]
    other = [c["control"] for c in controls if not c["passed"] and not c["failed"]]
    gate = "PASS" if (controls and not failed_controls and not other) else ("FAIL" if failed_controls else "UNKNOWN")
    # Named, with the job they live in — a developer looking at a 40-job run needs
    # to know WHICH control and WHERE, not just that the gate is unhappy.
    failed_detail = [
        {"control": c["control"], "job": c["job"], "conclusion": c["conclusion"]}
        for c in controls if c["failed"]
    ]
    open_detail = [
        {"control": c["control"], "job": c["job"],
         "status": c["status"], "conclusion": c["conclusion"]}
        for c in controls if not c["passed"] and not c["failed"]
    ]
    failed_steps = _failed_steps(run)
    run_succeeded = run.conclusion == "success"

    if failed_steps or failed_controls:
        note = (
            "Render for the user as a markdown table — a summary line (run URL, conclusion, "
            "built-from-main) then Step/Control | Job | Result rows with ✅/❌. Do not show JSON."
        )
    elif run_succeeded:
        note = (
            "The build SUCCEEDED — no failed steps. Give a one-line success summary with the run "
            "link and the built-from-main verdict; a table isn't needed. Do not show JSON."
        )
    else:
        note = (
            f"The run's overall conclusion is '{run.conclusion}' but NO individual step or control "
            "failure was recorded (e.g. a workflow-level / startup / config failure, or a job that "
            "never ran). Tell the user the run failed at the workflow level with no per-step detail, "
            "and link the run so they can inspect it. Do not show JSON."
        )

    report = {
        "found": True,
        "image": image or None,
        "tag": tag or getattr(run, "head_branch", None),
        "repo": repo_full,
        "commit": commit,
        "run": {
            "id": run.id,
            "url": run.html_url,
            "name": run.name,
            "status": run.status,
            "conclusion": run.conclusion,
        },
        "run_succeeded": run_succeeded,
        "failed_steps": failed_steps,
        "controls": controls,
        "failed_controls": failed_detail,
        "open_controls": open_detail,   # matched, but neither passed nor failed
        "gate": gate,
        "built_from_main": (
            _check_built_from_main(repo_obj, commit) if commit else {"result": None, "reason": "no commit on run"}
        ),
        "note": note,
    }
    return json.dumps(report, indent=2)

