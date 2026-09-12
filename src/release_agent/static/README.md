# Portal frontend — layout, and how to port it to React

Plain ES modules, no build step, served by FastAPI's `StaticFiles`. The
Backstage plugin (branch `backstage_poc`) is a React port of the same screens
against the same backend; this layout exists so either UI can become the final
one without the rules drifting between them.

## Three layers

| Layer | Files | What it holds | In a React port |
|---|---|---|---|
| **Rules** | `core/*.js` | Wording and decisions both UIs must agree on: queue destination text, the PRD/PRL1/CARE/DF tick rules, submission validation, formatting. **Pure** — no DOM, no fetch, no globals. | Import unchanged (plain JS with JSDoc types; `allowJs` in TypeScript). |
| **API contract** | `api.js` | Every backend call, named per route. Network failure throws; an HTTP error returns `{ok:false, status, error}`. Screens never call `fetch`. | Reimplement the same functions over the plugin's fetch/`discoveryApi` — or import it and swap `API_BASE`. Request/response models: `app_fastapi.py` (pydantic) and `/openapi.json`. |
| **Screens** | `forms/*.js`, `chat.js`, `insights.js`, `status.js`, `links.js`, `connect.js`, `palette.js` | DOM rendering and per-card state only. | Rewrite as components; nothing else changes. |

`forms.js` is an index re-exporting the screens, so older import paths keep working.

## Rules for new code

- A decision or a sentence the Backstage plugin would also need goes in
  `core/`, with a test in `tests/js/` — not inline in a screen.
- A new backend call goes in `api.js`.
- A screen module renders; if it starts deciding, move the decision to `core/`.
- Escape every value that enters `innerHTML` with `escapeHtml` (JSX does this
  for you in the port).

## Tests

- `tests/js/*.test.mjs` — the `core/` rules, on Node's built-in runner
  (`node --test tests/js/*.test.mjs`; nothing to install). pytest runs them via
  `tests/test_ui_js.py`, which also syntax-checks every module here.
- Screens have no automated tests yet; they are checked in a browser.

## Porting a screen to React (e.g. the queue table)

1. Keep the calls: `getQueue()`, `withdrawFromQueue({artifact_name, artifact_version, requested_by})`.
2. Keep the words: `queueDestination(q)`, `timeAgo(...)`, `shortName(...)`.
3. Rewrite `forms/queue_table.js`'s rendering as a component; its state is the
   queue plus one pending "remove" row.
4. Behaviour to preserve (it lives server-side, so the port gets it for free):
   removing sends the version shown and is refused if the queue moved on.
