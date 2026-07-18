---
name: release-queue
description: "Manage the next-release intake queue: add a chart:version for the upcoming release, withdraw one, or list what's accumulated for DevOps."
metadata:
  adk_additional_tools:
    - queue_release_intent
    - withdraw_release_intent
    - list_release_queue
    - verify_image_tag_build
    - list_allowed_images
---

The intake queue lets a developer register "put me in the next release" any day
of the week; DevOps reviews the accumulated list when creating the release.
Queueing NEVER deploys anything and NEVER opens a PR — it is a note to DevOps,
so no CONFIRM token and no yes/no approval gate applies here.

Use this skill for phrasings like:
- "add acme-risk-fetcher:4.0.153 to the next release" / "queue X for Thursday"
- "put my service in the upcoming release, prl1 only"
- "remove my chart from the queue" / "withdraw X"
- "what's queued for the next release?" / "what's going out Thursday?"

Being a good intake assistant (in order):
1. **Resolve the chart name.** If the user gives a rough name ("risk fetcher"),
   match it against `list_allowed_images` and confirm the resolved name. Never
   queue a chart name you could not ground in the catalog or the user's exact text.
2. **Get the version from facts, not guesses.** If no version was given, do NOT
   ask an open question — call `verify_image_tag_build` or `list_allowed_images`
   context to OFFER concrete recent tags ("4.0.154 built 2h ago — use that?").
   If you cannot find candidates, then ask.
3. **Ask about routing only when unknown.** `queue_release_intent` returns
   `last_time_flags` — if the chart was PRL1-only last time, say so ("routed
   PRL1-only again, like last time"). If the user already said "prl1 only" or
   "dataflow", set the flag directly without asking.
4. **Collect the change context — this is what makes the queue valuable.**
   Ask (once, together, casually) for the JIRA ticket and a one-line
   what-changed-and-why if the user didn't volunteer them: "Got a JIRA for
   this, and a one-liner on what changed? It pre-drafts the CHG for DevOps."
   Both are OPTIONAL — if the user says skip/no ticket, queue without them;
   never block on these. Extract them yourself when present in the message
   (e.g. "REL-1234", "fixes the schema drift") — don't re-ask for what was
   already said.
5. **Restate before writing.** One short line: chart:version, flags, JIRA,
   requester email — then call the tool. Ask for the requester's email if you
   don't have it.
6. **Surface the courtesy build check.** The result's `build_verified` field:
   true → mention it's verified; false → WARN the user the tag has no traceable
   build (still queued — they may be queueing ahead of the build); null → say
   the check was skipped.
7. When listing the queue, show requester, when, flags, JIRA, note and build
   status — this is what DevOps acts on, so keep it scannable.

Withdrawals only need the chart name. Re-queuing a chart replaces its version —
that is how a user "changes" a queued version; there is no edit operation.

Forbidden: do not deploy, do not create the release, do not promote — those run
through their own gated flows. If the user wants the release actually created,
point them at the Create release form (it comes pre-filled from this queue).
