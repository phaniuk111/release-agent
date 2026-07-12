"""Deterministic parsing helpers retained for the ADK release agent.

The production FastAPI and CLI surfaces run through ``adk_release_agent``; this
package holds only the pure text/JSON parsing helpers that the ADK deploy path
imports from :mod:`release_agent.agent.parsing`.
"""
