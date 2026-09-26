"""CARE release in mono mode: ONE committed file, and a PR a person merges.

With CARE_RELEASE_MODE=mono a CARE release is not a generated file-set: it is
CARE_RELEASE_FILE in CARE_RELEASE_REPO. The portal edits that one file on a
release branch and raises a PR against CARE_RELEASE_BASE_BRANCH. A person
reviews and merges it — the portal never does — and the repository's own
workflow takes it from there to the deployment repo.

  prepare : read the file at the base branch's head, splice this release into
            it (json_splice: only the values that change move, every other
            byte stays) and preview the exact diff. Reads only.
  apply   : refuse unless the base still holds the previewed file, the splice
            reproduces the approved text, and no other release PR has opened
            since the preview (the guard runs again: only a PR already
            carrying exactly this text — this approval, applied before — is
            not another release); commit the one file on a new release
            branch, raise the PR, and release its charts in the queue at once
            (they move to Release history; no merge is tracked).

Same prep/result contract as release_fileset, so the CONFIRM-token flow in
adk_release_agent/deploy.py is unchanged. DF releases, and CARE in the
default "fileset" mode, never come here.
"""
from __future__ import annotations

import difflib
import hashlib
import logging
from typing import Any

from . import attribution, json_splice
from ._common import settings, _get_github_client
from .release_fileset import _artifact_name_version, _slugify, partition_environments
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

    try:
        gh_repo = _get_github_client().get_repo(repo)
        blocker = _open_prd_pr_blocker(gh_repo, branches=[base], head_prefix=prefix)
    except Exception as e:
        return _refuse(f"Could not reach {repo}: {e}")
    if blocker is not None:
        return _refuse(_in_flight(blocker))
    try:
        head, old_text, blob_sha = _read_base(gh_repo, base, path)
    except Exception as e:
        return _refuse(f"Could not read {path} on {base} in {repo}: {e}")
    try:
        new_text = json_splice.splice_top_level(old_text, file_doc(details))
    except ValueError as e:
        return _refuse(f"{path} on {base} cannot take this release: {e}")
    if new_text == old_text:
        return _refuse(f"{base} already carries exactly this release — {path} would not change.")

    email, verified = _raised_by(details)
    branch = prefix + _slugify(details["release_name"])
    return {
        "ok": True,
        "mode": "mono",
        "kind": "care",
        "release_name": details["release_name"],
        "branch": branch,
        "branch_prefix": prefix,        # apply runs the same guard again
        "details": details,             # apply splices again from these
        "deployment_repo": repo,
        "landing_branch": base,
        "file": path,
        "artifacts": [{"name": nv[0], "tag": nv[1]}
                      for a in details["artefact"] if (nv := _artifact_name_version(a))],
        "base_commit": head,
        "base_blob_sha": blob_sha,      # apply refuses if the file on the base moved
        "content_hash": _content_hash(new_text),
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
            # What CONFIRM approves: the exact bytes the PR will carry.
            "file_diff": _diff(old_text, new_text, path),
        },
    }


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
                  path: str, text: str) -> tuple[str, Any, bool]:
    """(branch, its open PR or None, created now). A branch with an open PR
    into the base is reused only when it already carries exactly ``text`` —
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
        if not _carries(gh_repo, path, name, text):
            raise _Stop(f"PR #{pr.number} ({pr.html_url}) for this release name is open with different "
                        "content — nothing was pushed. One release at a time: merge or close it first.")
        return name, pr, False
    raise _Stop(f"{wanted} and {wanted}-2 … -{_MAX_BRANCH_SUFFIX} all exist in {repo} with no open PR "
                "— delete those leftover branches, then start the release again. Nothing was pushed.")


def _pr_body(details: dict[str, Any], path: str) -> str:
    email, verified = _raised_by(details)
    who = email if verified else f"{email} (unverified — typed on the form)"
    artefacts = "\n".join(f"- {a}" for a in details.get("artefact") or [])
    return (
        f"Release **{details['release_name']}** — updates `{path}`.\n\n"
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
    try:
        head, old_text, blob_sha = _read_base(gh_repo, base, path)
    except Exception as e:
        raise _Stop(f"Could not read {path} on {base} in {repo}: {e}. Nothing was pushed.") from None
    if blob_sha != prep["base_blob_sha"]:
        raise _Stop(f"The release file on {base} changed since the preview — start the release "
                    "again. Nothing was pushed.")
    try:
        text = json_splice.splice_top_level(old_text, file_doc(details))
    except ValueError as e:
        raise _Stop(f"{path} on {base} cannot take this release: {e}. Nothing was pushed.") from None
    if _content_hash(text) != prep["content_hash"]:
        raise _Stop("The release no longer matches what you approved — start the release again. "
                    "Nothing was pushed.")

    # The preview's guard is minutes old by now: another release PR may have
    # opened since, and two releases in flight is what the guard exists to stop.
    blocker = _open_prd_pr_blocker(gh_repo, branches=[base], head_prefix=prefix,
                                   skip=lambda open_pr: _carries(gh_repo, path, open_pr.head.ref, text))
    if blocker is not None:
        raise _Stop(_in_flight(blocker) + " Nothing was pushed.")

    branch, pr, created = _claim_branch(gh_repo, repo, wanted, base, head, path, text)
    committed = False
    if created:
        # Just cut from the head read above, so its file is exactly that blob.
        # A reused branch already carries ``text`` (_claim_branch), so only a
        # new one is ever written to.
        try:
            gh_repo.update_file(path, attribution.with_trailer(title), text, blob_sha,
                                branch=branch, **attribution.author_kwargs())
        except Exception as e:
            # urllib3 retries a PUT after a 5xx that may already have landed, and
            # the retry then fails on the stale sha: read back before failing.
            if not _carries(gh_repo, path, branch, text):
                _delete_branch(gh_repo, branch)
                raise _Stop(f"Could not commit {path} to {branch} in {repo}: {e}. Nothing was "
                            f"merged; {base} is unchanged.") from None
        committed = True

    action = "release_pr_exists"
    if pr is None:
        try:
            pr = gh_repo.create_pull(title=title, body=attribution.with_trailer(_pr_body(details, path)),
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

            recorded = mark_released(prep.get("release_name") or title, pr.number,
                                     prep.get("artifacts") or [], deployment_repo=repo)
        except Exception as e:  # noqa: BLE001 — never fails a raised PR
            recorded = {"ok": False, "error": str(e)}
        untracked = not recorded.get("ok") and not recorded.get("disabled")
        if untracked:
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
