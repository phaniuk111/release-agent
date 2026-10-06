"""Release-queue ELIGIBILITY GATE — is this chart:version allowed into the release?

Not an agent wrapper: no model, no ADK. Both lanes call this and must apply the
SAME rules — the queue FORM posts to /api/release-queue/batch, and the chat lane
reaches it through adk_release_agent.tools.queue_release_intent. It lived in the
agent package for historical reasons, which meant the web app had to import from
the agent to queue a chart; it belongs beside the event log it writes to.

What it checks, in order: who is asking (identity) -> the build run URL -> the
JIRA ticket exists -> the run really built THAT chart:version -> the release
controls passed (CONTROL_PREFIXES only; a failed step that is not a control does
not block) -> then it writes the queued event.
"""
from __future__ import annotations

from typing import Any

from .gh_tools import invoke as _invoke_tool   # module-level: tests rebind this


def queue_release_intent(
    artifact: str,
    requested_by: str,
    prl1_only: bool = False,
    df_only: bool = False,
    note: str = "",
    jira_ticket: str = "",
    change_details: str = "",
    build_run_url: str = "",
    target_envs: str = "",
) -> dict[str, Any]:
    """Register an artifact for the NEXT release (the intake queue): chart:version,
    the requester's email, PRL1-only / Dataflow-only routing, target_envs (which
    environments the developer picked, e.g. "prd" or "prd,prl1" — recorded intent,
    since a Dataflow entry may name both pipelines and which one is triggered is
    decided at deploy time), optional note for
    DevOps, the change context — jira_ticket (e.g. REL-1234) and change_details
    (what changed and why) — and build_run_url, the GitHub Actions run that
    built the tag. build_run_url is REQUIRED: nothing is queued without it —
    the run is checked NOW: it must be the run that built exactly this
    chart:version (the tag that triggered it, or the tag its tag step logged),
    and only a run whose build succeeded and whose controls ALL passed is
    queued (build_verified=true). A failed build or
    control, a control that has not passed yet, or a run with no controls at
    all makes the chart INELIGIBLE (eligible=false, with failed_controls /
    failed_steps / open_controls listed) so the dev fixes and re-runs first.
    Re-queuing a chart replaces its version. When the portal knows who is
    signed in, that verified email is recorded and requested_by is ignored."""
    from release_agent import identity
    from release_agent.tools import release_queue as _rq

    requested_by, refused = identity.actor(requested_by, identity.current())
    if refused:
        return {"ok": False, "eligible": False, "error": refused}
    name, version = _rq._split_artifact(artifact)
    verified: bool | None = None
    warnings: list[str] = []
    run_url = str(build_run_url or "").strip()
    if not run_url:
        return {
            "ok": False,
            "error": (
                "The GitHub Actions run URL that built this tag is required — "
                "I check the build and RLFT/RFTL controls before anything is "
                "queued. Ask the developer for the run URL (…/actions/runs/<id>)."
            ),
        }
    # Every field is required, in BOTH lanes — the form enforcing it while the
    # chat tool did not would just move the gap. Asked for by name so the agent
    # can prompt for exactly what is missing.
    missing = [
        label for value, label in (
            (jira_ticket, "the JIRA ticket"),
        ) if not str(value or "").strip()
    ]
    if missing:
        return {
            "ok": False,
            "error": (
                f"Still needed before this can be queued: {', '.join(missing)}. "
                "Ask the developer for it — nothing was queued."
            ),
        }

    # Resolve the JIRA key before anything is written. A typo'd key would
    # otherwise be copied verbatim into the change record and only be noticed by
    # whoever audits it — so a key that does not exist is refused, while an
    # unreachable JIRA is only a warning (an outage must not stop a release).
    jira_issue = None
    ticket = str(jira_ticket or "").strip()
    if ticket:
        from release_agent.tools import jira as _jira

        if _jira.jira_configured():
            try:
                jira_issue = _jira.get_issue(ticket)
            except _jira.JiraIssueNotFound as e:
                return {
                    "ok": False,
                    "error": (
                        f"JIRA ticket {ticket} could not be found ({e}). Check the key — "
                        "it goes into the change record. Nothing was queued."
                    ),
                }
            except _jira.JiraUnavailable as e:
                warnings.append(f"Could not verify {ticket} against JIRA ({e}) — queued unverified.")
            else:
                # The developer already told JIRA what changed; don't make them
                # type it twice.
                if not str(change_details or "").strip() and jira_issue.get("summary"):
                    change_details = jira_issue["summary"]

    # Checked AFTER the JIRA lookup, so a resolvable ticket satisfies it: the
    # developer already described the change once, in JIRA.
    if not str(change_details or "").strip():
        return {
            "ok": False,
            "error": (
                "Still needed before this can be queued: what changed and why. "
                "It drafts the change request on release day, so DevOps is not "
                "guessing. Nothing was queued."
            ),
        }

    # Eligibility gate: the dev pointed at the exact run — judge it.
    try:
        report = _invoke_tool("get_build_report", {"workflow_url": run_url})
    except Exception as e:
        report = {"found": False, "reason": str(e)}
    if not report.get("found"):
        return {
            "ok": False,
            "error": (
                f"Could not inspect that run ({report.get('reason')}). "
                "Check the URL — it must be a GitHub Actions run "
                "(…/actions/runs/<id>) in the build repo. Nothing was queued."
            ),
        }
    # One run, one artifact: the run's controls vouch only for what it built.
    from release_agent.config import settings as _settings

    run_id = (report.get("run") or {}).get("id")
    if _settings.queue_require_run_match and run_id and report.get("repo"):
        from release_agent.tools.controls import match_run_to_artifact

        match = match_run_to_artifact(report["repo"], run_id, name, version)
        if not match.get("ok"):
            return {
                "ok": False,
                "eligible": False,
                "artifact": f"{name}:{version}",
                "run_url": (report.get("run") or {}).get("url") or run_url,
                "built_tags": match.get("built") or [],
                "error": match.get("reason") + " Nothing was queued.",
                "reason": match.get("reason"),
            }
    # `controls` is the source of truth for the VERDICT — deriving from it means a
    # report without the convenience keys still refuses an ineligible build,
    # rather than reading as "no failures" and queueing it.
    controls = report.get("controls") or []
    failed_detail = report.get("failed_controls") or [
        {"control": c.get("control"), "job": c.get("job"), "conclusion": c.get("conclusion")}
        for c in controls if c.get("failed")
    ]
    open_detail = report.get("open_controls") or [
        {"control": c.get("control"), "job": c.get("job"),
         "status": c.get("status"), "conclusion": c.get("conclusion")}
        for c in controls if not c.get("passed") and not c.get("failed")
    ]
    # QUEUE_ALLOWED_FAILING_CONTROLS: some controls may fail without stopping the
    # chart — it is queued, but recorded and shown as failed. Everything else
    # still refuses, and a control implemented as a whole JOB takes its own
    # failed steps with it (they are that control's failure, not a second one).
    from release_agent.tools.controls import allowed_to_fail

    allowed_detail = [c for c in failed_detail if allowed_to_fail(str(c.get("control") or ""))]
    failed_detail = [c for c in failed_detail if c not in allowed_detail]
    failed_controls = [c.get("control") for c in failed_detail]
    # ELIGIBILITY IS DECIDED BY RELEASE CONTROLS ONLY (CONTROL_PREFIXES, i.e.
    # RCTLDEF…). A failed step that is NOT a control — "Generate HCC Report", an
    # image scanner, a notify step — does NOT block the release; it is reported
    # as a warning so it stays visible. The run's own conclusion is not a gate
    # either: one such step turns the whole run red, and refusing on that put
    # the release at the mercy of steps the release process does not govern.
    other_failures = [s for s in (report.get("failed_steps") or []) if isinstance(s, dict)]
    if failed_controls:
        return {
            "ok": False,
            "eligible": False,
            "artifact": f"{name}:{version}",
            "run_url": (report.get("run") or {}).get("url") or run_url,
            "run_conclusion": (report.get("run") or {}).get("conclusion"),
            "failed_controls": failed_controls,
            "failed_controls_detail": failed_detail,   # name + job, for "which one, where"
            "failed_steps": other_failures,            # context only — these did not refuse it
            "gate": report.get("gate"),
            "reason": (
                "This build is NOT eligible for the release — "
                + ", ".join(str(c) for c in failed_controls)
                + " failed. Fix it, re-run the build, then queue again with the new run."
            ),
        }
    # PASS = every control passed; with allowed failures, every OTHER one did.
    allowed_failures = [
        f"{c.get('control')}{' in job ' + c['job'] if c.get('job') and c.get('job') != c.get('control') else ''}"
        for c in allowed_detail
    ]
    verified = report.get("gate") == "PASS" or (bool(allowed_detail) and not open_detail)
    if not verified and _settings.queue_require_controls_pass:
        # Only a PASS queues. "Nothing failed" is not "passed": a control still
        # running (or skipped) has proven nothing yet, and a run where no step
        # or job matched the control prefixes has no controls at all — both
        # used to queue with a warning, which let an unchecked build into the
        # release. Say which of the two it is; the fix differs.
        if open_detail:
            named = ", ".join(
                f"{c['control']}{' in job ' + c['job'] if c.get('job') else ''} "
                f"({c.get('status') or c.get('conclusion') or 'not run'})"
                for c in open_detail
            )
            reason = (f"{len(open_detail)} control(s) had not passed in that run: {named}. "
                      "Every control must pass before a chart can be queued — let the run "
                      "finish (or re-run it), then queue with a run where they all pass.")
        else:
            prefixes = ", ".join(_settings.control_prefixes)
            reason = (f"No step or job in that run matched the control prefixes ({prefixes}), "
                      "so nothing shows the controls ran — it cannot be queued. Use the run of "
                      "the build workflow that carries the controls, or ask DevOps to check "
                      "CONTROL_PREFIXES.")
        return {
            "ok": False,
            "eligible": False,
            "artifact": f"{name}:{version}",
            "run_url": (report.get("run") or {}).get("url") or run_url,
            "gate": report.get("gate"),
            "open_controls": open_detail,
            "error": reason,
            "reason": reason,
        }
    if report.get("gate") == "UNKNOWN":
        # UNKNOWN has two very different causes and the developer's next step
        # differs, so never report them with one message. Saying "no controls
        # found" when controls exist but are open reads as the opposite of the
        # truth; saying "still running" when NOTHING matched hides a naming
        # mismatch that makes an ungated build look clean.
        if open_detail:
            named = ", ".join(
                f"{c['control']} ({c.get('status') or c.get('conclusion') or 'not run'})"
                for c in open_detail
            )
            warnings.append(
                f"Queued, but {len(open_detail)} control(s) had not passed in that run: "
                f"{named}. Re-queue with a run where they pass before release day."
            )
        elif not controls:
            prefixes = ", ".join(_settings.control_prefixes)
            warnings.append(
                f"Run succeeded but NO step or job matched the control prefixes "
                f"({prefixes}) — this build is queued ungated. Check the control "
                f"names in that workflow."
            )
    run_tag = str(report.get("tag") or "")
    if (not _settings.queue_require_run_match and version and run_tag
            and version not in run_tag and name not in run_tag):
        warnings.append(
            f"The run built '{run_tag}', which doesn't obviously match {name}:{version} — double-check the URL."
        )
    result = _rq.add_intent(
        artifact=artifact,
        requested_by=requested_by,
        prl1_only=prl1_only,
        df_only=df_only,
        note=note,
        build_verified=verified,
        jira_ticket=jira_ticket,
        change_details=change_details,
        build_run_url=run_url,
        target_envs=target_envs,
        allowed_failures=allowed_failures,
    )
    if result.get("ok"):
        result["eligible"] = True if verified else None
        if allowed_failures:
            result["allowed_failures"] = allowed_failures
            warnings.insert(0, (
                f"Queued. {', '.join(allowed_failures)} is OPEN — it failed on the build run "
                "and may be a false positive, so it is allowed (QUEUE_ALLOWED_FAILING_CONTROLS) "
                "and shown as open in the release queue until someone closes it manually. "
                "The release is not stopped by it."))
        if other_failures:
            # Not a refusal — but it must not be invisible either: the build did
            # have a red step, and whoever approves the release should know.
            named = ", ".join(
                f"{f.get('name')}{' in job ' + f['job'] if f.get('job') else ''}"
                for f in other_failures)
            result["failed_steps"] = other_failures
            warnings.append(
                f"Queued anyway: {named} failed on that run, but it is not a release "
                "control, so it does not block the release. Worth a look before you ship.")
        elif not report.get("run_succeeded"):
            # Red run, no failed control and no failed step to point at — a
            # workflow/startup-level failure. It does not block (controls decide)
            # but queueing silently off a red run would hide it.
            warnings.append(
                f"Queued anyway: that run's overall conclusion is "
                f"'{(report.get('run') or {}).get('conclusion') or 'not success'}' even though "
                "every release control passed — no individual step failure was recorded. "
                "Worth opening the run before you ship.")
        if warnings:
            result["warnings"] = warnings
        # The UI lists these by name; the sentence in warnings is the chat lane's
        # rendering of the same fact.
        if open_detail:
            result["open_controls"] = open_detail
        result["run_url"] = (report.get("run") or {}).get("url") or run_url
        if jira_issue:
            result["jira"] = jira_issue
    if result.get("ok"):
        result["build_verified"] = verified
        # Only with a queue configured: without one this read still reached
        # BigQuery — "<project>..release_intents" — and failed silently on
        # every queue call (found as bursts of notFound jobs in the job log).
        try:
            if not _rq.queue_enabled():
                raise LookupError("no release queue configured")
            events = _rq._fetch_events()
            result["last_shipped"] = _rq.last_shipped(events, name)
            result["last_time_flags"] = _rq.last_queued_flags(events, name)
        except Exception:
            pass
    return result
