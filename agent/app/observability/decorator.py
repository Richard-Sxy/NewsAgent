"""Compatibility names for the shared, allowlisted tracing decorator."""

from app.observability.decorators import DomainOutcomeError, traced

# Keep the archive's public names without a second tracing implementation.
trace = traced
DamainOutcomeError = DomainOutcomeError

__all__ = ["trace", "DomainOutcomeError", "DamainOutcomeError"]
