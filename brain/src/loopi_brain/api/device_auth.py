"""Device authentication: Caddy forwards the verified client-cert fingerprint."""
from fastapi import Header, HTTPException, Request

from .. import devices


async def current_device(
    request: Request, x_loopi_client_fingerprint: str | None = Header(default=None)
) -> dict:
    fingerprint = (x_loopi_client_fingerprint or "").strip().lower()
    device = await devices.get_active_device(request.app.state.pool, fingerprint)
    if device is None:
        raise HTTPException(status_code=401, detail="unknown or revoked device certificate")
    return device


def check_agent_version(
    request: Request, x_loopi_agent_version: str | None = Header(default=None)
) -> None:
    if x_loopi_agent_version is None:
        return
    major = x_loopi_agent_version.split(".", 1)[0]
    supported = request.app.state.settings.supported_agent_major
    if not major.isdigit() or int(major) != supported:
        raise HTTPException(
            status_code=426,
            detail=f"agent {x_loopi_agent_version} unsupported; need major {supported}",
        )
