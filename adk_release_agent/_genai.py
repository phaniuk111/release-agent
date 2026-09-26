"""One place for how this app calls Gemini outside the ADK agent.

The one-shot classifiers (deploy intent, scope guard, CHG draft) build their own
genai clients. They get the same transient-failure retry as the chat agent, but
fewer attempts: each is best-effort and already falls back when it fails, so a
long backoff would only delay an answer that does not depend on it.
"""
from __future__ import annotations

# 429: Vertex's dynamic shared quota — the shared pool was busy, not a quota
# this project exceeded. 5xx: a backend blip. Both clear within seconds.
RETRYABLE_STATUS = [429, 500, 502, 503, 504]


def retry_options(attempts: int):
    from google.genai import types

    return types.HttpRetryOptions(
        attempts=max(1, int(attempts)),
        initial_delay=1.0,
        max_delay=8.0,
        exp_base=2.0,
        jitter=0.5,
        http_status_codes=RETRYABLE_STATUS,
    )


def classifier_client():
    """A genai client for a best-effort one-shot call: two attempts, then give up."""
    from google import genai
    from google.genai import types

    return genai.Client(http_options=types.HttpOptions(retry_options=retry_options(2)))


def drafting_client(timeout_seconds: float, location: str = ""):
    """A classifier client whose every attempt is also bounded in time.

    A caller that waits on the answer from a request thread needs this: without
    a timeout, a Vertex call that never answers holds that thread indefinitely.
    ``location`` pins the Vertex endpoint when the drafting model is served
    somewhere other than GOOGLE_CLOUD_LOCATION — newer Gemini models (3.5
    Flash) answer only at "global" and are "not found" in a region.
    """
    import os

    from google import genai
    from google.genai import types

    http = types.HttpOptions(
        retry_options=retry_options(2),
        timeout=max(1000, int(float(timeout_seconds) * 1000)),   # the SDK counts milliseconds
    )
    if location:
        return genai.Client(vertexai=True, project=os.environ.get("GOOGLE_CLOUD_PROJECT") or None,
                            location=location, http_options=http)
    return genai.Client(http_options=http)
