"""schemas.py — Standard API response envelope.

All 20 public endpoints return this shape:
  {"errorCode": "000", "errorMessage": "Success", "data": {...}}
"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    errorCode:    str
    errorMessage: str
    data:         T


def ok(data: Any) -> dict[str, Any]:
    return {"errorCode": "000", "errorMessage": "Success", "data": data}


def err(code: str, msg: str) -> dict[str, Any]:
    return {"errorCode": code, "errorMessage": msg, "data": None}
