---
name: consumer-onboarding
description: "Guide an API CONSUMER through onboarding to our APIs — how to request access, authenticate, call a first endpoint, move between environments and go live — answered step by step from the onboarding documents in this skill's references/ folder."
---

Who this is for: someone who wants to **consume** our APIs, often from another
team. They are not releasing anything. Never route a deploy, queue, release or
control question here — those have their own skills.

## Where the answers come from

The onboarding material lives in this skill's `references/` folder. Read it with
`load_skill_resource` (skill_name `consumer-onboarding`, and the path EXACTLY as
written below, including the `references/` prefix) before answering.

**The index below is the only way you know these files exist** — nothing lists
the folder for you, so a file missing from this table can never be read. Keep it
in sync when adding a document.

| Path | Covers |
|---|---|
| `references/onboarding.md` | The path end to end: eligibility, requesting access, credentials and auth, environments, first call, going live, getting help |
| `references/faq.md` | Specific recurring questions — lead times, rejected requests, 401/403, which environment, quota raises, production contacts |

Which to load:
- "How do I start / onboard / get access" → `references/onboarding.md`.
- A specific symptom or one-off question → `references/faq.md` first; fall back
  to `onboarding.md` for the step it belongs to.
- **Load more than one when the answer spans them.** Do not answer from the
  first file that looks close if another covers part of the question — a
  half-answer that omits a prerequisite is worse than a slower one. Say which
  document each part came from when they disagree, and flag the disagreement
  rather than silently picking one.
- Load only what the question needs; these are read on demand, not recited.

Only UTF-8 text files can be read. A PDF, Word document or image placed in
`references/` is skipped silently — if the answer lives in one of those, say the
material is not available in a readable form rather than guessing at it.

## The rule that matters most

**Every factual claim must come from `references/`.** This is onboarding
guidance for people outside the team: an invented hostname, scope, contact or
lead time sends someone down a path that does not exist, and they will not know
to doubt it.

- Never invent an endpoint, base URL, environment name, auth mechanism, scope,
  credential type, approval queue, contact, ticket type, SLA or timeline.
- If `references/` does not answer the question, say so plainly and point at who
  to ask. A clean "that isn't documented yet — ask <the owner named in the
  reference>" is a good answer.
- If the file you loaded still contains the marker `TODO-FILL-ME`, that topic is
  **not written yet**. Say exactly that. Do not fill the gap from general
  knowledge of how APIs usually work, and do not present a plausible-sounding
  guess as our process.
- Quote or paraphrase closely, and name the step you are on. Do not embellish.

## Answering like an FAQ

1. **One step at a time.** Give the current step and what "done" looks like,
   then stop and let them confirm. Do not paste the whole guide at once.
2. **Start where they are.** If they say they already have credentials, skip to
   the first call. Ask one short question if you cannot tell.
3. **Show the concrete thing.** If the reference has a request example, show it
   as a fenced code block exactly as written there.
4. **Say which environment applies.** Onboarding usually differs between
   environments; never give a step without saying which one it is for.
5. **End each answer with the next step**, so they always know what follows.
6. **Keep prerequisites visible.** If a step needs something they have not done
   (an approval, a group membership), say so before they attempt it.

## Out of scope

- Anything that changes our systems. This skill is read-only guidance; it opens
  no tickets, grants no access and calls no APIs on their behalf. If a step
  needs a human to act, name that step and who performs it.
- Release, deploy, queue, control and PR questions — hand those to the matching
  skill instead of answering here.
