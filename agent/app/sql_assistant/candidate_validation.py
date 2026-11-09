"""Frozen candidate provenance and narrow semantic verification.

The snapshot digest is an integrity check, not a signature against a database
administrator. Authorization remains with the existing owner/run binding.
"""
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.errors import Text2SqlGuardError
from app.modules.hot_news.scoring.metric_sql_contract import validate_metric_sql_contract
from app.sql_assistant.guard import SqlAssistantGuard


def digest(value: dict) -> str:
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def validator_identity() -> dict:
    root = Path(__file__).resolve().parents[1]
    files = (Path(__file__), root / "sql_assistant/guard.py",
             root / "sql_assistant/planner.py",
             root / "modules/hot_news/scoring/metric_sql_contract.py")
    return {"version": "sql-candidate-validation-v1", "source_sha256": digest({
        path.name: sha256(path.read_bytes()).hexdigest() for path in files
    })}


class SqlCandidateTrace(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    contract_version: Literal["sql-candidate-plan-v1"] = "sql-candidate-plan-v1"
    mode: Literal["dbgpt_shadow", "dbgpt_candidate"]
    identity: dict[str, Any]
    identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["validated", "rejected", "failed", "unsupported"]
    reason_code: str | None = Field(default=None, max_length=80, pattern=r"^[a-z0-9_]+$")
    candidate_sql_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_identity(self):
        if digest(self.identity) != self.identity_sha256:
            raise ValueError("candidate identity digest mismatch")
        if self.identity.get("config", {}).get("mode") != self.mode:
            raise ValueError("candidate mode differs from frozen identity")
        if self.status == "validated" and self.candidate_sql_hash is None:
            raise ValueError("validated candidate requires SQL digest")
        return self


def validate_candidate(sql: str, *, approved_sql: str, params: dict,
                       guard: SqlAssistantGuard) -> None:
    # The guard accepts a stricter subset than the general metric comparator.
    guard.validate(approved_sql, params)
    guard.validate(sql, params)
    validate_metric_sql_contract(sql, approved_sql=approved_sql, dialect="postgres")


def validate_selected_plan(sql: str, *, sql_hash: str, approved_sql: str,
                           params: dict, trace: SqlCandidateTrace | None,
                           guard: SqlAssistantGuard) -> None:
    if sha256(sql.encode()).hexdigest() != sql_hash:
        raise Text2SqlGuardError("selected SQL digest mismatch")
    if trace is None or trace.mode == "dbgpt_shadow":
        if sql != approved_sql:
            raise Text2SqlGuardError("native SQL differs from approved compiler output")
        guard.validate(sql, params)
    else:
        if trace.status != "validated" or trace.candidate_sql_hash != sql_hash:
            raise Text2SqlGuardError("candidate was not validated for execution")
        if trace.identity.get("validator") != validator_identity():
            raise Text2SqlGuardError("candidate validator assets are unavailable or changed")
        validate_candidate(sql, approved_sql=approved_sql, params=params, guard=guard)
