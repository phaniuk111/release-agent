"""Composer DAG version bumps.

A Dataflow deploy builds a flex template and pushes it to a bucket under a
VERSION path. The Composer DAGs that launch those templates carry that version
as the fallback of a Jinja expression, so a deploy is only half-done until the
DAGs point at the new one::

    "gs://<bucket>/templates/<image>/{{dag_run.conf['version'] | default('0.0.494')}}/dataflow_job.json"

Only the ``default('…')`` value changes. Everything else on that line — bucket,
image path, the conf override that lets an operator pin a version per DAG run —
must survive untouched, which is why this edits the one literal rather than
rewriting the line.

No regex: the marker is located by index scanning (repo convention), so a
`default(...)` belonging to some other Jinja filter elsewhere in the file cannot
be hit by accident — only one preceded by the version conf lookup qualifies.
"""
from __future__ import annotations

from typing import Any

from ._common import settings, _get_github_client

# The expression that identifies OUR version fallback. Both quote styles occur
# in the wild ("conf['version']" and 'conf["version"]'), so the lookup is matched
# without its quotes and the quote character is read back from the text.
_CONF_PREFIX = "dag_run.conf["
_CONF_KEY = "version"
_DEFAULT_CALL = "default("


class DagVersionNotFound(LookupError):
    """No version fallback in this DAG — bumping it would be a silent no-op."""


def _find_version_spans(text: str) -> list[tuple[int, int, str]]:
    """Locate every ``default('<version>')`` that belongs to a version conf
    lookup. Returns (start, end, current_version) for each, in file order,
    where start:end covers just the version characters."""
    spans: list[tuple[int, int, str]] = []
    cursor = 0
    while True:
        conf_at = text.find(_CONF_PREFIX, cursor)
        if conf_at == -1:
            return spans
        cursor = conf_at + len(_CONF_PREFIX)

        # ['version'] or ["version"] — read the quoted key and require it to match
        if cursor >= len(text) or text[cursor] not in "'\"":
            continue
        quote = text[cursor]
        key_end = text.find(quote, cursor + 1)
        if key_end == -1 or text[cursor + 1:key_end] != _CONF_KEY:
            continue

        # the default() call must follow within this Jinja expression, so stop at
        # the closing braces rather than scanning into the next line
        expr_end = text.find("}}", key_end)
        default_at = text.find(_DEFAULT_CALL, key_end)
        if default_at == -1 or (expr_end != -1 and default_at > expr_end):
            continue

        value_at = default_at + len(_DEFAULT_CALL)
        if value_at >= len(text) or text[value_at] not in "'\"":
            continue
        vquote = text[value_at]
        value_end = text.find(vquote, value_at + 1)
        if value_end == -1:
            continue
        spans.append((value_at + 1, value_end, text[value_at + 1:value_end]))
        cursor = value_end


def current_versions(text: str) -> list[str]:
    """Every version fallback currently in this DAG, in file order."""
    return [version for _, _, version in _find_version_spans(text)]


def set_default_version(text: str, new_version: str) -> tuple[str, list[str]]:
    """Return (updated_text, versions_replaced).

    Raises DagVersionNotFound when the file carries no version fallback — a DAG
    that silently did not change is worse than a refusal, because the deploy
    would look complete.
    """
    spans = _find_version_spans(text)
    if not spans:
        raise DagVersionNotFound("no dag_run.conf['version'] default(...) in this file")
    replaced = []
    out = text
    # right-to-left so earlier offsets stay valid as the text length changes
    for start, end, current in reversed(spans):
        replaced.append(current)
        out = out[:start] + new_version + out[end:]
    return out, list(reversed(replaced))


# --------------------------------------------------------------- GitHub side

def _dag_dir(environment: str) -> str:
    """Folder holding the DAGs for an environment (e.g. 'uat')."""
    return (settings.composer_dag_dir_pattern or "{env}").format(env=environment.strip().lower())


def _resolve_repo(repo: str = "") -> str:
    """Form-supplied repo wins; empty falls back to the configured default."""
    return str(repo or "").strip() or settings.composer_repo


def _repo(repo: str = ""):
    full = _resolve_repo(repo)
    if not full:
        raise RuntimeError("No Composer DAGs repo given (set COMPOSER_REPO or name one).")
    return _get_github_client().get_repo(full)


def preview_dag_bump(dag_files: list[str], new_version: str,
                     environment: str = "uat", repo: str = "") -> dict[str, Any]:
    """What the DAG bump WOULD change — read-only, for the confirmation preview.

    Reports per file: the versions found and what they become. A file with no
    version fallback, or one already on the target, is reported as such rather
    than quietly contributing nothing.
    """
    directory = _dag_dir(environment)
    changes, problems = [], []
    for name in dag_files:
        path = f"{directory}/{name}"
        try:
            blob = _repo(repo).get_contents(path, ref=settings.composer_branch)
            text = blob.decoded_content.decode()
        except Exception as e:
            problems.append({"file": path, "error": str(e)})
            continue
        try:
            _, replaced = set_default_version(text, new_version)
        except DagVersionNotFound as e:
            problems.append({"file": path, "error": str(e)})
            continue
        changes.append({
            "file": path,
            "from": replaced,
            "to": new_version,
            "unchanged": all(v == new_version for v in replaced),
        })
    return {"repo": _resolve_repo(repo), "branch": settings.composer_branch,
            "dir": directory, "changes": changes, "problems": problems}


def apply_dag_bump(dag_files: list[str], new_version: str, environment: str = "uat",
                   image: str = "", run_url: str = "", repo: str = "") -> dict[str, Any]:
    """Open a PR bumping the named DAGs to ``new_version``.

    A PR, not a direct commit, so a human still merges it. The deploy Workflow
    calls this only once the flex-template run is green — the version names a
    template that exists — and the body carries that run's link as evidence.
    """
    if not dag_files:
        return {"ok": False, "error": "No DAG files selected — nothing to bump."}
    version = str(new_version or "").strip()
    if not version:
        return {"ok": False, "error": "No version given for the DAG bump."}

    directory = _dag_dir(environment)
    repo_full = _resolve_repo(repo)
    try:
        gh_repo = _repo(repo)
        base = gh_repo.get_branch(settings.composer_branch)
    except Exception as e:
        return {"ok": False, "error": f"Could not reach {repo_full or '<unset>'}: {e}"}

    slug = "".join(c if c.isalnum() or c in "-._" else "-" for c in f"{image or 'df'}-{version}")
    branch = f"dag-version/{environment.strip().lower()}-{slug}"
    try:
        gh_repo.create_git_ref(f"refs/heads/{branch}", base.commit.sha)
    except Exception as e:
        # An existing branch means a previous attempt for this exact version;
        # reuse it rather than failing the deploy that already dispatched.
        if "Reference already exists" not in str(e):
            return {"ok": False, "error": f"Could not create {branch}: {e}"}

    # Idempotent by construction — this runs again whenever a deploy is resumed,
    # and a previous attempt may have pushed the edits and then failed to open
    # the PR. So "does this need a change?" is asked of the BASE branch, never of
    # the work branch: asked of the branch, a retry after a half-finished attempt
    # finds its own edits, concludes nothing needs changing, and never opens the
    # PR at all.
    updated, problems = [], []
    for name in dag_files:
        path = f"{directory}/{name}"
        try:
            base_text = gh_repo.get_contents(path, ref=settings.composer_branch).decoded_content.decode()
            desired, replaced = set_default_version(base_text, version)
            branch_blob = gh_repo.get_contents(path, ref=branch)
        except Exception as e:
            problems.append({"file": path, "error": str(e)})
            continue
        if desired == base_text:
            updated.append({"file": path, "from": replaced, "to": version, "unchanged": True})
            continue
        if branch_blob.decoded_content.decode() != desired:
            try:
                gh_repo.update_file(
                    path,
                    f"Bump {name} DF template version to {version}",
                    desired,
                    branch_blob.sha,
                    branch=branch,
                )
            except Exception as e:
                problems.append({"file": path, "error": str(e)})
                continue
        updated.append({"file": path, "from": replaced, "to": version, "unchanged": False})

    if not any(not u["unchanged"] for u in updated):
        # Nothing to propose, so leave no trace: the branch was created before we
        # knew that, and an empty dag-version/* branch per attempt would silt up
        # the repo and look like a half-finished bump to anyone browsing it.
        try:
            repo_ref = gh_repo.get_git_ref(f"heads/{branch}")
            if repo_ref.object.sha == base.commit.sha:
                repo_ref.delete()
        except Exception:
            pass          # tidiness only — never fail a deploy over a stray branch
        return {
            "ok": True, "action": "dag_bump_not_needed", "branch": "",
            "updated": updated, "problems": problems,
            "note": f"No DAG needed a change to {version} — no PR raised.",
        }

    changed = [u["file"] for u in updated if not u["unchanged"]]
    # A PR from this branch may already exist — the earlier attempt that pushed
    # these edits may have opened it before failing further on. Return that one.
    existing = _open_pr_for(gh_repo, branch)
    if existing is not None:
        return {
            "ok": True, "action": "dag_bump_pr_exists", "branch": branch,
            "pr_number": existing.number, "pr_url": existing.html_url,
            "updated": updated, "problems": problems,
            "note": f"PR #{existing.number} already bumps {len(changed)} DAG(s) to {version}.",
        }
    body = (
        f"Point the {environment.upper()} Composer DAGs at DF template `{version}`"
        + (f" for `{image}`" if image else "") + ".\n\n"
        + "\n".join(f"- `{u['file']}`: {', '.join(u['from'])} → {version}"
                    for u in updated if not u["unchanged"])
        + (f"\n\nBuild run (green): {run_url}" if run_url else "")
    )
    try:
        pr = gh_repo.create_pull(
            title=f"Bump {environment.upper()} DAG DF template version to {version}",
            body=body, head=branch, base=settings.composer_branch,
        )
    except Exception as e:
        # Lost a race with another attempt opening the same PR: that is success.
        pr = _open_pr_for(gh_repo, branch)
        if pr is None:
            return {"ok": False, "error": f"DAGs updated on {branch} but the PR failed: {e}",
                    "branch": branch, "updated": updated, "problems": problems}
    return {
        "ok": True, "action": "dag_bump_pr_opened", "branch": branch,
        "pr_number": pr.number, "pr_url": pr.html_url,
        "updated": updated, "problems": problems,
        "note": f"PR #{pr.number} bumps {len(changed)} DAG(s) to {version}.",
    }


def _open_pr_for(gh_repo: Any, branch: str) -> Any | None:
    """The open PR whose head is ``branch``, or None. Best-effort."""
    try:
        owner = gh_repo.full_name.split("/")[0]
        for pr in gh_repo.get_pulls(state="open", head=f"{owner}:{branch}"):
            return pr
    except Exception:
        pass
    return None
