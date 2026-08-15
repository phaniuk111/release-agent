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


def _repo():
    if not settings.composer_repo:
        raise RuntimeError("No Composer DAGs repo configured (set COMPOSER_REPO).")
    return _get_github_client().get_repo(settings.composer_repo)


def preview_dag_bump(dag_files: list[str], new_version: str,
                     environment: str = "uat") -> dict[str, Any]:
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
            blob = _repo().get_contents(path, ref=settings.composer_branch)
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
    return {"repo": settings.composer_repo, "branch": settings.composer_branch,
            "dir": directory, "changes": changes, "problems": problems}


def apply_dag_bump(dag_files: list[str], new_version: str, environment: str = "uat",
                   image: str = "", run_url: str = "") -> dict[str, Any]:
    """Open a PR bumping the named DAGs to ``new_version``.

    A PR, not a direct commit: the flex-template build this version refers to is
    an Actions run that takes minutes and can fail, so committing now would be a
    claim about a build that has not finished. The PR body carries the run link
    so whoever merges can see it went green.
    """
    if not dag_files:
        return {"ok": False, "error": "No DAG files selected — nothing to bump."}
    version = str(new_version or "").strip()
    if not version:
        return {"ok": False, "error": "No version given for the DAG bump."}

    directory = _dag_dir(environment)
    try:
        repo = _repo()
        base = repo.get_branch(settings.composer_branch)
    except Exception as e:
        return {"ok": False, "error": f"Could not reach {settings.composer_repo}: {e}"}

    slug = "".join(c if c.isalnum() or c in "-._" else "-" for c in f"{image or 'df'}-{version}")
    branch = f"dag-version/{environment.strip().lower()}-{slug}"
    try:
        repo.create_git_ref(f"refs/heads/{branch}", base.commit.sha)
    except Exception as e:
        # An existing branch means a previous attempt for this exact version;
        # reuse it rather than failing the deploy that already dispatched.
        if "Reference already exists" not in str(e):
            return {"ok": False, "error": f"Could not create {branch}: {e}"}

    updated, problems = [], []
    for name in dag_files:
        path = f"{directory}/{name}"
        try:
            blob = repo.get_contents(path, ref=branch)
            text = blob.decoded_content.decode()
            new_text, replaced = set_default_version(text, version)
        except Exception as e:
            problems.append({"file": path, "error": str(e)})
            continue
        if new_text == text:
            updated.append({"file": path, "from": replaced, "to": version, "unchanged": True})
            continue
        try:
            repo.update_file(
                path,
                f"Bump {name} DF template version to {version}",
                new_text,
                blob.sha,
                branch=branch,
            )
        except Exception as e:
            problems.append({"file": path, "error": str(e)})
            continue
        updated.append({"file": path, "from": replaced, "to": version, "unchanged": False})

    if not any(not u["unchanged"] for u in updated):
        return {
            "ok": True, "action": "dag_bump_not_needed", "branch": branch,
            "updated": updated, "problems": problems,
            "note": f"Every selected DAG already points at {version} — no PR raised.",
        }

    changed = [u["file"] for u in updated if not u["unchanged"]]
    body = (
        f"Point the {environment.upper()} Composer DAGs at DF template `{version}`"
        + (f" for `{image}`" if image else "") + ".\n\n"
        + "\n".join(f"- `{u['file']}`: {', '.join(u['from'])} → {version}"
                    for u in updated if not u["unchanged"])
        + (f"\n\nBuild run: {run_url}\n\n**Merge once that run is green** — the template "
           "is not in the bucket until it finishes." if run_url else "")
    )
    try:
        pr = repo.create_pull(
            title=f"Bump {environment.upper()} DAG DF template version to {version}",
            body=body, head=branch, base=settings.composer_branch,
        )
    except Exception as e:
        return {"ok": False, "error": f"DAGs updated on {branch} but the PR failed: {e}",
                "branch": branch, "updated": updated, "problems": problems}
    return {
        "ok": True, "action": "dag_bump_pr_opened", "branch": branch,
        "pr_number": pr.number, "pr_url": pr.html_url,
        "updated": updated, "problems": problems,
        "note": f"PR #{pr.number} bumps {len(changed)} DAG(s) to {version}.",
    }
