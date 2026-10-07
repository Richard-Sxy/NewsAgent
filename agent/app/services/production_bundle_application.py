"""兼容旧导入路径，实际实现已经统一到 production_bundle.py"""

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
