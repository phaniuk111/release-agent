---
name: monitoring
description: "Answer questions about the team's monitoring: which PromQL checks are firing and why, and read-only metric questions (error rates, targets down, GCP API errors, Composer/Dataflow metrics) answered with PromQL."
metadata:
  adk_additional_tools:
    - monitoring_checks
    - query_metrics
---

Use this skill when the user asks what is alerting or firing, whether something
is healthy, why a monitoring check fired, or any question answered by metrics.

How to answer:
- "Is anything wrong / what is firing?" → `monitoring_checks`. Report each
  firing check by name with its series (labels and values), then the checks
  that could not run — an `unknown` check is NOT healthy, say so and give its
  hint. Only say "all clear" when every check is `ok`.
- "Why did check X fire?" → run `monitoring_checks`, then narrow down with
  `query_metrics`: break the same expression down by a label (`sum by (job)`,
  `topk(5, …)`), or look over a longer window (`[1h]`, `[1d]`).
- Other metric questions → `query_metrics`. Prefer aggregated queries (`sum by`,
  `count`, `topk`) so the answer fits; if `truncated` is true, say how many
  series there were and aggregate further rather than listing them.
- GCP services' own metrics are available through PromQL with the metric's
  domain rewritten, e.g. API errors by service:
  `sum by (service, response_code_class) (increase(serviceruntime_googleapis_com:api_request_count{monitored_resource="consumed_api",response_code_class!="2xx"}[1d]))`.

Rules:
- Facts come only from these tools. Never invent a value, a series or a cause —
  PromQL tells you WHAT is happening; say "the metrics show…" and keep a guess
  about WHY clearly labelled as one.
- When a query errors, show the error and the hint; do not retry the same query.
- For a deploy or release question the metrics raise ("did yesterday's deploy
  cause this?"), use the release-status skill for what changed and when.
