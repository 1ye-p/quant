"""Auth status/verify endpoints for the frontend API-key configuration flow.

- ``GET /auth/status`` — public. Reports whether a key is configured and the
  current auth mode, so the frontend can guide users to the settings page.
  Never discloses the key itself.
- ``GET /auth/verify`` — authenticated (``verify_api_key``). Returns 200 when
  the presented credential is valid; used by the settings page "test key"
  button.
"""

import os

from fastapi import APIRouter, Depends

from cquant.api_server.deps import verify_api_key

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/status")
async def auth_status() -> dict:
    return {
        "key_configured": bool(os.environ.get("CQUANT_API_KEY", "")),
        "mode": os.getenv("CQUANT_AUTH_MODE", "strict"),
    }


@router.get("/verify", dependencies=[Depends(verify_api_key)])
async def auth_verify() -> dict:
    return {"ok": True}
