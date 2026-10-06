"""One read cache for the support evidence, shared by everything that reads it.

The Investigate pipeline's collector and the single-source chat tools read the
same things — a source's logs, the audit logs, a Dataflow job, a PromQL check.
Ten engineers on one incident, or a follow-up question right after an
investigation ("show the worker log lines"), must cost ONE read, not one per
caller: Cloud Logging allows about 60 reads a minute per project.

    remembered(key, compute)  compute() once per key per TTL, single-flight;
                              only an ok answer is kept — a refusal or an outage
                              is retried by the next caller, and logged
    day(value)                the business date part of a key: a window is a
                              function of its business date, so keying on the
                              date (not on a window that ends "now") lets today's
                              readers share
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime
from typing import Any, Callable

_TTL = 600.0
_MAX = 256

_log = logging.getLogger(__name__)
_values: dict[tuple, tuple[float, dict[str, Any]]] = {}
_locks: dict[tuple, threading.Lock] = {}
_guard = threading.Lock()


def day(value: Any) -> str:
    """"2026-10-02" for a date, a datetime or an ISO string; "" for nothing."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value or "").strip()[:10]


def _fresh(key: tuple) -> dict[str, Any] | None:
    hit = _values.get(key)
    return hit[1] if hit and time.time() - hit[0] < _TTL else None


def remembered(key: tuple, compute: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    with _guard:
        hit = _fresh(key)
        if hit is not None:
            return hit
        if len(_locks) > 4 * _MAX:
            _locks.clear()
        lock = _locks.setdefault(key, threading.Lock())
    with lock:
        with _guard:
            hit = _fresh(key)
        if hit is not None:
            return hit        # computed while this caller waited
        value = compute()
        if not isinstance(value, dict) or not value.get("ok"):
            # The model only sees the dict; the operator needs the reason in the log.
            _log.warning("Evidence read failed | %s | %s", "/".join(str(k) for k in key[:2]),
                         str((value or {}).get("error") or "")[:300])
            return value
        with _guard:
            if len(_values) >= _MAX:
                _values.clear()
            _values[key] = (time.time(), value)
        return value


def clear() -> None:
    with _guard:
        _values.clear()
        _locks.clear()
