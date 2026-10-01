---
applyTo: "src/release_agent/static/**"
---
# Frontend (plain ES modules, no build step)

Three layers — see `static/README.md`:

- **`core/*.js` — rules.** Pure functions: no DOM, no `fetch`, no globals. Any
  decision or sentence the Backstage port would also need lives here, with a
  test in `tests/js/` (`node --test`).
- **`api.js` — every backend call.** Screens never call `fetch` directly.
- **Screens** (`forms/*.js`, `chat.js`, `status.js`, `palette.js`, …) render
  and keep per-card state only.

Conventions:

- Labelled fields use `labeledField(parent, spec)` from `forms/common.js`.
- Text from the server or from GitHub (PR titles, comments) is rendered with
  `renderMarkdown` (it escapes HTML first) or `escapeHtml` — never raw `innerHTML`.
- Bare URLs are split from trailing punctuation by `core/format.js` `splitUrl`.
- Libraries are vendored under `static/vendor/` — no CDN. Tailwind is a
  prebuilt file: only classes already in `vendor/tailwind.css` work; a new one
  needs `scripts/css/build.sh` and the regenerated CSS committed.
- Feature visibility comes from `window.PORTAL_UI` (server-side
  `features.ui_config`), e.g. `llm` and preview groups.
- The deploy and release forms send a required `jira` (`core/jira.js`).
