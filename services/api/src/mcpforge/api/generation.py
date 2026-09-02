"""Generation-related read endpoints.

`GET /api/frameworks` exists so the product can state what it supports without a
second list to drift. 01_PRD.md §9: framework support is a table in the product,
not a marketing claim.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from mcpforge.generation.adapters.registry import ADAPTERS

router = APIRouter(prefix="/api", tags=["generation"])


class FrameworkDto(BaseModel):
    framework: str
    display_name: str


@router.get("/frameworks", response_model=list[FrameworkDto])
async def list_supported_frameworks() -> list[FrameworkDto]:
    """Derived from the adapter registry, so it cannot advertise what is absent."""
    return [
        FrameworkDto(framework=a.info.framework, display_name=a.info.display_name) for a in ADAPTERS
    ]
