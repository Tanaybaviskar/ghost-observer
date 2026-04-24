"""
ghost/explainer/registry.py

Auto-detects which explainer backend to use based on env vars.
Priority: Gemini > Template (Ollama / OpenAI slots reserved for future steps).
"""
from __future__ import annotations

import os

from ghost.explainer.base import BaseExplainer


def get_explainer(backend: str | None = None) -> BaseExplainer:
    """Return the best available explainer.

    Args:
        backend: Force a specific backend name ("template", "gemini").
                 If None, auto-detect from environment.
    """
    if backend == "template":
        from ghost.explainer.template import TemplateExplainer
        return TemplateExplainer()

    if backend == "gemini" or (backend is None and os.environ.get("GEMINI_API_KEY")):
        from ghost.explainer.gemini import GeminiExplainer
        return GeminiExplainer()

    # Default fallback
    from ghost.explainer.template import TemplateExplainer
    return TemplateExplainer()