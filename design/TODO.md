# Parked work and open decisions

One list, so nothing lives only in a chat transcript. Each item says what it
is, what it needs to restart, and what would make it worth restarting.

## Parked — designed, not built

### Alert noise reduction — `design/ALERT_HYGIENE.md`
Analyse one team's Cloud Monitoring incidents (multi-tenant project, scoped by
`team` label or name prefix), rank repetitive self-closing alerts, suggest
condition changes, replay-test each against 90 days of metrics. Reads only;
writes nothing; Terraform is where the team applies a suggestion.
- **Needs to restart:** `roles/monitoring.viewer` on the project, plus one
  scope key (a `user_labels.team` value is best; a display-name prefix works).
- **First step:** stage 0 — a one-hour, one-off script, same scope filter,
  no code in the repo. Returns the top five noisy policies with count, per
  week, median duration, self-closed %, peak hour.
- **Worth continuing if:** > ~30% of incidents self-close within 5 minutes,
  or the top three policies produce more than half the volume. Otherwise
  a quarterly by-hand review is enough.
- **Known specifics:** alerts go to email, nobody acknowledges — "real" is
  four measured signals (long-lived, large, co-firing, not self-healing),
  never a label. Most policies are log-based metrics templated per service —
  the report collapses a template into one row and one suggestion.

### Image vulnerabilities (JFrog Xray) — parked 2026-09-26
Scanned 2026-09-24 with pip-audit, osv-scanner, grype and trivy (the union
approximates Xray's database). Parked by choice before any fix was tested.
- **App libraries — all transitive, all fixable:** anyio 4.14.1 → 4.14.2
  (CVE-2026-63374 9.3, -63349, -64847), cryptography 49.0.0 → 50.x
  (CVE-2026-69247 / GHSA-g6cj-pr64-35w5, 8.2 — a MAJOR bump: check pyOpenSSL's
  cap and the RS256 path in identity.py), pyasn1 0.6.3 → 0.6.4
  (CVE-2026-59884/-59885/-59886). The untested lock change is in
  `git stash` ("parked: security upgrade …"); redoing it from scratch is
  `uv lock --upgrade-package anyio --upgrade-package cryptography
  --upgrade-package pyasn1`, then add `[tool.uv] constraint-dependencies`
  floors, re-export requirements.txt, full tests, rescan.
- **Base image's own system-Python libraries (unused by the app, which runs
  from /opt/venv):** pip 24.0, setuptools 79.0.1, wheel 0.45.1,
  jaraco.context 5.3.0 (9 findings, 2 HIGH). Fix: remove them — and the
  ensurepip bundled wheels — in the runtime stage's existing RUN, no apt.
- **Debian OS layer:** 156 (44 HIGH), none with a released fix. Nothing to
  upgrade today; rebuild on the current slim tag as Debian ships fixes, and
  record Xray ignore rules for packages the app never uses.
- **Clean:** vendored Chart.js 4.4.9 and Font Awesome 6.5.1.
- **Needs to restart:** the scan output was local-only; re-run the four
  scanners (all install via Homebrew / `uvx pip-audit`) and build the image
  locally to scan the final layers, the way Xray does.

## Deferred by choice (2026-09-26)

### CARE release as one committed file — the reviewers' safeguards not built
Built: `CARE_RELEASE_MODE=mono` (`tools/care_release.py`) splices the release
into the one committed file, raises a PR the portal never merges, names who
raised it, and drains the queue when `pr_reconcile` sees the merge. Kept
low-risk on purpose: the person releasing owns the wording and the PR review
is the check. What the plan reviews asked for, and why each would matter:
- **Server-side re-validation of prose and provenance** — the form's checks run
  in the browser; JSON pasted into chat reaches the file with only
  `validate_release`'s shape checks (a chart that never passed the queue gate,
  any prose).
- **Evidence table in the PR** — per chart: previous → new version, ticket,
  developer text, run URL, controls. Without it the reviewer has nothing to
  check the summary against.
- **Segregation-of-duties checks** — with the server token the bot authors the
  PR, so the requester can approve and merge their own release; nothing reads
  the PR's reviews to flag it.
- **Action-run tracking after merge** — `released` means merged into the base,
  not deployed: a failed workflow run still drains the queue and nobody is told.
- **Drafted-by provenance event** — nothing durable records which fields a
  model drafted and which a person wrote (model and prompt version, facts hash).
- **Action trigger / env-var prerequisites** — the repo's workflow must run only
  on a push to the base branch and read the prose through env vars, never
  `${{ }}` inside `run:`; otherwise a branch push or shell-quoted prose acts
  before anyone reviews.
- **Team branch prefix + PR label** — `release/` can match other teams'
  branches in a shared repo (a false "release in flight"); a team namespace and
  a label would make the guard exact.
- **Withdraw-while-in-PR guard** — a chart withdrawn while its release PR is
  open stays in the file; the merge ships it and reconcile marks it released.
- **Status-banner repo split** — the banner still reads the deploy repo's
  guard branches, so an open mono-repo release PR does not show as in flight.

## Built — 2026-09-19

### BigQuery cost report — `design/BQ_COST.md`
Shipped as the **BQ cost report** pill in its own *Monitoring* group (the
"different audience" question was answered with a group of its own, not a
separate agent), the `bq-cost` skill and `/api/bq-cost/*` — released, not
preview (the PromQL checks stay in the preview *Check* row). What the
build learned that the design did not know is recorded at the top of
`BQ_COST.md` (anonymous datasets deny region-wide views, no partition count
in `JOBS`, parameterised dry runs price as 0 bytes, the scan labels and
excludes its own jobs).
- **Still true:** no JVM / no ZetaSQL; the model narrates and proposes, the
  numbers come from `INFORMATION_SCHEMA` and dry runs only; nothing applies
  itself.
- **Not built (by choice):** the weekly `CronJob` + Teams/email post from
  §8 — the on-demand pill and chat are enough until someone asks for a
  digest. The entry point exists (`python -m release_agent.tools.bq_cost`).
- **Enterprise switch-on:** see the follow-ups below.

## Built — 2026-09-20

### Agent observability: Langfuse over OpenTelemetry
`adk_release_agent/telemetry.py` replaces the hand-written JSONL tracer
(`tracing.py`, its test, `evals/route_stats.py` — all deleted). ADK's own
spans (turn → model calls with token usage → tool calls, Workflow nodes) go to
Langfuse when `LANGFUSE_HOST` + the two keys (Secret) are set; the standard
`OTEL_EXPORTER_OTLP_TRACES_*` variables reach any other sink unchanged;
nothing set = off. `TRACE_CONTENT` (default false) keeps prompts, arguments
and results out of the spans. The router's decision is the root `turn` span
carrying session/user ids. Verified against a local OTLP receiver (Langfuse
itself needs the project's keys — first real run is the owner's).

## Known and left alone — 2026-09-21 (after the parallel-testing sweep)

Everything the sweep found is fixed except these, each a decision rather than
an oversight. Recorded so nobody re-investigates them.

- **`?fresh=1` has no rate limit.** The refresh button can re-scan repeatedly.
  Left because a scan is ~42 MB of INFORMATION_SCHEMA (≈$0.00025), the cache is
  single-flight so concurrent refreshes coalesce, and a scan takes 16–50 s so
  sequential clicks queue behind it. A minimum-interval floor would make
  "refresh now" quietly not refresh, which is the worse trade.
- **A failed scan is cached for the full 5 minutes**, so a transient BigQuery
  failure sticks until someone refreshes. Deliberate: it stops a denied or
  broken BigQuery being hammered once per pill-open.
- **`ERROR opentelemetry.context: Failed to detach context`** appears in the
  log on deploy previews when tracing is on. NOT ours: an A/B against the
  previous commit with identical workload gave identical counts, and every
  frame belongs to `openinference.instrumentation.google_adk` or ADK's own
  runner utils. Upstream bug; suppress or report it if it ever matters.
- **The Langfuse key lives in `os.environ`** (as `OTEL_EXPORTER_OTLP_TRACES_HEADERS`)
  because that is the OTLP exporter's contract. `/api/diagnostics` never shows
  it, but an env dump in a child process would.
- **The preview-pill invariant test reads `form:` pills only**, so a `send:true`
  pill whose chat text reaches a gated tool is invisible to it. The server still
  refuses, so the worst case is a visible pill that answers "not yet".
- **The writes section counts the tool's own streaming inserts** when
  `BQ_COST_DATASET` is set. `STREAMING_TIMELINE_BY_PROJECT` carries no table
  identifier, so they cannot be subtracted the way the ranking subtracts its own
  jobs by label. A few rows per scan: small and unattributable, not wrong.

## Open — needs a working mesh to finish (2026-09-21)

### Managed CSM: sidecars never receive Traffic Director config
Deployed this chart twice to a fresh GKE Autopilot cluster with managed CSM.
Both times the injected Envoy reported `Traffic Director configuration was not
found for mesh "gsmrsvd-..."` and stayed `1/2`; the ingress gateway did the
same and served 502. Ruled out: deploy ordering (the second run waited for the
fleet to report ACTIVE and for the TD Mesh resource to exist — it failed
identically), and the documented remedy for the fleet's own
`MISSING_CONTROL_PLANE_CONFIG` warning (a `ControlPlaneRevision` named
`asm-managed` already existed, reconciled). A `kubectl rollout restart` cleared
it once and not the second time. Suspect the sandbox fleet's pre-existing
features (configmanagement, policycontroller, metering — all ACTIVE from an
older PoC) or a TD serving-side delay; a support case is the next step.
- **Consequence if it happens in the enterprise:** every deploy hangs at 1/2,
  `helm upgrade --wait` times out, and the app loses egress to the metadata
  server, so Vertex fails with a generic error. See the helm README.
- **Still unproven because of it:** whether the VirtualService's 120s route
  timeout cuts a long chat turn, and what the client sees when it does. A
  96s SSE stream DID pass through the app's own sidecar intact (no truncation),
  which is the only evidence gathered. The harness to finish this is written:
  scratchpad `gke/sse/sse_probe.py` (N concurrent streams, detects truncation
  and cross-talk, exit 1 on either) plus a 10s-timeout values fragment to force
  the timeout deliberately rather than waiting for a slow turn.
- **Worth knowing for that test:** the app calls Gemini with `stream: False`,
  so a turn's whole reply arrives as ONE token event — the wire is silent for
  the length of the call, which is what an idle timeout would act on.

## Skipped — deliberately

### Grafana-dashboard weekly memo
A narrative memo generated from a dashboard's PromQL panels. Skipped because
a memo has no action at the end of it and would go unread; the useful part
(week-over-week drift per service) only pays off joined to the release log,
and the alert-noise work above is the better use of the same access. The
research and the dashboard analysis (42 panels, 6 template vars, thresholds
mostly Grafana's default 80) are in the 2026-09-19 session transcript.

## Open decisions from the codebase review (2026-09-17)

- **`ScopeGuardPlugin`** — 181 lines, shipped as `SCOPE_GUARD: "log"`: pays a
  Gemini classifier call per off-topic-looking message and refuses nothing.
  Either set `enforce` or delete it. Decision, not a bug.
- **`config.py` alias collapse** — 63 of 169 env-alias spellings are unused;
  81 of 91 fields could be one-liners. ~230 lines. Not done because a
  removed spelling could break an env var the cluster relies on; do it when
  the ConfigMap can be audited alongside.
- **Vertex sessions / cross-process CONFIRM resume** — ~130 lines that only
  matter with `ADK_SESSION_BACKEND: vertex` and `replicaCount > 1`. Commit
  to it (then fix the replica count) or delete it and state that a restart
  cancels pending confirmations.
- **Inline HTML shell in `app_fastapi.py`** — 189 lines of markup in a
  Python string. Move to `static/index.html`; keep the server-side
  `{PORTAL_UI}` injection. Net zero lines, easier to edit.
- **`values.yaml` prose** — comments outnumber settings 2.6:1; the helm
  README already tabulates the keys. A move, not a delete: the CA-bundle and
  PDB notes are hard-won.

## Enterprise follow-ups (owner's side)

- Re-copy `adk-release-agent` — the DF-dispatch fix (`ab781f7`) and the
  controls-only queue gate (`39a030b`) postdate the last copy.
- Run the DF dispatch diagnostic to read GitHub's refusal reason (it is a
  scratch script; ask to have it committed as `scripts/df_why.py`).
- Confirm `QUEUE_ALLOWED_FAILING_CONTROLS: "1691"` is set in the ConfigMap —
  the allowance cannot apply if the key is empty.
- Confirm the `BUILD_TAG_STEP` / `BUILD_TAG_MARKER` values in the ConfigMap
  match what the pipeline logs (`Create new tag` / `New tag is:`).
- BQ cost report switch-on: grant the portal's GSA
  `roles/bigquery.resourceViewer` + `roles/bigquery.metadataViewer` +
  `roles/bigquery.jobUser` on the team's BigQuery project (no `dataViewer`);
  set `BQ_COST_REGION: "region-europe-west3"`, `BQ_COST_PROJECT` if it is
  not the Vertex project, no preview keys needed — it is released
  (add `bq-cost` / `Monitoring` to the preview keys only to restrict it). Optional memory: the
  `bq_cost_findings` table via the terraform variable + `BQ_COST_DATASET`.
- GCP: `sql-553@…` has a user-managed key — delete it if unused.
- `backstage_poc` branch: `eod1` in `backstage/app-config.yaml` console URLs
  (internal name on a public repo).
