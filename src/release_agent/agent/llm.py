"""LLM construction (Vertex AI via langchain-google-genai) + message helpers."""
from __future__ import annotations

from ..config import settings


# LLM - Vertex AI Gen AI SDK (via langchain-google-genai)
try:
    from langchain_google_genai import ChatGoogleGenerativeAI

    if settings.gcp_project:
        DEFAULT_MODEL = ChatGoogleGenerativeAI(
            model=settings.gemini_model,
            temperature=0,
            vertexai=True,
            project=settings.gcp_project,
            location=settings.gcp_location,
        )
    else:
        DEFAULT_MODEL = None
except Exception:
    DEFAULT_MODEL = None

def message_text(msg) -> str:
    """Extract human-readable text from a message.

    Newer Gemini models (2.5+) return `content` as a list of content blocks
    (text + thinking/signature metadata) rather than a plain string. Concatenate
    only the text blocks so callers never render the raw repr / thinking signatures.
    """
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif (
                isinstance(block, dict)
                and isinstance(block.get("text"), str)
                and block.get("type") in (None, "text")
            ):
                parts.append(block["text"])
        return "".join(parts)
    return str(content) if content else ""


def _get_llm():
    """Returns the configured LLM using Vertex AI Gen AI SDK.
    Project is resolved from env or gcloud (no hardcoding).
    """
    if DEFAULT_MODEL is None:
        from langchain_google_genai import ChatGoogleGenerativeAI

        if not settings.gcp_project:
            raise RuntimeError(
                "No GCP project found. Set GOOGLE_CLOUD_PROJECT env var or ensure "
                "'gcloud config set project ...' and run 'gcloud auth application-default login'"
            )

        return ChatGoogleGenerativeAI(
            model=settings.gemini_model,
            temperature=0,
            vertexai=True,
            project=settings.gcp_project,
            location=settings.gcp_location,
        )
    return DEFAULT_MODEL
