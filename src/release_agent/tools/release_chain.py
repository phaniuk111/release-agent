"""Which branches a release travels, per kind (CARE / DF).

A CARE release lands on SIT and is promoted to UAT, then PRD or PRL1. A DF
release repo may keep a different chain entirely — e.g. RELEASE_UAT and
RELEASE_PRD, no SIT — where the release PR goes straight to the UAT branch.
So each kind has an ordered chain of (environment, branch): the FIRST branch is
where the release PR lands, the rest are what it can be promoted to.

  CARE  SIT_BRANCH, UAT_BRANCH, PRD_BRANCH, PRL1_BRANCH (as ever)
  DF    DF_RELEASE_BRANCHES, e.g. "uat:RELEASE_UAT,prd:RELEASE_PRD";
        empty = the CARE chain (one repo, one set of branches)

The one-release-at-a-time guard watches the kind's own branches: a DF release
in flight must not block the CARE one, and vice versa.
"""
from __future__ import annotations

from ._common import settings

KINDS = ("care", "df")


def parse(raw: str) -> list[tuple[str, str]]:
    """'uat:RELEASE_UAT, prd:RELEASE_PRD' → [('uat', 'RELEASE_UAT'), ('prd', 'RELEASE_PRD')]."""
    chain = []
    for part in (raw or "").split(","):
        env, sep, branch = part.strip().partition(":")
        if sep and env.strip() and branch.strip():
            chain.append((env.strip().lower(), branch.strip()))
    return chain


def _care() -> list[tuple[str, str]]:
    return [("sit", settings.sit_branch), ("uat", settings.uat_branch),
            ("prd", settings.prd_branch), ("prl1", settings.prl1_branch)]


def chain(kind: str) -> list[tuple[str, str]]:
    if kind == "df":
        return parse(settings.df_release_branches) or _care()
    return _care()


def landing(kind: str) -> str:
    """The branch a new release PR targets (and whose tip the file-set is built on)."""
    return chain(kind)[0][1]


def targets(kind: str) -> dict[str, str]:
    """Environments the release can be promoted to → branch ('prod' = 'prd')."""
    out = dict(chain(kind)[1:])
    if "prd" in out:
        out.setdefault("prod", out["prd"])
    return out


def guard_branches(kind: str) -> list[str]:
    """Branches where an open PR means this kind's release is already in flight."""
    if kind == "df" and parse(settings.df_release_branches):
        return [branch for _, branch in chain("df")]
    configured = [b.strip() for b in settings.release_guard_branches if b and b.strip()]
    return configured or [settings.prd_branch]


def kind_of(details: dict, explicit: str = "") -> str:
    """A DF release is all Dataflow images (the DF form sends every artifact as
    one); anything else is CARE. An explicit, valid kind wins."""
    if explicit in KINDS:
        return explicit
    names = {a.rstrip("/").split("/")[-1].partition(":")[0] for a in details.get("artefact") or []}
    df = set(details.get("df_images") or [])
    return "df" if df and names and names <= df else "care"


def next_steps(kind: str) -> str:
    envs = [env for env in targets(kind) if env != "prod"]
    label = "the DF release" if kind == "df" else "release"
    if not envs:
        return ""
    if kind != "df" and envs[:1] == ["uat"]:
        rest = "/".join(envs[1:])
        return f"Promote with 'promote {label} to uat'" + (f" then 'promote {label} to {rest}'." if rest else ".")
    return f"Promote with 'promote {label} to {'/'.join(envs)}'."
