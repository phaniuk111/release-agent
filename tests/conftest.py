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
