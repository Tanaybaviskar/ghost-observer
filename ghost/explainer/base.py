"""
ghost/explainer/base.py

Abstract base class all explainer backends must implement.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ghost._aggregator import FunctionProfile


class BaseExplainer(ABC):
    """Given a FunctionProfile, produce a human-readable explanation."""

    @abstractmethod
    def explain(self, profile: FunctionProfile) -> str:
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        ...
