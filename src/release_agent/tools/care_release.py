"""CARE release in mono mode: ONE committed file, and a PR a person merges.

With CARE_RELEASE_MODE=mono a CARE release is not a generated file-set: it is
CARE_RELEASE_FILE in CARE_RELEASE_REPO. The portal edits that one file on a
release branch and raises a PR against CARE_RELEASE_BASE_BRANCH. A person
reviews and merges it — the portal never does — and the repository's own
workflow takes it from there to the deployment repo.

  prepare : read the file at the base branch's head, splice this release into
            it (json_splice: only the values that change move, every other
            byte stays) and preview the exact diff. Reads only.
            With CARE_PREVIOUS_TAGS_FILE set, the release file's CURRENT
            artefacts — the last merged release — are first carried into that
            {chart: version} file (a chart new to it is added), and the preview
            shows both diffs, that one first. A chart of this release whose
            queued build run logged the tag it started from (BUILD_BASE_TAG_STEP)
            takes that tag instead; the preview says which source each used.
  apply   : refuse unless the base still holds the previewed file(s), the
            splices reproduce the approved text, and no other release PR has
            opened since the preview (the guard runs again: only a PR already
            carrying exactly this text — this approval, applied before — is
            not another release); commit on a new release branch — the
            previous tags first, then the release file, two commits in that
            order — raise the PR, and release its charts in the queue at once
            (they move to Release history; no merge is tracked).

Same prep/result contract as release_fileset, so the CONFIRM-token flow in
adk_release_agent/deploy.py is unchanged. DF releases, and CARE in the
default "fileset" mode, never come here.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import logging
from typing import Any

from . import attribution, json_splice, progress
from ._common import settings, _get_github_client
from .release_fileset import (
    _artifact_name_version, _slugify, partition_environments, say_moving_charts,
)
from .release_window import _open_prd_pr_blocker

logger = logging.getLogger("release_copilot")

# The committed file's keys. validate_release's details also carry df_images,
# which routes the portal's own preview and never enters this file.
FILE_KEYS = (
    "release_name", "start_date", "end_date", "change_initiator", "change_summary",
    "change_description", "change_reason", "associated_risk", "consequence",
    "user_service_impact", "prl1_only", "artefact",
)
_MAX_BRANCH_SUFFIX = 20


class _Stop(Exception):
    """A refusal with the sentence the person reads."""


def file_doc(details: dict[str, Any]) -> dict[str, Any]:
    return {key: details[key] for key in FILE_KEYS}


# The Dataflow block's keys, each <CARE_RELEASE_DF_KEY_PREFIX>_<suffix>, in
# the order they are appended to a file that lacks them.
DF_SUFFIXES = ("release", "components", "prl1_only", "artefact")


def df_keys(prefix: str) -> dict[str, str]:
    """{suffix: key} for a prefix; {} when the block is off. Only a plain
    identifier: the names become keys of a committed file."""
    prefix = (prefix or "").strip()
    if not prefix:
        return {}
    if not all(ch.isalnum() or ch == "_" for ch in prefix):
        raise ValueError(f"CARE_RELEASE_DF_KEY_PREFIX must be letters, digits and _ (got {prefix!r})")
    return {s: f"{prefix}_{s}" for s in DF_SUFFIXES}


def df_block(payload: dict[str, Any], care_names: list[str], prefix: str) -> tuple[dict[str, Any], list[str]]:
    """PURE. ``(block, errors)``: the release's Dataflow artefacts as the release
    file's four keys — "Yes" with the components, the PRL1-only ones and the
    URLs, or "No" and empty lists when the release has none. ``{}`` with no
    errors when the block is off and nothing was asked for."""
    from .release_fileset import normalize_artifact

    keys = df_keys(prefix)
    urls = [normalize_artifact(a) for a in (payload.get("dataflow_artefact") or []) if str(a).strip()]
    prl1 = [s.strip() for s in (payload.get("dataflow_prl1_only") or []) if str(s).strip()]
    if not keys:
        if urls or prl1:
            return {}, ["This release carries Dataflow artefacts, but CARE_RELEASE_DF_KEY_PREFIX is not "
                        "set — the release file has no keys for them."]
        return {}, []
    errors: list[str] = []
    names: list[str] = []
    for url in urls:
        nv = _artifact_name_version(url)
        if nv is None:
            errors.append(f"Dataflow artefact has no ':version': {url}")
        elif nv[0] in names:
            errors.append(f"Duplicate Dataflow component: {nv[0]}")
        elif nv[0] in care_names:
            errors.append(f"{nv[0]} is both a CARE artefact and a Dataflow artefact — pick one.")
        else:
            names.append(nv[0])
    for svc in prl1:
        if svc not in names:
            errors.append(f"PRL1-only Dataflow component {svc!r} is not in the Dataflow artefacts "
                          "(a typo here would silently misroute it).")
    if errors:
        return {}, errors
    return {keys["release"]: "Yes" if urls else "No", keys["components"]: names,
            keys["prl1_only"]: [n for n in names if n in prl1], keys["artefact"]: urls}, []


def release_text(old_text: str, details: dict[str, Any], dataflow: dict[str, Any]) -> str:
    """The release file with this release spliced in: the CARE keys the file
    already has (never added — a missing one is a wrong file), then the
    Dataflow block, which is added after the last key when the file lacks it."""
    text = json_splice.splice_top_level(old_text, file_doc(details))
    if dataflow:
        text = json_splice.splice_top_level(text, dataflow, add_missing=True)
    return text


def dataflow_in(text: str, prefix: str) -> dict[str, Any]:
    """PURE. The Dataflow block a release file holds now: ``{release,
    artefact, prl1_only}`` from its keys, empty values for keys it lacks. The
    CARE form leaves an image this file already carries unticked, so a DF
    release the last CARE release named is not announced again."""
    keys = df_keys(prefix)
    if not keys:
        return {}
    doc = json.loads(text)
    if not isinstance(doc, dict):
        raise ValueError("the release file is not a JSON object")

    def strings(key: str) -> list[str]:
        value = doc.get(keys[key])
        return [str(v).strip() for v in value if str(v).strip()] if isinstance(value, list) else []

    return {"release": str(doc.get(keys["release"]) or ""), "artefact": strings("artefact"),
            "prl1_only": strings("prl1_only")}


def current_dataflow() -> dict[str, Any]:
    """Read-only: the Dataflow block in the release file at the base branch's
    head — what the last merged CARE release carried. Never raises."""
    prefix = settings.care_release_df_key_prefix
    if settings.care_release_mode != "mono" or not prefix.strip():
        return {"ok": False, "disabled": True}
    repo = settings.care_release_repo.strip()
    base = settings.care_release_base_branch.strip()
    path = settings.care_release_file.strip()
    if not (repo and base and path):
        return {"ok": False, "error": "CARE_RELEASE_REPO, CARE_RELEASE_BASE_BRANCH and CARE_RELEASE_FILE must be set."}
    try:
        _head, text, _sha = _read_base(_get_github_client().get_repo(repo), base, path)
        block = dataflow_in(text, prefix)
    except Exception as e:
        logger.warning("care release: could not read the Dataflow block of %s on %s", path, base, exc_info=True)
        return {"ok": False, "error": f"Could not read {path} on {base} in {repo}: {e}"}
    return {"ok": True, "repo": repo, "base": base, "file": path, **block}


def carried_tags(text: str) -> tuple[dict[str, str], list[str]]:
    """PURE. ``({chart: version}, entries skipped)`` of a release file's
    artefacts — what the previous release tags file takes before the release
    file is overwritten. An entry with no ``name:version`` is skipped, and said."""
    tags: dict[str, str] = {}
    skipped: list[str] = []
    for entry in json.loads(text).get("artefact") or []:
        nv = _artifact_name_version(str(entry))
        if nv:
            tags[nv[0]] = nv[1]
        else:
            skipped.append(str(entry))
    return tags, skipped


def previous_tags(old_text: str, from_builds: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """PURE. ``({chart: version}, entries skipped)`` the previous release tags
    file takes: the release file's current artefacts, then — the build being
    the authority on what a chart was built on — the tag each chart's build run
    logged as its starting point."""
    tags, skipped = carried_tags(old_text)
    tags.update(from_builds)
    return tags, skipped


def _base_tags_from_builds(details: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """``({chart: tag}, {chart: why not})`` — the tag each chart's queued build
    run logged as the one it started from. The queue gate reads it when the
    chart is queued and records it on the entry; an entry queued before that
    has its run read now. The run is the one the chart was queued with (the
    gate verified it built exactly this version), so a chart typed straight
    into the form has none. Reads only; never raises."""
    charts = [nv for a in details["artefact"] if (nv := _artifact_name_version(a))]
    if not charts or not settings.build_base_tag_step.strip():
        return {}, {}
    queued: dict[tuple[str, str], dict[str, Any]] = {}
    try:
        from .release_queue import current_queue
        for item in current_queue().get("queue") or []:
            queued[(str(item.get("artifact_name") or ""), str(item.get("artifact_version") or ""))] = item
    except Exception as e:      # the queue is optional; the release is not
        logger.warning("care release: could not read the queue for build runs: %s", e)
    from .controls import run_base_tag

    tags: dict[str, str] = {}
    missing: dict[str, str] = {}
    for name, version in charts:
        item = queued.get((name, version)) or {}
        recorded = str(item.get("previous_tag") or "").strip()
        if recorded:            # read from its build run when it was queued
            tags[name] = recorded
            continue
        url = str(item.get("build_run_url") or "")
        if not url:
            missing[name] = "no build run on record (it was not queued at this version)"
            continue
        progress.say(f"Reading which tag {name}:{version} was built on")
        got = run_base_tag(url)
        if got.get("ok"):
            tags[name] = got["tag"]
        else:
            missing[name] = got.get("reason") or "its build logged no base tag"
    return tags, missing


def _previous_sources(charts: list[str], from_builds: dict[str, str], missing: dict[str, str],
                      carried: dict[str, str]) -> list[str]:
    """One line per chart of this release: which previous tag it takes, and from where."""
    lines = []
    for name in charts:
        if name in from_builds:
            last = carried.get(name)
            differs = f" — the last release had {last}" if last and last != from_builds[name] else ""
            lines.append(f"{name}: {from_builds[name]}, the tag its build started from{differs}")
        elif name in carried:
            lines.append(f"{name}: {carried[name]} from the last release — {missing.get(name, 'not read from the build')}")
        elif name in missing:
            lines.append(f"{name}: not written — {missing[name]}, and the last release did not carry it")
    return lines


def _raised_by(details: dict[str, Any]) -> tuple[str, bool]:
    """(email, verified) of whoever raises the PR: the verified caller, or with
    identity off the email typed on the form — a claim, so the PR says so."""
    email = attribution.requester_email()
    if email:
        return email, True
    return str(details.get("change_initiator") or "").strip(), False


def _read_base(gh_repo: Any, base: str, path: str) -> tuple[str, str, str]:
    """(head sha, text, blob sha) of ``path`` at ``base``'s head. Raises on
    anything short of a readable file — never an empty stand-in, which a
    splice would turn into a file holding only this release's keys."""
    head = gh_repo.get_branch(base).commit.sha
    blob = gh_repo.get_contents(path, ref=head)
    if isinstance(blob, list):
        raise ValueError(f"{path} is a directory")
    return head, blob.decoded_content.decode("utf-8"), blob.sha


def _read_at(gh_repo: Any, path: str, ref: str) -> tuple[str, str]:
    """(text, blob sha) of ``path`` at commit ``ref`` — the same commit the
    release file was read at, so both files come from one moment of the base."""
    blob = gh_repo.get_contents(path, ref=ref)
    if isinstance(blob, list):
        raise ValueError(f"{path} is a directory")
    return blob.decoded_content.decode("utf-8"), blob.sha


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _lines(text: str) -> list[str]:
    # "\n" only: str.splitlines also breaks on characters a JSON string holds
    # raw (U+2028, form feed), which would show lines the file does not have.
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()           # the final newline ends a line; it is not one
    return lines


def _diff(old: str, new: str, path: str) -> str:
    lines = difflib.unified_diff(_lines(old), _lines(new),
                                 fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="")
    return "\n".join(lines) + "\n"


def _refuse(message: str) -> dict[str, Any]:
    return {"ok": False, "errors": [message]}


def _in_flight(pr: Any) -> str:
    return (f"A CARE release is already awaiting review: PR #{pr.number} ({pr.html_url}). "
            "One release at a time — merge or close it first.")


def _carries(gh_repo: Any, path: str, ref: str, text: str) -> bool:
    """Whether ``ref`` holds exactly ``text`` at ``path``. A file that cannot be
    read counts as not: a PR is only ever taken for this release when it is
    shown to carry the approved bytes."""
    try:
        return gh_repo.get_contents(path, ref=ref).decoded_content.decode("utf-8") == text
    except Exception:
        return False


def _carries_all(gh_repo: Any, files: list[tuple[str, str]], ref: str) -> bool:
    """Every (path, text) of this release at ``ref`` — one file alone is not it."""
    return all(_carries(gh_repo, path, ref, text) for path, text in files)


def prepare(payload: dict[str, Any], details: dict[str, Any]) -> dict[str, Any]:
    """Preview the release PR: the file diff it would raise. Writes nothing."""
    repo = settings.care_release_repo.strip()
    if not repo:
        return _refuse("CARE releases are raised as a PR (CARE_RELEASE_MODE=mono), but "
                       "CARE_RELEASE_REPO is not set — name the owner/repo whose release file it edits.")
    base = settings.care_release_base_branch.strip()
    path = settings.care_release_file.strip()
    prefix = settings.care_release_branch_prefix.strip()
    for name, value in (("CARE_RELEASE_BASE_BRANCH", base), ("CARE_RELEASE_FILE", path),
                        ("CARE_RELEASE_BRANCH_PREFIX", prefix)):
        if not value:
            return _refuse(f"CARE_RELEASE_MODE is mono but {name} is empty.")
    asked = str(payload.get("deployment_repo") or "").strip()
    if asked and asked.lower() != repo.lower():
        return _refuse(f"A CARE release is raised in {repo} (CARE_RELEASE_REPO), not {asked}.")
    if details.get("df_images"):
        return _refuse("A CARE release cannot carry Dataflow images — raise them in the DF release.")
    care_names = [nv[0] for a in details["artefact"] if (nv := _artifact_name_version(a))]
    try:
        dataflow, df_errors = df_block(payload, care_names, settings.care_release_df_key_prefix)
    except ValueError as e:
        return _refuse(str(e))
    if df_errors:
        return {"ok": False, "errors": df_errors}

    progress.say(f"Checking for an open release PR on {base}")
    try:
        gh_repo = _get_github_client().get_repo(repo)
        blocker = _open_prd_pr_blocker(gh_repo, branches=[base], head_prefix=prefix)
    except Exception as e:
        return _refuse(f"Could not reach {repo}: {e}")
    if blocker is not None:
        return _refuse(_in_flight(blocker))
    progress.say(f"Reading {path} on {base}")
    try:
        head, old_text, blob_sha = _read_base(gh_repo, base, path)
    except Exception as e:
        return _refuse(f"Could not read {path} on {base} in {repo}: {e}")
    progress.say("Working out the smallest change to the release file")
    try:
        new_text = release_text(old_text, details, dataflow)
    except ValueError as e:
        return _refuse(f"{path} on {base} cannot take this release: {e}")
    if new_text == old_text:
        return _refuse(f"{base} already carries exactly this release — {path} would not change.")

    # Before the release file is overwritten, what it holds now — the last
    # merged release — goes into the previous release tags file.
    prev_path = settings.care_previous_tags_file.strip()
    prev_old = prev_new = prev_blob = ""
    skipped: list[str] = []
    from_builds: dict[str, str] = {}
    sources: list[str] = []
    if prev_path:
        progress.say(f"Reading {prev_path}")
        try:
            prev_old, prev_blob = _read_at(gh_repo, prev_path, head)
        except Exception as e:
            return _refuse(f"Could not read {prev_path} on {base} in {repo}: {e} "
                           "(CARE_PREVIOUS_TAGS_FILE — set it empty to skip this step).")
        from_builds, missing = _base_tags_from_builds(details)
        tags, skipped = previous_tags(old_text, from_builds)
        if settings.build_base_tag_step.strip():
            sources = _previous_sources(care_names, from_builds, missing, carried_tags(old_text)[0])
        try:
            prev_new = json_splice.splice_top_level(prev_old, tags, add_missing=True)
        except ValueError as e:
            return _refuse(f"{prev_path} on {base} cannot take the previous release's tags: {e}")
    prev_changes = prev_new != prev_old

    email, verified = _raised_by(details)
    branch = prefix + _slugify(details["release_name"])
    diff = (_diff(prev_old, prev_new, prev_path) if prev_changes else "") + _diff(old_text, new_text, path)
    previous_note = ""
    if prev_path:
        previous_note = (("carries each chart's previous version (first commit)" if from_builds
                          else "carries the last release's versions (first commit)") if prev_changes
                         else "unchanged — it already holds "
                         + ("these versions" if from_builds else "the last release's versions"))
        if skipped:
            previous_note += f"; skipped (no name:version): {', '.join(skipped)}"
    return {
        "ok": True,
        "mode": "mono",
        "kind": "care",
        "release_name": details["release_name"],
        "branch": branch,
        "branch_prefix": prefix,        # apply runs the same guard again
        "details": details,             # apply splices again from these
        "dataflow": dataflow,           # ...and this block ({} when off)
        "deployment_repo": repo,
        "landing_branch": base,
        "file": path,
        "artifacts": [{"name": nv[0], "tag": nv[1]}
                      for a in details["artefact"] if (nv := _artifact_name_version(a))],
        "base_commit": head,
        "base_blob_sha": blob_sha,      # apply refuses if the file on the base moved
        "content_hash": _content_hash(new_text),
        "previous_file": prev_path,
        "previous_blob_sha": prev_blob,
        "previous_changes": prev_changes,
        "previous_content_hash": _content_hash(prev_new) if prev_changes else "",
        "previous_from_builds": from_builds,    # apply writes exactly these, never reads the logs again
        "pr_title": details["release_name"],
        "preview": {
            "release": details["release_name"],
            "window": f"{details['start_date']} -> {details['end_date']}",
            "initiator": details["change_initiator"],
            "raised_by": email if verified else f"{email} (unverified, typed on the form)",
            "repo": repo,
            "base": base,
            "file": path,
            "branch": branch,
            "environments": partition_environments(details),
            **({"dataflow": _df_preview(dataflow)} if dataflow else {}),
            **({"previous_tags_file": prev_path, "previous_tags": previous_note} if prev_path else {}),
            **({"previous_tags_by_chart": sources} if sources else {}),
            # What CONFIRM approves: the exact bytes the PR will carry — the
            # previous tags' diff first (first commit), then the release file's.
            "file_diff": diff,
        },
    }


def _df_preview(block: dict[str, Any]) -> dict[str, Any]:
    """The Dataflow block as the preview shows it, under plain names."""
    keys = df_keys(settings.care_release_df_key_prefix)
    return {"keys": list(block), "release": block.get(keys["release"]),
            "components": block.get(keys["components"]) or [], "prl1_only": block.get(keys["prl1_only"]) or []}


def _already_exists(error: Exception) -> bool:
    return "reference already exists" in (f"{error} {getattr(error, 'data', '') or ''}").lower()


def _open_pr(gh_repo: Any, owner: str, branch: str, base: str) -> Any | None:
    for pr in gh_repo.get_pulls(state="open", base=base, head=f"{owner}:{branch}"):
        return pr
    return None


def _delete_branch(gh_repo: Any, branch: str) -> None:
    """Remove a branch this apply cut but could not commit to. Left behind, it
    is a leftover with no PR, and every retry would move on to a -N name."""
    try:
        gh_repo.get_git_ref(f"heads/{branch}").delete()
    except Exception:
        pass          # tidiness only; the refusal already says what happened


def _claim_branch(gh_repo: Any, repo: str, wanted: str, base: str, head: str,
                  files: list[tuple[str, str]]) -> tuple[str, Any, bool]:
    """(branch, its open PR or None, created now). A branch with an open PR
    into the base is reused only when it already carries exactly ``files`` —
    this approval, applied before. With anything else it is another release
    of the same name (the name is a date, and two people can raise on the
    same day), and writing over it would change what that PR's reviewers
    approve: nothing is pushed. A branch with no PR is a leftover nobody is
    reviewing: never written to, the next free name is taken instead.
    Creating the ref IS the existence check, so two applies racing for a name
    cannot both get it."""
    owner = repo.split("/")[0]
    for n in range(1, _MAX_BRANCH_SUFFIX + 1):
        name = wanted if n == 1 else f"{wanted}-{n}"
        try:
            gh_repo.create_git_ref(f"refs/heads/{name}", head)
            return name, None, True
        except Exception as e:
            if not _already_exists(e):
                raise _Stop(f"Could not create {name} in {repo}: {e}. Nothing was pushed.") from None
        pr = _open_pr(gh_repo, owner, name, base)
        if pr is None:
            continue
        if not _carries_all(gh_repo, files, name):
            raise _Stop(f"PR #{pr.number} ({pr.html_url}) for this release name is open with different "
                        "content — nothing was pushed. One release at a time: merge or close it first.")
        return name, pr, False
    raise _Stop(f"{wanted} and {wanted}-2 … -{_MAX_BRANCH_SUFFIX} all exist in {repo} with no open PR "
                "— delete those leftover branches, then start the release again. Nothing was pushed.")


def _pr_body(details: dict[str, Any], path: str, previous: str = "", by_chart: list[str] | None = None,
             from_builds: bool = False) -> str:
    email, verified = _raised_by(details)
    who = email if verified else f"{email} (unverified — typed on the form)"
    artefacts = "\n".join(f"- {a}" for a in details.get("artefact") or [])
    takes = "each chart's previous version" if from_builds else "the last release's versions"
    first = (f"`{previous}` takes {takes} (first commit), then " if previous else "")
    sources = ("Previous versions:\n" + "\n".join(f"- {line}" for line in by_chart) + "\n\n"
               if previous and by_chart else "")
    return (
        f"Release **{details['release_name']}** — {first}updates `{path}`.\n\n"
        f"{sources}"
        f"Window: {details['start_date']} → {details['end_date']}\n"
        f"Raised by: {who}\n\n"
        f"{artefacts}\n\n"
        "Review and merge this PR; on merge the repository's workflow updates the "
        "deployment repo. The portal never merges it.\n"
    )


def apply(prep: dict[str, Any]) -> dict[str, Any]:
    """Raise the release PR. Never merges it, and never raises: a failure is a
    result, because the deploy-graph node running this must not fail."""
    try:
        return _apply(prep)
    except _Stop as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001 — see docstring
        return {"ok": False, "error": (
            f"Raising the release PR failed part-way: {type(e).__name__}: {e}"[:400]
            + f". Nothing was merged; check {prep.get('deployment_repo')} for a leftover "
            f"{prep.get('branch')} branch before starting again.")}


def _apply(prep: dict[str, Any]) -> dict[str, Any]:
    details = prep.get("details") or {}
    repo, base, path = prep.get("deployment_repo"), prep.get("landing_branch"), prep.get("file")
    wanted, title = prep.get("branch"), prep.get("pr_title") or prep.get("release_name")
    prefix = prep.get("branch_prefix")
    if not all((details, repo, base, path, wanted, title, prefix,
                prep.get("base_blob_sha"), prep.get("content_hash"))):
        raise _Stop("Prepared release is incomplete — start the release again.")

    gh_repo = _get_github_client().get_repo(repo)
    progress.say(f"Checking {base} has not moved since the preview")
    try:
        head, old_text, blob_sha = _read_base(gh_repo, base, path)
    except Exception as e:
        raise _Stop(f"Could not read {path} on {base} in {repo}: {e}. Nothing was pushed.") from None
    if blob_sha != prep["base_blob_sha"]:
        raise _Stop(f"The release file on {base} changed since the preview — start the release "
                    "again. Nothing was pushed.")
    try:
        text = release_text(old_text, details, prep.get("dataflow") or {})
    except ValueError as e:
        raise _Stop(f"{path} on {base} cannot take this release: {e}. Nothing was pushed.") from None
    if _content_hash(text) != prep["content_hash"]:
        raise _Stop("The release no longer matches what you approved — start the release again. "
                    "Nothing was pushed.")

    # The previous release tags, checked the same way: the file on the base as
    # previewed, and the carried tags reproducing the approved text.
    prev_path = prep.get("previous_file") or ""
    commits: list[tuple[str, str, str, str]] = []      # (path, text, blob sha, subject) in order
    if prev_path:
        try:
            prev_old, prev_blob = _read_at(gh_repo, prev_path, head)
        except Exception as e:
            raise _Stop(f"Could not read {prev_path} on {base} in {repo}: {e}. Nothing was pushed.") from None
        if prev_blob != prep.get("previous_blob_sha"):
            raise _Stop(f"{prev_path} on {base} changed since the preview — start the release again. "
                        "Nothing was pushed.")
        try:
            prev_new = json_splice.splice_top_level(
                prev_old, previous_tags(old_text, prep.get("previous_from_builds") or {})[0], add_missing=True)
        except ValueError as e:
            raise _Stop(f"{prev_path} on {base} cannot take the previous release's tags: {e}. "
                        "Nothing was pushed.") from None
        changes = prev_new != prev_old
        if changes != bool(prep.get("previous_changes")) or (
                changes and _content_hash(prev_new) != prep.get("previous_content_hash")):
            raise _Stop("The previous release tags no longer match what you approved — start the "
                        "release again. Nothing was pushed.")
        if changes:
            commits.append((prev_path, prev_new, prev_blob, f"Previous release tags before {title}"))
    commits.append((path, text, blob_sha, title))
    files = [(c[0], c[1]) for c in commits]

    # The preview's guard is minutes old by now: another release PR may have
    # opened since, and two releases in flight is what the guard exists to stop.
    progress.say(f"Checking for an open release PR on {base}")
    blocker = _open_prd_pr_blocker(gh_repo, branches=[base], head_prefix=prefix,
                                   skip=lambda open_pr: _carries_all(gh_repo, files, open_pr.head.ref))
    if blocker is not None:
        raise _Stop(_in_flight(blocker) + " Nothing was pushed.")

    progress.say(f"Claiming branch {wanted}")
    branch, pr, created = _claim_branch(gh_repo, repo, wanted, base, head, files)
    if branch != wanted:
        progress.say(f"{wanted} was taken — using {branch}")
    elif not created:
        progress.say(f"{branch} already carries this release")
    committed = False
    if created:
        # Just cut from the head read above, so each file is exactly the blob
        # read there. A reused branch already carries every file (_claim_branch),
        # so only a new one is ever written to — the previous tags first, then
        # the release file: two commits, in that order.
        for fpath, ftext, fblob, subject in commits:
            progress.say("Committing the release file" if fpath == path
                         else "Committing the previous release tags")
            try:
                gh_repo.update_file(fpath, attribution.commit_message(subject), ftext, fblob,
                                    branch=branch, **attribution.author_kwargs())
            except Exception as e:
                # urllib3 retries a PUT after a 5xx that may already have landed,
                # and the retry then fails on the stale sha: read back first.
                if not _carries(gh_repo, fpath, branch, ftext):
                    _delete_branch(gh_repo, branch)
                    raise _Stop(f"Could not commit {fpath} to {branch} in {repo}: {e}. Nothing was "
                                f"merged; {base} is unchanged.") from None
        committed = True

    action = "release_pr_exists"
    if pr is None:
        progress.say(f"Raising the PR into {base} — a person merges it")
        try:
            pr = gh_repo.create_pull(title=attribution.titled(title),
                                     body=attribution.with_trailer(
                                         _pr_body(details, path, prev_path if len(commits) > 1 else "",
                                                  (prep.get("preview") or {}).get("previous_tags_by_chart"),
                                                  bool(prep.get("previous_from_builds")))),
                                     head=branch, base=base)
            action = "release_pr_opened"
        except Exception as e:
            try:        # lost a race to open it: that PR is this release
                pr = _open_pr(gh_repo, repo.split("/")[0], branch, base)
            except Exception:
                pr = None
            if pr is None:
                raise _Stop(f"{path} is committed on {branch} in {repo}, but the PR could not be "
                            f"opened: {e}. Open a PR from {branch} into {base} by hand — nothing "
                            "was merged.") from None

    # Raising the PR IS the release as far as the queue is concerned: its charts
    # leave the queue now and appear in Release history under this PR. If the
    # release does not go through (the PR is closed, the workflow fails), they
    # are put back from Release history — no merge is tracked. Only once per
    # release: a re-apply that reuses the open PR unchanged has recorded it.
    untracked = False
    if action == "release_pr_opened" or committed:
        try:
            from .release_queue import mark_released

            say_moving_charts(prep.get("artifacts") or [])
            recorded = mark_released(prep.get("release_name") or title, pr.number,
                                     prep.get("artifacts") or [], deployment_repo=repo)
        except Exception as e:  # noqa: BLE001 — never fails a raised PR
            recorded = {"ok": False, "error": str(e)}
        untracked = not recorded.get("ok") and not recorded.get("disabled")
        if untracked:
            progress.say("The release queue could not be updated — its charts still show there")
            logger.warning("care release: PR %s#%s raised but the queue was not drained: %s",
                           repo, pr.number, recorded.get("error"))

    link = f"[PR #{pr.number}]({pr.html_url})"
    if action == "release_pr_opened":
        note = f"Release {link} raised in {repo} → {base}."
    else:
        note = (f"Release {link} was already open in {repo} → {base}"
                + (" — this release is now on it." if committed else " and already carries this release."))
    return {
        "ok": True,
        "action": action,
        "release_name": prep.get("release_name"),
        "branch": branch,
        "deployment_repo": repo,
        "pr_number": pr.number,
        "pr_url": pr.html_url,
        "merged": False,
        "committed": committed,
        "note": note + " Review and merge it — the portal never merges it. Its charts have moved from the "
                "release queue to Release history; if this release does not go through, put them back "
                "from there."
                + (" (The queue could not be updated just now — its charts still show there; remove them "
                   "by hand.)" if untracked else ""),
    }
