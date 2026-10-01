---
applyTo: "tests/**"
---
# Tests

- Run: `PYTHONPATH=src .venv/bin/python -m pytest -q`; JS: `node --test tests/js/*.test.mjs`.
- `tests/conftest.py` guards every test: the BigQuery queue is blanked, any real
  BigQuery query raises, and the deploy preview's GitHub check is stubbed. A test
  that needs real-looking data stubs the module it reads, never the network.
- Use the shared fakes in `tests/fakes.py` (`FakeRepo`, `FakePR`): they record
  every file write (`writes`), PR (`prs`) and merge (`merge_title`,
  `merge_message`), and `FakePR.get_files()` reports what a PR changes.
- A test's docstring says the behaviour and, when it came from a live finding,
  what was found. Name tests as sentences
  (`test_a_deploy_without_a_jira_is_refused`).
- Use neutral names only: `svc-a`, `orders-api`, `example-org/deploy`,
  `dev@example.com`, JIRA `ABC-1234`. Never real company, project or repo names.
- A thread started in a test does not inherit context variables (identity, the
  JIRA scope): run it with `contextvars.copy_context().run`, as
  `asyncio.to_thread` does in the app.
