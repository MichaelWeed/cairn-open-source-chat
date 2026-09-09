"""Read-only capability and compatibility discovery."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.capabilities import capability_manifest

router = APIRouter()


@router.get("/api/v1/capabilities")
async def get_capabilities() -> JSONResponse:
    return JSONResponse(capability_manifest())
