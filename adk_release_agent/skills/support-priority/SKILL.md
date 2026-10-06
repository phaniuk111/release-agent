---
name: support-priority
description: "The support team's own priority policy in plain English: how urgent a pipeline incident is (high, medium, low) and why. Load it when someone asks what the policy is or how priorities are decided; the support_triage and investigate_evidence results already carry the same text as `priority_policy`."
---

This skill is the support team's priority policy. **The team edits the policy
below in plain English** — no code, no special syntax. Write it the way you
would brief a new L1 colleague. The rest of this file says how the policy is
used and should rarely need changing.

## The team's priority policy

High — someone should look now:
- A report or feed that another team or a regulator is waiting on today did not
  produce its output.
- Many accounts or books failed together because one upstream feed or one
  process broke.
- The same piece of work has failed on three or more business dates in a row.
- A run says it succeeded but produced no output, or far less than usual —
  downstream users will read wrong numbers without knowing.

Medium — today, but it can wait for the high ones:
- Work is stuck, or work that ran yesterday never started today.
- Several runs failed but they share nothing obvious (no single feed or
  process), so it needs a look before anyone can say how bad it is.
- A single failure that has happened before.

Low — note it, follow the runbook:
- One run failed for the first time with a known, usually-transient cause
  (a quota limit, a lost worker) that a single re-trigger normally fixes.
- Anything already covered by a runbook entry whose action is just "wait".

When in doubt between two levels, choose the higher one and say what made it
close.

## How to use the policy

- Judge ONE incident at a time, against the policy above, using only the facts
  you were given (counts, what the runs share, history, runbook match, output
  volume, and — when investigating — the evidence). Never invent a fact to fit
  a rule, and never decide from the error text alone.
- Say it as one line: `Priority: HIGH — <one plain-English sentence naming what
  makes it that level>`. Write for an L1 person reading a queue: "12 accounts of
  the regulatory report are missing their data for today", not "count >= 10".
- The card already shows a level (`priority` / `priority_reason`). That is the
  portal's built-in rule, used when no AI is available. Only when your level is
  DIFFERENT, add it in brackets — `(the card says MEDIUM)` — so the person sees
  the two disagree; when they agree, say nothing about the card.
- A card reason of `critical: <value>` means the team listed that value as
  always important (SUPPORT_CRITICAL): treat it as High.
- The policy decides how urgent something is, not what to do about it: the
  action (wait / re-trigger / check / escalate) still comes from the runbook,
  the triage and the evidence.
