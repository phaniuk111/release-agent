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
