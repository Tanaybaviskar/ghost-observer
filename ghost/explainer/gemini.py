"""
ghost/explainer/gemini.py

Gemini explainer backend.

Activated when GEMINI_API_KEY is set in the environment.
Uses google-generativeai (pip install google-generativeai).

The prompt is fully grounded: every claim the model can make is anchored
to runtime facts injected into the system prompt.  The model cannot
hallucinate call counts, types, or latency — those are provided as data.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

from ghost._aggregator import FunctionProfile
from ghost._fn_key import parse_key
from ghost.explainer.base import BaseExplainer
from ghost.explainer.template import TemplateExplainer, _ns_to_human

if TYPE_CHECKING:
    pass


_SYSTEM_PROMPT = """\
You are Ghost, a Python runtime analysis assistant.
You explain what a function does at runtime based ONLY on the profiling data provided.
Do not speculate beyond the data.  Be concise (3-5 sentences max).
Highlight anything surprising: type inconsistencies, high exception rates, or latency anomalies.
"""


def _build_prompt(profile: FunctionProfile) -> str:
    module, qualname, lineno = parse_key(profile.fn_key)
    lines = [
        f"Function: {qualname} in module {module} (line {lineno})",
        f"Call count: {profile.call_count}",
        f"Exception rate: {profile.exception_rate:.1%}",
        f"Mean latency: {_ns_to_human(profile.mean_latency_ns)}",
        f"Min/Max latency: {_ns_to_human(profile.min_latency_ns)} / {_ns_to_human(profile.max_latency_ns)}",
        f"Arg type distribution: {profile.arg_type_dist}",
        f"Return type distribution: {profile.ret_type_dist}",
        f"Top callers: {list(profile.callers.items())[:5]}",
        f"Top callees: {list(profile.callees.items())[:5]}",
        "",
        "Based solely on this runtime data, explain what this function does, "
        "how it behaves, and flag any anomalies.",
    ]
    return "\n".join(lines)


class GeminiExplainer(BaseExplainer):
    def __init__(self) -> None:
        self._api_key = os.environ.get("GEMINI_API_KEY", "")
        self._client = None
        self._template_fallback = TemplateExplainer()

    @property
    def name(self) -> str:
        return "gemini"

    def _get_client(self):
        if self._client is None:
            try:
                import google.generativeai as genai  # type: ignore
                genai.configure(api_key=self._api_key)
                self._client = genai.GenerativeModel(
                    model_name="gemini-1.5-flash",
                    system_instruction=_SYSTEM_PROMPT,
                )
            except ImportError:
                return None
        return self._client

    def explain(self, profile: FunctionProfile) -> str:
        client = self._get_client()
        if client is None or not self._api_key:
            return self._template_fallback.explain(profile)

        prompt = _build_prompt(profile)
        try:
            response = client.generate_content(prompt)
            llm_text = response.text.strip()
        except Exception as exc:
            llm_text = f"[Gemini unavailable: {exc}]"

        # Prepend the structured template output so runtime facts are always visible
        template_section = self._template_fallback.explain(profile)
        return f"{template_section}\n\n── Gemini analysis ──\n{llm_text}"