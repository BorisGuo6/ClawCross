"""FastAPI routes for cross-computer agent harness state."""

from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Header, HTTPException

from api.harness_models import HarnessEventRequest
from harness.store import apply_harness_event, get_harness_state


def create_harness_router(
    *,
    verify_auth_or_token: Callable[[str, str, str | None], None],
) -> APIRouter:
    router = APIRouter()

    @router.get("/harness/state")
    async def read_harness_state(
        user_id: str,
        password: str = "",
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(user_id, password, x_internal_token)
        return get_harness_state(user_id)

    @router.post("/harness/event")
    async def write_harness_event(
        req: HarnessEventRequest,
        x_internal_token: str | None = Header(None),
    ):
        verify_auth_or_token(req.user_id, req.password, x_internal_token)
        try:
            return apply_harness_event(req.user_id, req.model_dump(exclude_none=True, exclude_defaults=True))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return router
