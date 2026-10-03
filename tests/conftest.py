"""Shared test guards.

The BQ intake queue resolves the GCP project from gcloud config even with no
env vars set, so without this fixture any test that exercises a deploy/release
apply path would write REAL telemetry events into the production BigQuery
table. Disable the queue for every test; tests that need queue behavior
monkeypatch release_queue's internals directly.
"""
import pytest

from release_agent.config import settings


@pytest.fixture(autouse=True)
def _disable_bq_queue(monkeypatch):
    monkeypatch.setattr(settings, "bq_dataset", "", raising=False)


@pytest.fixture(autouse=True)
def _no_real_bigquery(monkeypatch):
    """No test may send a query to real BigQuery. Blanking the dataset was not
    enough: a read that skipped the enabled check still reached the project the
    developer's gcloud points at, ten times a run (found in the job log as
    notFound queries). A test that needs query results stubs release_queue."""
    from google.cloud import bigquery

    def refuse(self, query, *args, **kwargs):
        raise AssertionError(f"a test sent a real BigQuery query: {str(query)[:80]}")

    monkeypatch.setattr(bigquery.Client, "query", refuse)


@pytest.fixture(autouse=True)
def _no_github_at_preview(monkeypatch):
    """A deploy preview asks GitHub whether a PR or another deploy blocks it
    (deploy._uat_deploy_blocked). Tests preview offline, as they always have; a
    test of that check sets its own answer here."""
    from adk_release_agent import deploy

    monkeypatch.setattr(deploy, "_uat_deploy_blocked", lambda req: "")


@pytest.fixture(autouse=True)
def _no_wait_for_the_sit_to_uat_pr(monkeypatch):
    """A UAT deploy waits for the repository's SIT -> UAT PR to appear. No test
    has that workflow, so none may sit out the real wait."""
    monkeypatch.setattr(settings, "uat_pr_wait_seconds", 0)


@pytest.fixture(autouse=True)
def _no_shared_findings_between_tests():
    """The investigate lane shares a finding per incident for ten minutes; a
    test must never be answered by the finding an earlier test produced."""
    from release_agent import adk_service
    from release_agent.tools.support import cache

    adk_service._findings.clear()
    adk_service._finding_locks.clear()
    cache.clear()       # the shared evidence reads, likewise
    yield
    adk_service._findings.clear()
    cache.clear()
