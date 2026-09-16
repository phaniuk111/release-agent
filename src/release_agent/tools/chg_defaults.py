"""Fill a release's change request from what the portal already knows.

DevOps used to type the same change-request fields every release from
information that was already here: the queued charts, their JIRA keys, the
developers' own "what changed and why", whether each build was verified, and
the previous release's number. This computes every field except the start and
end dates — those are a decision, not a fact.

Standard wording, not a model: each sentence is assembled from facts, so the
same inputs always give the same text and nothing can be invented. Where a fact
is missing the wording says so ("2 of 3 builds were verified at queue time")
rather than smoothing it over. A team whose change board wants its own phrasing
overrides the risk / consequence / impact templates in config; see
``render_template`` for the placeholders.

Nothing here submits anything. The fields land in an editable form, a human
edit always wins over a recomputed default, and the release still runs through
the deterministic preview → CONFIRM path.
"""
from __future__ import annotations

import datetime as _dt
import time
from typing import Any

from ._common import settings

# The release-PR title convention: "<prefix><Month Dayth YYYY> : Release <N>".
_NUMBER_MARKER = " : Release "
_MAX_LISTED_ITEMS = 4
_CACHE_SECONDS = 60.0
_number_cache: dict[str, tuple[float, int | None]] = {}


# ------------------------------------------------------------------ naming --
def _ordinal(day: int) -> str:
    if 10 <= day % 100 <= 20:
        return f"{day}th"
    return f"{day}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th') }"


def format_release_date(day: _dt.date) -> str:
    """July 23rd 2026 — the date as release names spell it."""
    return f"{day.strftime('%B')} {_ordinal(day.day)} {day.year}"


def release_number_from_title(title: str) -> int | None:
    """33 from "… : Release 33"; None when the title is not a numbered release."""
    head, sep, tail = str(title or "").rpartition(_NUMBER_MARKER)
    if not sep:
        return None
    digits = ""
    for ch in tail.strip():
        if not ch.isdigit():
            break
        digits += ch
    return int(digits) if digits else None


def next_release_number(titles: list[str]) -> int | None:
    """One past the highest numbered release among ``titles``, or None if none."""
    numbers = [n for n in (release_number_from_title(t) for t in titles) if n is not None]
    return max(numbers) + 1 if numbers else None


def next_release_number_for_repo(repo_full: str) -> int | None:
    """The next release number, read from the deploy repo's own release PRs.

    GitHub is the record: a release is a PR titled with its number, and the
    event log only knows the releases the portal happened to see. Cached per
    repo for a minute (the form asks on every tick). None on any failure — a
    name without a number is still a usable default, a failed form is not.
    """
    repo_full = str(repo_full or "").strip()
    if not repo_full:
        return None
    now = time.monotonic()
    cached = _number_cache.get(repo_full)
    if cached and now - cached[0] < _CACHE_SECONDS:
        return cached[1]
    try:
        import itertools

        from ._common import _get_github_client

        pulls = _get_github_client().get_repo(repo_full).get_pulls(
            state="all", sort="created", direction="desc")
        titles = [p.title for p in itertools.islice(pulls, 100)]
        number = next_release_number(titles) or 1
    except Exception:
        number = None
    _number_cache[repo_full] = (now, number)
    return number


def release_name(day: _dt.date, number: int | None, prefix: str | None = None) -> str:
    prefix = settings.release_name_prefix if prefix is None else prefix
    base = f"{prefix or ''}{format_release_date(day)}"
    return f"{base}{_NUMBER_MARKER}{number}" if number else base


# ------------------------------------------------------------------ fields --
def _plural(count: int, one: str, many: str) -> str:
    return one if count == 1 else many


def _items_phrase(items: list[dict[str, Any]], limit: int = _MAX_LISTED_ITEMS) -> str:
    labels = [f"{i['name']} {i['version']}" for i in items]
    if len(labels) <= limit:
        return ", ".join(labels)
    return ", ".join(labels[:limit]) + f" and {len(labels) - limit} more"


def render_template(template: str, values: dict[str, Any]) -> str:
    """Fill a team's own wording. Placeholders: {count} {noun} {items}
    {names} {jira_keys} {pipeline} {prl1_note} {verification} {rollback}
    {unchanged} {release_name}.
    An unknown placeholder is left as written rather than failing the form."""
    class _Keep(dict):
        def __missing__(self, key):
            return "{" + key + "}"
    try:
        return str(template).format_map(_Keep(values))
    except (ValueError, IndexError):
        return str(template)


def build_defaults(
    items: list[dict[str, Any]], kind: str = "care", day: _dt.date | None = None,
    number: int | None = None,
) -> dict[str, str]:
    """Every change-request field except start and end.

    ``items`` are the ticked release items: name, version, and — when they came
    from the queue — jira_ticket, change_details, requested_by, build_verified,
    prl1_only. An item typed straight into the form has none of those, and the
    wording treats its build as unverified, because it was never checked.
    """
    df = str(kind).lower() == "df"
    day = day or _dt.date.today()
    name = release_name(day, number)
    count = len(items)
    noun = _plural(count, "Dataflow image", "Dataflow images") if df else _plural(count, "chart", "charts")

    jira_keys = []
    for i in items:
        key = str(i.get("jira_ticket") or "").strip()
        if key and key not in jira_keys:
            jira_keys.append(key)
    jira_text = ", ".join(jira_keys)

    verified = [i for i in items if i.get("build_verified") is True]
    unverified = [i["name"] for i in items if i.get("build_verified") is not True]
    if count and not unverified:
        verification = ("Every build was verified at queue time from its GitHub Actions run, "
                        "with all release controls (RCTLD) passing.")
    elif count:
        verification = (f"{len(verified)} of {count} builds were verified at queue time; "
                        f"{', '.join(unverified)} {_plural(len(unverified), 'was', 'were')} not "
                        "— review before approving.")
    else:
        verification = ""

    prl1_note = ""
    if df:
        pipeline = "Dataflow release"
        rollback = "Rollback: point the Composer DAGs back at the previous template version."
    else:
        pipeline = "SIT → UAT → PRD"
        restricted = [i["name"] for i in items if i.get("prl1_only")]
        if restricted:
            prl1_note = (f"{', '.join(restricted)} "
                         f"{_plural(len(restricted), 'is PRL1-only and stops', 'are PRL1-only and stop')}"
                         " at PRL1.")
        rollback = "Rollback: revert the deployment-file change to redeploy the previous version."

    values = {
        "count": count, "noun": noun, "items": _items_phrase(items),
        "names": ", ".join(i["name"] for i in items), "jira_keys": jira_text or "the listed changes",
        "pipeline": pipeline, "prl1_note": prl1_note, "verification": verification,
        "rollback": rollback, "release_name": name,
        "unchanged": _plural(count, f"the {noun} stays on its current version",
                             f"the {noun} stay on their current versions"),
    }

    description = "\n".join(
        f"- {i['name']}:{i['version']}"
        + (f" ({i['jira_ticket']})" if i.get("jira_ticket") else "")
        + (f": {i['change_details']}" if i.get("change_details") else "")
        + (f" — {str(i['requested_by']).split('@')[0]}" if i.get("requested_by") else "")
        for i in items
    )

    risk_t = settings.chg_risk_template or (
        "Standard release of {count} {noun} ({items}) through the {pipeline} pipeline. "
        "{prl1_note} {verification} {rollback}")
    consequence_t = settings.chg_consequence_template or (
        "If this release does not go ahead, {jira_keys} will not be delivered and "
        "{unchanged}.")
    impact_t = settings.chg_impact_template or (
        "Dataflow flex-template update for {count} {noun} ({names}); running jobs keep their "
        "current template until relaunched. No downtime is planned."
        if df else
        "Rolling Helm deployment of {count} {noun} ({names}); no downtime is planned.")

    return {
        "release_name": name,
        "change_summary": f"{name} — {count} {noun}: {_items_phrase(items)}" if count else name,
        "change_description": description,
        "change_reason": (f"Delivers {jira_text}." if jira_keys
                          else "Delivers the changes listed in the description."),
        "associated_risk": " ".join(render_template(risk_t, values).split()),
        "consequence": " ".join(render_template(consequence_t, values).split()),
        "user_service_impact": " ".join(render_template(impact_t, values).split()),
    }
