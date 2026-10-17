"""Turn-bound confirmation controls; these are never model tool arguments."""

import json
from datetime import datetime, timedelta, timezone
from secrets import compare_digest, token_urlsafe
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError


CONFIRMATION_PREFIX = "capability-confirm:"


class ConfirmationControl(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    token: str = Field(min_length=20, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    action: Literal["confirm", "cancel"]


def parse_confirmation(content: str) -> ConfirmationControl | None:
    if not content.startswith(CONFIRMATION_PREFIX):
        return None
    try:
        return ConfirmationControl.model_validate(json.loads(content[len(CONFIRMATION_PREFIX):]))
    except (ValidationError, ValueError, TypeError) as exc:
        raise ValueError("confirmation_invalid") from exc


def new_confirmation(*, hook: str, suggested: dict, lifetime_seconds: float, input_seq: int = 0) -> dict:
    now = datetime.now(timezone.utc)
    return {"token": token_urlsafe(32), "hook": hook, "input_request_id": str(uuid4()),
            "expires_at": (now + timedelta(seconds=max(0, min(60, lifetime_seconds)))).isoformat(),
            "suggested": suggested, "state": "pending", "provenance": "model_suggestion", "input_seq": input_seq}


def matches_confirmation(control: ConfirmationControl, confirmation: dict) -> bool:
    token = confirmation.get("token")
    return isinstance(token, str) and compare_digest(control.token, token)


def confirmation_expired(confirmation: dict) -> bool:
    try:
        deadline = datetime.fromisoformat(confirmation["expires_at"])
        return deadline.tzinfo is None or deadline <= datetime.now(timezone.utc)
    except (KeyError, TypeError, ValueError):
        return True
