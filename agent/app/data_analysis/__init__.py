"""Bounded, deterministic analysis of aggregate ranked-news snapshots."""

from .engine import AnalysisInputError, analyze, validate_request

__all__ = ["AnalysisInputError", "analyze", "validate_request"]
