"""The Release history card's period as an .xlsx to download: a Releases sheet
(one row per chart a release carried) and a Deployed sheet (one row per deploy
that landed). Built from the same ``release_queue.history`` dict the card
reads, so the file says what the card shows for that period.

The workbook itself is ``bq_cost_xlsx.workbook_bytes`` — no spreadsheet
library. Pure: history in, bytes out; nothing here touches BigQuery.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from .bq_cost_xlsx import MEDIA_TYPE, Sheet, workbook_bytes

__all__ = ["MEDIA_TYPE", "filename", "sheets_for", "workbook"]

_RELEASE_HEADER = ["Released (UTC)", "Release", "PR", "Repository", "Raised by", "Chart", "Version", "Kind",
                   "Goes to", "JIRA", "Change details", "Note", "Queued by", "Build run", "Previous tag",
                   "In the next release"]
_DEPLOY_HEADER = ["Deployed (UTC)", "Chart", "Version", "Environment", "PR", "Repository", "Requested by", "Note"]


def _utc(ts: Any) -> str:
    """'YYYY-MM-DD HH:MM' — the event time as the card shows it, in UTC."""
    text = str(ts or "")
    return text[:16].replace("T", " ") if text else ""


def _goes_to(item: dict[str, Any]) -> str:
    envs = [e.strip().upper() for e in str(item.get("target_envs") or "").split(",") if e.strip()]
    if envs:
        return ", ".join(envs)
    return "PRL1" if item.get("prl1_only") else ""


def sheets_for(history: dict[str, Any]) -> list[Sheet]:
    releases: list[list[Any]] = []
    for rel in history.get("releases") or []:
        for it in rel.get("items") or []:
            releases.append([
                _utc(rel.get("released_at")), rel.get("release_name") or "", rel.get("pr_number"),
                rel.get("deployment_repo") or "", rel.get("released_by") or "",
                it.get("artifact_name") or "", it.get("artifact_version") or "",
                "DF" if it.get("df_only") else "CARE", _goes_to(it), it.get("jira_ticket") or "",
                it.get("change_details") or "", it.get("note") or "", it.get("queued_by") or "",
                it.get("build_run_url") or "", it.get("previous_tag") or "",
                "yes" if it.get("in_queue") else "",
            ])
    deployed = [[
        _utc(d.get("deployed_at")), d.get("artifact_name") or "", d.get("artifact_version") or "",
        str(d.get("environment") or "").upper(), d.get("pr_number"), d.get("deployment_repo") or "",
        d.get("requested_by") or "", d.get("note") or "",
    ] for d in history.get("deployments") or []]
    return [("Releases", _RELEASE_HEADER, releases), ("Deployed", _DEPLOY_HEADER, deployed)]


def workbook(history: dict[str, Any]) -> bytes:
    return workbook_bytes(sheets_for(history))


def _safe(text: str) -> str:
    """Only characters a Content-Disposition header keeps verbatim."""
    return "".join(ch for ch in text if ch.isalnum() or ch in "-_.")


def filename(history: dict[str, Any]) -> str:
    """release-history-<since>-to-<until>.xlsx for a period, else
    release-history-last-<days>-days-<today>.xlsx."""
    if history.get("since"):
        return f"release-history-{_safe(str(history['since']))}-to-{_safe(str(history.get('until') or ''))}.xlsx"
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    return f"release-history-last-{_safe(str(history.get('days') or ''))}-days-{today}.xlsx"
