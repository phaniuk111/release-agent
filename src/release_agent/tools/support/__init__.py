"""Support: L1 triage of a workflow control table, and investigating one incident.

Nothing in this package knows a real table — the table, its columns, status
words and owners are configuration (SUPPORT_*; the chart's `supportTable:`
block). The runbook is a plain-English skill in the image
(adk_release_agent/skills/support-runbook). Everything is read-only.

    config.py        the table and column mapping: roles, parsing, the data project
    discover.py      only SUPPORT_TABLE required: the mapping and status words found
                     from the table itself when they are not configured
    queries.py       the statements (BigQuery counts; only problems come back),
                     run through tools/bq_guard.py; the live mapping check
    report.py        PURE: query results → what failed, how widely, new or recurring
    runbook.py       the runbook skill: its known issues for the cards, its text for the AI
    incidents.py     the L1 layer: runbook match, action, owner, priority, ticket note
    triage.py        the entry points: triage(), for_model()

    source_logs.py   a source workload's container logs and the audit logs
    dataflow_job.py  one Dataflow job: state, errors, grouped worker logs, kind
    release_lookup.py  what is deployed where, what changed before a date
    evidence.py      the Investigate collector: every read in parallel, the
                     bundle cache, what the model may see, the model-free answer
    signals.py       PURE: signals, leads, and the judgement (is a cause
                     established; which action a failure kind implies)
    timeline.py      PURE: the dated evidence as one time-ordered list
    cache.py         the ONE shared read cache (collector and chat tools)
    _common.py       small helpers they share (the window, text, times)
    feedback.py      "was this right?" — append-only, accuracy stats
"""
