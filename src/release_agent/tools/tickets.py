"""CHG / RMG tickets named on a PR — read from its body and comments, deterministically.

The change-management tickets a release needs are not fields anywhere GitHub
knows about: they are named in the PR body or its comments. This module finds
those mentions with a tokenizer (no regex, like parsing.py) so the banner can
show them and the deployment tracker can tell the requester when one appears.

What counts: a token whose upper-cased form is <PREFIX>-<digits> or
<PREFIX><digits> for one of TICKET_PREFIXES ("CHG,RMG"), normalised to
PREFIX-digits. Only the ticket itself is read — never a state: the comments do
not say one, and a word guessed from them would be wrong. Across a PR the body
is read first, then the comments oldest to newest, and the newest mention per
prefix wins.

GitHub is asked once per (repo, PR) every ten minutes: the banner already costs
several calls per load and the tracker visits every pending PR each pass. Nothing
here raises — a PR that cannot be read comes back as {"error": ...}.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from ..config import settings

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 600.0
_MAX_COMMENTS = 300
_cache: dict[tuple[str, int], dict[str, Any]] = {}
_cache_lock = threading.Lock()


@dataclass(frozen=True)
class Ticket:
    id: str        # "CHG-12345" — always PREFIX-digits, upper-cased
    prefix: str    # "CHG"
    number: str    # "12345" — digits as written (leading zeros kept: it is a name, not a count)


def configured_prefixes() -> list[str]:
    return _words(settings.ticket_prefixes, upper=True)


def _words(raw: str, *, upper: bool = False) -> list[str]:
    out = []
    for part in str(raw or "").split(","):
        word = part.strip()
        word = word.upper() if upper else word.lower()
        if word and word not in out:
            out.append(word)
    return out


# ----- the tokenizer ----------------------------------------------------------------

def _tokens(line: str) -> list[str]:
    """Whitespace and punctuation separate tokens; "-" belongs to one ("CHG-12345",
    "svc-a"). A token's own leading or trailing dashes are dropped, so a bullet
    "- CHG-12345" or a dash glued to a word does not hide the ticket."""
    tokens, current = [], []
    for ch in line + " ":
        if ch.isalnum() or ch == "-":
            current.append(ch)
            continue
        if current:
            token = "".join(current).strip("-")
            if token:
                tokens.append(token)
            current = []
    return tokens


def _as_ticket(token: str, wanted: list[str]) -> tuple[str, str] | None:
    """(prefix, digits) when ``token`` names a ticket of a wanted prefix."""
    upper = token.upper()
    for prefix in wanted:
        if not upper.startswith(prefix):
            continue
        rest = upper[len(prefix):]
        if rest.startswith("-"):
            rest = rest[1:]
        if rest and rest.isdigit():
            return prefix, rest
    return None


def tickets_in(text: str, prefixes: list[str] | None = None) -> list[Ticket]:
    """Every ticket mention in ``text``, in reading order. Pure. ``prefixes``
    defaults to TICKET_PREFIXES."""
    wanted = [p.strip().upper() for p in (prefixes if prefixes is not None else configured_prefixes())
              if p and p.strip()]
    found: list[Ticket] = []
    for line in str(text or "").splitlines():
        for token in _tokens(line):
            hit = _as_ticket(token, wanted)
            if hit:
                prefix, digits = hit
                found.append(Ticket(id=f"{prefix}-{digits}", prefix=prefix, number=digits))
    return found


# ----- a PR's tickets ---------------------------------------------------------------

def _merge(found: dict[str, dict[str, Any]], text: str, source: str, url: str, wanted: list[str]) -> None:
    """Fold one text's mentions into ``found`` — a later mention replaces an earlier."""
    for ticket in tickets_in(text, wanted):
        found[ticket.prefix.lower()] = {"id": ticket.id, "source": source, "url": url}


def tickets_from(body: str, pr_url: str, comments: list[dict[str, Any]], wanted: list[str] | None = None) -> dict:
    """The newest mention per prefix across ``body`` then ``comments`` (oldest
    first, each {"body", "html_url"}). Pure: {"chg": {id, source, url} | None, ...}."""
    wanted = wanted if wanted is not None else configured_prefixes()
    found: dict[str, dict[str, Any] | None] = {p.lower(): None for p in wanted}
    _merge(found, body, "body", pr_url, wanted)
    for comment in comments:
        _merge(found, str(comment.get("body") or ""), "comment",
               str(comment.get("html_url") or pr_url), wanted)
    return found


def _read(github: Any, repo_full: str, pr_number: int, pr: Any = None) -> dict:
    pr = pr if pr is not None else github.get_repo(repo_full).get_pull(int(pr_number))
    comments = []
    for comment in pr.get_issue_comments():
        comments.append({"body": getattr(comment, "body", "") or "",
                         "html_url": getattr(comment, "html_url", "") or "",
                         "created_at": getattr(comment, "created_at", None)})
        if len(comments) >= _MAX_COMMENTS:
            break
    # GitHub lists issue comments oldest first; sorting makes that a fact here too.
    comments.sort(key=lambda c: (c["created_at"] is None, c["created_at"] or 0))
    return tickets_from(getattr(pr, "body", "") or "", getattr(pr, "html_url", "") or "", comments)


def pr_tickets(repo_full: str, pr_number: int, github: Any = None, *, fresh: bool = False, pr: Any = None) -> dict:
    """The tickets named on one PR: {"chg": {...} | None, "rmg": {...} | None, ...}
    per configured prefix, or {"error": ...}. Cached ten minutes per (repo, PR);
    never raises. ``pr`` is the PR object when the caller already holds it, so
    a cold cache does not fetch it again."""
    key = (str(repo_full or "").strip(), int(pr_number or 0))
    if not key[0] or not key[1]:
        return {"error": "a repo and a PR number are needed"}
    now = time.monotonic()
    if not fresh:
        with _cache_lock:
            hit = _cache.get(key)
        if hit and now - hit["at"] < _CACHE_TTL_SECONDS:
            return dict(hit["value"])
    try:
        if github is None:
            from ._common import _get_github_client

            github = _get_github_client()
        value = _read(github, *key, pr=pr)
    except Exception as e:  # noqa: BLE001 — a PR that cannot be read is a fact to show, not a crash
        logger.warning("tickets: could not read %s#%s: %s", key[0], key[1], e)
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}
    with _cache_lock:
        _cache[key] = {"at": now, "value": value}
    return dict(value)


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def columns(tickets: dict | None) -> dict[str, str]:
    """The event-log columns for a PR's tickets: chg and rmg (empty strings when
    absent, so a row says "none" rather than "unknown")."""
    tickets = tickets if isinstance(tickets, dict) and not tickets.get("error") else {}
    out = {}
    for prefix in ("chg", "rmg"):
        hit = tickets.get(prefix) or {}
        out[prefix] = str(hit.get("id") or "")
    return out


def note_first_seen(repo_full: str, pr_number: int, tickets: dict | None, *,
                    requested_by: str, what: str) -> dict:
    """Keep the event log's view of a pending PR's tickets current, and tell the
    requester the first time a CHG appears on it.

    Compares with the last `tickets` row of the deploy scope: a row is written
    only when a ticket changed (append-only, latest wins), and the notification
    goes out only for a CHG id not recorded before. {"recorded": bool,
    "notified": bool}; never raises."""
    from . import events_store, notifications

    try:
        cols = columns(tickets)
        if not any(cols.values()):
            return {"recorded": False, "notified": False}
        scope = f"deploy:{repo_full}#{int(pr_number)}"
        known = events_store.history("deploy", since_days=30, scope=scope, event_type="tickets")
        rows = known.get("rows") or [] if known.get("ok") else []
        last = {k: str(rows[0].get(k) or "") for k in cols} if rows else {}
        if last == cols:
            return {"recorded": False, "notified": False}
        seen_chg = {str(r.get("chg") or "") for r in rows}
        events_store.record({"domain": "deploy", "event_type": "tickets", "subject": scope, "scope": scope,
                             "repo": repo_full, "pr_number": int(pr_number), "actor": "portal", **cols})
        new_chg = cols["chg"] and cols["chg"] not in seen_chg
        notified = False
        if new_chg and requested_by:
            hit = (tickets or {}).get("chg") or {}
            out = notifications.notify(
                requested_by, kind="chg", title=f"{cols['chg']} for release {what}",
                body=f"Named in the PR {hit.get('source') or 'comments'} of #{int(pr_number)}",
                link=str(hit.get("url") or ""), scope=f"chg:{repo_full}#{int(pr_number)}",
                repo=repo_full, pr_number=int(pr_number))
            notified = bool(out.get("ok"))
        return {"recorded": True, "notified": notified}
    except Exception as e:  # noqa: BLE001
        logger.warning("tickets: could not record %s#%s: %s", repo_full, pr_number, e)
        return {"recorded": False, "notified": False, "error": str(e)[:200]}
