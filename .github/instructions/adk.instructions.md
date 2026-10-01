---
applyTo: "adk_release_agent/**,src/release_agent/adk_service.py"
---
# ADK app and chat router

- **Router order** (`adk_service.stream_chat`): a pending yes/no approval or
  CONFIRM token on this (owner, thread) first → the deterministic deploy parser
  → (model on) the classifier → the chat agent; (model off, `LLM_ENABLED=false`)
  the commands Workflow.
- **What needs a yes** is defined once, in `approvals.py`, and shared by the chat
  agent and `commands_workflow.py`. Do not add approval rules anywhere else.
- **Deploys and releases** run `deploy_workflow.py`: gate (preview + `RequestInput`
  with the CONFIRM token) → apply / cancel. Pending state lives in ADK session
  state so any replica can resume it.
- **Workflow nodes never raise** — ADK re-runs a failed node on resume, repeating
  side effects. Catch and return the failure as the node's output.
- **Blocking work off the event loop**: `asyncio.to_thread` in nodes; chat tools
  are wrapped with `tools.off_event_loop`. A `FunctionNode` runs sync code on the
  loop, which stalls every user.
- **Type a node's input** (`node_input: str`, not `Any`): `FunctionNode` hands
  the message over as the annotated type; untyped it is the Content object.
- **Drain the runner** (`async for` to the end) rather than `break` at an
  interrupt — leaving early cancels the Workflow's leftover tasks.
- **Free-form chat cannot mutate**: `safety.MutationGuardPlugin` blocks
  release-defining tools outside the deterministic Workflow.
- **Skills** live in `skills/<name>/SKILL.md`: YAML frontmatter with `name`,
  `description`, and `metadata.adk_additional_tools` (the tools that skill
  unlocks). Instructions say what the tool returns; they never invent facts.
- A turn is `mutated` only when a state-changing tool RAN (`_landed_changes`) —
  a call that paused for approval, or a rejection, changed nothing.
