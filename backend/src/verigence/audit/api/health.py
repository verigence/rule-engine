"""health.py — Liveness probe endpoint."""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health/live")
async def liveness() -> dict[str, str]:
    """Railway / Kubernetes liveness probe. Returns 200 when the process is up."""
    return {"status": "ok"}
