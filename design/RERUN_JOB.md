# Re-run a job — design

Status: built behind the preview feature `rerun-job` (Release group pill
"Re-run a job"). Code: `src/release_agent/tools/rerun_job.py`,
`src/release_agent/rerun_api.py`, `static/forms/rerun_job.js`,
`static/core/rerun.js`.

## The problem

Ten images are queued for the next release, and each one's build run has the
same control job failing. A known false positive such as 1691 is let into the
queue as "open" (`QUEUE_ALLOWED_FAILING_CONTROLS`). Today someone opens ten run
pages and clicks "Re-run job" ten times. They also have to work out which runs
are shared, which are too old, and which would rebuild an image. Then they
watch ten tabs.

## What it does

The person types **any job or step name**, for example `sdlc-peer-review`,
`unit-tests` or `RCTLDEF0001691`. The name is matched the way controls are
matched (`controls.control_matches`): `1691` finds `RCTLDEF0001691`, and
`*scan*` is a wildcard. The person ticks the queued images, and the portal
does four things:

1. **Preview (read-only).** It reads each distinct build run once. Images built
   by the same run share one re-run. Each image gets exactly one answer:

   | answer | when |
   |---|---|
   | will re-run job X | the run is complete, ≤ 30 days old, has exactly one job matching the name, and that job did not pass |
   | already passed | the job succeeded. It is offered only with "include passed" |
   | still running | the run or one of its attempts is in progress. GitHub refuses a re-run until it completes |
   | too old | the run is more than 30 days old. GitHub refuses it, so it needs a new build |
   | refused: rebuilds | the matched job, or a job that depends on it (from the workflow file's `needs`), is named like a build or publish job (`RERUN_PROTECTED_JOBS`). Re-running would rebuild or republish the image |
   | ambiguous | the name matches more than one job. The answer lists them so the person can type the full name |
   | no such job | nothing in that run is named like that |
   | unreadable | the run could not be read: no run URL, 404, network error |

   The preview also shows the GitHub budget: how many API calls the re-runs
   will use and how many the token has left before its reset time. If the
   budget does not cover them, the preview says how many will start.
2. **Yes / No.** The preview returns a single-use `plan_id`. It is bound to
   the caller (their verified email, else the typed one) and the chat thread,
   and it expires after 10 minutes. Start spends the plan before anything is
   sent to GitHub. What starts is exactly what was previewed. A second click,
   or a start from someone else, is refused.
3. **Start.** A background thread sends one `POST
   /repos/{repo}/actions/jobs/{job_id}/rerun` per run, **one second apart**.
   GitHub asks for a gap between writes, to avoid its secondary limit. The
   card polls `/api/rerun/{id}` every 3 s and shows a line per run as it
   happens. The work outlives the tab.
4. **Follow and tell.** The thread polls each started run until it completes.
   It uses conditional requests (`If-None-Match`), and a 304 costs no rate
   limit. It then reads the job's new attempt and records passed or failed
   again, with the failed step's name. It raises **one bell notification for
   the whole batch** ("sdlc-peer-review re-run finished: 7 passed, 1 failed
   again, 2 not started"). The notification links to the result card
   (`portal:rerun:<id>`).

The result card has two buttons:
- **Re-run the N that failed again** opens the preview again for just those
  images. It needs a new yes.
- **Start the N not started** does the same for the ones a limit or error
  stopped.

Nothing is retried automatically.

When an image's run passes its controls after the re-run, the queue is told.
A `reverified` event (append-only, invariant 3) replaces that image's
`allowed_failures` with whatever still fails, and the Controls column turns
green. Nothing leaves the queue, and nothing is re-gated.

## What it never does

- It never re-runs a protected job: a build, publish, release or deploy job,
  or a job a protected one depends on.
- It never re-runs a whole workflow and never cancels anything.
- It never retries a write by itself.
- It never re-runs a run that is still in progress, which GitHub refuses
  anyway.
- It touches no deployment, PR or release file. Invariant 1 is unaffected,
  because a CI re-run is not a deployment.

## Scale: what 10 or 50 images cost

| phase | GitHub calls |
|---|---|
| preview | 3 per distinct run (run, its latest-attempt jobs, its workflow file at that commit, cached per file and commit) |
| start | 1 POST per run, 1 s apart (50 runs ≈ 50 s, shown line by line) |
| follow | 1 conditional GET per unfinished run per poll (304 is free), plus 1 jobs read when it completes |

So 10 images sharing 8 runs cost about 24 + 8 + 16 calls. 50 runs cost about
350 calls out of a PAT's 5,000 per hour. `RERUN_MAX_RUNS` (default 50) caps a
batch. The poll gap stretches when the budget runs low (`poll_delay`, pure): 30 s
normally, and longer as the remaining calls approach the reserve.

## When GitHub says no

Every answer is classified (`classify`, pure) from the status code, the
`x-ratelimit-*` and `retry-after` headers, and the message:

| GitHub answer | what the person sees | what the batch does |
|---|---|---|
| primary limit (403/429, `x-ratelimit-remaining: 0`) | "not started — GitHub API limit reached; resets at 14:52" | stops starting; every remaining run is marked not started for that reason |
| secondary limit (403/429 "secondary rate limit", `retry-after`) | "not started — GitHub asked us to slow down; try again after 14:31" | stops starting (no automatic retry of a write) |
| no permission (403 "Resource not accessible…", 404 on the POST) | "not started — this token cannot re-run workflows in owner/repo (needs Actions: write)" | stops starting in that repo only; other repos carry on |
| too old (403 "…over a month ago") | "not started — run older than 30 days" | next run |
| already running (403/409 "…already running") | "not started — already re-running" | next run |
| network error / 5xx | "not started — GitHub could not be reached: …" | next run |
| limit hit while following | "stopped following — GitHub API limit; resets at 14:52" (the runs carry on, with links) | waits for the reset if it falls before the deadline, otherwise ends and notifies with what it knows |

The budget check runs before the first write. `/rate_limit` is free. If
`remaining - RESERVE` cannot cover the starts, only the ones that fit are
started. The rest are "not started — not enough GitHub API calls left
(N left, resets at …)". It never starts writes it already knows will be
refused.

## Tokens and identity

- Like the chat: a connected session PAT for the given thread wins (with an
  owner check), else the server's token. The re-run is attributed in GitHub to
  that token's user.
- The follow thread keeps the token it started with. It stops quietly as soon
  as the thread no longer holds its session PAT (invariant 5, the same rule as
  `df_run.py`). It never follows for longer than `SESSION_IDLE_MINUTES`.
- The requester is the verified caller, else the typed email (marked
  unverified), as the queue writes do. The bell goes to that email.

## State

- Plans: in memory, per process, with a 10-minute expiry. On more than one
  replica, a start that lands on another pod says "preview again".
- Batches: in memory while they run, plus rows in `portal_events` (domain
  `rerun`, subject `rerun:<id>`, latest row wins, `details` = the batch JSON).
  A row is written when the batch starts, when starts are done, and when it
  finishes. If the card reads a batch that its row says is running but this
  process does not hold, it says "no longer followed (the portal restarted)
  — open the runs".
- Queue: `reverified` events in `release_intents`, which only `reduce_queue`
  reads.

## Settings

| setting | default | meaning |
|---|---|---|
| `RERUN_PROTECTED_JOBS` | `build,publish,release,deploy` | words. A job whose name (its last ` / ` segment) contains one of them as a word is never re-run, alone or as a dependent |
| `RERUN_MAX_RUNS` | `50` | most distinct runs in one batch |

## Testing

- Unit: the pure pieces (match, protect, plan, budget, classify, poll_delay,
  summary), and the engine against a scripted fake GitHub. The fake covers
  primary and secondary limits mid-start, a permission refusal in one repo of
  two, a run going too old, already running, a network drop, a limit while
  following, the token dropping mid-follow, and a restart.
- Live (public sandbox repo with neutral names): 30 queued images over 25
  runs of a workflow whose control job fails on attempt 1 and passes on
  attempt 2, except where `-stuck` is in the tag. The sandbox also has a
  matrix run building three images, and a workflow whose control is a step
  inside `build-and-publish`.
- Live API limit: the engine runs **inside GitHub Actions** with the repo's
  `GITHUB_TOKEN`, which allows 1,000 calls an hour per repo. One job burns the
  token down to a handful of calls and then previews and starts a batch: the
  real primary-limit answers. Another job runs with `permissions: actions:
  read`: the real permission refusal.
