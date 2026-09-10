"""Deprecated compatibility imports for the canonical Bundle application service.

The implementation used to be duplicated in this module and
``app.services.production_bundle``. Keeping two lifecycle implementations made
evaluation, approval and activation rules depend on the import path. All
callers now execute the single canonical service; these aliases only keep older
imports source-compatible while they migrate.
"""

from __future__ import annotations

from typing import TypeAlias

from app.services.production_bundle import (
    ActivationWriteOutcome,
    CandidateWriteOutcome,
    EvaluationWriteOutcome,
    ProductionBundleApplicationService,
    ProductionBundleConflictError,
    ProductionBundleNotFoundError,
    ProductionBundlePersistenceError,
    ProductionBundleRuleViolation,
    ReviewWriteOutcome,
    RollbackWriteOutcome,
)


# Legacy names intentionally resolve to the canonical errors/outcomes.
ProductionBundleTargetNotFoundError = ProductionBundleNotFoundError
ProductionBundleDatasetNotReadyError = ProductionBundleRuleViolation
CandidateApplicationOutcome = CandidateWriteOutcome
EvaluationApplicationOutcome = EvaluationWriteOutcome
PromotionApplicationOutcome: TypeAlias = (
    ReviewWriteOutcome | ActivationWriteOutcome | RollbackWriteOutcome
)


__all__ = [
    "ActivationWriteOutcome",
    "CandidateApplicationOutcome",
    "CandidateWriteOutcome",
    "EvaluationApplicationOutcome",
    "EvaluationWriteOutcome",
    "ProductionBundleApplicationService",
    "ProductionBundleConflictError",
    "ProductionBundleDatasetNotReadyError",
    "ProductionBundleNotFoundError",
    "ProductionBundlePersistenceError",
    "ProductionBundleRuleViolation",
    "ProductionBundleTargetNotFoundError",
    "PromotionApplicationOutcome",
    "ReviewWriteOutcome",
    "RollbackWriteOutcome",
]
