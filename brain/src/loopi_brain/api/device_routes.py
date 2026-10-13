"""Device-facing endpoints of sync protocol v1 (docs/protocol/v1.openapi.yaml)."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import devices, items, pki
from .device_auth import check_agent_version, current_device

router = APIRouter()


class HwInfo(BaseModel):
    model: str = Field(max_length=100)
    serial: str = Field(max_length=100)
    agent_version: str = Field(max_length=32)


class PairRequest(BaseModel):
    token: str = Field(min_length=1, max_length=32)
    csr_pem: str = Field(max_length=8192)
    hw_info: HwInfo


class PairResponse(BaseModel):
    device_id: str
    cert_pem: str
    ca_cert_pem: str
    ca_fingerprint: str


@router.post(
    "/v1/pair",
    status_code=201,
    response_model=PairResponse,
    dependencies=[Depends(check_agent_version)],
)
async def pair(body: PairRequest, request: Request) -> PairResponse:
    settings = request.app.state.settings

    def issue(device_id: str) -> str:
        return pki.sign_csr(settings.pki_dir, body.csr_pem, device_id)

    try:
        result = await devices.register_device(
            request.app.state.pool, body.token.strip().upper(), body.hw_info.model_dump(), issue
        )
    except pki.PkiError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=401, detail="pair token invalid, used or expired")
    ca_pem = pki.ca_cert_pem(settings.pki_dir)
    return PairResponse(
        device_id=result["device_id"],
        cert_pem=result["cert_pem"],
        ca_cert_pem=ca_pem,
        ca_fingerprint=pki.fingerprint(ca_pem),
    )


@router.put("/v1/items/{item_id}", dependencies=[Depends(check_agent_version)])
async def put_item(item_id: str, request: Request, device: dict = Depends(current_device)) -> JSONResponse:
    settings = request.app.state.settings
    try:
        header = items.parse_header(item_id, request.headers, settings.default_video_fps)
        status, ack = await items.store_item(
            request.app.state.pool, settings, device["id"], header, request.stream()
        )
    except items.ItemRejected as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    return JSONResponse(ack, status_code=status)
